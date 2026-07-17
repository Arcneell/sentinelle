"""Fenêtre principale : grille / mono, rotation, boucles — tout se configure
dans l'interface (⚙), aucun fichier à éditer.

Règles bande passante appliquées ici :
  - seules les caméras affichées ont un flux ouvert ;
  - tout changement de vue (mono, rotation, étape de séquence) FERME les flux
    précédents avant d'ouvrir les suivants ;
  - profil eco-extreme : en grille la tuile est en mode photo (aucun flux).
"""

import logging
import math

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QStackedWidget,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..config import (AppConfig, load_config, purger_cameras_sequences,
                      save_config)
from .config_dialogs import CameraDialog, ConfigDialog, DvrDialog, SiteDialog
from .icons import app_icon, icon
from .photo_tile import PhotoTile
from .sequence_editor import SequenceEditor
from .tile import TileState, VideoTile, format_debit

logger = logging.getLogger(__name__)

MAX_TILES = 16
CAP_CHOICES = [("Auto (16 max)", 16), ("1×1", 1), ("2×2", 4), ("3×3", 9), ("4×4", 16)]


class MainWindow(QMainWindow):

    def __init__(self, config_path: str):
        super().__init__()
        self._cfg = AppConfig(path=config_path)
        self._tiles: dict[str, QWidget] = {}       # camera_id -> tuile (vidéo ou photo)
        self._mono_tile: VideoTile | None = None
        self._grid_dirty = False
        self._page = 0
        self._seq = None                            # séquence en cours de lecture
        self._seq_idx = -1
        self._motion = None                         # MotionMonitor (ONVIF)
        self._motion_ids = set()                    # caméras actuellement en mouvement
        self._icon_widgets = []                     # (widget, nom_icone) à recolorer
        self._settings = QSettings("Sentinelle", "viewer")

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1320, 820)
        self._build_ui()
        self._load_config(initial=True)

    # ------------------------------------------------------------------- UI

    # ------------------------------------------------------------------- UI
    #
    # Structure : une barre de titre applicative (marque + commandes), puis un
    # espace de travail scindé — panneau des caméras à gauche, mur d'images à
    # droite. Pas de barre d'outils Qt ni de dock : tout est intégré et plat.

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        vroot = QVBoxLayout(root)
        vroot.setContentsMargins(0, 0, 0, 0)
        vroot.setSpacing(0)
        vroot.addWidget(self._build_topbar())

        self._split = QSplitter(Qt.Horizontal)
        self._split.setObjectName("workspace")
        self._split.setHandleWidth(1)
        self._split.addWidget(self._build_sidebar())
        self._split.addWidget(self._build_stage())
        self._split.setStretchFactor(0, 0)
        self._split.setStretchFactor(1, 1)
        self._split.setSizes([260, 1060])
        vroot.addWidget(self._split, 1)
        self.setCentralWidget(root)

        self._apply_theme_chrome()

        # --- barre d'état ---
        self._status_streams = QLabel()
        self.statusBar().addPermanentWidget(self._status_streams)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

        self._init_timers()

    # ------------------------------------------------------------ barre de titre

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(56)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(8)

        marque = QLabel(APP_NAME)
        marque.setObjectName("brand")
        lay.addWidget(marque)
        lay.addWidget(self._sep())

        # retour grille (actif seulement en vue plein cadre)
        self._act_grid = self._tbtn("grid", "Grille", "Revenir à la grille (Ctrl+G)",
                                    self._retour_manuel_grille)
        self._act_grid.setEnabled(False)
        lay.addWidget(self._act_grid)

        self._cap_combo = QComboBox()
        for label, _cap in CAP_CHOICES:
            self._cap_combo.addItem(label)
        self._cap_combo.setToolTip("Nombre de caméras affichées par page")
        self._cap_combo.currentIndexChanged.connect(self._selection_changee)
        lay.addWidget(self._cap_combo)
        lay.addWidget(self._sep())

        # rotation
        self._act_rotation = self._tbtn("rotate", "Rotation",
                                        "Faire défiler automatiquement les caméras",
                                        self._rotation_basculee, checkable=True)
        lay.addWidget(self._act_rotation)
        self._rot_spin = QSpinBox()
        self._rot_spin.setRange(3, 3600)
        self._rot_spin.setSuffix(" s")
        self._rot_spin.setToolTip("Délai avant de passer à la suite")
        self._rot_spin.valueChanged.connect(self._rotation_duree_changee)
        lay.addWidget(self._rot_spin)
        lay.addWidget(self._sep())

        # boucles
        self._seq_combo = QComboBox()
        self._seq_combo.setMinimumWidth(150)
        self._seq_combo.setToolTip("Boucle à lire")
        lay.addWidget(self._seq_combo)
        self._act_seq = self._tbtn("play", "Lire",
                                   "Lire ou arrêter la boucle sélectionnée",
                                   self._seq_basculee, checkable=True)
        lay.addWidget(self._act_seq)
        self._btn_seq_edit = self._tbtn("pencil", "", "Créer et modifier les boucles",
                                        self._editer_sequences)
        lay.addWidget(self._btn_seq_edit)

        # détection de mouvement (masquée si aucune caméra ONVIF)
        self._motion_box = QWidget()
        mlay = QHBoxLayout(self._motion_box)
        mlay.setContentsMargins(0, 0, 0, 0)
        mlay.setSpacing(8)
        mlay.addWidget(self._sep())
        self._act_motion = self._tbtn("motion", "Mouvement",
                                      "Surligner les caméras qui détectent un mouvement (ONVIF)",
                                      self._motion_basculee, checkable=True)
        mlay.addWidget(self._act_motion)
        self._act_motion_auto = self._tbtn("grid", "Vue mouvement",
                                           "N'afficher que les caméras en mouvement",
                                           self._motion_auto_basculee, checkable=True)
        mlay.addWidget(self._act_motion_auto)
        lay.addWidget(self._motion_box)

        lay.addStretch(1)

        # commandes générales, alignées à droite
        self._act_pause = self._tbtn("pause", "Tout arrêter",
                                     "Fermer tous les flux sans perdre la sélection",
                                     self._pause_basculee, checkable=True)
        lay.addWidget(self._act_pause)

        self._btn_full = QToolButton()
        self._btn_full.setIcon(icon("maximize"))
        self._btn_full.setText("Plein écran")
        self._btn_full.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._btn_full.setPopupMode(QToolButton.MenuButtonPopup)
        self._btn_full.setToolTip("Plein écran (F11) — le menu permet de choisir l'écran")
        self._btn_full.clicked.connect(self._toggle_fullscreen)
        menu_full = QMenu(self._btn_full)
        menu_full.aboutToShow.connect(lambda: self._peupler_menu_ecrans(menu_full))
        self._btn_full.setMenu(menu_full)
        self._icon_widgets.append((self._btn_full, "maximize"))
        lay.addWidget(self._btn_full)

        self._btn_cfg = self._tbtn("settings", "Configuration",
                                   "Sites, caméras et réglages (Ctrl+,)",
                                   self._ouvrir_configuration)
        lay.addWidget(self._btn_cfg)

        # raccourcis clavier (indépendants des boutons)
        for seq, slot in (("Ctrl+G", self._retour_manuel_grille),
                          ("F11", self._toggle_fullscreen),
                          ("Ctrl+,", self._ouvrir_configuration)):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(slot)
            self.addAction(a)

        self._topbar = bar
        return bar

    # --------------------------------------------------------------- panneau caméras

    def _build_sidebar(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sidebar")
        panel.setMinimumWidth(210)
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        entete = QFrame()
        entete.setObjectName("sideHeader")
        eh = QHBoxLayout(entete)
        eh.setContentsMargins(14, 12, 14, 12)
        titre = QLabel("CAMÉRAS")
        titre.setObjectName("sideTitle")
        self._side_count = QLabel("")
        self._side_count.setObjectName("sideCount")
        eh.addWidget(titre)
        eh.addStretch(1)
        eh.addWidget(self._side_count)
        v.addWidget(entete)

        self._tree = QTreeWidget()
        self._tree.setObjectName("cameraTree")
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(14)
        self._tree.itemChanged.connect(self._coche_changee)
        self._tree.itemDoubleClicked.connect(self._arbre_double_clic)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._menu_arbre)
        v.addWidget(self._tree, 1)

        pied = QFrame()
        pied.setObjectName("sideFooter")
        ph = QVBoxLayout(pied)
        ph.setContentsMargins(10, 10, 10, 10)
        self._btn_add = QPushButton(icon("plus"), " Ajouter une caméra")
        self._btn_add.setObjectName("addBtn")
        self._btn_add.clicked.connect(self._ouvrir_configuration)
        self._icon_widgets.append((self._btn_add, "plus"))
        ph.addWidget(self._btn_add)
        v.addWidget(pied)

        self._sidebar = panel
        return panel

    # ------------------------------------------------------------------ mur d'images

    def _build_stage(self) -> QWidget:
        self._grid_page = QWidget()
        self._grid_layout = QGridLayout(self._grid_page)
        self._grid_layout.setContentsMargins(3, 3, 3, 3)
        self._grid_layout.setSpacing(3)

        self._placeholder = QLabel()
        self._placeholder.setAlignment(Qt.AlignCenter)

        self._mono_page = QWidget()
        self._mono_layout = QVBoxLayout(self._mono_page)
        self._mono_layout.setContentsMargins(3, 3, 3, 3)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._grid_page)
        self._stack.addWidget(self._mono_page)
        return self._stack

    def _init_timers(self):

        # regroupe les changements de coches (cocher un site = N items)
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(250)
        self._rebuild_timer.timeout.connect(self._selection_appliquee)

        # rotation / séquence
        self._rotation_timer = QTimer(self)
        self._rotation_timer.timeout.connect(self._rotation_tick)
        self._seq_timer = QTimer(self)
        self._seq_timer.setSingleShot(True)
        self._seq_timer.timeout.connect(self._seq_avancer)

        # enregistrement différé de la config (durée de rotation…)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1200)
        self._save_timer.timeout.connect(lambda: save_config(self._cfg))

    def _tbtn(self, nom_icone: str, texte: str, tooltip: str, slot,
              checkable: bool = False) -> QToolButton:
        """Bouton plat de la barre de titre (icône + texte)."""
        b = QToolButton()
        b.setIcon(icon(nom_icone))
        if texte:
            b.setText(texte)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        else:
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setToolTip(tooltip)
        b.setCheckable(checkable)
        b.setIconSize(QSize(18, 18))
        if checkable:
            b.toggled.connect(slot)
        else:
            b.clicked.connect(lambda _=False: slot())
        self._icon_widgets.append((b, nom_icone))
        return b

    def _sep(self) -> QFrame:
        s = QFrame()
        s.setObjectName("vsep")
        s.setFrameShape(QFrame.VLine)
        s.setFixedWidth(1)
        return s

    def _apply_theme_chrome(self):
        """Applique les couleurs du thème aux zones qui entourent la vidéo."""
        from .theme import t
        fond = f"background-color: {t('video_bg')};"
        self._grid_page.setStyleSheet(fond)
        self._mono_page.setStyleSheet(fond)
        self._placeholder.setStyleSheet(f"color: {t('text_dim')}; font-size: 14px;")

    def _all_tiles(self) -> list:
        if self._mono_tile is not None:
            return [self._mono_tile]
        return list(self._tiles.values())

    def _restyle_all(self):
        """Réapplique le thème courant à l'habillage et aux tuiles (sans couper
        les flux). Appelé après un changement de thème dans la configuration."""
        from .theme import t
        self._apply_theme_chrome()
        for widget, nom in self._icon_widgets:
            if nom == "play" and widget is self._act_seq:
                nom = "stop" if widget.isChecked() else "play"
            elif nom == "pause" and widget is self._act_pause:
                nom = "play" if widget.isChecked() else "pause"
            widget.setIcon(icon(nom, t("text")))
        for tile in self._all_tiles():
            if hasattr(tile, "restyle"):
                tile.restyle()

    # ----------------------------------------------------------- configuration

    def _load_config(self, initial: bool = False):
        try:
            cfg = load_config(self._cfg.path)
        except Exception as e:
            QMessageBox.critical(self, "Configuration", str(e))
            return

        self._seq_stop()
        self._leave_mono()
        self._vider_grille()
        self._cfg = cfg

        self._rot_spin.blockSignals(True)
        self._rot_spin.setValue(cfg.rotation_duree_s)
        self._rot_spin.blockSignals(False)
        self._rotation_timer.setInterval(cfg.rotation_duree_s * 1000)

        self._peupler_arbre()
        self._peupler_sequences()
        self._maj_visibilite_mouvement()
        self._selection_appliquee()

        if cfg.warnings:
            QMessageBox.warning(
                self, "Configuration — avertissements",
                "\n".join(cfg.warnings[:20]) + ("\n…" if len(cfg.warnings) > 20 else ""))

        if initial and not cfg.cameras:
            # premier lancement : on ouvre directement la configuration
            QTimer.singleShot(200, self._ouvrir_configuration)

    def _ouvrir_configuration(self):
        etait_en_seq = self._seq is not None
        self._seq_stop()
        dlg = ConfigDialog(self._cfg, self)
        accepte = dlg.exec()
        if accepte and dlg.modifie:
            save_config(self._cfg)
            self._load_config()
        elif not accepte and dlg.modifie:
            self._load_config()          # annulé → on repart du fichier
        elif etait_en_seq:
            self._update_status()
        # le thème a pu changer : réaligner l'habillage et les tuiles existantes
        self._restyle_all()

    def _editer_sequences(self):
        self._seq_stop()
        dlg = SequenceEditor(self._cfg, self)
        accepte = dlg.exec()
        if accepte and dlg.modifie:
            save_config(self._cfg)
            self._peupler_sequences()
        elif not accepte and dlg.modifie:
            self._load_config()

    def _peupler_arbre(self):
        cochees = set(self._settings.value("cameras_cochees", [], type=list))
        self._tree.blockSignals(True)
        self._tree.clear()
        for site in self._cfg.sites:
            cams = [c for c in self._cfg.cameras if c.site.id == site.id]
            if not cams:
                continue
            lien = " · 4G" if site.lien == "4g" else ""
            site_item = QTreeWidgetItem([f"{site.nom}{lien}"])
            site_item.setData(0, Qt.UserRole + 1, site.id)
            site_item.setFlags(site_item.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            for cam in cams:
                extra = " · photo" if cam.profil == "eco-extreme" else (
                    " · éco" if cam.profil == "eco" else "")
                item = QTreeWidgetItem([f"{cam.nom}{extra}"])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setData(0, Qt.UserRole, cam.id)
                item.setCheckState(0, Qt.Checked if cam.id in cochees else Qt.Unchecked)
                site_item.addChild(item)
            self._tree.addTopLevelItem(site_item)
        self._tree.expandAll()
        self._tree.blockSignals(False)
        n = len(self._cfg.cameras)
        self._side_count.setText(str(n) if n else "")

    def _a_camera_onvif(self) -> bool:
        return any(c.marque == "onvif" for c in self._cfg.cameras)

    def _maj_visibilite_mouvement(self):
        """La détection de mouvement passe par ONVIF : on n'affiche ses commandes
        que si au moins une caméra ONVIF est configurée."""
        visible = self._a_camera_onvif()
        self._motion_box.setVisible(visible)
        if not visible:
            if self._act_motion_auto.isChecked():
                self._act_motion_auto.setChecked(False)
            if self._act_motion.isChecked():
                self._act_motion.setChecked(False)

    def _peupler_sequences(self):
        self._seq_combo.clear()
        for s in self._cfg.sequences:
            self._seq_combo.addItem(s.nom)
        actif = bool(self._cfg.sequences)
        self._seq_combo.setEnabled(actif)
        self._act_seq.setEnabled(actif)

    # -------------------------------------------------------------- sélection

    def _cameras_cochees(self) -> list[str]:
        ids = []
        for i in range(self._tree.topLevelItemCount()):
            site_item = self._tree.topLevelItem(i)
            for j in range(site_item.childCount()):
                item = site_item.child(j)
                if item.checkState(0) == Qt.Checked:
                    ids.append(item.data(0, Qt.UserRole))
        return ids

    def _coche_changee(self, _item, _col):
        self._settings.setValue("cameras_cochees", self._cameras_cochees())
        self._seq_stop()
        self._page = 0
        self._rebuild_timer.start()

    def _selection_changee(self):
        self._page = 0
        self._selection_appliquee()

    def _cap(self) -> int:
        return CAP_CHOICES[self._cap_combo.currentIndex()][1]

    def _pages(self) -> list[list[str]]:
        # « Vue mouvement » : la grille suit les caméras en mouvement
        if self._act_motion_auto.isChecked():
            ids = [c.id for c in self._cfg.cameras if c.id in self._motion_ids]
        else:
            ids = self._cameras_cochees()
        cap = self._cap()
        if not ids:
            return []
        return [ids[i:i + cap] for i in range(0, len(ids), cap)]

    def _selection_appliquee(self):
        """Affiche la page courante de la sélection (hors lecture de séquence)."""
        if self._seq is not None:
            return
        if self._stack.currentWidget() is self._mono_page:
            self._grid_dirty = True
            return
        pages = self._pages()
        if not pages:
            self._set_grid([])
            return
        self._page %= len(pages)
        self._set_grid(pages[self._page])
        if len(pages) > 1:
            self.statusBar().showMessage(
                f"Page {self._page + 1}/{len(pages)}", 4000)

    # ----------------------------------------------------------------- grille

    def _make_tile(self, cam, vue: str):
        # profil « photo » en grille : image HTTP périodique (sites 4G contraints)
        if vue == "grille" and cam.profil == "eco-extreme":
            tile = PhotoTile(cam, vue)
        else:
            tile = VideoTile(cam, vue)
        tile.double_clicked.connect(self._tuile_double_clic)
        tile.state_changed.connect(self._update_status)
        tile.snapshot_saved.connect(
            lambda p: self.statusBar().showMessage(f"Image enregistrée : {p}", 6000))
        if cam.id in self._motion_ids and hasattr(tile, "set_motion"):
            tile.set_motion(True)
        return tile

    def _vider_grille(self):
        for tile in self._tiles.values():
            tile.shutdown()
            self._grid_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()

    def _set_grid(self, ids: list[str]):
        """Affiche exactement ces caméras dans la grille (diff incrémental :
        les tuiles conservées gardent leur flux ouvert)."""
        ids = [i for i in ids if self._cfg.camera(i)][:MAX_TILES]
        paused = self._act_pause.isChecked()

        # la grille reste TOUJOURS en substream (même à une seule caméra) :
        # le flux principal n'est ouvert qu'en vue mono (double-clic)
        vue = "grille"

        for cam_id in list(self._tiles):
            if cam_id not in ids:
                tile = self._tiles.pop(cam_id)
                tile.shutdown()
                self._grid_layout.removeWidget(tile)
                tile.setParent(None)
                tile.deleteLater()

        for cam_id in ids:
            if cam_id not in self._tiles:
                tile = self._make_tile(self._cfg.camera(cam_id), vue)
                self._tiles[cam_id] = tile
                if not paused:
                    tile.start()

        for tile in self._tiles.values():
            self._grid_layout.removeWidget(tile)
        self._grid_layout.removeWidget(self._placeholder)
        self._placeholder.setParent(None)

        # remise à zéro des étirements : sinon les colonnes/lignes d'une grille
        # plus grande restent réservées et la grille ne rétrécit pas
        for i in range(MAX_TILES + 1):
            self._grid_layout.setColumnStretch(i, 0)
            self._grid_layout.setRowStretch(i, 0)

        ordered = [self._tiles[cid] for cid in ids if cid in self._tiles]
        if not ordered:
            if self._act_motion_auto.isChecked():
                txt = "Vue mouvement active — en attente d'activité sur une caméra…"
            elif not self._cfg.cameras:
                txt = ("Aucune caméra configurée.\n"
                       "Ouvrez la Configuration pour ajouter vos sites et vos DVR.")
            else:
                txt = ("Sélectionnez des caméras dans le panneau de gauche.\n"
                       "Double-clic : plein écran   ·   Clic droit : options")
            self._placeholder.setText(txt)
            self._grid_layout.addWidget(self._placeholder, 0, 0)
        else:
            cols = math.ceil(math.sqrt(len(ordered)))
            for idx, tile in enumerate(ordered):
                self._grid_layout.addWidget(tile, idx // cols, idx % cols)
            for c in range(cols):
                self._grid_layout.setColumnStretch(c, 1)
            for r in range(math.ceil(len(ordered) / cols)):
                self._grid_layout.setRowStretch(r, 1)
        self._grid_dirty = False
        self._update_status()

    # ------------------------------------------------------------------- mono

    def _set_mono(self, cam_id: str):
        cam = self._cfg.camera(cam_id)
        if cam is None:
            return
        # économie de bande passante : fermer la grille AVANT d'ouvrir le mono
        for tile in self._tiles.values():
            tile.stop()
        self._leave_mono()

        self._mono_tile = self._make_tile(cam, "mono")
        self._mono_layout.addWidget(self._mono_tile)
        self._stack.setCurrentWidget(self._mono_page)
        self._act_grid.setEnabled(True)
        if not self._act_pause.isChecked():
            self._mono_tile.start()
        self._update_status()

    def _leave_mono(self):
        if self._mono_tile is not None:
            self._mono_tile.shutdown()
            self._mono_layout.removeWidget(self._mono_tile)
            self._mono_tile.setParent(None)
            self._mono_tile.deleteLater()
            self._mono_tile = None

    def _go_grid(self):
        self._leave_mono()
        self._stack.setCurrentWidget(self._grid_page)
        self._act_grid.setEnabled(False)
        if self._grid_dirty:
            self._selection_appliquee()
        elif not self._act_pause.isChecked():
            for tile in self._tiles.values():
                tile.start()
        self._update_status()

    def _retour_manuel_grille(self):
        self._seq_stop()
        self._go_grid()

    def _tuile_double_clic(self, cam_id: str):
        self._seq_stop()
        if self._stack.currentWidget() is self._mono_page:
            self._go_grid()
        else:
            self._set_mono(cam_id)

    def _arbre_double_clic(self, item, _col):
        cam_id = item.data(0, Qt.UserRole)
        if cam_id:
            self._seq_stop()
            self._set_mono(cam_id)

    # ------------------------------------------- édition à chaud (clic droit)

    def _menu_arbre(self, pos):
        """La configuration se modifie à tout moment, directement depuis l'arbre."""
        item = self._tree.itemAt(pos)
        cam_id = item.data(0, Qt.UserRole) if item else None
        site_id = item.data(0, Qt.UserRole + 1) if item else None

        menu = QMenu(self)
        if cam_id:
            cam = self._cfg.camera(cam_id)
            if cam:
                menu.addAction(icon("pencil"), f"Modifier « {cam.nom} »…",
                               lambda: self._modifier_camera(cam_id))
                menu.addAction(icon("trash"), "Supprimer cette caméra",
                               lambda: self._supprimer_camera(cam_id))
                menu.addSeparator()
                site_id = cam.site.id
        if site_id:
            site = self._cfg.site(site_id)
            if site:
                menu.addAction(icon("plus"), f"Ajouter un DVR sur « {site.nom} »…",
                               lambda: self._ajouter_dvr_rapide(site_id))
                menu.addAction(icon("pencil"), f"Modifier le site « {site.nom} »…",
                               lambda: self._modifier_site(site_id))
                menu.addSeparator()
        menu.addAction(icon("plus"), "Ajouter un DVR…", self._ajouter_dvr_rapide)
        menu.addAction(icon("settings"), "Configuration complète…",
                       self._ouvrir_configuration)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _appliquer_et_sauver(self):
        save_config(self._cfg)
        self._load_config()

    def _modifier_camera(self, cam_id: str):
        cam = self._cfg.camera(cam_id)
        if cam is None:
            return
        self._seq_stop()
        dlg = CameraDialog(self._cfg, self, camera=cam)
        if dlg.exec():
            dlg.appliquer()
            self._appliquer_et_sauver()

    def _supprimer_camera(self, cam_id: str):
        cam = self._cfg.camera(cam_id)
        if cam is None:
            return
        if QMessageBox.question(self, "Supprimer",
                                f"Supprimer la caméra « {cam.nom} » ?") != QMessageBox.Yes:
            return
        self._cfg.cameras = [c for c in self._cfg.cameras if c.id != cam_id]
        purger_cameras_sequences(self._cfg, {cam_id})
        self._appliquer_et_sauver()

    def _modifier_site(self, site_id: str):
        site = self._cfg.site(site_id)
        if site is None:
            return
        dlg = SiteDialog(self, site)
        if dlg.exec():
            site.nom, site.lien = dlg.valeurs()
            self._appliquer_et_sauver()

    def _ajouter_dvr_rapide(self, site_id: str | None = None):
        self._seq_stop()
        if not self._cfg.sites:
            self._ouvrir_configuration()
            return
        dlg = DvrDialog(self._cfg, self,
                        site_defaut=self._cfg.site(site_id) if site_id else None)
        if dlg.exec():
            self._appliquer_et_sauver()

    # --------------------------------------------------------------- rotation

    def _rotation_basculee(self, active: bool):
        if active:
            self._seq_stop()
            self._rotation_timer.setInterval(self._cfg.rotation_duree_s * 1000)
            self._rotation_timer.start()
        else:
            self._rotation_timer.stop()

    def _rotation_duree_changee(self, val: int):
        self._cfg.rotation_duree_s = val
        self._rotation_timer.setInterval(val * 1000)
        self._save_timer.start()          # enregistrement différé

    def _rotation_tick(self):
        if self._seq is not None or self._act_pause.isChecked():
            return
        if self._stack.currentWidget() is self._mono_page:
            # mono : caméra suivante parmi les cochées
            ids = self._cameras_cochees()
            if not ids or self._mono_tile is None:
                return
            try:
                i = ids.index(self._mono_tile.camera.id)
            except ValueError:
                i = -1
            self._set_mono(ids[(i + 1) % len(ids)])
        else:
            pages = self._pages()
            if len(pages) < 2:
                return
            self._page = (self._page + 1) % len(pages)
            self._set_grid(pages[self._page])
            self.statusBar().showMessage(f"Rotation — page {self._page + 1}/{len(pages)}", 3000)

    # -------------------------------------------------------------- séquences

    def _seq_basculee(self, active: bool):
        self._act_seq.setIcon(icon("stop") if active else icon("play"))
        self._act_seq.setText("Arrêter" if active else "Lire")
        if active:
            i = self._seq_combo.currentIndex()
            if not (0 <= i < len(self._cfg.sequences)):
                self._act_seq.setChecked(False)
                return
            self._act_rotation.setChecked(False)
            self._seq = self._cfg.sequences[i]
            self._seq_idx = -1
            self._seq_avancer()
        else:
            self._seq_stop()

    def _seq_stop(self):
        if self._seq is None and not self._act_seq.isChecked():
            return
        self._seq = None
        self._seq_idx = -1
        self._seq_timer.stop()
        if self._act_seq.isChecked():
            self._act_seq.blockSignals(True)
            self._act_seq.setChecked(False)
            self._act_seq.blockSignals(False)
        self._act_seq.setIcon(icon("play"))
        self._act_seq.setText("Lire")
        # retour à l'affichage piloté par la sélection
        if self._stack.currentWidget() is self._mono_page:
            self._go_grid()
        else:
            self._selection_appliquee()

    def _seq_avancer(self):
        if self._seq is None:
            return
        etapes = self._seq.etapes
        if not etapes:
            self._seq_stop()
            return
        self._seq_idx = (self._seq_idx + 1) % len(etapes)
        etape = etapes[self._seq_idx]

        if etape.mode == "mono":
            self._set_mono(etape.cameras[0])
        else:
            self._leave_mono()
            self._stack.setCurrentWidget(self._grid_page)
            self._act_grid.setEnabled(False)
            self._set_grid(etape.cameras)
        self.statusBar().showMessage(
            f"Boucle {self._seq.nom} : étape {self._seq_idx + 1}/{len(etapes)}",
            etape.duree_s * 1000)
        self._seq_timer.start(etape.duree_s * 1000)

    # ------------------------------------------------------------------ divers

    def _pause_basculee(self, paused: bool):
        self._act_pause.setText("Reprendre" if paused else "Tout arrêter")
        self._act_pause.setIcon(icon("play") if paused else icon("pause"))
        if paused:
            self._seq_stop()
        cibles = ([self._mono_tile] if self._mono_tile is not None
                  else list(self._tiles.values()))
        for tile in cibles:
            if tile is None:
                continue
            if paused:
                tile.stop("En pause")
            else:
                tile.start()

    # ---------------------------------------------------------- mouvement ONVIF

    def _motion_assurer(self):
        if self._motion is None:
            from ..motion import MotionMonitor
            self._motion = MotionMonitor(self)
            self._motion.motion_changed.connect(self._on_motion)

    def _motion_basculee(self, actif: bool):
        if actif:
            if not self._cfg.cameras:
                self._act_motion.setChecked(False)
                return
            self._motion_assurer()
            self._motion.surveiller(list(self._cfg.cameras))
            self.statusBar().showMessage(
                "Détection de mouvement activée (ONVIF).", 5000)
        else:
            if self._act_motion_auto.isChecked():
                self._act_motion_auto.setChecked(False)
            if self._motion is not None:
                self._motion.stop()
            for cam_id in list(self._motion_ids):
                self._appliquer_motion(cam_id, False)
            self._motion_ids.clear()

    def _motion_auto_basculee(self, actif: bool):
        if actif and not self._act_motion.isChecked():
            self._act_motion.setChecked(True)       # la vue auto implique la détection
        if actif:
            self.statusBar().showMessage(
                "Vue mouvement : la grille affiche les caméras qui bougent.", 5000)
        self._selection_appliquee()

    def _on_motion(self, cam_id: str, actif: bool):
        if actif:
            self._motion_ids.add(cam_id)
        else:
            self._motion_ids.discard(cam_id)
        self._appliquer_motion(cam_id, actif)
        if self._act_motion_auto.isChecked():
            self._selection_appliquee()

    def _appliquer_motion(self, cam_id: str, actif: bool):
        """Surligne la tuile correspondante si elle est affichée."""
        tuile = self._tiles.get(cam_id)
        if tuile is None and self._mono_tile is not None \
                and self._mono_tile.camera.id == cam_id:
            tuile = self._mono_tile
        if tuile is not None and hasattr(tuile, "set_motion"):
            tuile.set_motion(actif)

    def _peupler_menu_ecrans(self, menu: QMenu):
        menu.clear()
        for i, ecran in enumerate(QGuiApplication.screens()):
            g = ecran.geometry()
            act = menu.addAction(icon("monitor"), f"Écran {i + 1} — {g.width()}×{g.height()}")
            act.triggered.connect(lambda _=False, e=ecran: self._fullscreen_sur(e))

    def _fullscreen_sur(self, ecran):
        self._topbar.hide()
        self._sidebar.hide()
        self.statusBar().hide()
        self.setGeometry(ecran.geometry())
        self.showFullScreen()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self._topbar.show()
            self._sidebar.show()
            self.statusBar().show()
        else:
            self._fullscreen_sur(self.screen())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self.isFullScreen():
                self._toggle_fullscreen()
            elif self._seq is not None:
                self._seq_stop()
            elif self._stack.currentWidget() is self._mono_page:
                self._go_grid()
            return
        super().keyPressEvent(event)

    def _update_status(self):
        tiles = ([self._mono_tile] if self._mono_tile is not None
                 else list(self._tiles.values()))
        tiles = [t for t in tiles if t is not None]
        actifs = sum(1 for t in tiles if t.state == TileState.PLAYING)
        en_cours = sum(1 for t in tiles if t.state in (TileState.CONNECTING, TileState.BACKOFF))
        erreurs = sum(1 for t in tiles if t.state in (TileState.AUTH_FAILED, TileState.NO_PLAYER))
        debit = sum(getattr(t, "debit_bps", 0.0) for t in tiles)
        txt = f"Flux actifs : {actifs}"
        if debit > 0:
            txt += f" · ≈ {format_debit(debit)}"
        if en_cours:
            txt += f" · en reconnexion : {en_cours}"
        if erreurs:
            txt += f" · en erreur : {erreurs}"
        self._status_streams.setText(txt + "  ")

    def closeEvent(self, event):
        self._seq_timer.stop()
        self._rotation_timer.stop()
        if self._motion is not None:
            self._motion.stop()
        if self._save_timer.isActive():
            self._save_timer.stop()
            save_config(self._cfg)
        self._leave_mono()
        self._vider_grille()
        super().closeEvent(event)
