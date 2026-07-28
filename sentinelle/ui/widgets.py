"""Petits widgets transverses de l'interface.

EmptyState : écran vide soigné (icône, titre, explication, action) affiché à la
place du mur d'images quand il n'y a rien à montrer — plus engageant qu'un
texte gris, et l'action guide l'utilisateur vers l'étape suivante.

TileCaption : bandeau d'identité d'une tuile, placé SOUS l'image et partagé
par les tuiles vidéo et photo.

MemoireGeometrie : ouvre un dialogue en grand et retient la taille que
l'utilisateur lui a donnée.

BadgeDelegate : dessine, après le nom d'un élément d'arbre, de petites puces
colorées (« 4G », « éco », « photo ») au lieu de suffixes texte.
"""

from PySide6.QtCore import QObject, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QVBoxLayout, QWidget)

from ..reglages import reglages as _reglages
from .icons import icon
from .theme import police_data, police_ui, t

ROLE_BADGES = Qt.UserRole + 2


class MemoireGeometrie(QObject):
    """Fait s'ouvrir un dialogue en grand, puis retient sa taille.

    Les panneaux qui listent un parc entier (administration, configuration)
    s'ouvraient à leur taille minimale : il fallait les agrandir à la souris à
    chaque ouverture pour voir plus de trois lignes. Ils s'ouvrent maintenant
    à une large fraction de l'écran, et la taille choisie est reprise
    d'une fois sur l'autre.

    S'installe en une ligne, sans toucher au cycle de vie du dialogue :

        MemoireGeometrie(dialogue, "admin")
    """

    def __init__(self, dialogue, cle: str, fraction: float = 0.86,
                 maxi: tuple[int, int] = (1600, 1040)):
        super().__init__(dialogue)
        self._dialogue = dialogue
        self._cle = f"geometrie/{cle}"
        self._fraction = fraction
        self._maxi = maxi
        self._restaurer()
        # `finished` plutôt qu'un filtre d'événements : le filtre était encore
        # appelé pendant la destruction du dialogue, alors que les attributs
        # Python de l'objet avaient déjà disparu
        dialogue.finished.connect(self._enregistrer)

    def _ecran_disponible(self):
        ecran = self._dialogue.screen() or QGuiApplication.primaryScreen()
        return ecran.availableGeometry()

    def _restaurer(self):
        dispo = self._ecran_disponible()
        sauve = _reglages().value(self._cle)
        if sauve is not None and self._dialogue.restoreGeometry(sauve):
            # un poste peut avoir perdu l'écran sur lequel la taille a été
            # retenue : sans ce contrôle, le dialogue s'ouvrirait hors champ
            if dispo.intersects(self._dialogue.frameGeometry()):
                return
        largeur = min(self._maxi[0], int(dispo.width() * self._fraction))
        hauteur = min(self._maxi[1], int(dispo.height() * self._fraction))
        self._dialogue.resize(largeur, hauteur)
        self._dialogue.move(dispo.center() - self._dialogue.rect().center())

    def _enregistrer(self, *_):
        _reglages().setValue(self._cle, self._dialogue.saveGeometry())


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
        self._titre.setFont(police_ui(17, gras=True))
        self._titre.setStyleSheet(
            f"color: {t('text')}; background: transparent;")
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


class TileCaption(QFrame):
    """Bandeau d'identité d'une tuile, sous l'image.

    Sous et non au-dessus : l'œil lit l'image d'abord, l'identité ne fait que
    la confirmer — et les enregistreurs incrustent déjà leur propre horodatage
    en haut de l'image, deux bandes de texte empilées deviendraient illisibles.

    Le nom porte la caméra, le gris le site, la colonne de droite les données
    machine (HD/SD, profil, débit) en monospace pour que les chiffres ne
    sautent pas d'un rafraîchissement à l'autre.

    `alerte(True)` inverse le bandeau : c'est le signal « mouvement détecté ».
    Il est volontairement achromatique — la couleur reste réservée à l'état de
    connexion, et un bandeau blanc sur un mur sombre se voit de plus loin
    qu'un rouge de plus.
    """

    _MARGES = 16 + 7 * 2                    # marges du bandeau + deux espaces

    def __init__(self, nom: str, site: str, parent=None):
        super().__init__(parent)
        self.setObjectName("tileCaption")
        self._alerte = False
        self._nom_complet = nom
        self._nom = QLabel(nom)
        self._site = QLabel(site)
        self._data = QLabel("")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(7)
        lay.addWidget(self._nom)
        lay.addWidget(self._site)
        lay.addStretch(1)
        lay.addWidget(self._data)
        self.restyle()

    def set_data(self, texte: str):
        self._data.setText(texte)
        self._ajuster()

    def minimumSizeHint(self) -> QSize:
        """Le bandeau ne doit jamais imposer sa largeur à la tuile : sinon un
        nom de caméra long fixait la largeur minimale d'une case, et une
        grille 4×4 débordait au lieu d'abréger le texte (voir `_ajuster`)."""
        s = super().minimumSizeHint()
        s.setWidth(min(s.width(), 80))
        return s

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._ajuster()

    def _ajuster(self):
        """Fait tenir le bandeau dans la largeur réelle de la tuile.

        En 4×4 sur un écran modeste, identité et données finissaient par se
        chevaucher. Ordre de sacrifice : le site d'abord (le mur est déjà
        groupé par site dans le panneau), le nom de la caméra ensuite, jamais
        les données — c'est la colonne qui dit si le flux tient."""
        dispo = self.width() - self._MARGES
        if dispo <= 0:
            return
        fm_nom = QFontMetrics(self._nom.font())
        fm_site = QFontMetrics(self._site.font())
        largeur_data = QFontMetrics(self._data.font()).horizontalAdvance(
            self._data.text())
        reste = dispo - largeur_data

        besoin_site = fm_site.horizontalAdvance(self._site.text())
        avec_site = reste - besoin_site >= fm_nom.horizontalAdvance(self._nom_complet)
        self._site.setVisible(avec_site)
        if not avec_site:
            reste += 7                      # l'espace du site est récupéré
        abrege = fm_nom.elidedText(self._nom_complet, Qt.ElideRight, max(0, reste))
        if abrege != self._nom.text():       # évite une boucle de mise en page
            self._nom.setText(abrege)

    def alerte(self, actif: bool):
        if actif == self._alerte:
            return
        self._alerte = actif
        self.restyle()

    def restyle(self):
        fond = t("text") if self._alerte else t("tile_header")
        principal = t("on_accent") if self._alerte else t("text")
        second = t("on_accent") if self._alerte else t("text_dim")
        self.setStyleSheet(f"QFrame#tileCaption {{ background: {fond}; }}")
        self._nom.setFont(police_ui(12, gras=True))
        self._site.setFont(police_ui(12))
        self._data.setFont(police_data(12))
        self._nom.setStyleSheet(f"color: {principal}; background: transparent;")
        self._site.setStyleSheet(f"color: {second}; background: transparent;")
        self._data.setStyleSheet(f"color: {second}; background: transparent;")
        self._ajuster()


# Les puces sont volontairement toutes neutres : « 4G », « éco » et « photo »
# décrivent un réglage permanent, pas un état. Les colorer mettrait de l'ambre
# figé dans le panneau, en concurrence avec l'ambre — lui, transitoire — des
# tuiles en cours de connexion. Le libellé porte déjà l'information.
_COULEURS_BADGES: dict[str, str] = {}


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
