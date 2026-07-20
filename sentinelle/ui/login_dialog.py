"""Page de connexion au serveur Sentinelle.

Présentation type « page web » : carte centrée, logo, champs épurés et bouton
d'action pleine largeur. Entrée valide le formulaire ; l'erreur s'affiche sous
les champs sans fermer la fenêtre.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout)

from .icons import app_icon
from .theme import t


class LoginDialog(QDialog):
    """Ouvre une session sur le serveur. `self.remote` porte la session en cas
    de succès ; `infos()` donne l'URL/identifiants saisis."""

    def __init__(self, url: str, username: str = "", parent=None,
                 url_editable: bool = False):
        super().__init__(parent)
        self.remote = None
        self.memoriser = False
        self._url_editable = url_editable
        self.setWindowTitle("Sentinelle — Connexion")
        self.setWindowIcon(app_icon())
        self.setObjectName("loginPage")
        self.setFixedWidth(400)
        self.setStyleSheet(f"QDialog#loginPage {{ background: {t('bg')}; }}")

        # --- en-tête : logo + marque ---
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        pix = app_icon().pixmap(84, 84)
        logo.setPixmap(pix)

        marque = QLabel("SENTINELLE")
        marque.setAlignment(Qt.AlignCenter)
        marque.setStyleSheet(f"color: {t('text')}; font-size: 22px; "
                             f"font-weight: 700; letter-spacing: 4px;")
        sous_titre = QLabel("Connexion au serveur de vidéosurveillance")
        sous_titre.setAlignment(Qt.AlignCenter)
        sous_titre.setStyleSheet(f"color: {t('text_dim')}; font-size: 13px;")

        # --- champs ---
        champ_css = (f"QLineEdit {{ background: {t('surface_alt')}; "
                     f"color: {t('text')}; border: 1px solid {t('border')}; "
                     f"border-radius: 8px; padding: 11px 14px; font-size: 14px; }}"
                     f"QLineEdit:focus {{ border-color: {t('accent')}; }}")
        self._user = QLineEdit(username)
        self._user.setPlaceholderText("Identifiant")
        self._user.setClearButtonEnabled(True)
        self._pwd = QLineEdit()
        self._pwd.setPlaceholderText("Mot de passe")
        self._pwd.setEchoMode(QLineEdit.Password)
        for champ in (self._user, self._pwd):
            champ.setStyleSheet(champ_css)
            champ.returnPressed.connect(self._tenter)

        self._memo = QCheckBox("Rester connecté sur ce poste")
        self._memo.setChecked(bool(username))

        self._erreur = QLabel("")
        self._erreur.setWordWrap(True)
        self._erreur.setAlignment(Qt.AlignCenter)
        self._erreur.setStyleSheet(f"color: {t('danger')};")
        self._erreur.hide()

        self._btn = QPushButton("Se connecter")
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setStyleSheet(
            f"QPushButton {{ background: {t('accent')}; color: {t('on_accent')}; "
            f"border: none; border-radius: 8px; padding: 12px; font-size: 14px; "
            f"font-weight: 600; text-align: center; }}"
            f"QPushButton:hover {{ background: {t('accent_hover')}; }}"
            f"QPushButton:disabled {{ background: {t('elevated')}; "
            f"color: {t('text_faint')}; }}")
        self._btn.clicked.connect(self._tenter)

        # --- pied : serveur ---
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {t('border_soft')}; border: none; "
                          f"max-height: 1px;")
        lbl_srv = QLabel("Serveur")
        lbl_srv.setStyleSheet(f"color: {t('text_faint')}; font-size: 12px;")
        self._url = QLineEdit(url)
        self._url.setPlaceholderText("http://serveur:8080")
        self._url.setReadOnly(not self._url_editable)
        if not self._url_editable:
            self._url.setToolTip("Adresse verrouillée — modifiable par un "
                                 "administrateur (Administration → Mode)")
        self._url.setStyleSheet(
            f"QLineEdit {{ background: transparent; color: {t('text_dim')}; "
            f"border: none; border-bottom: 1px solid {t('border_soft')}; "
            f"border-radius: 0; padding: 4px 2px; font-size: 12px; }}"
            f"QLineEdit:focus {{ border-bottom-color: {t('accent')}; "
            f"color: {t('text')}; }}")
        ligne_srv = QHBoxLayout()
        ligne_srv.setSpacing(10)
        ligne_srv.addWidget(lbl_srv)
        ligne_srv.addWidget(self._url, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 32, 36, 24)
        lay.setSpacing(0)
        lay.addWidget(logo)
        lay.addSpacing(10)
        lay.addWidget(marque)
        lay.addSpacing(4)
        lay.addWidget(sous_titre)
        lay.addSpacing(26)
        lay.addWidget(self._user)
        lay.addSpacing(10)
        lay.addWidget(self._pwd)
        lay.addSpacing(12)
        lay.addWidget(self._memo)
        lay.addSpacing(16)
        lay.addWidget(self._btn)
        lay.addSpacing(8)
        lay.addWidget(self._erreur)
        lay.addSpacing(14)
        lay.addWidget(sep)
        lay.addSpacing(8)
        lay.addLayout(ligne_srv)

        (self._pwd if username else self._user).setFocus()

    def _afficher_erreur(self, texte: str):
        self._erreur.setText(texte)
        self._erreur.setVisible(bool(texte))

    def _tenter(self):
        from ..remote import ErreurServeur, ServeurDistant
        url = self._url.text().strip()
        user = self._user.text().strip()
        if not url:
            self._afficher_erreur("Renseignez l'adresse du serveur (en bas).")
            return
        if not user:
            self._afficher_erreur("Renseignez votre identifiant.")
            return
        self._btn.setEnabled(False)
        self._btn.setText("Connexion…")
        self.repaint()
        srv = ServeurDistant(url)
        try:
            srv.login(user, self._pwd.text())
        except ErreurServeur as e:
            self._afficher_erreur(str(e))
            self._btn.setEnabled(True)
            self._btn.setText("Se connecter")
            return
        self.remote = srv
        self.memoriser = self._memo.isChecked()
        self.accept()

    def infos(self) -> dict:
        return {"url": self._url.text().strip(), "username": self._user.text().strip(),
                "password": self._pwd.text(), "memoriser": self.memoriser}
