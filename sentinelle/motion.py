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
        self._actifs = {}                   # camera_id -> timestamp du dernier « actif »
        self._lock = threading.Lock()
        self._retombee = QTimer(self)
        self._retombee.setInterval(1000)
        self._retombee.timeout.connect(self._verifier_retombee)

    def actifs(self) -> set:
        with self._lock:
            return set(self._actifs)

    # ------------------------------------------------------------- cycle de vie

    def surveiller(self, cameras: list):
        """(Re)définit la liste des caméras surveillées."""
        voulus = {c.id: c for c in cameras if c.hote and c.user}
        for cam_id in list(self._threads):
            if cam_id not in voulus:
                self._arreter_cam(cam_id)
        for cam_id, cam in voulus.items():
            if cam_id not in self._threads:
                self._demarrer_cam(cam)
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
        self._signaler(cam_id, False)

    # ---------------------------------------------------------------- interne

    def _boucle(self, cam, ev: threading.Event):
        from .onvif import OnvifCamera
        echecs = 0
        while not ev.is_set():
            try:
                oc = OnvifCamera(cam.hote, cam.user, cam.password, port=cam.port_http)
                endpoint = oc.abonner_mouvement("PT1M")
            except Exception as e:
                echecs += 1
                if echecs >= 3:
                    logger.info(f"[{cam.id}] pas d'événements ONVIF ({e})")
                    return
                self._attendre(ev, min(2 ** echecs, 20))
                continue
            echecs = 0
            t_renouv = time.time() + 50
            while not ev.is_set():
                try:
                    evenements = oc.tirer_mouvement(endpoint, "PT5S")
                except Exception:
                    break                       # ré-abonnement
                for _source, actif in evenements:
                    if actif:
                        self._marquer_actif(cam.id)
                    else:
                        self._signaler(cam.id, False)
                        with self._lock:
                            self._actifs.pop(cam.id, None)
                if time.time() > t_renouv:
                    break                       # renouvelle l'abonnement

    def _marquer_actif(self, cam_id):
        with self._lock:
            nouveau = cam_id not in self._actifs
            self._actifs[cam_id] = time.time()
        if nouveau:
            self.motion_changed.emit(cam_id, True)

    def _signaler(self, cam_id, actif):
        self.motion_changed.emit(cam_id, actif)

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
