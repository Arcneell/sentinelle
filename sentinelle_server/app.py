"""API du serveur Sentinelle.

Authentification par comptes utilisateurs : chaque poste se connecte avec un
login + mot de passe et reçoit un jeton de session (Bearer). Deux rôles :
  - user  : visualisation des caméras auxquelles il a droit ;
  - admin : gestion complète (utilisateurs, caméras/sites, boucles, réglages).

Tout le contrôle d'accès est appliqué CÔTÉ SERVEUR : la projection de
configuration ne contient que les caméras autorisées, l'accès aux flux est
validé à chaque lecture par le relais (auth externe → /api/relay-auth), et les
identifiants DVR ne quittent jamais le serveur.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from sentinelle.snapshot import fetch_snapshot

from . import __version__
from .auth import MIN_MDP
from .motion import EventHub, MotionMonitor
from .relay import Relay
from .store import Store

logger = logging.getLogger(__name__)

# Anti-force-brute du login : au-delà de LOGIN_MAX échecs sur LOGIN_WINDOW_S
# depuis une même IP, on refuse (429) sans vérifier le mot de passe. Volontaire-
# ment par IP (et non par compte) pour ne pas permettre de verrouiller à
# distance le compte d'un mur d'images (déni de service).
LOGIN_WINDOW_S = 300
LOGIN_MAX = 8


def _ip_interne(ip: str) -> bool:
    """Adresse privée (RFC 1918) ou loopback — le réseau Docker/LAN de confiance."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback


def _ip_client(request: Request) -> str:
    """IP d'origine pour la limitation du login.

    X-Forwarded-For n'est pris en compte que si le pair TCP est lui-même sur le
    réseau interne (cas du reverse proxy Caddy) : sinon un client direct
    pourrait forger l'en-tête et changer d'« IP » à chaque tentative pour
    contourner la limitation."""
    pair = request.client.host if request.client else "?"
    xff = request.headers.get("x-forwarded-for", "")
    if xff and _ip_interne(pair):
        return xff.split(",")[0].strip()
    return pair


