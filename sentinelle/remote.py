"""Mode serveur : client de l'API Sentinelle Server.

En mode serveur, l'application ne lit plus config.yaml : elle récupère auprès
du serveur une projection de la configuration SANS identifiants DVR — les flux
pointent vers le relais vidéo du serveur, les snapshots et le PTZ passent par
son API, et les événements de mouvement arrivent par un flux SSE.

L'administration (jeton admin) récupère la configuration complète — mots de
passe omis : un champ laissé vide à l'enregistrement conserve la valeur déjà
stockée côté serveur, comme dans les dialogues locaux.
"""

import json
import logging
import os
import tempfile
import threading
from urllib.parse import quote, urlparse

import requests
import yaml
from PySide6.QtCore import QObject, Signal

from .config import AppConfig, Camera, load_config

logger = logging.getLogger(__name__)

TIMEOUT = (3.05, 10)


def _json(r: "requests.Response") -> dict:
    """Décode une réponse JSON en levant ErreurServeur si le corps n'en est pas
    un. Un portail captif ou un proxy 4G peut répondre 200 avec du HTML : sans
    cette garde, le JSONDecodeError non typé tuerait silencieusement le thread
    appelant (boîte de connexion figée sur « Connexion… », sans message)."""
    try:
        data = r.json()
    except ValueError:
        raise ErreurServeur("réponse du serveur illisible (pas du JSON)")
    if not isinstance(data, dict):
        raise ErreurServeur("réponse du serveur inattendue")
    return data


class ErreurServeur(RuntimeError):
    """Serveur injoignable ou réponse invalide."""


class JetonInvalide(ErreurServeur):
    """Jeton refusé (401)."""


class DroitsInsuffisants(ErreurServeur):
    """Jeton valide mais sans droits d'administration (403)."""


