"""Détection de mouvement via les événements ONVIF.

Pour chaque caméra joignable en ONVIF, un thread s'abonne au service Events
(PullPoint) et tire les notifications en continu. Quand une caméra signale un
mouvement (topic Motion / SimpleItem IsMotion), le moniteur émet un signal Qt.

Un mouvement est considéré comme terminé soit sur un événement « faux », soit
après un délai sans rafraîchissement (certaines caméras n'émettent que le début).
Robustesse : une caméra sans service Events voit simplement son thread s'arrêter,
sans gêner les autres.
"""

import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)

RETOMBEE_S = 8          # mouvement effacé après ce délai sans nouvel événement « actif »


class MotionMonitor(QObject):
    """Surveille le mouvement d'un ensemble de caméras via ONVIF Events."""

    motion_changed = Signal(str, bool)      # camera_id, actif

    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads = {}                  # camera_id -> thread
        self._stop = {}                     # camera_id -> Event
        self._ident = {}                    # camera_id -> empreinte (hote/user/mdp/port)
        self._actifs = {}                   # camera_id -> timestamp du dernier « actif »
        self._lock = threading.Lock()
        self._retombee = QTimer(self)
        self._retombee.setInterval(1000)
        self._retombee.timeout.connect(self._verifier_retombee)

    def actifs(self) -> set:
        with self._lock:
            return set(self._actifs)

    # ------------------------------------------------------------- cycle de vie

    @staticmethod
    def _empreinte(cam) -> tuple:
        # relancer le thread si l'adresse/les identifiants changent (sinon on
        # interroge l'ancien hôte, avec un risque de lockout du compte DVR)
        return (cam.hote, cam.user, cam.password, cam.port_http)

    def surveiller(self, cameras: list):
        """(Re)définit la liste des caméras surveillées."""
        voulus = {c.id: c for c in cameras if c.hote and c.user}
        for cam_id in list(self._threads):
            if cam_id not in voulus or self._ident.get(cam_id) != self._empreinte(voulus[cam_id]):
                self._arreter_cam(cam_id)
        for cam_id, cam in voulus.items():
            th = self._threads.get(cam_id)
            if th is None or not th.is_alive():
                # thread jamais lancé, ou mort (abandon 401) : (re)démarrer —
                # un rechargement de config redonne ainsi sa chance à la caméra
                if th is not None:
                    self._arreter_cam(cam_id)
                self._demarrer_cam(cam)
                self._ident[cam_id] = self._empreinte(cam)
        if self._threads and not self._retombee.isActive():
            self._retombee.start()

    def stop(self):
        self._retombee.stop()
        for cam_id in list(self._threads):
            self._arreter_cam(cam_id)
        with self._lock:
            self._actifs.clear()

    def _demarrer_cam(self, cam):
        ev = threading.Event()
        th = threading.Thread(target=self._boucle, args=(cam, ev), daemon=True,
                              name=f"motion-{cam.id}")
        self._threads[cam.id] = th
        self._stop[cam.id] = ev
        th.start()

    def _arreter_cam(self, cam_id):
        ev = self._stop.pop(cam_id, None)
        if ev:
            ev.set()
        self._threads.pop(cam_id, None)
        self._ident.pop(cam_id, None)
        self._signaler(cam_id, False)

    # ---------------------------------------------------------------- interne

    def _boucle(self, cam, ev: threading.Event):
        from .onvif import OnvifCamera
        echecs = 0
        while not ev.is_set():
            try:
                oc = OnvifCamera(cam.hote, cam.user, cam.password, port=cam.port_http)
                endpoint = oc.abonner_mouvement("PT1M")
            except PermissionError as e:
                # identifiants refusés : on n'insiste JAMAIS (lockout du compte
                # DVR) ; l'entrée est retirée pour qu'un rechargement de config
                # avec identifiants corrigés puisse relancer la surveillance.
                logger.warning(f"[{cam.id}] événements ONVIF refusés ({e}) — abandon")
                self._oublier(cam.id)
                return
            except Exception as e:
                # site 4G coupé, caméra qui redémarre… : on n'abandonne plus
                # définitivement, on espace les tentatives (plafond 5 min)
                echecs += 1
                if echecs == 3:
                    logger.info(f"[{cam.id}] événements ONVIF indisponibles ({e}) "
                                "— nouvelles tentatives espacées")
                self._attendre(ev, min(2 ** min(echecs, 8), 300))
                continue
            echecs = 0
            t_renouv = time.time() + 50
            while not ev.is_set():
                try:
                    evenements = oc.tirer_mouvement(endpoint, "PT5S")
                except Exception:
                    break                       # ré-abonnement
                if ev.is_set():
                    return          # arrêté pendant le PullMessages (jusqu'à 12 s) :
                                    # ne pas surligner une tuile après stop()
                for _source, actif in evenements:
                    if actif:
                        self._marquer_actif(cam.id)
                    else:
                        self._signaler(cam.id, False)
                        with self._lock:
                            self._actifs.pop(cam.id, None)
                if time.time() > t_renouv:
                    oc.desabonner_mouvement(endpoint)   # libère avant de ré-abonner
                    break                       # renouvelle l'abonnement

    def _oublier(self, cam_id):
        """Retire une caméra abandonnée pour que surveiller() puisse relancer."""
        self._threads.pop(cam_id, None)
        self._stop.pop(cam_id, None)
        self._ident.pop(cam_id, None)

    def _marquer_actif(self, cam_id):
        with self._lock:
            nouveau = cam_id not in self._actifs
            self._actifs[cam_id] = time.time()
        if nouveau:
            self._signaler(cam_id, True)

    def _signaler(self, cam_id, actif):
        try:
            self.motion_changed.emit(cam_id, actif)
        except RuntimeError:
            pass                    # objet Qt détruit (fermeture de l'application)

    def _verifier_retombee(self):
        maintenant = time.time()
        expires = []
        with self._lock:
            for cam_id, t in list(self._actifs.items()):
                if maintenant - t > RETOMBEE_S:
                    expires.append(cam_id)
                    del self._actifs[cam_id]
        for cam_id in expires:
            self.motion_changed.emit(cam_id, False)

    def _attendre(self, ev, s):
        fin = time.time() + s
        while time.time() < fin and not ev.is_set():
            time.sleep(0.1)