def create_app(data_dir: str | None = None) -> FastAPI:
    store = Store(data_dir)
    relay = Relay()
    hub = EventHub()
    monitor = MotionMonitor(hub.publier)
    ptz_locks: dict[str, threading.Lock] = {}
    ptz_clients: dict[str, object] = {}
    login_fails: dict[str, list] = {}          # ip -> [horodatages d'échec récents]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        hub.loop = asyncio.get_running_loop()
        relay.sync_fond(store)
        monitor.surveiller(store.cfg.cameras)
        logger.info(f"Sentinelle Server {__version__} — "
                    f"{len(store.cfg.cameras)} caméra(s), "
                    f"{len(store.users.users)} compte(s)")
        yield
        monitor.stop()

    app = FastAPI(title="Sentinelle Server", version=__version__, lifespan=lifespan)

    # ------------------------------------------------------------------- auth

    def _token(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if auth.lower().startswith("basic "):
            try:
                decode = base64.b64decode(auth[6:]).decode("utf-8")
                return decode.split(":", 1)[1] if ":" in decode else ""
            except Exception:
                return ""
        return request.query_params.get("token", "")

    def user_courant(request: Request):
        u = store.users.user_du_jeton(_token(request))
        if u is None:
            raise HTTPException(401, "session invalide — reconnectez-vous")
        return u

    def exiger_admin(request: Request):
        u = user_courant(request)
        if not u.admin:
            raise HTTPException(403, "réservé aux administrateurs")
        return u

    # ---------------------------------------------------------------- session

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.post("/api/login")
    async def login(request: Request):
        ip = _ip_client(request)
        now = time.time()
        if len(login_fails) > 512:
            # purge des IP sans échec récent (sinon croissance sans borne)
            for k in [k for k, v in login_fails.items()
                      if not v or now - v[-1] > LOGIN_WINDOW_S]:
                login_fails.pop(k, None)
        recents = [t for t in login_fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
        if len(recents) >= LOGIN_MAX:
            login_fails[ip] = recents
            retry = int(LOGIN_WINDOW_S - (now - recents[0]))
            raise HTTPException(429, f"trop de tentatives — réessayez dans {retry}s",
                                headers={"Retry-After": str(max(1, retry))})
        try:
            corps = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        user = store.users.authentifier(str(corps.get("username", "")),
                                        str(corps.get("password", "")))
        if user is None:
            recents.append(now)
            login_fails[ip] = recents
            raise HTTPException(401, "identifiant ou mot de passe incorrect")
        login_fails.pop(ip, None)              # succès → on repart de zéro pour cette IP
        return {"token": store.users.emettre_jeton(user),
                "username": user.username, "role": user.role,
                "version": __version__}

    @app.get("/api/session")
    def session(request: Request):
        """État de la session courante (validité restante du jeton), pour le
        rafraîchissement proactif côté client."""
        u = user_courant(request)
        return {"ok": True, "username": u.username, "role": u.role,
                "reste_s": store.users.reste_jeton(_token(request))}

    @app.post("/api/account/password")
    async def changer_mon_mdp(request: Request):
        u = user_courant(request)
        try:
            corps = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        ancien = str(corps.get("ancien", ""))
        nouveau = str(corps.get("nouveau", ""))
        if store.users.authentifier(u.username, ancien) is None:
            raise HTTPException(403, "mot de passe actuel incorrect")
        if len(nouveau) < MIN_MDP:
            raise HTTPException(422, f"nouveau mot de passe trop court (min {MIN_MDP})")
        store.users.definir_mot_de_passe(u.username, nouveau)
        # le jeton signé dépend de l'empreinte du mdp → renouveler la session
        u2 = store.users.users[u.username]
        return {"ok": True, "token": store.users.emettre_jeton(u2)}

    # ----------------------------------------------------------- configuration

    def _boucles_utilisateur(u, visibles: set) -> list[dict]:
        """Boucles personnelles du compte, épurées des caméras devenues
        inaccessibles (les droits ont pu changer depuis leur création)."""
        resultat = []
        for s in u.sequences:
            etapes = []
            for e in s.get("etapes") or []:
                cams = [c for c in (e.get("cameras") or []) if c in visibles]
                if cams:
                    etapes.append({"mode": e.get("mode", "grille"),
                                   "cameras": cams,
                                   "duree_s": e.get("duree_s", 30)})
            if etapes:
                resultat.append({"nom": s.get("nom", ""), "etapes": etapes})
        return resultat

    def _rondes_partagees(u, visibles: set) -> list[dict]:
        """Rondes partagées attribuées au compte, épurées des caméras non
        visibles (mêmes règles que les boucles personnelles). Une ronde vidée
        de toutes ses étapes n'est pas renvoyée."""
        resultat = []
        for s in store.cfg.sequences:
            if not (s.tous or u.username in s.utilisateurs):
                continue
            etapes = []
            for e in s.etapes:
                cams = [c for c in e.cameras if c in visibles]
                if cams:
                    etapes.append({"mode": e.mode, "cameras": cams,
                                   "duree_s": e.duree_s})
            if etapes:
                resultat.append({"id": s.id, "nom": s.nom, "etapes": etapes,
                                 "partagee": True})
        return resultat

    def _valider_etapes(brut, cams_autorisees: set) -> list[dict]:
        """Épure une liste d'étapes reçue : modes connus, caméras autorisées,
        durées bornées. Les étapes sans caméra valide sont éliminées."""
        etapes = []
        for e in (brut or [])[:100]:
            mode = str(e.get("mode", "grille"))
            if mode not in ("grille", "mono"):
                continue
            cams = [str(c) for c in (e.get("cameras") or [])
                    if str(c) in cams_autorisees]
            if mode == "mono":
                cams = cams[:1]
            if not cams:
                continue
            try:
                duree = max(3, min(3600, int(e.get("duree_s", 30))))
            except (TypeError, ValueError):
                duree = 30
            etapes.append({"mode": mode, "cameras": cams[:16], "duree_s": duree})
        return etapes

    @app.get("/api/config")
    def config_vue(request: Request):
        """Projection pour l'affichage : caméras autorisées, sans identifiant DVR."""
        u = user_courant(request)
        cfg = store.cfg
        visibles = store.users.cameras_visibles(u, cfg)
        cams = [c for c in cfg.cameras if c.id in visibles]
        sites_utiles = {c.site.id for c in cams}
        return {
            "version": __version__,
            "compte": {"username": u.username, "role": u.role},
            "rotation_duree_s": cfg.rotation_duree_s,
            "relay": {"port": int(store.params["relay_port"])},
            "sites": [{"id": s.id, "nom": s.nom, "lien": s.lien}
                      for s in cfg.sites if s.id in sites_utiles],
            "cameras": [{
                "id": c.id, "nom": c.nom, "site": c.site.id, "profil": c.profil,
                "photo_intervalle_s": c.photo_intervalle_s,
                "ptz": bool(c.ptz), "onvif": c.marque == "onvif",
                "snapshot": bool(c.snapshot_url()),
                "main": f"{c.id}-main", "sub": f"{c.id}-sub",
            } for c in cams],
            "sequences": (_rondes_partagees(u, visibles)
                          + _boucles_utilisateur(u, visibles)),
        }

    @app.put("/api/account/sequences")
    async def mes_boucles(request: Request):
        """Enregistre les boucles personnelles du compte connecté."""
        u = user_courant(request)
        try:
            corps = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        brut = corps.get("sequences") if isinstance(corps, dict) else None
        if not isinstance(brut, list):
            raise HTTPException(400, "liste de boucles attendue")
        visibles = store.users.cameras_visibles(u, store.cfg)
        valides = []
        for s in brut[:50]:                              # borne raisonnable
            nom = str(s.get("nom", "")).strip()[:80]
            etapes = _valider_etapes(s.get("etapes"), visibles)
            if nom and etapes:
                valides.append({"nom": nom, "etapes": etapes})
        store.users.definir_sequences(u.username, valides)
        return {"ok": True, "sequences": len(valides)}

    # ------------------------------------------------------- rondes partagées

    @app.get("/api/rounds")
    def rondes_liste(request: Request):
        """Rondes partagées complètes, avec leur attribution (administration)."""
        exiger_admin(request)
        return {"sequences": [{
            "id": s.id, "nom": s.nom,
            "etapes": [e.to_dict() for e in s.etapes],
            "tous": s.tous, "utilisateurs": list(s.utilisateurs),
        } for s in store.cfg.sequences]}

    @app.put("/api/rounds")
    async def rondes_remplacer(request: Request):
        """Remplace les rondes partagées et leur attribution (administration)."""
        exiger_admin(request)
        try:
            corps = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        brut = corps.get("sequences") if isinstance(corps, dict) else None
        if not isinstance(brut, list):
            raise HTTPException(400, "liste de rondes attendue")

        from sentinelle.config import Etape, Sequence, slugify
        cam_ids = {c.id for c in store.cfg.cameras}
        comptes = set(store.users.users)
        warnings, valides, ids_pris = [], [], set()
        for s in brut[:50]:
            nom = str(s.get("nom", "")).strip()[:80]
            etapes = _valider_etapes(s.get("etapes"), cam_ids)
            if not nom or not etapes:
                warnings.append(f"ronde '{nom or '?'}' sans étape valide — ignorée")
                continue
            inconnus = [str(x) for x in (s.get("utilisateurs") or [])
                        if str(x) not in comptes]
            if inconnus:
                warnings.append(f"ronde '{nom}' : compte(s) inconnu(s) "
                                f"{', '.join(inconnus[:5])} — retiré(s)")
            ident = str(s.get("id", "")).strip() or slugify(nom)
            cand, i = ident, 2
            while cand in ids_pris:
                cand, i = f"{ident}-{i}", i + 1
            ids_pris.add(cand)
            valides.append(Sequence(
                nom=nom, id=cand,
                etapes=[Etape(**e) for e in etapes],
                tous=bool(s.get("tous", False)),
                utilisateurs=[str(x) for x in (s.get("utilisateurs") or [])
                              if str(x) in comptes]))
        store.remplacer_rondes(valides)
        logger.info(f"Rondes partagées mises à jour ({len(valides)})")
        return {"ok": True, "sequences": len(valides), "warnings": warnings}

    @app.get("/api/config/full")
    def config_complete(request: Request):
        """Configuration complète pour l'administration — mots de passe omis."""
        exiger_admin(request)
        cfg = store.cfg
        cameras = []
        for c in cfg.cameras:
            d = c.to_dict()
            d["password"] = ""
            cameras.append(d)
        return {
            "options": {"rotation_duree_s": cfg.rotation_duree_s},
            "sites": [{"id": s.id, "nom": s.nom, "lien": s.lien} for s in cfg.sites],
            "cameras": cameras,
            "sequences": [s.to_dict() for s in cfg.sequences],
        }

    @app.put("/api/config")
    async def config_remplacer(request: Request):
        exiger_admin(request)
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        if not isinstance(data, dict):
            raise HTTPException(400, "corps JSON invalide")
        try:
            warnings = store.remplacer_config(data)
        except Exception as e:
            raise HTTPException(422, f"configuration rejetée : {e}")
        # les clients ONVIF (PTZ) sont mis en cache par caméra avec l'hôte et les
        # identifiants du moment : on les jette pour que la nouvelle config (IP ou
        # mot de passe modifiés) reprenne effet sans redémarrer le serveur.
        ptz_clients.clear()
        relay.sync_fond(store)
        monitor.surveiller(store.cfg.cameras)
        logger.info(f"Configuration remplacée ({len(store.cfg.cameras)} caméras)")
        return {"ok": True, "warnings": warnings}

    # ------------------------------------------------------------- utilisateurs

    @app.get("/api/users")
    def users_liste(request: Request):
        exiger_admin(request)
        return {"users": store.users.liste_publique()}

    @app.put("/api/users")
    async def users_remplacer(request: Request):
        exiger_admin(request)
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        entrees = data.get("users") if isinstance(data, dict) else data
        if not isinstance(entrees, list):
            raise HTTPException(400, "liste d'utilisateurs attendue")
        try:
            warnings = store.users.remplacer(entrees)
        except ValueError as e:
            raise HTTPException(422, str(e))
        logger.info(f"Utilisateurs mis à jour ({len(store.users.users)} comptes)")
        return {"ok": True, "warnings": warnings}

    # ---------------------------------------------------------------- médias

    def _cam_autorisee(request: Request, cam_id: str):
        u = user_courant(request)
        cam = store.cfg.camera(cam_id)
        if cam is None or not u.peut_voir(cam):
            raise HTTPException(404, "caméra inconnue ou non autorisée")
        return cam

    @app.get("/api/snapshot/{cam_id}")
    def snapshot(cam_id: str, request: Request):
        cam = _cam_autorisee(request, cam_id)
        url = cam.snapshot_url()
        if not url:
            raise HTTPException(404, "pas de snapshot pour cette caméra")
        data, kind, detail = fetch_snapshot(url, cam.user, cam.password)
        if kind != "ok" or not data:
            raise HTTPException(502, f"snapshot indisponible ({kind}: {detail[:120]})")
        return Response(content=data, media_type="image/jpeg")

    # ------------------------------------------------- autorisation du relais

    @app.post("/api/relay-auth")
    async def relay_auth(request: Request):
        """Appelé par MediaMTX à chaque accès. Autorise la lecture d'un flux si
        le jeton (mot de passe RTSP) correspond à un compte ayant droit à la
        caméra demandée. Réponse 2xx = autorisé, sinon refus."""
        try:
            corps = await request.json()
        except Exception:
            raise HTTPException(400, "requête invalide")
        action = corps.get("action", "")
        if action not in ("read", "playback"):
            # publish : refusé depuis une IP publique (nos chemins sont alimentés
            # par une source à la demande interne ; aucun poste ne publie vers le
            # relais). Une requête SANS ip (appel interne MediaMTX) reste tolérée
            # — sinon un changement de comportement de MediaMTX couperait tous
            # les flux. Le reste (api/metrics) est déjà exclu côté MediaMTX.
            ip = str(corps.get("ip", ""))
            if action == "publish" and ip and not _ip_interne(ip):
                raise HTTPException(403, "publication externe refusée")
            return {"ok": True}
        user = store.users.user_du_jeton(str(corps.get("password", "")))
        if user is None:
            raise HTTPException(401, "jeton invalide")
        chemin = str(corps.get("path", ""))
        cam_id = chemin
        for suff in ("-main", "-sub"):
            if cam_id.endswith(suff):
                cam_id = cam_id[: -len(suff)]
                break
        cam = store.cfg.camera(cam_id)
        if cam is None or not user.peut_voir(cam):
            raise HTTPException(403, "accès à ce flux non autorisé")
        return {"ok": True}

    # ---------------------------------------------------------------- PTZ

    def _ptz_client(cam):
        oc = ptz_clients.get(cam.id)
        if oc is None:
            from sentinelle.onvif import OnvifCamera
            oc = OnvifCamera(cam.hote, cam.user, cam.password, port=cam.port_http)
            ptz_clients[cam.id] = oc
        return oc

    @app.post("/api/ptz/{cam_id}/move")
    async def ptz_move(cam_id: str, request: Request):
        cam = _cam_autorisee(request, cam_id)
        if not cam.ptz:
            raise HTTPException(400, "caméra non motorisée")
        try:
            corps = await request.json()
        except Exception:
            corps = {}
        pan = float(corps.get("pan", 0.0)); tilt = float(corps.get("tilt", 0.0))
        zoom = float(corps.get("zoom", 0.0))
        lock = ptz_locks.setdefault(cam_id, threading.Lock())

        def run():
            with lock:
                _ptz_client(cam).ptz_move(cam.onvif_profile, pan, tilt, zoom)
        try:
            await asyncio.to_thread(run)
        except Exception as e:
            raise HTTPException(502, f"PTZ : {e}")
        return {"ok": True}

    @app.post("/api/ptz/{cam_id}/stop")
    async def ptz_stop(cam_id: str, request: Request):
        cam = _cam_autorisee(request, cam_id)
        if not cam.ptz:
            raise HTTPException(400, "caméra non motorisée")
        lock = ptz_locks.setdefault(cam_id, threading.Lock())

        def run():
            with lock:
                _ptz_client(cam).ptz_stop(cam.onvif_profile)
        try:
            await asyncio.to_thread(run)
        except Exception as e:
            raise HTTPException(502, f"PTZ : {e}")
        return {"ok": True}

    # ------------------------------------------------------------- événements

    @app.get("/api/events")
    async def events(request: Request):
        """Flux SSE des mouvements, limité aux caméras autorisées de l'utilisateur."""
        u = user_courant(request)
        token = _token(request)
        visibles = store.users.cameras_visibles(u, store.cfg)
        q = hub.abonner()

        def _revalider() -> set | None:
            """Recalcule les caméras visibles depuis l'état courant (droits ou
            jeton ont pu changer pendant la diffusion). None = session finie."""
            u2 = store.users.user_du_jeton(token)
            if u2 is None:
                return None
            return store.users.cameras_visibles(u2, store.cfg)

        async def gen():
            try:
                for cam_id in hub.actifs_courants():
                    if cam_id in visibles:
                        yield f"data: {json.dumps({'camera': cam_id, 'actif': True})}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=15)
                        vis = _revalider()
                        if vis is None:
                            return                    # droits révoqués / jeton expiré
                        if evt.get("camera") in vis:
                            yield f"data: {json.dumps(evt)}\n\n"
                    except asyncio.TimeoutError:
                        if _revalider() is None:
                            return
                        yield ": keepalive\n\n"
            finally:
                hub.desabonner(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------- diagnostic

    @app.get("/api/relay/status")
    def relay_status(request: Request):
        exiger_admin(request)
        try:
            etat = relay.etat()
        except Exception as e:
            return {"pret": relay.pret, "erreur": str(e)}
        return {"pret": relay.pret, "erreur": relay.derniere_erreur, "paths": etat}

    return app
