"""Éditeur de rondes — 100 % dans l'interface.

Une ronde = suite d'étapes jouées en boucle. Chaque étape affiche soit une
grille de caméras choisies, soit une caméra en mono, pendant une durée donnée.
Les flux de l'étape précédente sont fermés avant d'ouvrir les suivants
(économie de bande passante).

En mode serveur, les rondes partagées (gérées par un administrateur) sont
affichées verrouillées : consultables et lisibles, mais modifiables uniquement
via une copie personnelle (bouton Dupliquer). Le panneau d'administration
réutilise StepsEditor pour éditer les rondes partagées elles-mêmes.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..config import AppConfig, Etape, Sequence
from .camera_picker import CameraPicker
from .icons import icon
from .texte import compte


def _duree_lisible(secondes: int) -> str:
    m, s = divmod(int(secondes), 60)
    if m and s:
        return f"{m} min {s:02d} s"
    return f"{m} min" if m else f"{s} s"


def dupliquer_sequence(seq: Sequence, nom: str | None = None) -> Sequence:
    """Copie profonde d'une ronde (sans attribution ni id : nouvelle ronde)."""
    return Sequence(
        nom=nom or f"{seq.nom} (copie)",
        etapes=[Etape(mode=e.mode, cameras=list(e.cameras), duree_s=e.duree_s)
                for e in seq.etapes])


class EtapeDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None, etape: Etape | None = None):
        super().__init__(parent)
        self.setWindowTitle("Étape" if etape else "Nouvelle étape")
        self.setMinimumSize(420, 520)

        self._mode = QComboBox()
        self._mode.addItem("Grille (plusieurs caméras)", "grille")
        self._mode.addItem("Mono (une caméra plein cadre)", "mono")
        mono = bool(etape and etape.mode == "mono")
        if mono:
            self._mode.setCurrentIndex(1)

        self._duree = QSpinBox()
        self._duree.setRange(3, 3600)
        self._duree.setSuffix(" s")
        self._duree.setValue(etape.duree_s if etape else 30)

        self._picker = CameraPicker(cfg, self, single=mono)
        if etape:
            self._picker.set_ids(etape.cameras)

        form = QFormLayout()
        form.addRow("Mode :", self._mode)
        form.addRow("Durée :", self._duree)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Valider")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(QLabel("Caméras affichées pendant l'étape :"))
        lay.addWidget(self._picker, 1)
        lay.addWidget(boutons)

        self._mode.currentIndexChanged.connect(
            lambda: self._picker.set_single(self._mode.currentData() == "mono"))

    def _valider(self):
        n = len(self._picker.ids())
        if self._mode.currentData() == "mono" and n != 1:
            QMessageBox.warning(self, "Étape",
                                "Le mode mono demande exactement une caméra cochée.")
            return
        if self._mode.currentData() == "grille" and not (1 <= n <= 16):
            QMessageBox.warning(self, "Étape", "Cochez entre 1 et 16 caméras.")
            return
        self.accept()

    def etape(self) -> Etape:
        return Etape(mode=self._mode.currentData(),
                     cameras=self._picker.ids(),
                     duree_s=self._duree.value())


