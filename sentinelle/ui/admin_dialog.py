"""Panneau d'administration (comptes admin uniquement).

Regroupe la gestion serveur, séparée de la Configuration des utilisateurs :
  - Caméras et sites : édition de la configuration centrale ;
  - Rondes : rondes partagées, attribuées à tous ou à certains comptes ;
  - Utilisateurs : comptes, rôles, droits d'accès par site / caméra ;
  - Réglages : durée de rotation, mode du poste.

Les envois au serveur (config, rondes, utilisateurs) se font à l'enregistrement,
avec gestion des erreurs ; la fenêtre ne se ferme pas si un envoi échoue, et
une fermeture avec des modifications en attente demande confirmation.
"""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from ..config import AppConfig, Etape, Sequence
from .camera_picker import CameraPicker
from .config_dialogs import CameraManagerWidget
from .icons import icon
from .texte import compte
from .sequence_editor import StepsEditor, dupliquer_sequence


class UserEditDialog(QDialog):
    """Création / édition d'un compte et de ses droits."""

    def __init__(self, cfg: AppConfig, parent=None, user: dict | None = None):
        super().__init__(parent)
        self._user = user or {}
        self.setWindowTitle("Compte" if user else "Nouveau compte")
        self.setMinimumSize(460, 560)

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
        form.addRow("Identifiant :", self._nom)
        form.addRow("Mot de passe :", self._mdp)
        form.addRow("Rôle :", self._role)

        # droits : sélecteur commun (un site coché en entier = tout le site,
        # y compris ses futures caméras)
        self._picker = CameraPicker(cfg, self)
        self._picker.set_droits(self._user.get("sites"), self._user.get("cameras"))

        self._grp_droits = QGroupBox("Caméras autorisées")
        gd = QVBoxLayout(self._grp_droits)
        self._hint_admin = QLabel("Un administrateur voit toutes les caméras "
                                  "et gère le serveur : rien à restreindre ici.")
        self._hint_admin.setObjectName("hint")
        self._hint_admin.setWordWrap(True)
        gd.addWidget(self._hint_admin)
        gd.addWidget(self._picker, 1)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Valider")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self._grp_droits, 1)
        lay.addWidget(boutons)
        self._maj_droits()

    def _maj_droits(self):
        # un admin voit tout : les droits par caméra n'ont pas de sens
        admin = self._role.currentData() == "admin"
        self._picker.setVisible(not admin)
        self._hint_admin.setVisible(admin)

    def _valider(self):
        if not self._nom.text().strip():
            QMessageBox.warning(self, "Compte", "L'identifiant est obligatoire.")
            return
        if self._user is None or not self._user:
            if not self._mdp.text():
                QMessageBox.warning(self, "Compte", "Définissez un mot de passe.")
                return
        if self._mdp.text() and len(self._mdp.text()) < 8:
            # même minimum que le serveur (MIN_MDP) : évite un rejet tardif
            QMessageBox.warning(self, "Compte",
                                "Le mot de passe doit faire au moins 8 caractères.")
            return
        self.accept()

    def valeurs(self) -> dict:
        d = {
            "username": self._nom.text().strip(),
            "role": self._role.currentData(),
            "tout": self._role.currentData() == "admin",
            "sites": self._picker.sites_ids(),
            "cameras": self._picker.ids_hors_sites(),
        }
        if self._mdp.text():
            d["password"] = self._mdp.text()
        return d


