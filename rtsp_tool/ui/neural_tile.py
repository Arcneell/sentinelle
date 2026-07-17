"""Tuile « reconstruction temps réel » — flux passé dans le réseau ncnn.

Même interface publique que VideoTile (start/stop/shutdown/set_enhance,
signaux double_clicked/state_changed/snapshot_saved) pour être interchangeable
dans MainWindow. Affiche les frames reconstruites dans un QLabel (comme PhotoTile).
"""

import logging
from datetime import datetime

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..config import Camera
from .. import neural
from .tile import TileState, _DOT_COLORS, snapshot_path

logger = logging.getLogger(__name__)


class NeuralTile(QFrame):
    double_clicked = Signal(str)
    state_changed = Signal()
    snapshot_saved = Signal(str)
    _frame_prete = Signal(object)          # ndarray RGB reconstruit
    _etat = Signal(str)

    def __init__(self, camera: Camera, vue: str = "mono",
                 cible: str = neural.CIBLE_DEFAUT, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.vue = vue
        self.state = TileState.IDLE
        self.debit_bps = 0.0
        self._cible = cible
        self._worker = None
        self._stopped = True
        self._last_rgb = None
        self._url = camera.url_pour_vue(vue)

        self._build_ui()
        self._frame_prete.connect(self._afficher)
        self._etat.connect(self._on_etat)

    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("NeuralTile { background-color: #101010; border: 1px solid #303030; }")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background-color: #1c1c1c;")
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 2, 6, 2)
        self._dot = QLabel(); self._dot.setFixedSize(10, 10)
        self._title = QLabel(f"{self.camera.nom} — {self.camera.site.nom}")
        self._title.setStyleSheet("color: #d0d0d0; font-weight: bold;")
        self._info = QLabel("IA")
        self._info.setStyleSheet("color: #7fb0ff;")
        h.addWidget(self._dot); h.addWidget(self._title)
        h.addStretch(); h.addWidget(self._info)
        root.addWidget(header)

        self._image = QLabel()
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setStyleSheet("background-color: black;")
        self._image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._image.setMinimumSize(32, 24)
        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #a0a0a0; padding: 10px;")
        body = QVBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0)
        body.addWidget(self._image, 1); body.addWidget(self._status)
        root.addLayout(body, 1)
        self._status.hide()
        self._set_state(TileState.IDLE, "En attente")

    def _set_state(self, state, message=""):
        self.state = state
        self._dot.setStyleSheet(f"background-color: {_DOT_COLORS[state]}; border-radius: 5px;")
        if message and state != TileState.PLAYING:
            self._status.setText(message); self._status.show()
        else:
            self._status.hide()
        self.state_changed.emit()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self.camera.id); event.accept()

    def set_motion(self, actif: bool):
        c = "#e04040" if actif else "#303030"
        w = 3 if actif else 1
        self.setStyleSheet(f"NeuralTile {{ background-color: #101010; border: {w}px solid {c}; }}")

    def contextMenuEvent(self, event):
        from .icons import icon
        menu = QMenu(self)
        act = menu.addAction(icon("camera"), "Enregistrer l'image reconstruite")
        act.setEnabled(self._last_rgb is not None)
        if menu.exec(event.globalPos()) is act and self._last_rgb is not None:
            from PIL import Image
            path = snapshot_path(self.camera)
            Image.fromarray(self._last_rgb).save(path)
            self.snapshot_saved.emit(path)

    # -- interface commune avec VideoTile --

    def set_enhance(self, niveau):
        pass                     # la tuile neuronale EST le niveau max

    def start(self):
        if not neural.disponible():
            self._set_state(TileState.NO_PLAYER,
                            "Moteur de reconstruction non installé.\n"
                            "Clic droit sur une caméra, puis « Reconstruire "
                            "l'image » pour le télécharger.")
            return
        if self._worker is not None:
            return
        self._stopped = False
        self._set_state(TileState.CONNECTING, "Démarrage…")
        self._worker = neural.NeuralWorker(
            self._url, self._cible,
            on_frame=lambda a: self._frame_prete.emit(a),
            on_state=lambda s: self._etat.emit(s))
        self._ajuster_cible()
        self._worker.start()

    def _ajuster_cible(self):
        """Adapte la résolution d'entrée du réseau à la taille affichée."""
        if self._worker is None:
            return
        # plein écran : jusqu'à la source complète ; grille : GPU partagé
        h_max = 576 if self.vue == "mono" else 270
        self._worker.target_h = neural.hauteur_pour_affichage(self.height(), h_max)

    def stop(self, message="En pause"):
        self._stopped = True
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        self.debit_bps = 0.0
        if self.state not in (TileState.NO_PLAYER,):
            self._set_state(TileState.IDLE, message)

    def shutdown(self):
        self.stop()

    def _on_etat(self, s):
        if self._stopped:
            return
        if s == "PLAYING":
            return                        # l'état passe à PLAYING à la 1re frame
        mapping = {"Connexion…": (TileState.CONNECTING, "Connexion…"),
                   "Flux injoignable": (TileState.BACKOFF, "Flux injoignable — nouvel essai")}
        st, msg = mapping.get(s, (TileState.CONNECTING, s))
        self._set_state(st, msg)

    def _afficher(self, rgb):
        if self._stopped:
            return
        rgb = np.ascontiguousarray(rgb)       # défensif : QImage exige du C-contigu
        self._last_rgb = rgb
        h, w, _ = rgb.shape
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        pix = QPixmap.fromImage(img.copy())
        self._image.setPixmap(pix.scaled(self._image.size(), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        if self.state != TileState.PLAYING:
            self._set_state(TileState.PLAYING)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._ajuster_cible()
        if self._last_rgb is not None:
            self._afficher(self._last_rgb)
