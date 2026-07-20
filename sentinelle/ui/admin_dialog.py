"""Panneau d'administration (comptes admin uniquement).

Regroupe la gestion serveur, séparée de la Configuration des utilisateurs :
  - Utilisateurs : comptes, rôles, droits d'accès par site / caméra ;
  - Caméras et sites : édition de la configuration centrale ;
  - Réglages : durée de rotation, boucles.

Les envois au serveur (config, utilisateurs) se font à l'enregistrement, avec
gestion des erreurs ; la fenêtre ne se ferme pas si un envoi échoue.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSpinBox, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..config import AppConfig
from .config_dialogs import CameraManagerWidget
from .icons import icon


class UserEditDialog(QDialog):
    """Création / édition d'un compte et de ses droits."""

    def __init__(self, cfg: AppConfig, parent=None, user: dict | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self._user = user or {}
        self.setWindowTitle("Compte" if user else "Nouveau compte")
        self.setMinimumSize(460, 520)

        self._nom = QLineEdit(self._user.get("username", ""))
        self._nom.setPlaceholderText("identifiant de connexion")
        self._nom.setEnabled(user is None)          # nom figé en édition
        self._mdp = QLineEdit(); self._mdp.setEchoMode(QLineEdit.Password)
        self._mdp.setPlaceholderText("Laisser vide pour conserver le mot de passe"
                                     if user else "mot de passe")
        self._role = QComboBox()
        self._role.addItem("Utilisateur", "user")
        self._role.addItem("Administrateur", "admin")
        if self._user.get("role") == "admin":
            self._role.setCurrentIndex(1)
        self._role.currentIndexChanged.connect(self._maj_droits)

        form = QFormLayout()
        form.addRow("Identifiant", self._nom)
        form.addRow("Mot de passe", self._mdp)
        form.addRow("Rôle", self._role)

        # droits : arbre sites/caméras à cocher (un site coché = tout le site,
        # y compris ses futures caméras)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        sites_coches = set(self._user.get("sites") or [])
        cams_cochees = set(self._user.get("cameras") or [])
        for site in cfg.sites:
            si = QTreeWidgetItem([site.nom])
            si.setData(0, Qt.UserRole, ("site", site.id))
            si.setFlags(si.flags() | Qt.ItemIsUserCheckable)
            si.setCheckState(0, Qt.Checked if site.id in sites_coches else Qt.Unchecked)
            for cam in [c for c in cfg.cameras if c.site.id == site.id]:
                ci = QTreeWidgetItem([cam.nom])
                ci.setData(0, Qt.UserRole, ("camera", cam.id))
                ci.setFlags(ci.flags() | Qt.ItemIsUserCheckable)
                coche = site.id in sites_coches or cam.id in cams_cochees
                ci.setCheckState(0, Qt.Checked if coche else Qt.Unchecked)
                si.addChild(ci)
            self._tree.addTopLevelItem(si)
        self._tree.expandAll()

        self._grp_droits = QGroupBox("Caméras autorisées")
        gd = QVBoxLayout(self._grp_droits)
        gd.addWidget(self._tree, 1)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self._grp_droits, 1)
        lay.addWidget(boutons)
        self._maj_droits()

    def _maj_droits(self):
        # un admin voit tout : les droits par caméra n'ont pas de sens
        self._grp_droits.setDisabled(self._role.currentData() == "admin")

    def _valider(self):
        if not self._nom.text().strip():
            QMessageBox.warning(self, "Compte", "L'identifiant est obligatoire.")
            return
        if self._user is None or not self._user:
            if not self._mdp.text():
                QMessageBox.warning(self, "Compte", "Définissez un mot de passe.")
                return
        self.accept()

    def valeurs(self) -> dict:
        sites, cams = [], []
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            kind, ident = si.data(0, Qt.UserRole)
            if si.checkState(0) == Qt.Checked:
                sites.append(ident)
                continue                                    # site entier
            for j in range(si.childCount()):
                ci = si.child(j)
                if ci.checkState(0) == Qt.Checked:
                    cams.append(ci.data(0, Qt.UserRole)[1])
        d = {
            "username": self._nom.text().strip(),
            "role": self._role.currentData(),
            "tout": self._role.currentData() == "admin",
            "sites": sites, "cameras": cams,
        }
        if self._mdp.text():
            d["password"] = self._mdp.text()
        return d


