"""Tuile vidéo : un flux RTSP, son état et sa politique de reconnexion.

Machine d'états (patterns repris de vision-ai/capture.py) :
  IDLE → CONNECTING → PLAYING
                    ↘ BACKOFF (timeout/réseau : 5 s → 10 min, ×2, reset au succès)
                    ↘ AUTH_FAILED (401 : ARRÊT DÉFINITIF — jamais de retry auto,
                       sinon lockout du compte côté DVR Hikvision)

Chaque tuile a sa propre instance libmpv (thread mpv indépendant) : un flux qui
meurt n'affecte jamais les autres tuiles.

RÈGLE ABSOLUE : aucun appel à libmpv depuis le thread Qt. Toute commande et
toute lecture de propriété entre dans le cœur de mpv, qui peut être bloqué dans
une lecture réseau sur un flux figé (network_timeout=15 s, courant sur un site
4G). Un simple `stop` ou une lecture de débit gelait alors TOUTE l'interface —
et une page ouverte par-dessus (administration, configuration) restait blanche
et inerte. Tout passe donc par le thread FIFO de la tuile (voir `_mpv_appel`).
"""

import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSizePolicy, QStackedLayout, QVBoxLayout, QWidget)

from ..config import Camera, mask_url
from ..player import MPV_IMPORT_ERROR, create_player, mpv_disponible
from ..probe import classify_text, ffprobe_available, probe_rtsp
from .icons import icon

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15
BACKOFF_MIN = 5
BACKOFF_MAX = 600
BACKOFF_FACTOR = 2

# Raisons end-file de libmpv (client.h). evt.data.reason est un ENTIER nu
# (str() donne « 2 », jamais « stop » — l'ancien filtre textuel ne matchait
# jamais et chaque arrêt volontaire était compté comme un échec).
# 2 = STOP : arrêt demandé (command("stop") ou remplacement par loadfile) ;
# 3 = QUIT. Ni l'un ni l'autre n'est un échec ; 0 = EOF (flux live coupé) et
# 4 = ERROR restent des échecs à reconnecter.
_ENDFILE_BENIN = (2, 3)

# threads mpv chargés d'une libération encore en vol (voir _liberer_player) :
# joints à la fermeture de l'application pour ne pas tuer un terminate() en
# plein démontage
_liberations_lock = threading.Lock()
_liberations: set = set()


def attendre_liberations(timeout_s: float = 5.0):
    """Attend (borné) la fin des libérations mpv en arrière-plan — à appeler à
    la fermeture : sortir du process pendant un terminate() laissait mpv en
    course avec la destruction des fenêtres natives."""
    import time
    fin = time.time() + timeout_s
    with _liberations_lock:
        threads = list(_liberations)
    for th in threads:
        restant = fin - time.time()
        if restant <= 0:
            break
        th.join(restant)

_libx11 = None


def _mapper_enfants_x11(wid: int):
    """Ceinture de sécurité contre le bug d'incrustation de mpv (x11_common) :
    quand le MapNotify du parent arrive pendant l'initialisation de mpv, mpv le
    prend pour celui de SA fenêtre enfant et ne la mappe jamais — le flux est
    décodé mais la tuile reste noire. XMapWindow étant idempotent, on mappe
    toute fenêtre enfant du wid restée cachée. Sans effet hors X11/XWayland."""
    global _libx11
    if sys.platform == "win32":
        return
    try:
        from PySide6.QtGui import QGuiApplication
        if not QGuiApplication.platformName().lower().startswith("xcb"):
            return
        import ctypes
        if _libx11 is None:
            x = ctypes.CDLL("libX11.so.6")
            x.XOpenDisplay.restype = ctypes.c_void_p
            x.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x.XQueryTree.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                ctypes.POINTER(ctypes.c_uint)]
            x.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x.XFree.argtypes = [ctypes.c_void_p]
            x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
            d = x.XOpenDisplay(None)
            if not d:
                return
            _libx11 = (x, d)
        x, d = _libx11
        racine, parent = ctypes.c_ulong(), ctypes.c_ulong()
        enfants, n = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not x.XQueryTree(d, wid, ctypes.byref(racine), ctypes.byref(parent),
                            ctypes.byref(enfants), ctypes.byref(n)):
            return
        for i in range(n.value):
            x.XMapWindow(d, enfants[i])
        if enfants:
            x.XFree(enfants)
        x.XSync(d, 0)
    except Exception:
        pass                # le pire cas doit rester « pas de vidéo », pas un crash


KIND_LABELS = {
    "timeout": "délai dépassé",
    "network": "site injoignable",
    # mode serveur uniquement : jeton relais refusé (expiré/révoqué), rafraîchi
    # par le contrôle de session — l'accès direct DVR ne passe jamais ici
    "auth": "accès refusé (jeton en cours de rafraîchissement)",
    "other": "erreur de lecture",
}


