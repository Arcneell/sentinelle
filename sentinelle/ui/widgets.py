"""Petits widgets transverses de l'interface.

EmptyState : écran vide soigné (icône, titre, explication, action) affiché à la
place du mur d'images quand il n'y a rien à montrer — plus engageant qu'un
texte gris, et l'action guide l'utilisateur vers l'étape suivante.

BadgeDelegate : dessine, après le nom d'un élément d'arbre, de petites puces
colorées (« 4G », « éco », « photo ») au lieu de suffixes texte.
"""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

from .icons import icon
from .theme import t

ROLE_BADGES = Qt.UserRole + 2


class EmptyState(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._icone = QLabel()
        self._icone.setAlignment(Qt.AlignCenter)
        self._titre = QLabel()
        self._titre.setAlignment(Qt.AlignCenter)
        self._texte = QLabel()
        self._texte.setAlignment(Qt.AlignCenter)
        self._texte.setWordWrap(True)
        self._bouton = QPushButton()
        self._bouton.setObjectName("addBtn")
        self._bouton.setCursor(Qt.PointingHandCursor)
        self._action = None
        self._bouton.clicked.connect(self._declencher)

        lay = QVBoxLayout(self)
        lay.addStretch(3)
        lay.addWidget(self._icone)
        lay.addSpacing(14)
        lay.addWidget(self._titre)
        lay.addSpacing(6)
        lay.addWidget(self._texte)
        lay.addSpacing(18)
        lay.addWidget(self._bouton, 0, Qt.AlignCenter)
        lay.addStretch(4)
        self.restyle()

    def restyle(self):
        self._titre.setStyleSheet(
            f"color: {t('text')}; font-size: 17px; font-weight: 600; "
            "background: transparent;")
        self._texte.setStyleSheet(
            f"color: {t('text_dim')}; font-size: 13px; background: transparent;")

    def afficher(self, icone: str, titre: str, texte: str,
                 bouton: str = "", action=None):
        self._icone.setPixmap(icon(icone, t("text_faint")).pixmap(52, 52))
        self._titre.setText(titre)
        self._texte.setText(texte)
        self._action = action
        self._bouton.setText(f"  {bouton}  ")
        self._bouton.setVisible(bool(bouton and action))

    def _declencher(self):
        if self._action is not None:
            self._action()


# couleur de texte des puces par libellé (fond commun sombre)
_COULEURS_BADGES = {"4G": "warn", "éco": "ok", "photo": "accent"}


class BadgeDelegate(QStyledItemDelegate):
    """Item d'arbre avec puces dessinées après le texte.

    Les éléments qui portent une liste de libellés dans ROLE_BADGES voient ces
    libellés rendus en petites pastilles arrondies ; les autres sont rendus
    normalement."""

    def paint(self, painter: QPainter, option, index):
        badges = index.data(ROLE_BADGES)
        if not badges:
            return super().paint(painter, option, index)

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        texte = opt.text
        opt.text = ""                               # fond, coche et focus seuls
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)
        fm = QFontMetrics(opt.font)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(opt.font)
        selection = bool(opt.state & QStyle.State_Selected)
        painter.setPen(QColor(t("selection_text") if selection else t("text")))
        libelle = fm.elidedText(texte, Qt.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, libelle)

        police_badge = QFontMetrics(opt.font)
        x = rect.x() + fm.horizontalAdvance(libelle) + 8
        for b in badges:
            larg = police_badge.horizontalAdvance(b) + 12
            haut = police_badge.height() + 2
            if x + larg > rect.right():
                break                               # plus de place : on s'arrête
            r = QRectF(x, rect.center().y() - haut / 2, larg, haut)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t("surface_alt")))
            painter.drawRoundedRect(r, haut / 2, haut / 2)
            painter.setPen(QColor(t(_COULEURS_BADGES.get(b, "text_dim"))))
            painter.drawText(r, Qt.AlignCenter, b)
            x += larg + 5
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        if index.data(ROLE_BADGES):
            s.setHeight(max(s.height(), 26))
        return s
