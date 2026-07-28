"""Détection de mouvement ONVIF côté serveur.

Même logique que le moniteur du client autonome (abonnement PullPoint par
caméra, tirage continu, retombée après silence), mais en threads purs — pas de
Qt côté serveur. Un seul abonnement ONVIF par caméra pour tout le parc : les
clients reçoivent les événements via le flux SSE de l'API.

L'EventHub fait le pont threads → asyncio : il déduplique les transitions
(actif/inactif) et distribue chaque événement aux abonnés SSE connectés.
"""

import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)

RETOMBEE_S = 8      # mouvement effacé après ce délai sans nouvel événement « actif »


class EventHub:
    """État des mouvements + diffusion aux abonnés SSE (thread-safe)."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[asyncio.Queue] = []
        self._actifs: set[str] = set()
        self._lock = threading.Lock()

    def abonner(self) -> asyncio.Queue:
        q = asyncio.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def desabonner(self, q: asyncio.Queue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def actifs_courants(self) -> list[str]:
        with self._lock:
            return sorted(self._actifs)

    def publier(self, cam_id: str, actif: bool):
        """Appelé depuis les threads de surveillance. N'émet que les transitions."""
        with self._lock:
            if actif == (cam_id in self._actifs):
                return
            (self._actifs.add if actif else self._actifs.discard)(cam_id)
            subs = list(self._subs)
        if self.loop is None:
            return
        evt = {"camera": cam_id, "actif": actif}
        for q in subs:
            self.loop.call_soon_threadsafe(q.put_nowait, evt)


class MotionMonitor:
    """Surveille le mouvement d'un ensemble de caméras via ONVIF Events."""

    def __init__(self, on_change):
        self._on_change = on_change            # callable(cam_id, actif)
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._ident: dict[str, tuple] = {}     # cam_id -> empreinte (hote/user/mdp/port)
        self._actifs: dict[str, float] = {}    # cam_id -> dernier « actif »
        self._lock = threading.Lock()
        self._checker: threading.Thread | None = None
        self._fini = threading.Event()

    @staticmethod
    def _empreinte(cam) -> tuple:
        # une caméra dont l'adresse ou les identifiants changent doit voir son
        # thread relancé : sinon on continue d'interroger l'ancien hôte (et des
        # échecs d'auth répétés peuvent verrouiller le compte DVR)
        return (cam.hote, cam.user, cam.password, cam.port_http)

    def surveiller(self, cameras: list):
        """(Re)définit la liste des caméras surveillées."""
        voulus = {c.id: c for c in cameras if c.hote and c.user}
        for cam_id in list(self._threads):
            if cam_id not in voulus or self._ident.get(cam_id) != self._empreinte(voulus[cam_id]):
                self._arreter_cam(cam_id)
        for cam_id, cam in voulus.items():
            if cam_id not in self._threads:
                ev = threading.Event()
                th = threading.Thread(target=self._boucle, args=(cam, ev),
                                      daemon=True, name=f"motion-{cam_id}")
                self._threads[cam_id] = th
                self._stops[cam_id] = ev
                self._ident[cam_id] = self._empreinte(cam)
                th.start()
        if self._threads and self._checker is None:
            self._checker = threading.Thread(target=self._verifier_retombee,
                                             daemon=True, name="motion-retombee")
            self._checker.start()

    def stop(self):
        self._fini.set()
        for cam_id in list(self._threads):
            self._arreter_cam(cam_id)
        with self._lock:
            self._actifs.clear()

    def _arreter_cam(self, cam_id: str):
        ev = self._stops.pop(cam_id, None)
        if ev:
            ev.set()
        self._threads.pop(cam_id, None)
        self._ident.pop(cam_id, None)
        with self._lock:
            self._actifs.pop(cam_id, None)
        self._on_change(cam_id, False)

    # ---------------------------------------------------------------- interne

    def _boucle(self, cam, ev: threading.Event):
        from sentinelle.onvif import OnvifCamera
        echecs = 0
        signale = False
        while not ev.is_set():
            try:
                oc = OnvifCamera(cam.hote, cam.user, cam.password, port=cam.port_http)
                endpoint = oc.abonner_mouvement("PT1M")
            except PermissionError as e:
                # identifiants refusés : on n'insiste JAMAIS — des échecs d'auth
                # répétés peuvent verrouiller le compte du DVR. Le thread s'arrête
                # ; une nouvelle config (identifiants corrigés) le relancera.
                logger.warning(f"[{cam.id}] événements ONVIF refusés ({e}) — abandon")
                return
            except Exception as e:
                echecs += 1
                # hors auth (DVR qui redémarre, site 4G coupé…) : NE JAMAIS
                # abandonner définitivement — la détection doit reprendre seule
                # dès que l'appareil répond. On journalise une fois (au 3e échec)
                # puis on réessaie avec un backoff plafonné.
                if echecs == 3 and not signale:
                    logger.info(f"[{cam.id}] pas d'événements ONVIF ({e}) — "
                                f"nouvelles tentatives en tâche de fond")
                    signale = True
                self._attendre(ev, min(2 ** min(echecs, 6), 60))
                continue
            if signale:
                logger.info(f"[{cam.id}] événements ONVIF rétablis")
            echecs = 0
            signale = False
            t_renouv = time.time() + 50
            while not ev.is_set():
                try:
                    evenements = oc.tirer_mouvement(endpoint, "PT5S")
                except Exception:
                    break                       # ré-abonnement
                for _source, actif in evenements:
                    if actif:
                        with self._lock:
                            self._actifs[cam.id] = time.time()
                        self._on_change(cam.id, True)
                    else:
                        with self._lock:
                            self._actifs.pop(cam.id, None)
                        self._on_change(cam.id, False)
                if time.time() > t_renouv:
                    oc.desabonner_mouvement(endpoint)   # libère avant de ré-abonner
                    break                       # renouvelle l'abonnement

    def _verifier_retombee(self):
        while not self._fini.is_set():
            time.sleep(1)
            maintenant = time.time()
            expires = []
            with self._lock:
                for cam_id, t in list(self._actifs.items()):
                    if maintenant - t > RETOMBEE_S:
                        expires.append(cam_id)
                        del self._actifs[cam_id]
            for cam_id in expires:
                self._on_change(cam_id, False)

    @staticmethod
    def _attendre(ev: threading.Event, s: float):
        ev.wait(s)
