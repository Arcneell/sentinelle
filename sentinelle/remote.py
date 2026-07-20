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
import time
from urllib.parse import quote, urlparse

import requests
import yaml
from PySide6.QtCore import QObject, Signal

from .config import AppConfig, Camera, load_config

logger = logging.getLogger(__name__)

TIMEOUT = (3.05, 10)


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
            raise ErreurServeur(f"HTTP {r.status_code}")
        data = r.json()
        self.jeton = data.get("token", "")
        self.username = data.get("username", username)
        self.role = data.get("role", "user")
        return data

    def changer_mot_de_passe(self, ancien: str, nouveau: str):
        data = self._req("POST", "/api/account/password",
                         json={"ancien": ancien, "nouveau": nouveau}).json()
        if data.get("token"):
            self.jeton = data["token"]        # la session est renouvelée

    def pousser_boucles(self, sequences: list):
        """Enregistre les boucles personnelles du compte connecté."""
        self._req("PUT", "/api/account/sequences",
                  json={"sequences": [s.to_dict() for s in sequences]})

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
        return self._req("GET", "/api/users").json().get("users", [])

    def users_pousser(self, users: list[dict]) -> list[str]:
        r = self._req("PUT", "/api/users", json={"users": users})
        try:
            return list(r.json().get("warnings") or [])
        except Exception:
            return []

    # ---------------------------------------------------------- configuration

    def config_vue(self) -> AppConfig:
        """Projection pour l'affichage : caméras pointant vers le relais."""
        p = self._req("GET", "/api/config").json()
        cfg = AppConfig(path="")
        cfg.rotation_duree_s = max(3, int(p.get("rotation_duree_s", 20)))
        compte = p.get("compte") or {}
        self.role = compte.get("role", self.role)
        self.username = compte.get("username", self.username)

        relay = p.get("relay") or {}
        hote = urlparse(self.base).hostname or "localhost"
        # le jeton de session sert de mot de passe RTSP : le relais l'envoie à
        # l'API qui vérifie les droits de l'utilisateur sur la caméra demandée
        cred = f"sentinelle:{quote(self.jeton, safe='')}@"
        base_rtsp = f"rtsp://{cred}{hote}:{int(relay.get('port', 8554))}/"

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
                cfg.sequences.append(Sequence(nom=str(s.get("nom", "")), etapes=etapes))
        return cfg

    def config_admin(self) -> AppConfig:
        """Configuration complète pour l'édition (mots de passe vides = conservés)."""
        data = self._req("GET", "/api/config/full").json()
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
        from .config import obfusquer
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._resp = None

    def surveiller(self, _cameras=None):
        """Démarre l'écoute (la liste des caméras est gérée côté serveur)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._boucle, daemon=True,
                                        name="motion-sse")
        self._thread.start()

    def stop(self):
        self._stop.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()        # débloque iter_lines immédiatement
            except Exception:
                pass
        self._thread = None

    def _boucle(self):
        while not self._stop.is_set():
            try:
                r = requests.get(
                    self._serveur.url_events(),
                    headers={"Authorization": f"Bearer {self._serveur.jeton}"},
                    stream=True, timeout=(3.05, 40))
                if r.status_code != 200:
                    raise ErreurServeur(f"HTTP {r.status_code}")
                self._resp = r
                for ligne in r.iter_lines():
                    if self._stop.is_set():
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
                if self._stop.is_set():
                    return
                logger.info(f"Événements serveur interrompus ({e}) — reconnexion")
                time.sleep(2)
            finally:
                self._resp = None
