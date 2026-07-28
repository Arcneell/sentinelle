"""Assistant de premier lancement : choix du mode de fonctionnement du poste.

Présenté une seule fois (tant que le mode n'a jamais été défini). Le choix est
ensuite verrouillé : il ne se change qu'avec un compte administrateur. En mode
serveur, l'adresse et la connexion se saisissent ensuite sur la page de login.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QVBoxLayout

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
            f"border: 1px solid {t('border')}; border-radius: 3px; }}"
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
    {"mode": "serveur"} (l'adresse est demandée ensuite, à la connexion)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resultat = None
        self.setWindowTitle("Sentinelle — Premier lancement")
        self.setWindowIcon(app_icon())
        self.setObjectName("setupPage")
        self.setFixedWidth(480)
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
            "L'adresse et les identifiants sont demandés juste après.")
        carte_srv.clic.connect(self._choix_serveur)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 32, 40, 32)
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

    def _choix_local(self):
        self.resultat = {"mode": "local"}
        self.accept()

    def _choix_serveur(self):
        self.resultat = {"mode": "serveur"}
        self.accept()