class TileState(Enum):
    IDLE = auto()
    CONNECTING = auto()
    PLAYING = auto()
    BACKOFF = auto()
    AUTH_FAILED = auto()
    NO_PLAYER = auto()      # libmpv absent


# Jeton de couleur du liseré par état. La couleur n'apparaît QUE lorsqu'il y a
# quelque chose à signaler : une tuile qui lit correctement n'a qu'un cerne
# neutre. Un mur sain est donc entièrement gris — le moindre liseré coloré se
# repère de loin, sans avoir à lire une pastille de 10 px.
_BEZEL_TOKENS = {
    TileState.IDLE: "bezel_idle",
    TileState.CONNECTING: "warn",
    TileState.PLAYING: "bezel",
    TileState.BACKOFF: "warn",
    TileState.AUTH_FAILED: "danger",
    TileState.NO_PLAYER: "danger",
}

_BEZEL_PX = 2               # constant : voir theme.py (« bezel »)


def snapshot_path(camera) -> str:
    """Chemin horodaté pour une capture manuelle (Images/Sentinelle/).

    Suit le dossier « images » réel du poste (xdg-user-dirs : ~/Images sur un
    Debian francophone, Pictures/OneDrive sous Windows) — le chemin anglophone
    codé en dur créait un ~/Pictures parallèle invisible dans GNOME Fichiers."""
    base = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
    dossier = Path(base or Path.home()) / "Sentinelle"
    dossier.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(dossier / f"{camera.id}-{stamp}.jpg")


def format_debit(bps: float) -> str:
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mb/s"
    return f"{bps / 1000:.0f} kb/s"