class UsersWidget(QWidget):
    """Liste des comptes + création / édition / suppression."""

    _charge = Signal(object, str)               # (users|None, erreur) — thread → UI

    def __init__(self, remote, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._remote = remote
        self._cfg = cfg
        self.modifie = False
        self._users: list[dict] = []
        self._charge.connect(self._on_charge)

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

    def usernames(self) -> list[str]:
        return [u["username"] for u in self._users]

    def _charger(self):
        """Charge les comptes HORS du thread UI (l'appel gelait l'ouverture du
        panneau d'administration sur réseau lent)."""
        self._liste.clear()
        self._liste.addItem("Chargement…")
        # désactivé pendant le chargement : sinon un ajout/édition local serait
        # écrasé par la réponse serveur qui arrive après (perte silencieuse)
        self.setEnabled(False)
        remote = self._remote

        def work():
            from ..remote import ErreurServeur
            data, err, ok = None, "", False
            try:
                data = remote.users_liste()
                ok = True
            except ErreurServeur as e:
                err = str(e) or "serveur injoignable"
            except Exception as e:
                err = str(e) or "erreur inattendue"
            finally:
                if not ok and not err:
                    err = "erreur inattendue"
                try:
                    self._charge.emit(data, err) # finally : réactive toujours le widget
                except RuntimeError:
                    pass                         # widget détruit entre-temps
        threading.Thread(target=work, daemon=True, name="admin-users").start()

    def _on_charge(self, data, err: str):
        self.setEnabled(True)
        if err:
            QMessageBox.warning(self, "Utilisateurs", f"Chargement impossible : {err}")
            self._users = []
        else:
            self._users = data or []
        self._rafraichir()

    def _rafraichir(self, selection: str | None = None):
        # préserve la sélection et la position de lecture entre deux mises à jour
        if selection is None and self._liste.currentItem() is not None:
            selection = self._liste.currentItem().data(Qt.UserRole)
        scroll = self._liste.verticalScrollBar().value()
        self._liste.clear()
        for u in self._users:
            if u.get("role") == "admin" or u.get("tout"):
                portee = "toutes les caméras"
            else:
                ns, nc = len(u.get("sites") or []), len(u.get("cameras") or [])
                portee = f"{compte(ns, 'site')}, {compte(nc, 'caméra')}"
            role = "administrateur" if u.get("role") == "admin" else "utilisateur"
            it = QListWidgetItem(f"{u['username']}  ·  {role}  ·  {portee}")
            it.setIcon(icon("lock" if u.get("role") == "admin" else "user"))
            it.setData(Qt.UserRole, u["username"])
            self._liste.addItem(it)
            if u["username"] == selection:
                self._liste.setCurrentItem(it)
        self._liste.verticalScrollBar().setValue(scroll)

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
            self._rafraichir(v["username"])

    def _modifier(self):
        u = self._selection()
        if not u:
            return
        dlg = UserEditDialog(self._cfg, self, user=u)
        if dlg.exec():
            v = dlg.valeurs()
            u.update(v)
            self.modifie = True
            self._rafraichir(u["username"])

    def _mot_de_passe(self):
        u = self._selection()
        if not u:
            return
        mdp, ok = QInputDialog.getText(self, "Mot de passe",
                                       f"Nouveau mot de passe pour « {u['username']} » :",
                                       QLineEdit.Password)
        if not ok or not mdp:
            return
        if len(mdp) < 8:                     # même minimum que le serveur (MIN_MDP)
            QMessageBox.warning(self, "Mot de passe",
                                "Le mot de passe doit faire au moins 8 caractères.")
            return
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

    def donnees(self) -> list[dict]:
        """Comptes à envoyer au serveur (sans effet de bord réseau)."""
        return list(self._users)


class RondesWidget(QWidget):
    """Rondes partagées : édition + attribution aux comptes.

    Une ronde attribuée apparaît automatiquement chez les comptes concernés
    (filtrée à leurs caméras autorisées), sans qu'ils aient à la recréer."""

    _charge = Signal(object, str)               # (rondes brutes|None, erreur)

    def __init__(self, remote, cfg: AppConfig, users_widget: UsersWidget,
                 parent=None):
        super().__init__(parent)
        self._remote = remote
        self._cfg = cfg
        self._users_widget = users_widget       # source des comptes (à jour)
        self.modifie = False
        self._rondes: list[Sequence] = []
        self._charge.connect(self._on_charge)

        # ---- colonne de gauche : liste des rondes ----
        self._liste = QListWidget()
        self._liste.currentRowChanged.connect(self._selection_changee)
        btn_nouv = QPushButton(icon("plus"), " Nouvelle")
        btn_nouv.clicked.connect(self._nouvelle)
        btn_ren = QPushButton(icon("pencil"), " Renommer")
        btn_ren.clicked.connect(self._renommer)
        btn_dup = QPushButton(icon("copy"), " Dupliquer")
        btn_dup.clicked.connect(self._dupliquer)
        btn_sup = QPushButton(icon("trash"), " Supprimer")
        btn_sup.clicked.connect(self._supprimer)

        gauche = QVBoxLayout()
        gauche.addWidget(QLabel("Rondes partagées :"))
        gauche.addWidget(self._liste, 1)
        for b in (btn_nouv, btn_ren, btn_dup, btn_sup):
            gauche.addWidget(b)

        # ---- colonne de droite : étapes + attribution ----
        self._steps = StepsEditor(cfg, self)
        self._steps.modifie.connect(self._etapes_modifiees)

        self._tous = QCheckBox("Attribuer à tous les comptes")
        self._tous.toggled.connect(self._attribution_changee)
        self._comptes = QListWidget()
        self._comptes.setMaximumHeight(140)
        self._comptes.itemChanged.connect(self._attribution_changee)
        grp = QGroupBox("Attribution")
        gl = QVBoxLayout(grp)
        gl.setContentsMargins(12, 8, 12, 8)
        gl.addWidget(self._tous)
        gl.addWidget(self._comptes)

        droite = QVBoxLayout()
        droite.addWidget(self._steps, 1)
        droite.addWidget(grp)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        gauche_w = QWidget(); gauche_w.setLayout(gauche)
        droite_w = QWidget(); droite_w.setLayout(droite)
        lay.addWidget(gauche_w, 1)
        lay.addWidget(droite_w, 2)

        self._charger()

    # ------------------------------------------------------------- chargement

    def _charger(self):
        """Charge les rondes partagées HORS du thread UI."""
        self._liste.clear()
        self._liste.addItem("Chargement…")
        self.setEnabled(False)                   # cf. UsersWidget : anti-écrasement
        remote = self._remote

        def work():
            from ..remote import ErreurServeur
            brut, err, ok = None, "", False
            try:
                brut = remote.rounds_liste()
                ok = True
            except ErreurServeur as e:
                err = str(e) or "serveur injoignable"
            except Exception as e:
                err = str(e) or "erreur inattendue"
            finally:
                if not ok and not err:
                    err = "erreur inattendue"
                try:
                    self._charge.emit(brut, err)
                except RuntimeError:
                    pass
        threading.Thread(target=work, daemon=True, name="admin-rondes").start()

    def _on_charge(self, brut, err: str):
        self.setEnabled(True)
        if err:
            QMessageBox.warning(self, "Rondes", f"Chargement impossible : {err}")
            brut = []
        self._rondes = []
        for s in (brut or []):
            self._rondes.append(Sequence(
                nom=str(s.get("nom", "")), id=str(s.get("id", "")),
                etapes=[Etape(mode=str(e.get("mode", "grille")),
                              cameras=[str(c) for c in (e.get("cameras") or [])],
                              duree_s=int(e.get("duree_s", 30)))
                        for e in (s.get("etapes") or [])],
                tous=bool(s.get("tous", False)),
                utilisateurs=[str(x) for x in (s.get("utilisateurs") or [])]))
        self._maj_liste()

    def rafraichir_comptes(self):
        """Réaligne la liste d'attribution sur les comptes du moment (l'onglet
        Utilisateurs a pu en créer ou en supprimer)."""
        self._peupler_comptes(self._ronde_courante())

    # -------------------------------------------------------------- sélection

    def _ronde_courante(self) -> Sequence | None:
        i = self._liste.currentRow()
        return self._rondes[i] if 0 <= i < len(self._rondes) else None

    def _maj_liste(self, selection: int = 0):
        self._liste.blockSignals(True)
        self._liste.clear()
        for s in self._rondes:
            portee = ("tous les comptes" if s.tous
                      else compte(len(s.utilisateurs), "compte"))
            it = QListWidgetItem(
                f"{s.nom}  ({compte(len(s.etapes), 'étape')} · {portee})")
            it.setIcon(icon("route"))
            self._liste.addItem(it)
        self._liste.blockSignals(False)
        if self._rondes:
            self._liste.setCurrentRow(min(selection, len(self._rondes) - 1))
        self._selection_changee()

    def _selection_changee(self):
        seq = self._ronde_courante()
        self._steps.set_sequence(seq)
        self._peupler_comptes(seq)

    def _peupler_comptes(self, seq: Sequence | None):
        self._comptes.blockSignals(True)
        self._tous.blockSignals(True)
        self._comptes.clear()
        attribues = set(seq.utilisateurs) if seq else set()
        self._tous.setChecked(bool(seq and seq.tous))
        for nom in self._users_widget.usernames():
            it = QListWidgetItem(nom)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if nom in attribues else Qt.Unchecked)
            self._comptes.addItem(it)
        actif = seq is not None
        self._tous.setEnabled(actif)
        self._comptes.setEnabled(actif and not (seq and seq.tous))
        self._tous.blockSignals(False)
        self._comptes.blockSignals(False)

    # ---------------------------------------------------------------- actions

    def _etapes_modifiees(self):
        self.modifie = True
        self._maj_libelle_courant()

    def _attribution_changee(self, *_):
        seq = self._ronde_courante()
        if seq is None:
            return
        seq.tous = self._tous.isChecked()
        seq.utilisateurs = [self._comptes.item(i).text()
                            for i in range(self._comptes.count())
                            if self._comptes.item(i).checkState() == Qt.Checked]
        self._comptes.setEnabled(not seq.tous)
        self.modifie = True
        self._maj_libelle_courant()

    def _maj_libelle_courant(self):
        i = self._liste.currentRow()
        seq = self._ronde_courante()
        if seq is None:
            return
        portee = ("tous les comptes" if seq.tous
                  else compte(len(seq.utilisateurs), "compte"))
        self._liste.item(i).setText(
            f"{seq.nom}  ({compte(len(seq.etapes), 'étape')} · {portee})")

    def _nouvelle(self):
        if not self._cfg.cameras:
            QMessageBox.information(self, "Rondes",
                                    "Ajoutez d'abord des caméras (onglet Caméras et sites).")
            return
        nom, ok = QInputDialog.getText(self, "Nouvelle ronde", "Nom :",
                                       text=f"Ronde {len(self._rondes) + 1}")
        if ok and nom.strip():
            self._rondes.append(Sequence(nom=nom.strip(), tous=True))
            self.modifie = True
            self._maj_liste(len(self._rondes) - 1)

    def _renommer(self):
        seq = self._ronde_courante()
        if not seq:
            return
        nom, ok = QInputDialog.getText(self, "Renommer", "Nom :", text=seq.nom)
        if ok and nom.strip():
            seq.nom = nom.strip()
            self.modifie = True
            self._maj_libelle_courant()

    def _dupliquer(self):
        seq = self._ronde_courante()
        if not seq:
            return
        copie = dupliquer_sequence(seq)
        copie.tous, copie.utilisateurs = seq.tous, list(seq.utilisateurs)
        self._rondes.append(copie)
        self.modifie = True
        self._maj_liste(len(self._rondes) - 1)

    def _supprimer(self):
        seq = self._ronde_courante()
        if not seq:
            return
        if QMessageBox.question(
                self, "Supprimer",
                f"Supprimer la ronde partagée « {seq.nom} » ?\n\n"
                "Elle disparaîtra chez tous les comptes auxquels elle est "
                "attribuée.") != QMessageBox.Yes:
            return
        self._rondes.remove(seq)
        self.modifie = True
        self._maj_liste()

    def donnees(self) -> list[dict]:
        """Rondes (avec étapes) à envoyer — sans effet de bord réseau."""
        return [{"id": s.id, "nom": s.nom,
                 "etapes": [e.to_dict() for e in s.etapes],
                 "tous": s.tous, "utilisateurs": list(s.utilisateurs)}
                for s in self._rondes if s.etapes]

    def vides(self) -> list[str]:
        """Noms des rondes sans étape (non enregistrées, signalées à l'admin)."""
        return [s.nom for s in self._rondes if not s.etapes]