class UsersWidget(QWidget):
    """Liste des comptes + création / édition / suppression."""

    def __init__(self, remote, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._remote = remote
        self._cfg = cfg
        self.modifie = False
        self._users: list[dict] = []

        self._liste = QListWidget()
        self._liste.itemDoubleClicked.connect(lambda *_: self._modifier())
        btn_add = QPushButton(icon("plus"), " Nouveau compte")
        btn_add.clicked.connect(self._ajouter)
        btn_edit = QPushButton(icon("pencil"), " Modifier")
        btn_edit.clicked.connect(self._modifier)
        btn_pwd = QPushButton(icon("lock"), " Mot de passe")
        btn_pwd.clicked.connect(self._mot_de_passe)
        btn_del = QPushButton(icon("trash"), " Supprimer")
        btn_del.clicked.connect(self._supprimer)
        col = QHBoxLayout()
        for b in (btn_add, btn_edit, btn_pwd, btn_del):
            col.addWidget(b)
        col.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(col)
        lay.addWidget(self._liste, 1)
        self._charger()

    def _charger(self):
        from ..remote import ErreurServeur
        try:
            self._users = self._remote.users_liste()
        except ErreurServeur as e:
            QMessageBox.warning(self, "Utilisateurs", f"Chargement impossible : {e}")
            self._users = []
        self._rafraichir()

    def _rafraichir(self):
        self._liste.clear()
        for u in self._users:
            if u.get("role") == "admin" or u.get("tout"):
                portee = "toutes les caméras"
            else:
                ns, nc = len(u.get("sites") or []), len(u.get("cameras") or [])
                portee = f"{ns} site(s), {nc} caméra(s)"
            role = "admin" if u.get("role") == "admin" else "utilisateur"
            it = QListWidgetItem(f"{u['username']}  ·  {role}  ·  {portee}")
            it.setData(Qt.UserRole, u["username"])
            self._liste.addItem(it)

    def _selection(self) -> dict | None:
        it = self._liste.currentItem()
        if not it:
            return None
        nom = it.data(Qt.UserRole)
        return next((u for u in self._users if u["username"] == nom), None)

    def _ajouter(self):
        dlg = UserEditDialog(self._cfg, self, user=None)
        if dlg.exec():
            v = dlg.valeurs()
            if any(u["username"] == v["username"] for u in self._users):
                QMessageBox.warning(self, "Compte", "Cet identifiant existe déjà.")
                return
            self._users.append(v)
            self.modifie = True
            self._rafraichir()

    def _modifier(self):
        u = self._selection()
        if not u:
            return
        dlg = UserEditDialog(self._cfg, self, user=u)
        if dlg.exec():
            v = dlg.valeurs()
            u.update(v)
            self.modifie = True
            self._rafraichir()

    def _mot_de_passe(self):
        u = self._selection()
        if not u:
            return
        mdp, ok = QInputDialog.getText(self, "Mot de passe",
                                       f"Nouveau mot de passe pour « {u['username']} » :",
                                       QLineEdit.Password)
        if ok and mdp:
            u["password"] = mdp
            self.modifie = True

    def _supprimer(self):
        u = self._selection()
        if not u:
            return
        if QMessageBox.question(self, "Supprimer",
                                f"Supprimer le compte « {u['username']} » ?") != QMessageBox.Yes:
            return
        self._users = [x for x in self._users if x["username"] != u["username"]]
        self.modifie = True
        self._rafraichir()

    def pousser(self) -> bool:
        """Envoie les comptes au serveur. Retourne True si tout s'est bien passé."""
        from ..remote import ErreurServeur
        try:
            warnings = self._remote.users_pousser(self._users)
        except ErreurServeur as e:
            QMessageBox.critical(self, "Utilisateurs", f"Enregistrement impossible :\n{e}")
            return False
        if warnings:
            QMessageBox.warning(self, "Utilisateurs — avertissements",
                                "\n".join(warnings[:20]))
        return True


class AdminDialog(QDialog):
    """Panneau d'administration global (onglets)."""

    def __init__(self, cfg: AppConfig, remote, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._remote = remote
        self.recharger = False          # signale à la fenêtre de recharger
        self.demande_mode = None        # "local" | "serveur" : bascule demandée
        self.setWindowTitle("Administration du serveur")
        self.setWindowIcon(icon("settings"))
        self.setMinimumSize(780, 640)

        self._tabs = QTabWidget()
        self._manager = CameraManagerWidget(cfg, self)
        self._tabs.addTab(self._manager, "Caméras et sites")
        self._users = UsersWidget(remote, cfg, self)
        self._tabs.addTab(self._users, "Utilisateurs")

        reglages = QWidget()
        rl = QVBoxLayout(reglages)
        self._rot = QSpinBox(); self._rot.setRange(3, 3600); self._rot.setSuffix(" s")
        self._rot.setValue(cfg.rotation_duree_s)
        grp = QGroupBox("Réglages généraux")
        gf = QFormLayout(grp)
        gf.setContentsMargins(12, 8, 12, 8); gf.setHorizontalSpacing(18)
        gf.addRow("Durée de rotation par défaut", self._rot)
        rl.addWidget(grp)

        # mode de fonctionnement de CE poste (action admin)
        mode = QGroupBox("Mode de fonctionnement de ce poste")
        mv = QVBoxLayout(mode)
        mv.setContentsMargins(12, 8, 12, 8)
        btn_autre = QPushButton(icon("search"), " Connecter à un autre serveur…")
        btn_autre.clicked.connect(self._changer_serveur)
        btn_local = QPushButton(icon("lock"), " Repasser en mode autonome")
        btn_local.clicked.connect(self._repasser_local)
        mv.addWidget(btn_autre)
        mv.addWidget(btn_local)
        rl.addWidget(mode)
        rl.addStretch(1)
        self._tabs.addTab(reglages, "Réglages")

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Enregistrer")
        boutons.button(QDialogButtonBox.Cancel).setText("Fermer")
        boutons.accepted.connect(self._terminer)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self._tabs, 1)
        lay.addWidget(boutons)

    def _repasser_local(self):
        # la bascule (confirmation + application) est gérée par la fenêtre principale
        self.demande_mode = "local"
        self.reject()

    def _changer_serveur(self):
        self.demande_mode = "serveur"
        self.reject()

    def _terminer(self):
        cfg_a_pousser = self._manager.modifie or self._rot.value() != self._cfg.rotation_duree_s
        if self._rot.value() != self._cfg.rotation_duree_s:
            self._cfg.rotation_duree_s = self._rot.value()

        from ..remote import ErreurServeur
        if cfg_a_pousser:
            try:
                warnings = self._remote.pousser(self._cfg)
            except ErreurServeur as e:
                QMessageBox.critical(self, "Serveur", f"Configuration non enregistrée :\n{e}")
                return
            if warnings:
                QMessageBox.warning(self, "Configuration — avertissements",
                                    "\n".join(warnings[:20]))
            self.recharger = True

        if self._users.modifie:
            if not self._users.pousser():
                return
            self.recharger = True
        self.accept()
