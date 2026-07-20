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
import threading
from collections import deque
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
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

KIND_LABELS = {
    "timeout": "délai dépassé",
    "network": "site injoignable",
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
    """Chemin horodaté pour une capture manuelle (Images/Sentinelle/)."""
    dossier = Path.home() / "Pictures" / "Sentinelle"
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
    _evt_ended = Signal(int, str)           # génération, reason
    _probe_done = Signal(int, str, str)     # génération, kind, detail

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
        self._stack.setStackingMode(QStackedLayout.StackOne)
        self._video = _VideoSurface()
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setWordWrap(True)
        self._stack.addWidget(self._status)   # index 0
        self._stack.addWidget(self._video)    # index 1
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
        from .icons import icon
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
        return ("HD" if flux == "main" else "sub") + eco

    def _set_state(self, state: TileState, message: str = ""):
        self.state = state
        self._dot.setStyleSheet(
            f"background-color: {_DOT_COLORS[state]}; border-radius: 5px;")
        if state == TileState.PLAYING:
            self._stack.setCurrentIndex(1)
        else:
            self._status.setText(message)
            self._stack.setCurrentIndex(0)
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
        path = snapshot_path(self.camera)
        try:
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
        """Destruction de la tuile : libère mpv et arrête le PTZ."""
        self._ptz_shutdown()            # stoppe un mouvement en cours + le worker
        self.stop()
        if self._player is not None:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None

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
            reason = ""
            try:
                reason = str(getattr(evt.data, "reason", "") or "")
            except Exception:
                pass
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
        self._gen += 1              # nouvelle tentative : périme les sondes précédentes
        self._probing = False
        self._set_state(TileState.CONNECTING, "Connexion…")
        self._log_tail.clear()
        try:
            self._player.play(self._url)
        except Exception as e:
            logger.warning(f"[{self.camera.id}] loadfile a échoué : {e}")
            self._handle_failure()
            return
        self._connect_timer.start()

    def _on_mpv_log(self, level, component, message):
        # appelé depuis le thread mpv — deque est thread-safe pour append
        if level in ("error", "warn", "fatal"):
            self._log_tail.append(f"{component}: {message}")

    def _on_playing(self, gen: int):
        # événement d'une connexion précédente (retry/reconnexion entre-temps) → ignorer
        if gen != self._gen or self._stopping:
            return
        self._connect_timer.stop()
        if self._failures > 0:
            logger.info(f"[{self.camera.id}] reconnecté après {self._failures} échec(s)")
        self._failures = 0
        self._set_state(TileState.PLAYING)
        self._debit_timer.start()
        if self._zoom:
            self._set_zoom(self._zoom)
        if self._aspect_mode != "fit":
            self.set_aspect_mode(self._aspect_mode)
        if self.camera.reconnexion_preventive_s > 0:
            self._preventive_timer.start(self.camera.reconnexion_preventive_s * 1000)

    def _on_ended(self, gen: int, reason: str):
        if gen != self._gen:
            return          # end-file d'une connexion périmée (remplacement de flux)
        if self._stopping or self.state in (TileState.AUTH_FAILED, TileState.IDLE):
            return
        if "stop" in reason.lower():
            return
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
            self._enter_auth_failed(log_text[-200:])
            return

        # logs mpv peu parlants → ffprobe (si présent) tranche en arrière-plan
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
        if self._stopping or self.state == TileState.AUTH_FAILED:
            return
        if kind == "auth":
            self._enter_auth_failed(detail[:200])
        elif kind == "ok":
            # le flux répond : l'échec était transitoire
            self._schedule_retry("other")
        else:
            self._schedule_retry(kind if kind in KIND_LABELS else "other")

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
