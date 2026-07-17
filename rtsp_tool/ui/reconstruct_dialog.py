"""Reconstruction IA d'une image de caméra : orchestration + visionneuse.

Point d'entrée : reconstruire_image(parent, camera, src_png)
  - propose le téléchargement du moteur s'il est absent (~45 Mo, une fois) ;
  - lance la reconstruction en arrière-plan (5-20 s selon GPU) ;
  - affiche le résultat côte à côte avec l'original, avec l'avertissement
    « détails inventés par l'IA » ;
  - le résultat est enregistré dans Images/Sentinelle.
"""

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QMessageBox, QProgressDialog, QVBoxLayout)

from .. import reconstruct

AVERTISSEMENT = (
    "Les détails de l'image reconstruite sont des estimations du modèle : "
    "ils ne permettent pas d'identifier une plaque ou un visage.")


class _Travail(QObject):
    fini = Signal(bool, str)        # ok, chemin_ou_erreur


class ResultatDialog(QDialog):
    def __init__(self, parent, titre: str, src_png: str, dst_png: str):
        super().__init__(parent)
        self.setWindowTitle(f"Reconstruction : {titre}")

        avant = QPixmap(src_png)
        apres = QPixmap(dst_png)
        h = 460
        lbl_avant, lbl_apres = QLabel(), QLabel()
        lbl_avant.setPixmap(avant.scaledToHeight(h, Qt.SmoothTransformation))
        lbl_apres.setPixmap(apres.scaledToHeight(h, Qt.SmoothTransformation))
        cap_avant, cap_apres = QLabel("Original"), QLabel("Reconstruite")
        for c in (cap_avant, cap_apres):
            c.setAlignment(Qt.AlignCenter)
            c.setStyleSheet("font-weight: bold;")

        col1 = QVBoxLayout(); col1.addWidget(cap_avant); col1.addWidget(lbl_avant)
        col2 = QVBoxLayout(); col2.addWidget(cap_apres); col2.addWidget(lbl_apres)
        cote = QHBoxLayout(); cote.addLayout(col1); cote.addLayout(col2)

        note = QLabel(AVERTISSEMENT + f"\n\nEnregistrée : {dst_png}")
        note.setWordWrap(True)
        note.setStyleSheet("color: #b0b0b0;")

        boutons = QDialogButtonBox(QDialogButtonBox.Close)
        btn_dossier = boutons.addButton("Ouvrir le dossier",
                                        QDialogButtonBox.ActionRole)
        btn_dossier.clicked.connect(
            lambda: _ouvrir_dossier(Path(dst_png).parent))
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(cote)
        lay.addWidget(note)
        lay.addWidget(boutons)


def _ouvrir_dossier(dossier: Path):
    import subprocess
    import sys
    if sys.platform == "win32":
        subprocess.Popen(["explorer", str(dossier)])
    else:
        subprocess.Popen(["xdg-open", str(dossier)])


def reconstruire_image(parent, camera, src_png: str):
    """Orchestration complète (téléchargement éventuel + reconstruction + vue)."""
    if not reconstruct.disponible():
        if QMessageBox.question(
                parent, "Reconstruction",
                "Le moteur de reconstruction (45 Mo) doit être téléchargé "
                "une première fois. Continuer ?"
        ) != QMessageBox.Yes:
            return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = str(Path.home() / "Pictures" / "Sentinelle"
              / f"{camera.id}-{stamp}-reconstruite.png")

    prog = QProgressDialog("Reconstruction en cours…", None, 0, 0, parent)
    prog.setWindowTitle("Reconstruction")
    prog.setWindowModality(Qt.WindowModal)
    prog.setMinimumDuration(0)
    prog.setCancelButton(None)

    travail = _Travail(parent)

    def routine():
        if not reconstruct.disponible():
            ok, msg = reconstruct.telecharger()
            if not ok:
                try:
                    travail.fini.emit(False, f"téléchargement du moteur : {msg}")
                except RuntimeError:
                    pass
                return
        ok, msg = reconstruct.reconstruire(src_png, dst)
        try:
            travail.fini.emit(ok, msg)
        except RuntimeError:
            pass

    def sur_fini(ok: bool, msg: str):
        prog.close()
        if not ok:
            QMessageBox.warning(parent, "Reconstruction",
                                f"Échec de la reconstruction : {msg}")
            return
        ResultatDialog(parent, camera.nom, src_png, msg).exec()

    travail.fini.connect(sur_fini)
    threading.Thread(target=routine, daemon=True, name="reconstruct-ia").start()
    prog.show()
