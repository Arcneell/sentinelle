"""Tuile vidéo : un flux RTSP, son état et sa politique de reconnexion.

Machine d'états (patterns repris de vision-ai/capture.py) :
  IDLE → CONNECTING → PLAYING
                    ↘ BACKOFF (timeout/réseau : 5 s → 10 min, ×2, reset au succès)
                    ↘ AUTH_FAILED (401 : ARRÊT DÉFINITIF — jamais de retry auto,
                       sinon lockout du compte côté DVR Hikvision)

Chaque tuile a sa propre instance libmpv (thread mpv indépendant) : un flux qui
meurt n'affecte jamais les autres tuiles.
"""

import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSizePolicy, QStackedLayout, QVBoxLayout, QWidget)

from ..config import Camera, mask_url
from ..player import MPV_IMPORT_ERROR, create_player, mpv_disponible
from ..probe import classify_text, ffprobe_available, probe_rtsp

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

# threads de libération mpv encore en vol (voir VideoTile.dispose) : joints à la
# fermeture de l'application pour ne pas tuer un terminate() en plein démontage
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


_DOT_COLORS = {
    TileState.IDLE: "#808080",
    TileState.CONNECTING: "#e0a030",
    TileState.PLAYING: "#3fbf5f",
    TileState.BACKOFF: "#e0a030",
    TileState.AUTH_FAILED: "#e04040",
    TileState.NO_PLAYER: "#e04040",
}


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

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = QWidget()
        h = QHBoxLayout(self._header)
        h.setContentsMargins(8, 4, 8, 4)
        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._title = QLabel(f"{self.camera.nom} — {self.camera.site.nom}")
        self._flux_label = QLabel(self._flux_text())
        h.addWidget(self._dot)
        h.addWidget(self._title)
        h.addStretch()
        h.addWidget(self._flux_label)
        root.addWidget(self._header)

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
        self._header.setStyleSheet(f"background-color: {t('tile_header')};")
        self._title.setStyleSheet(f"color: {t('text')}; font-weight: 600;")
        self._flux_label.setStyleSheet(f"color: {t('text_dim')};")
        self._status.setStyleSheet(
            f"color: {t('tile_status_text')}; background-color: {t('video_bg')}; "
            f"padding: 12px;")
        self._dot.setStyleSheet(
            f"background-color: {_DOT_COLORS[self.state]}; border-radius: 5px;")
        if self._controls is not None:
            self._controls.setStyleSheet(f"background-color: {t('tile_header')};")

    def _apply_frame_style(self):
        from .theme import t
        couleur = t("danger") if self._motion_on else t("border")
        largeur = 3 if self._motion_on else 1
        self.setStyleSheet(
            f"VideoTile {{ background-color: {t('tile_bg')}; "
            f"border: {largeur}px solid {couleur}; }}")

    def _build_controls(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 3, 6, 3)
        h.setSpacing(4)

        if self.camera.ptz:
            for libelle, dx, dy, dz, info in (
                ("↖", -0.5, 0.5, 0, "haut-gauche"), ("↑", 0, 0.5, 0, "haut"),
                ("↗", 0.5, 0.5, 0, "haut-droite"), ("←", -0.5, 0, 0, "gauche"),
                ("→", 0.5, 0, 0, "droite"), ("↙", -0.5, -0.5, 0, "bas-gauche"),
                ("↓", 0, -0.5, 0, "bas"), ("↘", 0.5, -0.5, 0, "bas-droite"),
            ):
                b = QPushButton(libelle)
                b.setObjectName("compact"); b.setFixedWidth(30)
                b.setToolTip(f"Orientation {info} (maintenir enfoncé)")
                b.pressed.connect(lambda x=dx, y=dy, z=dz: self._ptz(x, y, z))
                b.released.connect(self._ptz_stop)
                h.addWidget(b)
            zin = QPushButton("Z+"); zin.setObjectName("compact"); zin.setFixedWidth(34)
            zin.setToolTip("Zoom optique (maintenir enfoncé)")
            zin.pressed.connect(lambda: self._ptz(0, 0, 0.5)); zin.released.connect(self._ptz_stop)
            zout = QPushButton("Z−"); zout.setObjectName("compact"); zout.setFixedWidth(34)
            zout.setToolTip("Dézoom optique (maintenir enfoncé)")
            zout.pressed.connect(lambda: self._ptz(0, 0, -0.5)); zout.released.connect(self._ptz_stop)
            h.addWidget(zin); h.addWidget(zout)
            h.addSpacing(10)

        h.addStretch()
        lbl = QLabel("Zoom"); lbl.setObjectName("hint")
        h.addWidget(lbl)
        for txt, fn, tip in (("＋", self.zoom_in, "Zoom numérique avant"),
                             ("－", self.zoom_out, "Zoom numérique arrière"),
                             ("⟳", self.zoom_reset, "Réinitialiser le zoom")):
            b = QPushButton(txt); b.setObjectName("compact"); b.setFixedWidth(32)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        return bar

    def _flux_text(self) -> str:
        flux = self.camera.flux_pour_vue(self.vue)
        eco = " · éco" if self.camera.profil.startswith("eco") else ""
        return ("HD" if flux == "main" else "SD") + eco

    def _set_state(self, state: TileState, message: str = ""):
        self.state = state
        self._dot.setStyleSheet(
            f"background-color: {_DOT_COLORS[state]}; border-radius: 5px;")
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
        self._motion_on = actif
        self._apply_frame_style()

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
        if self._player is None:
            return
        try:
            if mode == "stretch":
                self._player["keepaspect"] = False
                self._player["panscan"] = 0.0
            elif mode == "crop":
                self._player["keepaspect"] = True
                self._player["panscan"] = 1.0
            else:
                self._player["keepaspect"] = True
                self._player["panscan"] = 0.0
        except Exception:
            pass

    def _save_snapshot(self):
        if self._player is None or self.state != TileState.PLAYING:
            return
        try:
            path = snapshot_path(self.camera)
            self._player.command("screenshot-to-file", path, "video")
            self.snapshot_saved.emit(path)
        except Exception as e:
            logger.warning(f"[{self.camera.id}] capture impossible : {e}")

    # ------------------------------------------------------ zoom numérique

    def zoom_in(self):
        self._set_zoom(self._zoom + 0.3)

    def zoom_out(self):
        self._set_zoom(self._zoom - 0.3)

    def zoom_reset(self):
        self._set_zoom(0.0)

    def _set_zoom(self, z: float):
        self._zoom = max(0.0, min(z, 3.0))
        if self._player is None:
            return
        try:
            self._player["video-zoom"] = self._zoom
            if self._zoom == 0.0:                 # recentre en dézoom complet
                self._player["video-pan-x"] = 0.0
                self._player["video-pan-y"] = 0.0
        except Exception:
            pass

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
        """Affiche le débit réseau réellement consommé par la tuile."""
        if self._player is None or self.state != TileState.PLAYING:
            return
        bps = 0.0
        try:
            speed = self._player.cache_speed          # octets/s lus sur le réseau
            if speed:
                bps = float(speed) * 8
        except Exception:
            try:
                bps = float(self._player.video_bitrate or 0)
            except Exception:
                bps = 0.0
        self.debit_bps = bps
        base = self._flux_text()
        self._flux_label.setText(f"{base} · {format_debit(bps)}" if bps else base)

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
        self.debit_bps = 0.0
        self._flux_label.setText(self._flux_text())
        if self._player is not None:
            try:
                self._player.command("stop")
            except Exception:
                pass
        if self.state not in (TileState.AUTH_FAILED, TileState.NO_PLAYER):
            self._set_state(TileState.IDLE, message)

    def shutdown(self):
        """Destruction de la tuile : libère mpv et arrête le PTZ (synchrone)."""
        self._ptz_shutdown()            # stoppe un mouvement en cours + le worker
        self.stop()
        if self._player is not None:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None

    def dispose(self):
        """Comme shutdown() + deleteLater(), mais libère mpv HORS du thread Qt.

        terminate() joint le thread d'événements mpv et démonte le flux RTSP :
        en série sur 9-16 tuiles, cela gelait l'interface plusieurs secondes à
        chaque changement de vue sur les mini-PC. La tuile se cache tout de
        suite, la libération se fait en arrière-plan, puis le widget se détruit
        (la fenêtre X11 du wid reste vivante tant que mpv ne l'a pas lâchée)."""
        self._ptz_shutdown()
        self.stop()
        self.hide()
        player, self._player = self._player, None
        if player is None:
            self.deleteLater()
            return
        self._libere_fait = False
        self._libere.connect(self._liberation_finie)
        # chien de garde : si terminate() reste bloqué (flux RTSP figé — cas connu
        # de ce projet), _libere ne serait jamais émis et le widget + le thread
        # fuiraient indéfiniment sur un poste 24/7. Passé ce délai, on détruit
        # quand même la tuile (le thread mpv, démon, mourra au pire à l'arrêt).
        # La forme à 3 arguments se déconnecte seule si la tuile est déjà détruite.
        QTimer.singleShot(15000, self, self._liberation_finie)

        def work():
            try:
                player.terminate()
            except Exception:
                pass
            finally:
                with _liberations_lock:
                    _liberations.discard(th)
            try:
                self._libere.emit()
            except RuntimeError:
                pass                    # widget déjà détruit (fermeture d'appli)

        th = threading.Thread(target=work, daemon=True,
                              name=f"mpv-term-{self.camera.id}")
        with _liberations_lock:
            _liberations.add(th)
        th.start()

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
        self._retry_timer.stop()    # un seul réessai armé à la fois
        self._set_state(TileState.CONNECTING, "Connexion…")
        self._log_tail.clear()
        try:
            self._player.play(self._url)
        except Exception as e:
            logger.warning(f"[{self.camera.id}] loadfile a échoué : {e}")
            self._handle_failure()
            return
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
        pilote manque (va-driver-all non installé)."""
        try:
            hw = str(self._player.hwdec_current or "no")
        except Exception:
            return
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
        try:
            self._player.command("stop")
        except Exception:
            pass
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
