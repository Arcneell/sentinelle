"""Sélecteur de caméras unifié : arbre par site, recherche, compteur.

Composant unique pour tout endroit où l'on choisit des caméras (étapes de
ronde, droits d'un compte...) : mêmes gestes partout. Deux modes :
  - multi  : cases à cocher, un site coché = tout le site (tristate) ;
  - single : une seule caméra à la fois (cocher décoche la précédente).
"""

import unicodedata

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..config import AppConfig


def _simplifier(texte: str) -> str:
    """Minuscules sans accents, pour une recherche tolérante."""
    s = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


class CameraPicker(QWidget):
    """Arbre sites > caméras avec recherche et cases à cocher."""

    selection_changee = Signal()

    def __init__(self, cfg: AppConfig, parent=None, single: bool = False):
        super().__init__(parent)
        self._single = single
        self._verrou = False              # évite la récursion dans itemChanged

        self._recherche = QLineEdit()
        self._recherche.setPlaceholderText("Rechercher une caméra ou un site…")
        self._recherche.setClearButtonEnabled(True)
        self._recherche.textChanged.connect(self._filtrer)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        self._tree.itemChanged.connect(self._coche_changee)

        self._compteur = QLabel("")
        self._compteur.setObjectName("hint")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self._recherche)
        lay.addWidget(self._tree, 1)
        lay.addWidget(self._compteur)

        self.peupler(cfg)

    # ------------------------------------------------------------ construction

    def peupler(self, cfg: AppConfig, cochees: set | None = None):
        cochees = cochees or set()
        self._verrou = True
        self._tree.clear()
        for site in cfg.sites:
            cams = [c for c in cfg.cameras if c.site.id == site.id]
            if not cams:
                continue
            lien = " · 4G" if site.lien == "4g" else ""
            si = QTreeWidgetItem([f"{site.nom}{lien}"])
            si.setData(0, Qt.UserRole + 1, site.id)
            if not self._single:
                si.setFlags(si.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                si.setCheckState(0, Qt.Unchecked)
            for cam in cams:
                extra = " · photo" if cam.profil == "eco-extreme" else (
                    " · éco" if cam.profil == "eco" else "")
                ci = QTreeWidgetItem([f"{cam.nom}{extra}"])
                ci.setData(0, Qt.UserRole, cam.id)
                ci.setFlags(ci.flags() | Qt.ItemIsUserCheckable)
                ci.setCheckState(0, Qt.Checked if cam.id in cochees else Qt.Unchecked)
                si.addChild(ci)
            self._tree.addTopLevelItem(si)
        self._tree.expandAll()
        self._verrou = False
        self._maj_compteur()

    def set_single(self, single: bool):
        """Bascule entre sélection multiple et caméra unique. En passant en
        mode unique, seule la première caméra cochée est conservée."""
        if single == self._single:
            return
        self._single = single
        self._verrou = True
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            if single:
                si.setFlags(si.flags() & ~(Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate))
                si.setData(0, Qt.CheckStateRole, None)      # retire la case du site
            else:
                si.setFlags(si.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                etats = {si.child(j).checkState(0) for j in range(si.childCount())}
                si.setCheckState(0, Qt.Checked if etats == {Qt.Checked}
                                 else (Qt.Unchecked if etats == {Qt.Unchecked}
                                       else Qt.PartiallyChecked))
        if single:
            premiers = self.ids()[:1]
            for it in self._chaque_camera():
                if it.checkState(0) == Qt.Checked \
                        and it.data(0, Qt.UserRole) not in premiers:
                    it.setCheckState(0, Qt.Unchecked)
        self._verrou = False
        self._maj_compteur()

    # -------------------------------------------------------------- sélection

    def _chaque_camera(self):
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            for j in range(si.childCount()):
                yield si.child(j)

    def ids(self) -> list[str]:
        """Ids des caméras cochées, dans l'ordre de l'arbre."""
        return [it.data(0, Qt.UserRole) for it in self._chaque_camera()
                if it.checkState(0) == Qt.Checked]

    def set_ids(self, ids):
        voulu = set(ids or [])
        self._verrou = True
        for it in self._chaque_camera():
            it.setCheckState(0, Qt.Checked if it.data(0, Qt.UserRole) in voulu
                             else Qt.Unchecked)
        self._verrou = False
        self._maj_compteur()

    # droits par site (écran des comptes) : un site entièrement coché vaut
    # « tout le site », y compris ses futures caméras

    def sites_ids(self) -> list[str]:
        """Ids des sites entièrement cochés."""
        return [self._tree.topLevelItem(i).data(0, Qt.UserRole + 1)
                for i in range(self._tree.topLevelItemCount())
                if self._tree.topLevelItem(i).checkState(0) == Qt.Checked]

    def ids_hors_sites(self) -> list[str]:
        """Ids des caméras cochées dont le site ne l'est pas entièrement."""
        entiers = set(self.sites_ids())
        ids = []
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            if si.data(0, Qt.UserRole + 1) in entiers:
                continue
            ids += [si.child(j).data(0, Qt.UserRole)
                    for j in range(si.childCount())
                    if si.child(j).checkState(0) == Qt.Checked]
        return ids

    def set_droits(self, sites, cameras):
        """Coche les sites entiers et les caméras isolées d'un compte."""
        sites, cameras = set(sites or []), set(cameras or [])
        self._verrou = True
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            site_entier = si.data(0, Qt.UserRole + 1) in sites
            for j in range(si.childCount()):
                ci = si.child(j)
                coche = site_entier or ci.data(0, Qt.UserRole) in cameras
                ci.setCheckState(0, Qt.Checked if coche else Qt.Unchecked)
        self._verrou = False
        self._maj_compteur()

    def _coche_changee(self, item, _col):
        if self._verrou:
            return
        if self._single and item.checkState(0) == Qt.Checked \
                and item.data(0, Qt.UserRole):
            # une seule caméra à la fois : décocher les autres
            self._verrou = True
            for it in self._chaque_camera():
                if it is not item:
                    it.setCheckState(0, Qt.Unchecked)
            self._verrou = False
        self._maj_compteur()
        self.selection_changee.emit()

    def _maj_compteur(self):
        from .texte import compte
        n = len(self.ids())
        if self._single:
            self._compteur.setText("Aucune caméra choisie" if n == 0
                                   else "1 caméra choisie")
        else:
            self._compteur.setText(
                "Aucune caméra sélectionnée" if n == 0
                else compte(n, "caméra sélectionnée", "caméras sélectionnées"))

    # -------------------------------------------------------------- recherche

    def _filtrer(self, texte: str):
        cle = _simplifier(texte.strip())
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            site_ok = cle in _simplifier(si.text(0))
            visibles = 0
            for j in range(si.childCount()):
                ci = si.child(j)
                cam_ok = not cle or site_ok or cle in _simplifier(ci.text(0))
                ci.setHidden(not cam_ok)
                visibles += 0 if ci.isHidden() else 1
            si.setHidden(bool(cle) and visibles == 0)
        self._tree.expandAll()
