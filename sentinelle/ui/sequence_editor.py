"""Éditeur de séquences (« boucles ») — 100 % dans l'interface.

Une séquence = suite d'étapes jouées en boucle. Chaque étape affiche soit une
grille de caméras choisies, soit une caméra en mono, pendant une durée donnée.
Les flux de l'étape précédente sont fermés avant d'ouvrir les suivants
(économie de bande passante).
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout)

from ..config import AppConfig, Etape, Sequence
from .icons import icon


class EtapeDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None, etape: Etape | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle("Étape" if etape else "Nouvelle étape")
        self.setMinimumSize(380, 420)

        self._mode = QComboBox()
        self._mode.addItem("Grille (plusieurs caméras)", "grille")
        self._mode.addItem("Mono (une caméra plein cadre)", "mono")
        if etape and etape.mode == "mono":
            self._mode.setCurrentIndex(1)

        self._duree = QSpinBox()
        self._duree.setRange(3, 3600)
        self._duree.setSuffix(" s")
        self._duree.setValue(etape.duree_s if etape else 30)

        self._liste = QListWidget()
        deja = set(etape.cameras) if etape else set()
        for cam in cfg.cameras:
            it = QListWidgetItem(f"{cam.nom} — {cam.site.nom}")
            it.setData(Qt.UserRole, cam.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if cam.id in deja else Qt.Unchecked)
            self._liste.addItem(it)

        self._aide = QLabel()
        self._aide.setObjectName("hint")
        self._mode.currentIndexChanged.connect(self._maj_aide)
        self._maj_aide()

        form = QFormLayout()
        form.addRow("Mode :", self._mode)
        form.addRow("Durée :", self._duree)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(QLabel("Caméras affichées pendant l'étape :"))
        lay.addWidget(self._liste, 1)
        lay.addWidget(self._aide)
        lay.addWidget(boutons)

    def _maj_aide(self):
        self._aide.setText("Cochez UNE caméra (mode mono)."
                           if self._mode.currentData() == "mono"
                           else "Cochez les caméras de la grille (max 16).")

    def _cochees(self) -> list:
        return [self._liste.item(i).data(Qt.UserRole)
                for i in range(self._liste.count())
                if self._liste.item(i).checkState() == Qt.Checked]

    def _valider(self):
        n = len(self._cochees())
        if self._mode.currentData() == "mono" and n != 1:
            QMessageBox.warning(self, "Étape", "Le mode mono demande exactement "
                                               "une caméra cochée.")
            return
        if self._mode.currentData() == "grille" and not (1 <= n <= 16):
            QMessageBox.warning(self, "Étape", "Cochez entre 1 et 16 caméras.")
            return
        self.accept()

    def etape(self) -> Etape:
        return Etape(mode=self._mode.currentData(),
                     cameras=self._cochees(),
                     duree_s=self._duree.value())


class SequenceEditor(QDialog):
    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self.modifie = False
        self.setWindowTitle("Boucles")
        self.setMinimumSize(640, 440)

        self._seqs = QListWidget()
        self._seqs.currentRowChanged.connect(self._maj_etapes)
        btn_nouv = QPushButton(icon("plus"), " Nouvelle")
        btn_nouv.clicked.connect(self._nouvelle)
        btn_ren = QPushButton(icon("pencil"), " Renommer")
        btn_ren.clicked.connect(self._renommer)
        btn_sup = QPushButton(icon("trash"), " Supprimer")
        btn_sup.clicked.connect(self._supprimer)

        gauche = QVBoxLayout()
        gauche.addWidget(QLabel("Boucles :"))
        gauche.addWidget(self._seqs, 1)
        gauche.addWidget(btn_nouv)
        gauche.addWidget(btn_ren)
        gauche.addWidget(btn_sup)

        self._etapes = QListWidget()
        self._etapes.itemDoubleClicked.connect(lambda *_: self._modifier_etape())
        btn_e_add = QPushButton(icon("plus"), " Ajouter une étape…")
        btn_e_add.clicked.connect(self._ajouter_etape)
        btn_e_mod = QPushButton(icon("pencil"), " Modifier…")
        btn_e_mod.clicked.connect(self._modifier_etape)
        btn_e_del = QPushButton(icon("trash"), " Retirer")
        btn_e_del.clicked.connect(self._retirer_etape)
        btn_up = QPushButton(icon("arrow-up"), "")
        btn_up.setToolTip("Monter l'étape")
        btn_up.clicked.connect(lambda: self._deplacer(-1))
        btn_down = QPushButton(icon("arrow-down"), "")
        btn_down.setToolTip("Descendre l'étape")
        btn_down.clicked.connect(lambda: self._deplacer(+1))
        ligne = QHBoxLayout()
        for b in (btn_e_add, btn_e_mod, btn_e_del, btn_up, btn_down):
            ligne.addWidget(b)

        droite = QVBoxLayout()
        droite.addWidget(QLabel("Étapes (jouées en boucle) :"))
        droite.addWidget(self._etapes, 1)
        droite.addLayout(ligne)

        centre = QHBoxLayout()
        centre.addLayout(gauche, 1)
        centre.addLayout(droite, 2)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Enregistrer")
        boutons.accepted.connect(self.accept)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(centre, 1)
        lay.addWidget(boutons)

        self._maj_seqs()

    # ------------------------------------------------------------- séquences

    def _seq_courante(self) -> Sequence | None:
        i = self._seqs.currentRow()
        return self._cfg.sequences[i] if 0 <= i < len(self._cfg.sequences) else None

    def _maj_seqs(self, selection: int = 0):
        self._seqs.clear()
        for s in self._cfg.sequences:
            self._seqs.addItem(f"{s.nom}  ({len(s.etapes)} étape(s))")
        if self._cfg.sequences:
            self._seqs.setCurrentRow(min(selection, len(self._cfg.sequences) - 1))
        self._maj_etapes()

    def _nouvelle(self):
        if not self._cfg.cameras:
            QMessageBox.information(self, "Boucles",
                                    "Ajoutez d'abord des caméras (bouton Configuration).")
            return
        nom, ok = QInputDialog.getText(self, "Nouvelle boucle", "Nom :",
                                       text=f"Ronde {len(self._cfg.sequences) + 1}")
        if ok and nom.strip():
            self._cfg.sequences.append(Sequence(nom=nom.strip()))
            self.modifie = True
            self._maj_seqs(len(self._cfg.sequences) - 1)

    def _renommer(self):
        seq = self._seq_courante()
        if not seq:
            return
        nom, ok = QInputDialog.getText(self, "Renommer", "Nom :", text=seq.nom)
        if ok and nom.strip():
            seq.nom = nom.strip()
            self.modifie = True
            self._maj_seqs(self._seqs.currentRow())

    def _supprimer(self):
        seq = self._seq_courante()
        if not seq:
            return
        if QMessageBox.question(self, "Supprimer",
                                f"Supprimer la boucle « {seq.nom} » ?") == QMessageBox.Yes:
            self._cfg.sequences.remove(seq)
            self.modifie = True
            self._maj_seqs()

    # ----------------------------------------------------------------- étapes

    def _libelle(self, e: Etape) -> str:
        noms = []
        for cid in e.cameras:
            cam = self._cfg.camera(cid)
            noms.append(cam.nom if cam else cid)
        mode = "Mono" if e.mode == "mono" else f"Grille ×{len(e.cameras)}"
        return f"{mode} · {e.duree_s}s · {', '.join(noms)}"

    def _maj_etapes(self):
        self._etapes.clear()
        seq = self._seq_courante()
        if seq:
            for e in seq.etapes:
                self._etapes.addItem(self._libelle(e))

    def _ajouter_etape(self):
        seq = self._seq_courante()
        if not seq:
            self._nouvelle()
            seq = self._seq_courante()
            if not seq:
                return
        dlg = EtapeDialog(self._cfg, self)
        if dlg.exec():
            seq.etapes.append(dlg.etape())
            self.modifie = True
            self._maj_seqs(self._seqs.currentRow())
            self._etapes.setCurrentRow(self._etapes.count() - 1)

    def _modifier_etape(self):
        seq = self._seq_courante()
        i = self._etapes.currentRow()
        if not seq or not (0 <= i < len(seq.etapes)):
            return
        dlg = EtapeDialog(self._cfg, self, etape=seq.etapes[i])
        if dlg.exec():
            seq.etapes[i] = dlg.etape()
            self.modifie = True
            self._maj_seqs(self._seqs.currentRow())
            self._etapes.setCurrentRow(i)

    def _retirer_etape(self):
        seq = self._seq_courante()
        i = self._etapes.currentRow()
        if not seq or not (0 <= i < len(seq.etapes)):
            return
        del seq.etapes[i]
        self.modifie = True
        self._maj_seqs(self._seqs.currentRow())

    def _deplacer(self, delta: int):
        seq = self._seq_courante()
        i = self._etapes.currentRow()
        if not seq or not (0 <= i < len(seq.etapes)) or not (0 <= i + delta < len(seq.etapes)):
            return
        seq.etapes[i], seq.etapes[i + delta] = seq.etapes[i + delta], seq.etapes[i]
        self.modifie = True
        self._maj_seqs(self._seqs.currentRow())
        self._etapes.setCurrentRow(i + delta)
