"""Assistant de premier lancement : choix du mode de fonctionnement du poste.

Présenté une seule fois (tant que le mode n'a jamais été défini). Le choix est
ensuite verrouillé : il ne se change qu'avec un compte administrateur.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

from .icons import app_icon
from .theme import t


class _Carte(QFrame):
    """Option cliquable : titre + description sur plusieurs lignes."""

    clic = Signal()

    def __init__(self, titre: str, description: str, parent=None):
        super().__init__(parent)
        self.setObjectName("carteSetup")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QFrame#carteSetup {{ background: {t('surface')}; "
            f"border: 1px solid {t('border')}; border-radius: 10px; }}"
            f"QFrame#carteSetup:hover {{ border-color: {t('accent')}; "
            f"background: {t('surface_alt')}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 14)
        lay.setSpacing(5)
        lbl_t = QLabel(titre)
        lbl_t.setStyleSheet(f"color: {t('text')}; font-size: 15px; font-weight: 700;")
        lbl_d = QLabel(description)
        lbl_d.setWordWrap(True)
        lbl_d.setStyleSheet(f"color: {t('text_dim')}; font-size: 13px;")
        lay.addWidget(lbl_t)
        lay.addWidget(lbl_d)

    def mousePressEvent(self, event):
        self.clic.emit()
        event.accept()


class SetupDialog(QDialog):
    """Retourne le mode choisi via `resultat` : {"mode": "local"} ou
    {"mode": "serveur", "url": ...}."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resultat = None
        self.setWindowTitle("Sentinelle — Premier lancement")
        self.setWindowIcon(app_icon())
        self.setObjectName("setupPage")
        self.setMinimumWidth(560)
        self.setStyleSheet(f"QDialog#setupPage {{ background: {t('bg')}; }}")

        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setPixmap(app_icon().pixmap(72, 72))
        titre = QLabel("Bienvenue dans Sentinelle")
        titre.setAlignment(Qt.AlignCenter)
        titre.setStyleSheet(f"color: {t('text')}; font-size: 20px; font-weight: 700;")
        sous = QLabel("Comment ce poste doit-il fonctionner ?")
        sous.setAlignment(Qt.AlignCenter)
        sous.setStyleSheet(f"color: {t('text_dim')}; font-size: 14px;")

        carte_local = _Carte(
            "Poste autonome",
            "Ce poste se connecte directement aux DVR. La configuration "
            "(sites, caméras) est gérée localement, sans serveur.")
        carte_local.clic.connect(self._choix_local)
        carte_srv = _Carte(
            "Serveur central",
            "Ce poste se connecte au serveur Sentinelle avec un compte. "
            "La configuration et les droits sont gérés côté serveur.")
        carte_srv.clic.connect(self._choix_serveur)

        # étape serveur (masquée au départ)
        self._bloc_srv = QFrame()
        bs = QVBoxLayout(self._bloc_srv)
        bs.setContentsMargins(0, 0, 0, 0)
        bs.setSpacing(8)
        self._url = QLineEdit()
        self._url.setPlaceholderText("Adresse du serveur — http://serveur:8080")
        self._url.setStyleSheet(
            f"QLineEdit {{ background: {t('surface_alt')}; color: {t('text')}; "
            f"border: 1px solid {t('border')}; border-radius: 8px; "
            f"padding: 11px 14px; font-size: 14px; }}"
            f"QLineEdit:focus {{ border-color: {t('accent')}; }}")
        self._url.returnPressed.connect(self._valider_serveur)
        self._btn_continuer = QPushButton("Continuer")
        self._btn_continuer.setCursor(Qt.PointingHandCursor)
        self._btn_continuer.setStyleSheet(
            f"QPushButton {{ background: {t('accent')}; color: {t('on_accent')}; "
            f"border: none; border-radius: 8px; padding: 12px; font-size: 14px; "
            f"font-weight: 600; }}"
            f"QPushButton:hover {{ background: {t('accent_hover')}; }}")
        self._btn_continuer.clicked.connect(self._valider_serveur)
        self._erreur = QLabel("")
        self._erreur.setAlignment(Qt.AlignCenter)
        self._erreur.setWordWrap(True)
        self._erreur.setStyleSheet(f"color: {t('danger')};")
        self._erreur.hide()
        bs.addWidget(self._url)
        bs.addWidget(self._btn_continuer)
        bs.addWidget(self._erreur)
        self._bloc_srv.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 32, 40, 30)
        lay.setSpacing(0)
        lay.addWidget(logo)
        lay.addSpacing(12)
        lay.addWidget(titre)
        lay.addSpacing(4)
        lay.addWidget(sous)
        lay.addSpacing(24)
        lay.addWidget(carte_local)
        lay.addSpacing(12)
        lay.addWidget(carte_srv)
        lay.addSpacing(14)
        lay.addWidget(self._bloc_srv)

    def _choix_local(self):
        self.resultat = {"mode": "local"}
        self.accept()

    def _choix_serveur(self):
        self._bloc_srv.show()
        self._url.setFocus()
        self.adjustSize()

    def _valider_serveur(self):
        url = self._url.text().strip()
        if not url.startswith(("http://", "https://")):
            self._erreur.setText("Adresse invalide — exemple : http://serveur:8080")
            self._erreur.show()
            return
        self.resultat = {"mode": "serveur", "url": url}
        self.accept()
