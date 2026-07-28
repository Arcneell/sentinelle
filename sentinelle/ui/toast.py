"""Notifications éphémères (« toasts ») non bloquantes.

Petit bandeau qui glisse depuis le bas de la fenêtre, reste quelques secondes
puis s'efface. Remplace les QMessageBox pour les simples confirmations : rien
à cliquer, l'utilisateur n'est pas interrompu.
"""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel

from .icons import icon
from .theme import t

_DUREE_MS = 3500

_GENRES = {
    "ok": ("check-circle", "ok"),
    "info": ("check-circle", "accent"),
    "alerte": ("alert", "warn"),
}


class _Toast(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self._icone = QLabel()
        self._texte = QLabel()
        self._texte.setWordWrap(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(9)
        lay.addWidget(self._icone)
        lay.addWidget(self._texte)
        self._effet = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effet)
        self._minuteur = QTimer(self)
        self._minuteur.setSingleShot(True)
        self._minuteur.timeout.connect(self._disparaitre)
        self._anim = None
        self.hide()

    def montrer(self, texte: str, genre: str):
        nom_icone, couleur = _GENRES.get(genre, _GENRES["info"])
        self._icone.setPixmap(icon(nom_icone, t(couleur)).pixmap(18, 18))
        self._texte.setText(texte)
        self.setStyleSheet(
            f"QFrame#toast {{ background: {t('elevated')}; "
            f"border: 1px solid {t('border')}; border-radius: 8px; }}"
            f"QLabel {{ color: {t('text')}; font-size: 13px; background: transparent; }}")
        self.adjustSize()
        self._replacer()
        self.show()
        self.raise_()

        # fondu + glissement de quelques pixels vers le haut en apparaissant
        if self._anim is not None:
            self._anim.stop()
        self._effet.setOpacity(0.0)
        fondu = QPropertyAnimation(self._effet, b"opacity", self)
        fondu.setDuration(180)
        fondu.setStartValue(0.0)
        fondu.setEndValue(1.0)
        glisse = QPropertyAnimation(self, b"pos", self)
        glisse.setDuration(220)
        glisse.setStartValue(self.pos() + QPoint(0, 14))
        glisse.setEndValue(self.pos())
        glisse.setEasingCurve(QEasingCurve.OutCubic)
        self._anim = fondu
        fondu.start()
        glisse.start()
        self._minuteur.start(_DUREE_MS)

    def _replacer(self):
        p = self.parentWidget()
        if p is None:
            return
        x = (p.width() - self.width()) // 2
        y = p.height() - self.height() - 46         # au-dessus de la barre d'état
        self.move(max(0, x), max(0, y))

    def _disparaitre(self):
        fondu = QPropertyAnimation(self._effet, b"opacity", self)
        fondu.setDuration(300)
        fondu.setStartValue(1.0)
        fondu.setEndValue(0.0)
        fondu.finished.connect(self.hide)
        self._anim = fondu
        fondu.start()


def toast(fenetre, texte: str, genre: str = "ok"):
    """Affiche une notification éphémère en bas de `fenetre`.

    genre : "ok" (confirmation), "info", "alerte"."""
    if fenetre is None:
        return
    existant = getattr(fenetre, "_toast_widget", None)
    if existant is None:
        existant = _Toast(fenetre)
        fenetre._toast_widget = existant
    existant.montrer(texte, genre)