class _VideoSurface(QWidget):
    """Widget natif dans lequel mpv dessine (via wid)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setStyleSheet("background-color: #0a0b0d;")   # zone vidéo : toujours sombre
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)


class VideoTile(QFrame):
    """Une caméra affichée, dans une vue donnée ('grille' ou 'mono')."""

    double_clicked = Signal(str)            # camera_id
    state_changed = Signal()
    snapshot_saved = Signal(str)            # chemin de la capture manuelle

    # signaux internes — émis depuis le thread mpv / threads de probe,
    # délivrés sur le thread Qt (queued)
    _evt_playing = Signal(int)              # génération
    _evt_ended = Signal(int, int)           # génération, reason (code libmpv)
    _probe_done = Signal(int, str, str)     # génération, kind, detail
    _libere = Signal()                      # terminate() mpv fini (dispose)
    _debit_lu = Signal(int, float)           # génération, bits/s (lus hors UI)
    _hwdec_lu = Signal(str)                  # mode de décodage réel (lu hors UI)

    def __init__(self, camera: Camera, vue: str, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.vue = vue
        self.state = TileState.IDLE
        self.debit_bps = 0.0

        self._player = None
        self._url = camera.url_pour_vue(vue)
        self._stopping = False
        self._failures = 0
        self._probing = False
        self._gen = 0                       # génération : invalide les résultats async périmés
        self._zoom = 0.0                    # zoom numérique (video-zoom mpv, log2)
        self._ptz_cam = None
        self._ptz_queue = None              # file FIFO : Stop suit toujours Move
        self._ptz_thread = None
        self._ptz_moving = False
        self._mpv_queue = None              # file FIFO des appels libmpv (hors UI)
        self._mpv_thread = None
        self._debit_en_vol = False          # une lecture de débit est déjà en file
        self._aspect_mode = "fit"            # fit | crop | stretch
        self._motion_on = False              # surlignage « mouvement détecté »
        self._controls = None
        self._log_tail = deque(maxlen=80)   # dernières lignes mpv pour diagnostic
        self._hwdec_signale = False         # avertissement « décodage logiciel » émis

        self._build_ui()

        self._debit_timer = QTimer(self)
        self._debit_timer.setInterval(2000)
        self._debit_timer.timeout.connect(self._update_debit)

        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.setInterval(CONNECT_TIMEOUT_S * 1000)
        self._connect_timer.timeout.connect(self._on_connect_timeout)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._connect)

        self._preventive_timer = QTimer(self)
        self._preventive_timer.timeout.connect(self._preventive_reconnect)

        self._evt_playing.connect(self._on_playing)
        self._evt_ended.connect(self._on_ended)
        self._probe_done.connect(self._on_probe_done)
        self._debit_lu.connect(self._on_debit_lu)
        self._hwdec_lu.connect(self._on_hwdec_lu)

    # --------------------------------------------------- appels libmpv (hors UI)
    #
    # Un seul thread par tuile, en FIFO : l'ordre des commandes est conservé
    # (un « stop » ne peut pas devancer le « loadfile » qu'il annule) et le
    # handle mpv n'est plus jamais utilisé après son terminate(), qui est le
    # dernier travail de la file. Voir l'en-tête du module pour le pourquoi.

    def _mpv_appel(self, travail):
        """Empile un appel libmpv sur le thread mpv de la tuile."""
        if self._mpv_queue is None:
            import queue
            q = self._mpv_queue = queue.Queue()
            cam_id = self.camera.id         # jamais `self` dans le thread : la
                                            # tuile peut être détruite avant lui

            def worker():
                while True:
                    job = q.get()
                    if job is None:
                        return
                    try:
                        job()
                    except Exception as e:
                        logger.debug(f"[{cam_id}] appel mpv ignoré : {e}")

            self._mpv_thread = threading.Thread(target=worker, daemon=True,
                                                name=f"mpv-{cam_id}")
            self._mpv_thread.start()
        self._mpv_queue.put(travail)

    def _liberer_player(self, fin=None):
        """Empile le terminate() du lecteur puis ferme la file : plus aucun
        appel ne peut suivre (handle libéré). Retourne le thread chargé de la
        libération (None s'il n'y a rien à libérer), enregistré pour que
        `attendre_liberations` le borne à la fermeture de l'application."""
        player, self._player = self._player, None
        if player is None:
            if self._mpv_queue is not None:      # file sans lecteur : on la ferme
                self._mpv_queue.put(None)
                self._mpv_queue, self._mpv_thread = None, None
            return None
        if self._mpv_queue is None:
            self._mpv_appel(lambda: None)   # lecteur créé mais jamais sollicité
        q, th = self._mpv_queue, self._mpv_thread
        self._mpv_queue, self._mpv_thread = None, None

        def liberer():
            try:
                player.terminate()
            except Exception:
                pass
            finally:
                with _liberations_lock:
                    _liberations.discard(th)
            if fin is not None:
                fin()

        with _liberations_lock:
            _liberations.add(th)
        q.put(liberer)
        q.put(None)                         # le thread s'arrête après terminate()
        return th

    def _emettre_libere(self):
        try:
            self._libere.emit()
        except RuntimeError:
            pass                            # widget déjà détruit (fermeture d'appli)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        from .widgets import TileCaption
        self._caption = TileCaption(self.camera.nom, self.camera.site.nom)
        self._caption.set_data(self._flux_text())

        body = QWidget()
        self._stack = QStackedLayout(body)
        # StackAll : la surface vidéo reste visible (fenêtre X mappée) EN
        # PERMANENCE, le texte d'état opaque s'affiche PAR-DESSUS. En mode
        # StackOne, la fenêtre native de _video n'était mappée qu'au passage
        # en lecture — or ce MapNotify du parent arrive pendant l'initialisation
        # de mpv (déclenchée par le même événement file-loaded), et mpv le
        # confond avec celui de SA fenêtre enfant (x11_common ne filtre pas) :
        # il ne mappe alors JAMAIS sa fenêtre → flux décodé mais tuile noire.
        # Parent mappé d'emblée = plus de MapNotify tardif à confondre.
        # (Diagnostiqué sur mur GLK/XWayland, mpv 0.40 ; voir aussi
        # _mapper_enfants_x11, la ceinture de sécurité.)
        self._stack.setStackingMode(QStackedLayout.StackAll)
        self._video = _VideoSurface()
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setWordWrap(True)
        self._stack.addWidget(self._status)   # index 0 — couche du dessus
        self._stack.addWidget(self._video)    # index 1 — toujours visible dessous
        self._stack.setCurrentIndex(0)        # l'état reste la couche haute
        root.addWidget(body, 1)
        root.addWidget(self._caption)          # identité SOUS l'image

        # barre de commandes (vue mono) : zoom numérique + PTZ si motorisée
        if self.vue == "mono":
            self._controls = self._build_controls()
            root.addWidget(self._controls)

        self.restyle()
        self._set_state(TileState.IDLE, "En attente")

    def restyle(self):
        """(Ré)applique les couleurs du thème courant sans couper le flux."""
        from .theme import t
        self._apply_frame_style()
        self._caption.restyle()
        self._status.setStyleSheet(
            f"color: {t('tile_status_text')}; background-color: {t('video_bg')}; "
            f"padding: 12px;")
        if self._controls is not None:
            self._controls.setStyleSheet(f"background-color: {t('tile_header')};")

    def _apply_frame_style(self):
        """Le liseré EST l'indicateur d'état — d'où l'absence de pastille.

        Sa largeur ne change jamais : la surface vidéo n'est donc pas
        redimensionnée à chaque changement d'état (mpv reste tranquille)."""
        from .theme import t
        couleur = t("text") if self._motion_on else t(_BEZEL_TOKENS[self.state])
        self.setStyleSheet(
            f"VideoTile {{ background-color: {t('tile_bg')}; "
            f"border: {_BEZEL_PX}px solid {couleur}; }}")

    def _build_controls(self) -> QWidget:
        """Barre de commandes de la vue plein cadre.

        Deux amas nommés, aux extrémités : à gauche ce qui bouge la caméra
        (orientation, zoom optique), à droite ce qui ne fait que recadrer
        l'image reçue (zoom numérique). Sans ces intitulés, deux jeux de
        boutons « + / − » voisins laissaient croire qu'ils font la même
        chose."""
        from .theme import police_ui, t

        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 5)
        h.setSpacing(3)

        def intitule(texte: str) -> QLabel:
            lbl = QLabel(texte)
            lbl.setObjectName("hint")
            lbl.setFont(police_ui(12))
            lbl.setStyleSheet(f"color: {t('text_faint')}; background: transparent;")
            return lbl

        def touche(libelle: str, tip: str, largeur: int = 32) -> QPushButton:
            b = QPushButton(libelle)
            b.setObjectName("compact")
            b.setFixedWidth(largeur)
            b.setToolTip(tip)
            return b

        if self.camera.ptz:
            h.addWidget(intitule("Orientation"))
            h.addSpacing(4)
            for libelle, dx, dy, info in (
                ("↖", -0.5, 0.5, "haut-gauche"), ("↑", 0, 0.5, "haut"),
                ("↗", 0.5, 0.5, "haut-droite"), ("←", -0.5, 0, "gauche"),
                ("→", 0.5, 0, "droite"), ("↙", -0.5, -0.5, "bas-gauche"),
                ("↓", 0, -0.5, "bas"), ("↘", 0.5, -0.5, "bas-droite"),
            ):
                b = touche(libelle, f"Orienter vers le {info} (maintenir enfoncé)")
                b.pressed.connect(lambda x=dx, y=dy: self._ptz(x, y, 0))
                b.released.connect(self._ptz_stop)
                h.addWidget(b)

            h.addSpacing(14)
            h.addWidget(intitule("Zoom optique"))
            h.addSpacing(4)
            for libelle, dz, tip in (("+", 0.5, "Zoom optique (maintenir enfoncé)"),
                                     ("−", -0.5, "Dézoom optique (maintenir enfoncé)")):
                b = touche(libelle, tip)
                b.pressed.connect(lambda z=dz: self._ptz(0, 0, z))
                b.released.connect(self._ptz_stop)
                h.addWidget(b)

        h.addStretch(1)
        h.addWidget(intitule("Zoom numérique"))
        h.addSpacing(4)
        for libelle, fn, tip in (("+", self.zoom_in, "Agrandir l'image reçue"),
                                 ("−", self.zoom_out, "Réduire l'agrandissement"),
                                 ("", self.zoom_reset, "Revenir au cadrage d'origine")):
            b = touche(libelle, tip)
            if not libelle:
                b.setIcon(icon("rotate"))       # glyphe ⟳ absent de trop de polices
                b.setIconSize(QSize(13, 13))
            b.clicked.connect(fn)
            h.addWidget(b)
        return bar

    def _flux_text(self) -> str:
        flux = self.camera.flux_pour_vue(self.vue)
        eco = " · éco" if self.camera.profil.startswith("eco") else ""
        return ("HD" if flux == "main" else "SD") + eco

    def _set_state(self, state: TileState, message: str = ""):
        self.state = state
        self._apply_frame_style()
        if state == TileState.PLAYING:
            # on cache l'étiquette au lieu de changer de page : la surface
            # vidéo ne doit jamais être dé-mappée/re-mappée (voir _build_ui)
            self._status.hide()
        else:
            self._status.setText(message)
            self._status.show()
        self.state_changed.emit()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self.camera.id)
        event.accept()

    def set_motion(self, actif: bool):
        """Mouvement détecté : le liseré et le bandeau passent en blanc.

        Achromatique volontairement — un mouvement n'est pas une panne, et le
        rouge reste réservé aux tuiles réellement en défaut."""
        self._motion_on = actif
        self._apply_frame_style()
        self._caption.alerte(actif)

    def contextMenuEvent(self, event):
        from .icons import icon
        menu = QMenu(self)
        act_snap = menu.addAction(icon("camera"), "Enregistrer une image")
        act_snap.setEnabled(self.state == TileState.PLAYING)

        remplir = menu.addMenu("Cadrage")
        for mode, libelle in (("fit", "Ajusté (défaut)"),
                              ("crop", "Remplir en recadrant"),
                              ("stretch", "Étirer")):
            a = remplir.addAction(libelle)
            a.setCheckable(True)
            a.setChecked(self._aspect_mode == mode)
            a.triggered.connect(lambda _=False, m=mode: self.set_aspect_mode(m))

        choix = menu.exec(event.globalPos())
        if choix is act_snap:
            self._save_snapshot()

    def set_aspect_mode(self, mode: str):
        self._aspect_mode = mode
        player = self._player
        if player is None:
            return
        keepaspect = mode != "stretch"
        panscan = 1.0 if mode == "crop" else 0.0

        def appliquer():
            player["keepaspect"] = keepaspect
            player["panscan"] = panscan
        self._mpv_appel(appliquer)

    def _save_snapshot(self):
        player = self._player
        if player is None or self.state != TileState.PLAYING:
            return
        path = snapshot_path(self.camera)
        cam_id = self.camera.id

        def capturer():
            try:
                player.command("screenshot-to-file", path, "video")
            except Exception as e:
                logger.warning(f"[{cam_id}] capture impossible : {e}")
                return
            try:
                self.snapshot_saved.emit(path)
            except RuntimeError:
                pass                        # tuile détruite entre-temps
        self._mpv_appel(capturer)

    # ------------------------------------------------------ zoom numérique

    def zoom_in(self):
        self._set_zoom(self._zoom + 0.3)

    def zoom_out(self):
        self._set_zoom(self._zoom - 0.3)

    def zoom_reset(self):
        self._set_zoom(0.0)

    def _set_zoom(self, z: float):
        self._zoom = max(0.0, min(z, 3.0))
        player = self._player
        if player is None:
            return
        zoom = self._zoom

        def appliquer():
            player["video-zoom"] = zoom
            if zoom == 0.0:                       # recentre en dézoom complet
                player["video-pan-x"] = 0.0
                player["video-pan-y"] = 0.0
        self._mpv_appel(appliquer)

    # -------------------------------------------------------------- PTZ
    #
    # Toutes les commandes PTZ passent par UN seul thread (file FIFO) : ainsi le
    # Stop est toujours exécuté après le Move correspondant — jamais l'inverse
    # (sinon la caméra pourrait tourner sans fin). Filet supplémentaire : le
    # ContinuousMove porte un Timeout côté caméra (voir onvif.ptz_move).

    def _ptz_ensure_worker(self):
        if self._ptz_queue is not None:
            return
        import queue
        cam = self.camera
        remote = getattr(cam, "remote", None)
        if remote is not None:
            # mode serveur : le PTZ est relayé par l'API (les identifiants DVR
            # ne sont pas sur le poste client)
            def move(pan, tilt, zoom):
                remote.ptz_move(cam.id, pan, tilt, zoom)

            def stop():
                remote.ptz_stop(cam.id)
        else:
            from ..onvif import OnvifCamera
            self._ptz_cam = OnvifCamera(cam.hote, cam.user, cam.password,
                                        port=cam.port_http)
            tok = cam.onvif_profile

            def move(pan, tilt, zoom):
                self._ptz_cam.ptz_move(tok, pan, tilt, zoom)

            def stop():
                self._ptz_cam.ptz_stop(tok)

        q = queue.Queue()
        self._ptz_queue = q

        def worker():
            while True:
                job = q.get()               # file capturée localement (pas self._…)
                if job is None:
                    return
                kind, args = job
                try:
                    if kind == "move":
                        move(*args)
                    else:
                        stop()
                except Exception as e:
                    logger.warning(f"[{cam.id}] PTZ {kind}: {e}")

        self._ptz_thread = threading.Thread(target=worker, daemon=True,
                                            name=f"ptz-{cam.id}")
        self._ptz_thread.start()

    def _ptz(self, pan: float, tilt: float, zoom: float):
        if not self.camera.ptz:
            return
        self._ptz_ensure_worker()
        self._ptz_moving = True
        self._ptz_queue.put(("move", (pan, tilt, zoom)))

    def _ptz_stop(self):
        if not self.camera.ptz or self._ptz_queue is None or not self._ptz_moving:
            return
        self._ptz_moving = False
        self._ptz_queue.put(("stop", ()))

    def _ptz_shutdown(self):
        q = self._ptz_queue
        if q is None:
            return
        if self._ptz_moving:                 # tuile détruite bouton enfoncé → stop
            q.put(("stop", ()))
            self._ptz_moving = False
        q.put(None)                          # termine le worker (file capturée localement)
        self._ptz_queue = None

    def _update_debit(self):
        """Demande le débit réseau réellement consommé par la tuile.

        La LECTURE part sur le thread mpv : `cache-speed` prend le verrou du
        demuxer, que le thread réseau garde pendant toute une lecture bloquée —
        interrogé depuis le thread Qt, ce compteur d'affichage gelait la fenêtre
        entière pendant le timeout réseau d'une seule caméra."""
        player = self._player
        if player is None or self.state != TileState.PLAYING or self._debit_en_vol:
            return                          # lecture précédente encore en file
        self._debit_en_vol = True
        gen = self._gen

        def lire():
            bps = 0.0
            try:
                speed = player.cache_speed            # octets/s lus sur le réseau
                if speed:
                    bps = float(speed) * 8
            except Exception:
                try:
                    bps = float(player.video_bitrate or 0)
                except Exception:
                    bps = 0.0
            try:
                self._debit_lu.emit(gen, bps)
            except RuntimeError:
                pass                        # tuile détruite entre-temps
        self._mpv_appel(lire)

    def _on_debit_lu(self, gen: int, bps: float):
        self._debit_en_vol = False
        if gen != self._gen or self.state != TileState.PLAYING:
            return                          # lecture d'une connexion périmée
        self.debit_bps = bps
        base = self._flux_text()
        self._caption.set_data(f"{base} · {format_debit(bps)}" if bps else base)

    # ---------------------------------------------------------- cycle de vie

    def start(self):
        """(Re)démarre le flux. Ne retente jamais un échec d'authentification."""
        if self.state == TileState.AUTH_FAILED:
            return
        if not mpv_disponible():
            self._set_state(TileState.NO_PLAYER,
                            f"Lecteur vidéo (libmpv) introuvable.\n{MPV_IMPORT_ERROR}")
            return
        self._stopping = False
        self._connect()

    def stop(self, message: str = "En pause"):
        """Ferme le flux réseau (caméra hors écran = zéro connexion)."""
        self._stopping = True
        self._gen += 1              # invalide toute sonde/diagnostic en vol
        self._probing = False
        self._connect_timer.stop()
        self._retry_timer.stop()
        self._preventive_timer.stop()
        self._debit_timer.stop()
        self._debit_en_vol = False
        self.debit_bps = 0.0
        self._caption.set_data(self._flux_text())
        player = self._player
        if player is not None:
            # « stop » démonte le flux RTSP : bloquant, jamais sur le thread Qt
            self._mpv_appel(lambda: player.command("stop"))
        if self.state not in (TileState.AUTH_FAILED, TileState.NO_PLAYER):
            self._set_state(TileState.IDLE, message)

    def shutdown(self):
        """Destruction de la tuile : arrête le PTZ et libère mpv.

        La libération part sur le thread mpv de la tuile — un terminate() sur un
        flux figé bloque longtemps, et l'enchaîner sur 16 tuiles depuis le thread
        Qt rendait la fermeture de l'application interminable. `closeEvent`
        borne l'attente avec `attendre_liberations`."""
        self._ptz_shutdown()            # stoppe un mouvement en cours + le worker
        self.stop()
        self._liberer_player()

    def dispose(self):
        """Comme shutdown() + deleteLater() : la tuile se cache tout de suite,
        mpv est libéré en arrière-plan, puis le widget se détruit (la fenêtre
        X11 du wid reste vivante tant que mpv ne l'a pas lâchée)."""
        self._ptz_shutdown()
        self.stop()
        self.hide()
        self._libere_fait = False
        self._libere.connect(self._liberation_finie)
        if self._liberer_player(fin=self._emettre_libere) is None:
            self._liberation_finie()    # aucun lecteur : destruction immédiate
            return
        # chien de garde : si terminate() reste bloqué (flux RTSP figé — cas connu
        # de ce projet), _libere ne serait jamais émis et le widget + le thread
        # fuiraient indéfiniment sur un poste 24/7. Passé ce délai, on détruit
        # quand même la tuile (le thread mpv, démon, mourra au pire à l'arrêt).
        # La forme à 3 arguments se déconnecte seule si la tuile est déjà détruite.
        QTimer.singleShot(15000, self, self._liberation_finie)

    def _liberation_finie(self):
        """Détruit la tuile une seule fois, que la libération de mpv ait fini
        normalement (_libere) ou que le chien de garde ait expiré."""
        if getattr(self, "_libere_fait", False):
            return
        self._libere_fait = True
        self.deleteLater()

    def retry_auth(self):
        """Réarmement MANUEL après correction des identifiants (action utilisateur
        explicite — seul cas où AUTH_FAILED est levé)."""
        if self.state == TileState.AUTH_FAILED:
            self._failures = 0
            self._set_state(TileState.IDLE, "Réessai…")
            self.start()

    # ------------------------------------------------------------- connexion

    def _ensure_player(self):
        if self._player is not None:
            return
        self._player = create_player(self._video.winId(), self._on_mpv_log)

        # les callbacks arrivent depuis le thread mpv et peuvent tomber pendant
        # la destruction de la tuile → on ignore l'émission si l'objet Qt est mort
        @self._player.event_callback("file-loaded")
        def _loaded(_evt):
            try:
                self._evt_playing.emit(self._gen)
            except RuntimeError:
                pass

        @self._player.event_callback("end-file")
        def _ended(evt):
            try:
                reason = int(getattr(evt.data, "reason", -1))
            except Exception:
                reason = -1
            try:
                self._evt_ended.emit(self._gen, reason)
            except RuntimeError:
                pass

    def _connect(self):
        if self._stopping or self.state == TileState.AUTH_FAILED:
            return
        try:
            self._ensure_player()
        except Exception as e:
            self._set_state(TileState.NO_PLAYER, f"Erreur lecteur : {e}")
            return
        # l'URL est re-résolue à CHAQUE tentative : en mode serveur le jeton de
        # session (incrusté dans l'URL du relais) est rafraîchi périodiquement —
        # une URL figée au constructeur rejouait l'ancien jeton à l'infini et la
        # tuile ne revenait jamais après une expiration.
        try:
            self._url = self.camera.url_pour_vue(self.vue)
        except Exception:
            pass                        # caméra incomplète : on garde l'URL connue
        self._gen += 1              # nouvelle tentative : périme les sondes précédentes
        self._probing = False
        self._debit_en_vol = False
        self._retry_timer.stop()    # un seul réessai armé à la fois
        self._set_state(TileState.CONNECTING, "Connexion…")
        self._log_tail.clear()
        # loadfile passe par le cœur de mpv : bloquant si le flux précédent est
        # figé. L'échec revient par le chemin normal (end-file), pas par exception.
        player, url, gen, cam_id = self._player, self._url, self._gen, self.camera.id

        def charger():
            try:
                player.play(url)
            except Exception as e:
                logger.warning(f"[{cam_id}] loadfile a échoué : {e}")
                try:
                    self._evt_ended.emit(gen, 4)      # 4 = ERROR (client.h)
                except RuntimeError:
                    pass
        self._mpv_appel(charger)
        self._connect_timer.start()

    def _verifier_apres_lecture(self):
        """3 s après le début de lecture : sortie vidéo configurée, décodeur
        stabilisé — moment fiable pour la ceinture de mappage et le diagnostic."""
        if self.state != TileState.PLAYING or self._player is None:
            return
        _mapper_enfants_x11(int(self._video.winId()))
        self._log_hwdec()

    def _log_hwdec(self):
        """Rend visible le mode de décodage réel : le repli VA-API → logiciel de
        mpv est silencieux, et c'est lui qui sature les mini-PC quand le
        pilote manque (va-driver-all non installé). Lecture hors thread Qt."""
        player = self._player
        if player is None:
            return

        def lire():
            try:
                hw = str(player.hwdec_current or "no")
            except Exception:
                return
            try:
                self._hwdec_lu.emit(hw)
            except RuntimeError:
                pass                        # tuile détruite entre-temps
        self._mpv_appel(lire)

    def _on_hwdec_lu(self, hw: str):
        logger.debug(f"[{self.camera.id}] décodage : {hw}")
        if (sys.platform != "win32" and hw == "no" and not self._hwdec_signale
                and os.environ.get("SENTINELLE_MPV_HWDEC", "") != "no"):
            self._hwdec_signale = True
            logger.warning(
                f"[{self.camera.id}] décodage LOGICIEL (VA-API indisponible ?) — "
                "charge CPU élevée ; vérifier le paquet va-driver-all")

    def _on_mpv_log(self, level, component, message):
        # appelé depuis le thread mpv — deque est thread-safe pour append
        if level in ("error", "warn", "fatal"):
            self._log_tail.append(f"{component}: {message}")
            # visibles dans le journal en --verbose : sans cela, la RAISON d'un
            # échec VA-API (ou de tout repli silencieux de mpv) restait
            # enfermée dans le tooltip de la tuile
            if (level != "warn" or "vaapi" in component
                    or "vaapi" in message.lower() or "hwdec" in message.lower()):
                logger.debug(f"[{self.camera.id}] mpv {level} "
                             f"[{component}] {message.strip()}")

    def _on_playing(self, gen: int):
        # événement d'une connexion précédente (retry/reconnexion entre-temps) → ignorer
        if gen != self._gen or self._stopping:
            return
        self._connect_timer.stop()
        self._retry_timer.stop()    # un réessai encore armé rebouclerait un flux sain
        if self._failures > 0:
            logger.info(f"[{self.camera.id}] reconnecté après {self._failures} échec(s)")
        self._failures = 0
        self._set_state(TileState.PLAYING)
        # file-loaded précède la première trame : la sortie vidéo de mpv n'est
        # pas encore configurée. On repasse dans 3 s pour (1) mapper sa fenêtre
        # si le bug d'incrustation l'a laissée cachée et (2) lire le mode de
        # décodage réellement retenu — lu tout de suite, hwdec-current répond
        # « no » à tort (faux avertissements « décodage LOGICIEL »).
        _mapper_enfants_x11(int(self._video.winId()))
        QTimer.singleShot(3000, self, self._verifier_apres_lecture)
        self._debit_timer.start()
        if self._zoom:
            self._set_zoom(self._zoom)
        if self._aspect_mode != "fit":
            self.set_aspect_mode(self._aspect_mode)
        if self.camera.reconnexion_preventive_s > 0:
            self._preventive_timer.start(self.camera.reconnexion_preventive_s * 1000)

    def _on_ended(self, gen: int, reason: int):
        if gen != self._gen:
            return          # end-file d'une connexion périmée (remplacement de flux)
        if self._stopping or self.state in (TileState.AUTH_FAILED, TileState.IDLE):
            return
        if reason in _ENDFILE_BENIN:
            return          # arrêt provoqué par nous (stop / remplacement de flux)
        self._connect_timer.stop()
        self._handle_failure()

    def _on_connect_timeout(self):
        if self._stopping or self.state != TileState.CONNECTING:
            return
        logger.warning(f"[{self.camera.id}] timeout de connexion ({CONNECT_TIMEOUT_S}s) "
                       f"sur {mask_url(self._url)}")
        player = self._player
        if player is not None:
            self._mpv_appel(lambda: player.command("stop"))
        self._handle_failure(kind_hint="timeout")

    def _preventive_reconnect(self):
        if self.state == TileState.PLAYING and not self._stopping:
            logger.info(f"[{self.camera.id}] reconnexion préventive")
            self._connect()

    # ------------------------------------------------------------- diagnostic

    def _handle_failure(self, kind_hint: str = ""):
        """Classe l'échec : auth → stop définitif ; sinon backoff exponentiel."""
        # copie figée : le thread mpv peut appender pendant qu'on itère
        log_text = "\n".join(list(self._log_tail))
        kind = classify_text(log_text)

        if kind == "auth":
            self._echec_auth(log_text[-200:])
            return

        # logs mpv peu parlants → ffprobe (si présent) tranche en arrière-plan
        if kind == "other" and not kind_hint and not ffprobe_available():
            from ..probe import avertir_ffprobe_absent
            avertir_ffprobe_absent()
        if kind == "other" and not kind_hint and ffprobe_available() and not self._probing:
            self._probing = True
            url = self._url
            gen = self._gen                 # fige la génération de cette tentative
            def work():
                k, detail = probe_rtsp(url)
                try:
                    self._probe_done.emit(gen, k, detail)
                except RuntimeError:
                    pass
            threading.Thread(target=work, daemon=True, name=f"probe-{self.camera.id}").start()
            self._set_state(TileState.CONNECTING, "Diagnostic…")
            self._connect_timer.start()     # filet : ne pas rester bloqué en diagnostic
            return

        self._schedule_retry(kind_hint or kind)

    def _on_probe_done(self, gen: int, kind: str, detail: str):
        # résultat périmé (tuile arrêtée/reconnectée entre-temps) → ignorer
        if gen != self._gen:
            return
        self._probing = False
        if self._stopping or self.state != TileState.CONNECTING:
            # seul l'état « Diagnostic… » attend ce verdict : si la tuile a déjà
            # basculé (timeout → BACKOFF, lecture repartie…), ne pas replanifier
            # un second réessai ni renverser un flux vivant.
            return
        if kind == "auth":
            self._echec_auth(detail[:200])
        elif kind == "ok":
            # le flux répond : l'échec était transitoire
            self._schedule_retry("other")
        else:
            self._schedule_retry(kind if kind in KIND_LABELS else "other")

    def _echec_auth(self, detail: str):
        """Aiguille un 401 : arrêt définitif en accès DIRECT au DVR (identifiants
        réels — risque de lockout Hikvision), mais simple réessai en mode SERVEUR
        (le mot de passe RTSP est un jeton relais : un 401 = jeton expiré/révoqué,
        rafraîchi par le contrôle de session ; aucun compte DVR n'est sollicité)."""
        if getattr(self.camera, "remote", None) is not None:
            self._schedule_retry("auth")
            return
        self._enter_auth_failed(detail)

    def _enter_auth_failed(self, detail: str):
        logger.error(
            f"[{self.camera.id}] 401 UNAUTHORIZED sur {mask_url(self._url)} — "
            f"ARRÊT DÉFINITIF des tentatives (risque de lockout du compte DVR). "
            f"Corriger les identifiants puis recharger la config. {detail}")
        self._connect_timer.stop()
        self._retry_timer.stop()
        self._set_state(
            TileState.AUTH_FAILED,
            "Identifiants refusés.\n"
            "Corrigez-les dans la configuration ; les essais sont\n"
            "suspendus pour éviter le blocage du compte DVR.")

    def _schedule_retry(self, kind: str):
        self._failures += 1
        delay = min(BACKOFF_MIN * (BACKOFF_FACTOR ** (self._failures - 1)), BACKOFF_MAX)
        label = KIND_LABELS.get(kind, "erreur de lecture")
        logger.warning(f"[{self.camera.id}] échec ({kind}) n°{self._failures} sur "
                       f"{mask_url(self._url)} — nouvel essai dans {delay}s")
        self._set_state(TileState.BACKOFF,
                        f"Échec : {label}\nNouvel essai dans {delay}s "
                        f"(tentative {self._failures})")
        self._retry_timer.start(delay * 1000)