class StepsEditor(QWidget):
    """Liste des étapes d'une ronde : ajout, édition, retrait, réordonnancement
    (glisser-déposer ou flèches). Opère sur la Sequence passée à set_sequence.
    Réutilisé par l'éditeur de rondes et le panneau d'administration."""

    modifie = Signal()

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._seq: Sequence | None = None
        self._verrou = False

        self._liste = QListWidget()
        self._liste.itemDoubleClicked.connect(lambda *_: self._modifier())
        self._liste.setDragDropMode(QAbstractItemView.InternalMove)
        self._liste.model().rowsMoved.connect(self._reordonnee)

        self._btn_add = QPushButton(icon("plus"), " Ajouter une étape…")
        self._btn_add.clicked.connect(self._ajouter)
        self._btn_mod = QPushButton(icon("pencil"), " Modifier…")
        self._btn_mod.clicked.connect(self._modifier)
        self._btn_del = QPushButton(icon("trash"), " Retirer")
        self._btn_del.clicked.connect(self._retirer)
        self._btn_up = QPushButton(icon("arrow-up"), "")
        self._btn_up.setToolTip("Monter l'étape")
        self._btn_up.clicked.connect(lambda: self._deplacer(-1))
        self._btn_down = QPushButton(icon("arrow-down"), "")
        self._btn_down.setToolTip("Descendre l'étape")
        self._btn_down.clicked.connect(lambda: self._deplacer(+1))
        ligne = QHBoxLayout()
        for b in (self._btn_add, self._btn_mod, self._btn_del,
                  self._btn_up, self._btn_down):
            ligne.addWidget(b)

        self._info = QLabel("")
        self._info.setObjectName("hint")
        self._info.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Étapes (jouées en boucle, glisser pour réordonner) :"))
        lay.addWidget(self._liste, 1)
        lay.addLayout(ligne)
        lay.addWidget(self._info)

        self.set_sequence(None)

    # ------------------------------------------------------------------ état

    def set_sequence(self, seq: Sequence | None, verrou: bool = False):
        self._seq = seq
        self._verrou = verrou
        self._rafraichir()

    def _rafraichir(self, selection: int | None = None):
        self._liste.blockSignals(True)
        self._liste.clear()
        if self._seq:
            for idx, e in enumerate(self._seq.etapes):
                it = QListWidgetItem(self._libelle(e))
                it.setData(Qt.UserRole, idx)
                self._liste.addItem(it)
        self._liste.blockSignals(False)
        if selection is not None and 0 <= selection < self._liste.count():
            self._liste.setCurrentRow(selection)

        actif = self._seq is not None and not self._verrou
        for b in (self._btn_add, self._btn_mod, self._btn_del,
                  self._btn_up, self._btn_down):
            b.setEnabled(actif)
        self._liste.setDragDropMode(
            QAbstractItemView.InternalMove if actif else QAbstractItemView.NoDragDrop)
        if self._verrou and self._seq is not None:
            self._info.setText("Ronde partagée gérée par l'administrateur : "
                               "dupliquez-la pour en faire une version personnelle "
                               "modifiable.")
        elif self._seq and self._seq.etapes:
            total = sum(e.duree_s for e in self._seq.etapes)
            self._info.setText(f"Cycle complet : {_duree_lisible(total)}")
        else:
            self._info.setText("")

    def _libelle(self, e: Etape) -> str:
        noms = []
        for cid in e.cameras:
            cam = self._cfg.camera(cid)
            noms.append(cam.nom if cam else cid)
        mode = "Mono" if e.mode == "mono" else f"Grille ×{len(e.cameras)}"
        return f"{mode} · {e.duree_s}s · {', '.join(noms)}"

    # ---------------------------------------------------------------- actions

    def _reordonnee(self, *_):
        """Resynchronise le modèle depuis l'ordre visuel après un dépôt."""
        if not self._seq:
            return
        ordre = [self._liste.item(i).data(Qt.UserRole)
                 for i in range(self._liste.count())]
        self._seq.etapes = [self._seq.etapes[i] for i in ordre]
        self._rafraichir(self._liste.currentRow())
        self.modifie.emit()

    def _ajouter(self):
        if not self._seq or self._verrou:
            return
        dlg = EtapeDialog(self._cfg, self)
        if dlg.exec():
            self._seq.etapes.append(dlg.etape())
            self._rafraichir(len(self._seq.etapes) - 1)
            self.modifie.emit()

    def _modifier(self):
        i = self._liste.currentRow()
        if not self._seq or self._verrou or not (0 <= i < len(self._seq.etapes)):
            return
        dlg = EtapeDialog(self._cfg, self, etape=self._seq.etapes[i])
        if dlg.exec():
            self._seq.etapes[i] = dlg.etape()
            self._rafraichir(i)
            self.modifie.emit()

    def _retirer(self):
        i = self._liste.currentRow()
        if not self._seq or self._verrou or not (0 <= i < len(self._seq.etapes)):
            return
        del self._seq.etapes[i]
        self._rafraichir(min(i, len(self._seq.etapes) - 1))
        self.modifie.emit()

    def _deplacer(self, delta: int):
        i = self._liste.currentRow()
        if not self._seq or self._verrou \
                or not (0 <= i < len(self._seq.etapes)) \
                or not (0 <= i + delta < len(self._seq.etapes)):
            return
        e = self._seq.etapes
        e[i], e[i + delta] = e[i + delta], e[i]
        self._rafraichir(i + delta)
        self.modifie.emit()