class ServeurDistant:
    """Accès à l'API du serveur, authentifié par une session (login/mot de passe)."""

    def __init__(self, url: str, jeton: str = ""):
        self.base = url.rstrip("/")
        self.jeton = jeton
        self.username = ""
        self.role = ""

    @property
    def connecte(self) -> bool:
        return bool(self.jeton)

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    # ---------------------------------------------------------------- session

    def login(self, username: str, mot_de_passe: str) -> dict:
        try:
            r = requests.post(self.base + "/api/login",
                              json={"username": username, "password": mot_de_passe},
                              timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise ErreurServeur(f"serveur injoignable ({type(e).__name__})")
        if r.status_code == 401:
            raise JetonInvalide("identifiant ou mot de passe incorrect")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                pass
            # ex. 429 : « trop de tentatives — réessayez dans Ns »
            raise ErreurServeur(detail or f"HTTP {r.status_code}")
        data = _json(r)
        self.jeton = data.get("token", "")
        # jeton relay (mot de passe RTSP) rafraîchi EN MÊME TEMPS que la session :
        # sinon un renouvellement silencieux laisserait le mot de passe RTSP
        # d'origine expirer et couperait tous les flux. Conservé si absent (ancien
        # serveur) — jamais remplacé par le jeton de session (fuirait sur le fil).
        rt = data.get("relay_token")
        if rt:
            self._relay_jeton = rt
        self.username = data.get("username", username)
        self.role = data.get("role", "user")
        return data

    def session_reste(self) -> int | None:
        """Secondes restant avant expiration du jeton (None si non fourni).
        Lève JetonInvalide si la session n'est plus valable."""
        data = _json(self._req("GET", "/api/session"))
        reste = data.get("reste_s")
        return int(reste) if reste is not None else None

    def changer_mot_de_passe(self, ancien: str, nouveau: str):
        data = _json(self._req("POST", "/api/account/password",
                               json={"ancien": ancien, "nouveau": nouveau}))
        if data.get("token"):
            self.jeton = data["token"]        # la session est renouvelée
        # le changement de mot de passe invalide TOUS les jetons (signature liée
        # au hash) : rafraîchir aussi le jeton relay, sinon le RTSP tombe en 401
        if data.get("relay_token"):
            self._relay_jeton = data["relay_token"]

    def deconnecter(self):
        """Révoque la session côté serveur (déconnexion de tous les appareils).
        Sans effet si le serveur ne connaît pas l'endpoint (version ancienne)."""
        try:
            self._req("POST", "/api/account/logout")
        except ErreurServeur:
            pass

    def pousser_boucles(self, sequences: list):
        """Enregistre les rondes personnelles du compte connecté. Les rondes
        partagées (gérées par un admin) ne transitent jamais par ici."""
        self._req("PUT", "/api/account/sequences",
                  json={"sequences": [s.to_dict() for s in sequences
                                      if not s.partagee]})

    # -------------------------------------------------------- rondes partagées

    def rounds_liste(self) -> list[dict]:
        """Rondes partagées avec attribution (administration)."""
        return _json(self._req("GET", "/api/rounds")).get("sequences", [])

    def rounds_pousser(self, sequences: list[dict]) -> list[str]:
        """Remplace les rondes partagées (administration). Retourne les warnings."""
        r = self._req("PUT", "/api/rounds", json={"sequences": sequences})
        try:
            return list(r.json().get("warnings") or [])
        except Exception:
            return []

    # ------------------------------------------------------------------ HTTP

    def _req(self, methode: str, chemin: str, **kwargs) -> requests.Response:
        try:
            r = requests.request(
                methode, self.base + chemin,
                headers={"Authorization": f"Bearer {self.jeton}"},
                timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as e:
            raise ErreurServeur(f"serveur injoignable ({type(e).__name__})")
        if r.status_code == 401:
            raise JetonInvalide("session expirée — reconnectez-vous")
        if r.status_code == 403:
            raise DroitsInsuffisants("action réservée aux administrateurs")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                pass
            raise ErreurServeur(f"HTTP {r.status_code}" + (f" — {detail}" if detail else ""))
        return r

    # ------------------------------------------------------------ utilisateurs

    def users_liste(self) -> list[dict]:
        return _json(self._req("GET", "/api/users")).get("users", [])

    def users_pousser(self, users: list[dict]) -> list[str]:
        r = self._req("PUT", "/api/users", json={"users": users})
        try:
            return list(r.json().get("warnings") or [])
        except Exception:
            return []

    # ---------------------------------------------------------- configuration

    def config_vue(self) -> AppConfig:
        """Projection pour l'affichage : caméras pointant vers le relais."""
        p = _json(self._req("GET", "/api/config"))
        cfg = AppConfig(path="")
        cfg.rotation_duree_s = max(3, int(p.get("rotation_duree_s", 20)))
        compte = p.get("compte") or {}
        self.role = compte.get("role", self.role)
        self.username = compte.get("username", self.username)

        relay = p.get("relay") or {}
        self._relay_port = int(relay.get("port", 8554))
        # jeton de portée « relay » = mot de passe RTSP, distinct du jeton de
        # session. JAMAIS de repli sur le jeton de session : l'employer comme mot
        # de passe RTSP le ferait transiter en clair sur le fil (flux non
        # chiffré) et anéantirait le cloisonnement des portées.
        self._relay_jeton = str(relay.get("token") or "")
        base_rtsp = self._base_rtsp()

        from .config import Site
        for s in p.get("sites") or []:
            cfg.sites.append(Site(id=str(s["id"]), nom=str(s.get("nom") or s["id"]),
                                  lien=str(s.get("lien", "fibre"))))
        for c in p.get("cameras") or []:
            site = cfg.site(str(c.get("site")))
            if site is None:
                continue
            cam = Camera(
                id=str(c["id"]), nom=str(c.get("nom") or c["id"]), site=site,
                profil=str(c.get("profil", "normal")),
                marque="custom",
                url_mainstream=base_rtsp + str(c.get("main", "")),
                url_substream=base_rtsp + str(c.get("sub", "")),
                # le jeton part en auth Basic (champ mot de passe), pas dans l'URL
                url_snapshot=(f"{self.base}/api/snapshot/{c['id']}"
                              if c.get("snapshot") else ""),
                user="jeton" if c.get("snapshot") else "",
                password=self.jeton if c.get("snapshot") else "",
                photo_intervalle_s=max(2, int(c.get("photo_intervalle_s", 10))),
                ptz=bool(c.get("ptz")),
            )
            # attributs transitoires du mode serveur (jamais persistés)
            cam.remote = self
            cam.remote_onvif = bool(c.get("onvif"))
            # chemins bruts du relais : permettent de régénérer les URLs en
            # place quand le jeton est rafraîchi (voir maj_jeton_urls)
            cam._relay_main = str(c.get("main", ""))
            cam._relay_sub = str(c.get("sub", ""))
            cam._relay_snapshot = bool(c.get("snapshot"))
            cfg.cameras.append(cam)

        from .config import Etape, Sequence
        cam_ids = {c.id for c in cfg.cameras}
        for s in p.get("sequences") or []:
            etapes = []
            for e in s.get("etapes") or []:
                cams = [str(x) for x in (e.get("cameras") or []) if str(x) in cam_ids]
                if cams:
                    etapes.append(Etape(mode=str(e.get("mode", "grille")),
                                        cameras=cams,
                                        duree_s=max(3, int(e.get("duree_s", 30)))))
            if etapes:
                cfg.sequences.append(Sequence(
                    nom=str(s.get("nom", "")), etapes=etapes,
                    id=str(s.get("id", "")),
                    partagee=bool(s.get("partagee", False))))
        return cfg

    def _base_rtsp(self) -> str:
        """Préfixe RTSP du relais. Le mot de passe est le jeton de portée
        « relay » (pas le jeton de session) : le relais l'envoie à l'API qui
        vérifie les droits sur la caméra. Cloisonné pour qu'une capture du flux
        RTSP non chiffré ne donne pas accès à l'API HTTP."""
        hote = urlparse(self.base).hostname or "localhost"
        jeton_relay = getattr(self, "_relay_jeton", "")   # jamais self.jeton (fuite)
        cred = f"sentinelle:{quote(jeton_relay, safe='')}@"
        return f"rtsp://{cred}{hote}:{getattr(self, '_relay_port', 8554)}/"

    def maj_jeton_urls(self, cfg: AppConfig):
        """Après un rafraîchissement du jeton : met à jour EN PLACE les URLs et
        identifiants des caméras (le jeton y est incrusté), SANS reconstruire le
        mur. Le relais ne vérifie le jeton qu'à l'OUVERTURE d'un flux : les
        lectures en cours survivent, seules les (re)connexions futures ont
        besoin du jeton frais — les tuiles re-résolvent leur URL à chaque
        tentative."""
        base_rtsp = self._base_rtsp()
        for cam in cfg.cameras:
            if getattr(cam, "remote", None) is not self:
                continue
            if getattr(cam, "_relay_main", ""):
                cam.url_mainstream = base_rtsp + cam._relay_main
            if getattr(cam, "_relay_sub", ""):
                cam.url_substream = base_rtsp + cam._relay_sub
            if getattr(cam, "_relay_snapshot", False):
                cam.password = self.jeton

    def config_admin(self) -> AppConfig:
        """Configuration complète pour l'édition (mots de passe vides = conservés)."""
        data = _json(self._req("GET", "/api/config/full"))
        fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="sentinelle-admin-")
        os.close(fd)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            cfg = load_config(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        cfg.path = ""
        return cfg

    def pousser(self, cfg: AppConfig) -> list[str]:
        """Envoie la configuration complète au serveur. Retourne ses warnings."""
        data = {
            "options": {"rotation_duree_s": cfg.rotation_duree_s},
            "sites": [{"id": s.id, "nom": s.nom, "lien": s.lien} for s in cfg.sites],
            "cameras": [c.to_dict() for c in cfg.cameras],
            "sequences": [s.to_dict() for s in cfg.sequences],
        }
        r = self._req("PUT", "/api/config", json=data)
        try:
            return list(r.json().get("warnings") or [])
        except Exception:
            return []

    # -------------------------------------------------------------------- PTZ

    def ptz_move(self, cam_id: str, pan: float, tilt: float, zoom: float = 0.0):
        self._req("POST", f"/api/ptz/{cam_id}/move",
                  json={"pan": pan, "tilt": tilt, "zoom": zoom})

    def ptz_stop(self, cam_id: str):
        self._req("POST", f"/api/ptz/{cam_id}/stop")

    # ------------------------------------------------------------- événements

    def url_events(self) -> str:
        return f"{self.base}/api/events"


class EcouteurMouvement(QObject):
    """Écoute le flux SSE des événements de mouvement du serveur.

    Même interface que MotionMonitor (surveiller/stop + signal motion_changed)
    pour être interchangeable dans MainWindow.
    """

    motion_changed = Signal(str, bool)          # camera_id, actif

    def __init__(self, serveur: ServeurDistant, parent=None):
        super().__init__(parent)
        self._serveur = serveur
        self._stop_evt: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._resp = None

    def surveiller(self, _cameras=None):
        """Démarre l'écoute (la liste des caméras est gérée côté serveur).

        Un Event d'arrêt NEUF est créé à chaque démarrage et passé au thread :
        réutiliser (et ré-armer) un Event partagé « ressuscitait » l'ancien
        thread encore bloqué dans iter_lines — double écoute et stop() cassé."""
        if self._thread is not None and self._thread.is_alive():
            return
        ev = threading.Event()
        self._stop_evt = ev
        self._thread = threading.Thread(target=self._boucle, args=(ev,),
                                        daemon=True, name="motion-sse")
        self._thread.start()

    def stop(self):
        if self._stop_evt is not None:
            self._stop_evt.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()        # débloque iter_lines immédiatement
            except Exception:
                pass
        self._thread = None

    def _boucle(self, ev: threading.Event):
        echecs = 0
        while not ev.is_set():
            r = None
            try:
                r = requests.get(
                    self._serveur.url_events(),
                    headers={"Authorization": f"Bearer {self._serveur.jeton}"},
                    stream=True, timeout=(3.05, 40))
                if r.status_code != 200:
                    raise ErreurServeur(f"HTTP {r.status_code}")
                self._resp = r
                echecs = 0
                for ligne in r.iter_lines():
                    if ev.is_set():
                        return
                    if not ligne or not ligne.startswith(b"data:"):
                        continue                      # keepalives / commentaires
                    try:
                        evt = json.loads(ligne[5:].strip())
                        cam_id = str(evt["camera"])
                        actif = bool(evt["actif"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    try:
                        self.motion_changed.emit(cam_id, actif)
                    except RuntimeError:
                        return                        # objet Qt détruit
            except Exception as e:
                if ev.is_set():
                    return
                echecs += 1
                logger.info(f"Événements serveur interrompus ({e}) — reconnexion")
                ev.wait(min(2 ** echecs, 60))         # backoff, arrêt réactif
            finally:
                # ne nettoyer que SA réponse : un ancien thread qui finit de
                # mourir ne doit pas effacer celle du thread relancé entre-temps
                if self._resp is r:
                    self._resp = None