class AdminDialog(QDialog):
    """Panneau d'administration global (onglets)."""

    _sauve_termine = Signal(list)       # [(etape, ok, warnings, err)] — thread → UI

    def __init__(self, cfg: AppConfig, remote, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._remote = remote
        self.recharger = False          # signale à la fenêtre de recharger
        self.enregistre = False         # au moins un envoi a réussi
        self.demande_mode = None        # "local" | "serveur" : bascule demandée
        self._sauvegarde_en_cours = False
        self._sauve_termine.connect(self._on_sauve_termine)
        self.setWindowTitle("Administration du serveur")
        self.setWindowIcon(icon("settings"))
        self.setMinimumSize(820, 660)

        self._tabs = QTabWidget()
        self._manager = CameraManagerWidget(cfg, self)
        self._tabs.addTab(self._manager, icon("camera"), "Caméras et sites")
        self._users = UsersWidget(remote, cfg, self)
        self._rondes = RondesWidget(remote, cfg, self._users, self)
        # les comptes se chargent en arrière-plan : réaligner l'attribution des
        # rondes dès qu'ils arrivent (l'onglet Rondes lit la liste des comptes)
        self._users._charge.connect(lambda *_: self._rondes.rafraichir_comptes())
        self._tabs.addTab(self._rondes, icon("route"), "Rondes")
        self._tabs.addTab(self._users, icon("users"), "Utilisateurs")
        # les comptes ont pu changer dans l'onglet Utilisateurs : réaligner
        # l'attribution des rondes à chaque affichage de l'onglet
        self._tabs.currentChanged.connect(
            lambda i: self._rondes.rafraichir_comptes()
            if self._tabs.widget(i) is self._rondes else None)

        reglages = QWidget()
        rl = QVBoxLayout(reglages)
        self._rot = QSpinBox(); self._rot.setRange(3, 3600); self._rot.setSuffix(" s")
        self._rot.setValue(cfg.rotation_duree_s)
        self._rot.setMaximumWidth(140)
        grp = QGroupBox("Réglages généraux")
        gf = QFormLayout(grp)
        gf.setContentsMargins(12, 8, 12, 8); gf.setHorizontalSpacing(18)
        gf.addRow("Durée de rotation par défaut :", self._rot)
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
        self._tabs.addTab(reglages, icon("settings"), "Réglages")

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Enregistrer")
        boutons.button(QDialogButtonBox.Cancel).setText("Fermer")
        boutons.accepted.connect(self._terminer)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self._tabs, 1)
        lay.addWidget(boutons)

    def _modifications_en_attente(self) -> bool:
        return (self._manager.modifie or self._users.modifie
                or self._rondes.modifie
                or self._rot.value() != self._cfg.rotation_duree_s)

    def reject(self):
        # un enregistrement est en vol (thread) : ignorer toute fermeture (bouton
        # Fermer, Échap, ou X du gestionnaire de fenêtres) jusqu'à sa fin — sinon
        # un « Enregistrer » relancerait un second thread concurrent (double push)
        if self._sauvegarde_en_cours:
            return
        # ne jamais jeter des modifications sans prévenir
        if self.demande_mode is None and self._modifications_en_attente():
            r = QMessageBox.question(
                self, "Modifications non enregistrées",
                "Des modifications n'ont pas été enregistrées.\n"
                "Enregistrer avant de fermer ?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Save:
                self._terminer()
                return
        super().reject()

    def _repasser_local(self):
        # la bascule (confirmation + application) est gérée par la fenêtre principale
        self.demande_mode = "local"
        self.reject()

    def _changer_serveur(self):
        self.demande_mode = "serveur"
        self.reject()

    _TITRES_ERREUR = {"config": "Serveur", "users": "Utilisateurs", "rondes": "Rondes"}
    _TITRES_AVERT = {"config": "Configuration — avertissements",
                     "users": "Utilisateurs — avertissements",
                     "rondes": "Rondes — avertissements"}

    def _terminer(self):
        if self._sauvegarde_en_cours:            # ré-entrance : un envoi tourne déjà
            return
        cfg_a_pousser = self._manager.modifie or self._rot.value() != self._cfg.rotation_duree_s
        if self._rot.value() != self._cfg.rotation_duree_s:
            self._cfg.rotation_duree_s = self._rot.value()

        # données figées sur le thread UI, envoi séquentiel en arrière-plan.
        # Ordre : config, puis comptes AVANT rondes (une ronde attribuée à un
        # compte créé dans la session doit le trouver déjà enregistré).
        etapes = []
        if cfg_a_pousser:
            cfg = self._cfg
            etapes.append(("config", (lambda: self._remote.pousser(cfg)), []))
        if self._users.modifie:
            users = self._users.donnees()
            etapes.append(("users", (lambda: self._remote.users_pousser(users)), []))
        if self._rondes.modifie:
            data = self._rondes.donnees()
            vides = self._rondes.vides()
            etapes.append(("rondes", (lambda: self._remote.rounds_pousser(data)), vides))
        if not etapes:
            self.accept()
            return

        self._sauvegarde_en_cours = True
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)

        def work():
            from ..remote import ErreurServeur
            resultats = []
            try:
                for nom, fn, vides in etapes:
                    try:
                        warnings = list(fn() or [])
                        warnings += [f"ronde '{n}' sans étape — non enregistrée" for n in vides]
                        resultats.append((nom, True, warnings, ""))
                    except ErreurServeur as e:
                        resultats.append((nom, False, [], str(e) or "serveur injoignable"))
                        break                    # arrêt à la première erreur
                    except Exception as e:
                        resultats.append((nom, False, [], str(e) or "erreur inattendue"))
                        break
            finally:
                # toujours émettre : sinon le dialogue reste désactivé ET
                # infermable (reject() est bloqué pendant la sauvegarde)
                try:
                    self._sauve_termine.emit(resultats)
                except RuntimeError:
                    pass
        threading.Thread(target=work, daemon=True, name="admin-save").start()

    def _on_sauve_termine(self, resultats):
        QApplication.restoreOverrideCursor()
        self.setEnabled(True)
        self._sauvegarde_en_cours = False
        if not resultats:
            # thread mort avant tout traitement : ne JAMAIS fermer en « succès »
            # (perte silencieuse) — laisser le panneau ouvert avec les modifs
            QMessageBox.critical(self, "Serveur",
                                 "Enregistrement impossible : erreur inattendue.")
            return
        echec = None
        for nom, ok, warnings, err in resultats:
            if not ok:
                echec = (nom, err)
                break
            if nom == "config":
                self._manager.modifie = False
            elif nom == "users":
                self._users.modifie = False
            elif nom == "rondes":
                self._rondes.modifie = False
            self.recharger = True
            self.enregistre = True
            if warnings:
                QMessageBox.warning(self, self._TITRES_AVERT[nom],
                                    "\n".join(warnings[:20]))
        if echec is not None:
            QMessageBox.critical(self, self._TITRES_ERREUR[echec[0]],
                                 f"Enregistrement impossible :\n{echec[1]}")
            return                               # laisser le panneau ouvert
        self.accept()
