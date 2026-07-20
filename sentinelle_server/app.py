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
import json
import logging
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from sentinelle.snapshot import fetch_snapshot

from . import __version__
from .motion import EventHub, MotionMonitor
from .relay import Relay
from .store import Store

logger = logging.getLogger(__name__)


def create_app(data_dir: str | None = None) -> FastAPI:
    store = Store(data_dir)
    relay = Relay()
    hub = EventHub()
    monitor = MotionMonitor(hub.publier)
    ptz_locks: dict[str, threading.Lock] = {}
    ptz_clients: dict[str, object] = {}

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
        try:
            corps = await request.json()
        except Exception:
            raise HTTPException(400, "corps JSON invalide")
        user = store.users.authentifier(str(corps.get("username", "")),
                                        str(corps.get("password", "")))
        if user is None:
            raise HTTPException(401, "identifiant ou mot de passe incorrect")
        return {"token": store.users.emettre_jeton(user),
                "username": user.username, "role": user.role,
                "version": __version__}

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
        if len(nouveau) < 4:
            raise HTTPException(422, "nouveau mot de passe trop court")
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
            "sequences": _boucles_utilisateur(u, visibles),
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
            etapes = []
            for e in (s.get("etapes") or [])[:100]:
                mode = str(e.get("mode", "grille"))
                if mode not in ("grille", "mono"):
                    continue
                cams = [str(c) for c in (e.get("cameras") or []) if str(c) in visibles]
                if mode == "mono":
                    cams = cams[:1]
                if not cams:
                    continue
                try:
                    duree = max(3, min(3600, int(e.get("duree_s", 30))))
                except (TypeError, ValueError):
                    duree = 30
                etapes.append({"mode": mode, "cameras": cams[:16], "duree_s": duree})
            if nom and etapes:
                valides.append({"nom": nom, "etapes": etapes})
        store.users.definir_sequences(u.username, valides)
        return {"ok": True, "sequences": len(valides)}

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
            # publish (source à la demande) et le reste : géré en interne
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
        visibles = store.users.cameras_visibles(u, store.cfg)
        q = hub.abonner()

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
                        if evt.get("camera") in visibles:
                            yield f"data: {json.dumps(evt)}\n\n"
                    except asyncio.TimeoutError:
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