class SequenceEditor(QDialog):
    """Rondes du poste (mode autonome) ou du compte (mode serveur)."""

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self.modifie = False
        self.setWindowTitle("Rondes")
        self.setWindowIcon(icon("route"))
        self.setMinimumSize(760, 500)

        self._seqs = QListWidget()
        self._seqs.currentRowChanged.connect(self._selection_changee)
        btn_nouv = QPushButton(icon("plus"), " Nouvelle")
        btn_nouv.clicked.connect(self._nouvelle)
        self._btn_ren = QPushButton(icon("pencil"), " Renommer")
        self._btn_ren.clicked.connect(self._renommer)
        self._btn_dup = QPushButton(icon("copy"), " Dupliquer")
        self._btn_dup.setToolTip("Copier cette ronde en ronde personnelle modifiable")
        self._btn_dup.clicked.connect(self._dupliquer)
        self._btn_sup = QPushButton(icon("trash"), " Supprimer")
        self._btn_sup.clicked.connect(self._supprimer)

        gauche = QVBoxLayout()
        gauche.addWidget(QLabel("Rondes :"))
        gauche.addWidget(self._seqs, 1)
        gauche.addWidget(btn_nouv)
        gauche.addWidget(self._btn_ren)
        gauche.addWidget(self._btn_dup)
        gauche.addWidget(self._btn_sup)

        self._steps = StepsEditor(cfg, self)
        self._steps.modifie.connect(self._etapes_modifiees)

        centre = QHBoxLayout()
        centre.addLayout(gauche, 1)
        centre.addWidget(self._steps, 2)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Enregistrer")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
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
        self._seqs.blockSignals(True)
        self._seqs.clear()
        for s in self._cfg.sequences:
            it = QListWidgetItem(f"{s.nom}  ({compte(len(s.etapes), 'étape')})")
            if s.partagee:
                it.setIcon(icon("lock"))
                it.setToolTip("Ronde partagée, gérée par l'administrateur")
            self._seqs.addItem(it)
        self._seqs.blockSignals(False)
        if self._cfg.sequences:
            self._seqs.setCurrentRow(min(selection, len(self._cfg.sequences) - 1))
        self._selection_changee()

    def _selection_changee(self):
        seq = self._seq_courante()
        self._steps.set_sequence(seq, verrou=bool(seq and seq.partagee))
        self._btn_ren.setEnabled(seq is not None and not seq.partagee)
        self._btn_sup.setEnabled(seq is not None and not seq.partagee)
        self._btn_dup.setEnabled(seq is not None)

    def _etapes_modifiees(self):
        self.modifie = True
        i = self._seqs.currentRow()
        seq = self._seq_courante()
        if seq is not None:
            self._seqs.item(i).setText(f"{seq.nom}  ({compte(len(seq.etapes), 'étape')})")

    def _nouvelle(self):
        if not self._cfg.cameras:
            QMessageBox.information(self, "Rondes",
                                    "Ajoutez d'abord des caméras (bouton Configuration).")
            return
        nom, ok = QInputDialog.getText(self, "Nouvelle ronde", "Nom :",
                                       text=f"Ronde {len(self._cfg.sequences) + 1}")
        if ok and nom.strip():
            self._cfg.sequences.append(Sequence(nom=nom.strip()))
            self.modifie = True
            self._maj_seqs(len(self._cfg.sequences) - 1)

    def _renommer(self):
        seq = self._seq_courante()
        if not seq or seq.partagee:
            return
        nom, ok = QInputDialog.getText(self, "Renommer", "Nom :", text=seq.nom)
        if ok and nom.strip():
            seq.nom = nom.strip()
            self.modifie = True
            self._maj_seqs(self._seqs.currentRow())

    def _dupliquer(self):
        seq = self._seq_courante()
        if not seq:
            return
        self._cfg.sequences.append(dupliquer_sequence(seq))
        self.modifie = True
        self._maj_seqs(len(self._cfg.sequences) - 1)

    def _supprimer(self):
        seq = self._seq_courante()
        if not seq or seq.partagee:
            return
        if QMessageBox.question(self, "Supprimer",
                                f"Supprimer la ronde « {seq.nom} » ?") == QMessageBox.Yes:
            self._cfg.sequences.remove(seq)
            self.modifie = True
            self._maj_seqs()
