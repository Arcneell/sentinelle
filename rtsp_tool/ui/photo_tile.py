"""Tuile « mode photo » (profil eco-extreme, vue grille).

Aucun flux vidéo : un snapshot JPEG est récupéré toutes les N secondes via
l'API HTTP du DVR (ISAPI Hikvision / CGI Dahua). Quelques ko par image au lieu
de ~300 kbps en continu — c'est le mode pour les sites 4G contraints.

Mêmes règles que la tuile vidéo : 401 = arrêt définitif (lockout DVR),
erreurs réseau = backoff exponentiel plafonné à 10 min.
"""

import logging
import threading
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..config import Camera, mask_url
from ..snapshot import fetch_snapshot
from .tile import _DOT_COLORS, BACKOFF_MAX, TileState

logger = logging.getLogger(__name__)

KIND_LABELS = {
    "timeout": "délai dépassé",
    "network": "site injoignable",
    "other": "snapshot indisponible",
}


class _ImageLabel(QLabel):
    """Affiche un QPixmap redimensionné en conservant le ratio."""

    def __init__(self):
        super().__init__()
        self._pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: black;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(QSize(32, 24))

    def set_image(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._pixmap and not self._pixmap.isNull():
            self.setPixmap(self._pixmap.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class PhotoTile(QFrame):
    """Interface identique à VideoTile : start/stop/shutdown, double_clicked…"""

    double_clicked = Signal(str)
    state_changed = Signal()
    snapshot_saved = Signal(str)
    _result = Signal(object, str, str)      # data|None, kind, detail

    def __init__(self, camera: Camera, vue: str = "grille", parent=None):
        super().__init__(parent)
        self.camera = camera
        self.vue = vue
        self.state = TileState.IDLE
        self.debit_bps = 0.0

        self._url = camera.snapshot_url()
        self._intervalle = max(2, camera.photo_intervalle_s)
        self._failures = 0
        self._fetching = False
        self._stopped = True
        self._last_bytes: bytes | None = None

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fetch)
        self._result.connect(self._on_result)

    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "PhotoTile { background-color: #101010; border: 1px solid #303030; }")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background-color: #1c1c1c;")
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 2, 6, 2)
        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._title = QLabel(f"{self.camera.nom} — {self.camera.site.nom}")
        self._title.setStyleSheet("color: #d0d0d0; font-weight: bold;")
        self._info = QLabel(f"photo · {self._intervalle}s")
        self._info.setStyleSheet("color: #707070;")
        h.addWidget(self._dot)
        h.addWidget(self._title)
        h.addStretch()
        h.addWidget(self._info)
        root.addWidget(header)

        self._image = _ImageLabel()
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #a0a0a0; padding: 8px;")
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._image, 1)
        body.addWidget(self._status)
        root.addLayout(body, 1)
        self._status.hide()

        self._set_state(TileState.IDLE, "En attente")

    def _set_state(self, state: TileState, message: str = ""):
        self.state = state
        self._dot.setStyleSheet(
            f"background-color: {_DOT_COLORS[state]}; border-radius: 5px;")
        if message and state != TileState.PLAYING:
            self._status.setText(message)
            self._status.show()
        else:
            self._status.hide()
        self.state_changed.emit()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self.camera.id)
        event.accept()

    def set_motion(self, actif: bool):
        c = "#e04040" if actif else "#303030"
        w = 3 if actif else 1
        self.setStyleSheet(f"PhotoTile {{ background-color: #101010; border: {w}px solid {c}; }}")

    def contextMenuEvent(self, event):
        from .icons import icon
        menu = QMenu(self)
        act = menu.addAction(icon("camera"), "Enregistrer l'image")
        act.setEnabled(self._last_bytes is not None)
        act_ia = menu.addAction(icon("search"), "Reconstruire l'image…")
        act_ia.setEnabled(self._last_bytes is not None)
        choix = menu.exec(event.globalPos())
        if choix is act:
            self._save_snapshot()
        elif choix is act_ia:
            self._reconstruire_ia()

    def _reconstruire_ia(self):
        if not self._last_bytes:
            return
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="rtsp-tool-ia-")
        os.close(fd)
        from PySide6.QtGui import QPixmap
        pix = QPixmap()
        if not pix.loadFromData(self._last_bytes) or not pix.save(tmp, "PNG"):
            return
        from .reconstruct_dialog import reconstruire_image
        reconstruire_image(self.window(), self.camera, tmp)

    # ---------------------------------------------------------- cycle de vie

    def start(self):
        if self.state == TileState.AUTH_FAILED:
            return
        if not self._url:
            self._set_state(TileState.NO_PLAYER,
                            "Pas d'URL snapshot pour cette caméra\n"
                            "(marque custom : renseigner « URL snapshot »)")
            return
        self._stopped = False
        self._set_state(TileState.CONNECTING, "Chargement…")
        self._fetch()

    def stop(self, message: str = "En pause"):
        self._stopped = True
        self._timer.stop()
        self.debit_bps = 0.0
        if self.state not in (TileState.AUTH_FAILED, TileState.NO_PLAYER):
            self._set_state(TileState.IDLE, message)

    def shutdown(self):
        self.stop()

    # -------------------------------------------------------------- fetching

    def _fetch(self):
        if self._stopped or self._fetching:
            return
        self._fetching = True
        url, user, pwd = self._url, self.camera.user, self.camera.password

        def work():
            data, kind, detail = fetch_snapshot(url, user, pwd)
            try:
                self._result.emit(data, kind, detail)
            except RuntimeError:
                pass    # tuile détruite pendant la requête

        threading.Thread(target=work, daemon=True,
                         name=f"photo-{self.camera.id}").start()

    def _on_result(self, data, kind: str, detail: str):
        self._fetching = False
        if self._stopped:
            return

        if kind == "auth":
            logger.error(f"[{self.camera.id}] auth refusée sur {mask_url(self._url)} — "
                         f"ARRÊT DÉFINITIF du mode photo. {detail}")
            self._set_state(TileState.AUTH_FAILED,
                            "Identifiants refusés\n"
                            "Tentatives stoppées pour protéger le compte DVR.\n"
                            "Corriger via le bouton Configuration.")
            return

        if kind == "ok" and data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self._last_bytes = data
                self._failures = 0
                self.debit_bps = len(data) * 8 / self._intervalle
                self._image.set_image(pixmap)
                self._info.setText(f"photo · {self._intervalle}s · "
                                   f"{datetime.now().strftime('%H:%M:%S')}")
                self._set_state(TileState.PLAYING)
                self._timer.start(self._intervalle * 1000)
                return
            kind, detail = "other", "image illisible"

        self._failures += 1
        delay = min(self._intervalle * (2 ** self._failures), BACKOFF_MAX)
        label = KIND_LABELS.get(kind, "erreur")
        logger.warning(f"[{self.camera.id}] photo : échec ({kind}) n°{self._failures} — "
                       f"nouvel essai dans {delay}s. {detail[:120]}")
        self._set_state(TileState.BACKOFF,
                        f"Échec : {label}\nNouvel essai dans {delay}s")
        self._timer.start(delay * 1000)

    def _save_snapshot(self):
        if not self._last_bytes:
            return
        from .tile import snapshot_path
        path = snapshot_path(self.camera)
        with open(path, "wb") as f:
            f.write(self._last_bytes)
        self.snapshot_saved.emit(path)
