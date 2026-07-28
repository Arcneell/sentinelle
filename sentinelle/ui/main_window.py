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
import threading

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QStackedWidget, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import APP_NAME
from ..config import (AppConfig, load_config, purger_cameras_sequences,
                      save_config)
from .config_dialogs import CameraDialog, ConfigDialog, DvrDialog, SiteDialog
from .icons import app_icon, icon
from .photo_tile import PhotoTile
from .tile import TileState, VideoTile, format_debit

logger = logging.getLogger(__name__)

MAX_TILES = 16
CAP_CHOICES = [("Auto (16 max)", 16), ("1×1", 1), ("2×2", 4), ("3×3", 9), ("4×4", 16)]
# espacement entre deux démarrages de tuiles (voir _set_grid) : assez long pour
# sérialiser les initialisations VA-API, assez court pour remplir un mur de 16
# en ~3 s
ESPACEMENT_DEMARRAGE_MS = 200


class MainWindow(QMainWindow):

    # résultat du contrôle de session exécuté en arrière-plan :
    # "ok" | "reconnecte" | "interactif" | "erreur"
    _session_resultat = Signal(str)
    # résultat du chargement de configuration serveur exécuté en arrière-plan :
    # (cfg | None, erreur "" | "jeton" | message, initial)
    _config_resultat = Signal(object, str, bool)
    # résultat d'un appel serveur générique lancé par _executer_reseau :
    # (résultat | None, erreur "" | message)
    _reseau_resultat = Signal(object, str)

    def __init__(self, config_path: str):
        super().__init__()
        self._config_path = config_path             # config locale (mode autonome)
        self._cfg = AppConfig(path=config_path)
        self._cfg_fetch_en_cours = False            # un chargement serveur est en vol
        self._cfg_retry = 0                         # relances auto après erreur serveur
        self._session_ui_en_cours = False           # page de connexion à l'écran
        self._tiles: dict[str, QWidget] = {}       # camera_id -> tuile (vidéo ou photo)
        self._mono_tile: VideoTile | None = None
        self._grid_dirty = False
        self._page = 0
        self._seq = None                            # séquence en cours de lecture
        self._seq_idx = -1
        self._motion = None                         # moniteur local ou écouteur serveur
        self._motion_ids = set()                    # caméras actuellement en mouvement
        self._icon_widgets = []                     # (widget, nom_icone) à recolorer
        self._session_verif_en_cours = False        # un contrôle de session est en vol
        self._login_differe = None                   # (user, mdp) à jouer HORS du thread UI
        from ..reglages import reglages
        self._settings = reglages()
        self._remote = self._creer_remote()         # None = mode autonome

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1100, 640)      # la barre de commandes reste entière
        self.resize(1320, 820)
        self._build_ui()
        # le mode est déjà choisi (assistant de premier lancement, cf. __main__)
        self.demarrage_annule = not self._assurer_session()
        if self.demarrage_annule:
            return                       # connexion serveur abandonnée → on n'ouvre pas
        self._maj_bouton_admin()
        self._load_config(initial=True)

    # ------------------------------------------------------------ mode serveur

    def _creer_remote(self):
        """Construit l'accès (non authentifié) au serveur si le mode le prévoit.
        L'adresse peut être vide au tout premier paramétrage : elle sera saisie
        sur la page de connexion."""
        if self._settings.value("mode", "local") != "serveur":
            return None
        url = self._settings.value("serveur_url", "", type=str).strip()
        from ..remote import ServeurDistant
        return ServeurDistant(url)

    def _assurer_session(self, silencieux: bool = True) -> bool:
        """Garantit une session serveur ouverte. Tente les identifiants mémorisés,
        sinon demande une connexion. Retourne True si connecté OU si une tentative
        silencieuse est différée (elle sera jouée hors du thread UI).

        silencieux=True : ne JAMAIS appeler login() sur le thread UI (gel jusqu'à
        ~13 s sur réseau 4G lent au démarrage). Avec des identifiants mémorisés,
        on diffère le login au thread de _load_config et on rend la main aussitôt.
        silencieux=False : la tentative silencieuse a déjà échoué (jeton refusé) —
        on passe directement à la connexion interactive."""
        if self._remote is None or self._remote.connecte:
            return True
        from ..config import desobfusquer, obfusquer
        # tentative silencieuse avec les identifiants mémorisés : DIFFÉRÉE hors
        # du thread UI (le login réseau bloquant est joué par _load_config)
        user = self._settings.value("serveur_user", "", type=str)
        mdp = desobfusquer(self._settings.value("serveur_pass", "", type=str))
        if silencieux and user and mdp:
            self._login_differe = (user, mdp)
            return True
        self._login_differe = None
        # connexion interactive ; l'adresse est saisissable seulement si elle
        # n'est pas encore connue (premier paramétrage du poste)
        from PySide6.QtWidgets import QApplication
        from .login_dialog import LoginDialog
        dlg = LoginDialog(self._remote.base, user, self,
                          url_editable=not self._remote.base)
        # pendant que la fenêtre principale est masquée (déconnexion), empêcher
        # que la fermeture de la page de login fasse quitter l'application
        app = QApplication.instance()
        prev = app.quitOnLastWindowClosed() if app else True
        if app:
            app.setQuitOnLastWindowClosed(False)
        try:
            accepte = bool(dlg.exec()) and dlg.remote is not None
        finally:
            if app:
                app.setQuitOnLastWindowClosed(prev)
        if not accepte:
            return False
        self._remplacer_remote(dlg.remote)
        infos = dlg.infos()
        self._settings.setValue("serveur_url", infos["url"])
        if infos["memoriser"]:
            self._settings.setValue("serveur_user", infos["username"])
            self._settings.setValue("serveur_pass", obfusquer(infos["password"]))
        else:
            self._settings.remove("serveur_user")
            self._settings.remove("serveur_pass")
        return True

    def _remplacer_remote(self, nouveau):
        """Remplace l'objet de session serveur. L'écouteur de mouvement garde une
        référence sur l'ancien objet (jeton mort) : on le défait ici — il sera
        recréé avec la nouvelle session par _load_config si la détection est
        active."""
        self._remote = nouveau
        if self._motion is not None:
            self._motion.stop()
            self._motion = None

    def _maj_session_timer(self):
        """Active la surveillance de session en mode serveur uniquement."""
        if self._remote is not None:
            if not self._session_timer.isActive():
                self._session_timer.start()
        else:
            self._session_timer.stop()

    def _verifier_session(self):
        """Contrôle périodique de la session serveur, exécuté HORS du thread UI
        (un serveur injoignable gèlerait sinon l'interface à chaque tick).

        Dans le thread : interroge /api/session et, si le jeton est mort ou
        expire dans moins d'un jour, retente un login avec les identifiants
        mémorisés (le jeton est rafraîchi en place sur l'objet partagé). Le
        résultat revient sur le thread Qt via _session_resultat."""
        if (self._remote is None or not self._remote.connecte
                or self._session_verif_en_cours or self._session_ui_en_cours):
            return
        from ..config import desobfusquer
        self._session_verif_en_cours = True
        remote = self._remote
        self._session_remote = remote           # sur quel objet ce contrôle porte
        user = self._settings.value("serveur_user", "", type=str)
        mdp = desobfusquer(self._settings.value("serveur_pass", "", type=str))

        def work():
            from ..remote import ErreurServeur, JetonInvalide
            res = "erreur"                       # défaut sûr : au pire on retentera
            try:
                try:
                    reste = remote.session_reste()
                except JetonInvalide:
                    reste = 0                    # jeton mort : rafraîchir tout de suite
                # seuil = 2 ticks du contrôle (15 min) : l'ancien seuil de 24 h
                # déclenchait un rafraîchissement (donc un rechargement complet)
                # toutes les 15 min dès que le TTL serveur était ≤ 24 h
                if reste is None or reste >= 1800:
                    res = "ok"
                elif user and mdp:
                    try:
                        remote.login(user, mdp)  # rafraîchit remote.jeton en place
                        res = "reconnecte"
                    except JetonInvalide:
                        # identifiants mémorisés devenus faux : session à refaire
                        res = "interactif" if reste <= 0 else "ok"
                    except ErreurServeur:
                        res = "erreur"
                else:
                    res = "interactif" if reste <= 0 else "ok"
            except ErreurServeur:
                res = "erreur"                   # réseau : on retentera au prochain tick
            except Exception:
                # jamais laisser le thread mourir sans émettre : sinon le drapeau
                # _session_verif_en_cours resterait bloqué et le contrôle de
                # session ne repartirait plus jamais.
                logger.exception("Contrôle de session : erreur inattendue")
            try:
                self._session_resultat.emit(res)
            except RuntimeError:
                pass                             # fenêtre détruite entre-temps
        threading.Thread(target=work, daemon=True, name="session-check").start()

    def _on_session_resultat(self, res: str):
        if res == "interactif" and QApplication.activeModalWidget() is not None:
            # ne pas ouvrir la page de connexion PAR-DESSUS un dialogue en cours :
            # on repasse dans 2 s (le drapeau reste levé : pas de double contrôle)
            QTimer.singleShot(2000, lambda: self._on_session_resultat(res))
            return
        self._session_verif_en_cours = False
        # la session a pu changer pendant le contrôle (déconnexion, bascule de
        # mode, autre serveur) : dans ce cas le résultat ne la concerne plus
        if self._remote is None or self._remote is not getattr(self, "_session_remote", None):
            return
        if res == "reconnecte":
            # jeton rafraîchi EN PLACE : mettre à jour les URLs des caméras sans
            # reconstruire le mur — le relais ne vérifie le jeton qu'à l'ouverture
            # d'un flux, les lectures en cours survivent, et les tuiles utilisent
            # le nouveau jeton à leur prochaine (re)connexion.
            self._remote.maj_jeton_urls(self._cfg)
        elif res == "interactif":
            self._remote.jeton = ""
            self._session_ui_en_cours = True
            try:
                ok = self._assurer_session()
            finally:
                self._session_ui_en_cours = False
            if ok:
                self._load_config()

    def _deconnecter(self):
        """Ferme l'interface principale et rouvre la page de connexion. Une
        nouvelle connexion réaffiche l'interface ; si elle est abandonnée,
        l'application se ferme."""
        self._settings.remove("serveur_user")
        self._settings.remove("serveur_pass")
        # couper la session en cours
        if self._act_motion.isChecked():
            self._act_motion.setChecked(False)
        if self._motion is not None:
            self._motion.stop()
            self._motion = None
        self._seq_stop()
        self._leave_mono()
        self._vider_grille()

        self.hide()                              # ferme l'interface principale
        # révoquer la session côté serveur (déconnexion réelle de tous les
        # appareils) — best-effort, dans un thread pour ne pas geler l'UI.
        # join(3) borné AVANT d'ouvrir la page de connexion : la révocation
        # (bump de version côté serveur) doit précéder un éventuel re-login
        # immédiat sur le MÊME compte, sinon elle invaliderait le jeton tout
        # neuf. Fenêtre cachée à cet instant : la courte attente est invisible.
        ancien = self._remote
        if ancien is not None and ancien.connecte:
            th = threading.Thread(target=ancien.deconnecter, daemon=True,
                                  name="logout")
            th.start()
            th.join(timeout=3)
        self._remote = self._creer_remote()      # accès non authentifié
        if self._assurer_session():              # rouvre la page de connexion
            self._maj_bouton_admin()
            self._load_config()
            self.showNormal()
            self.raise_()
            self.activateWindow()
        else:
            self.close()                         # connexion abandonnée → on ferme

    def _maj_bouton_admin(self):
        """Adapte l'interface aux droits : bouton Administration et pied latéral."""
        est_admin = self._remote is not None and self._remote.connecte and self._remote.admin
        self._act_admin.setVisible(est_admin)
        # pied du panneau caméras : ajout local, gestion (admin) ou rien
        if self._remote is None:
            self._btn_add.setText(" Ajouter une caméra")
            self._side_footer.setVisible(True)
        elif est_admin:
            self._btn_add.setText(" Gérer les caméras")
            self._side_footer.setVisible(True)
        else:
            self._side_footer.setVisible(False)

    def _action_pied_lateral(self):
        if self._remote is None:
            self._ouvrir_configuration()
        else:
            self._ouvrir_admin()

    # ------------------------------------------------------ appels serveur async

    def _executer_reseau(self, fn, on_ok, titre: str):
        """Exécute un appel serveur bloquant HORS du thread UI, avec l'interface
        désactivée (l'event loop continue de tourner : pas de gel « ne répond
        pas »). Le résultat revient sur le thread Qt et déclenche on_ok, ou une
        boîte d'erreur en cas d'échec. Un seul appel à la fois (les actions qui
        l'utilisent sont déclenchées depuis des dialogues modaux)."""
        from PySide6.QtCore import Qt as _Qt
        self._reseau_on_ok = on_ok
        self._reseau_titre = titre
        self.setEnabled(False)
        QApplication.setOverrideCursor(_Qt.WaitCursor)

        def work():
            from ..remote import ErreurServeur
            res, err, ok = None, "", False
            try:
                res = fn()
                ok = True
            except ErreurServeur as e:
                err = str(e) or "serveur injoignable"
            except Exception as e:
                logger.exception("Appel serveur : erreur inattendue")
                err = str(e) or "erreur inattendue"
            finally:
                # émettre dans le finally : même une erreur inattendue (y compris
                # hors Exception) réactive l'UI — sinon fenêtre désactivée +
                # curseur d'attente bloqués définitivement
                if not ok and not err:
                    err = "erreur inattendue"
                try:
                    self._reseau_resultat.emit(res, err)
                except RuntimeError:
                    pass                         # fenêtre détruite entre-temps
        threading.Thread(target=work, daemon=True, name="reseau").start()

    def _on_reseau_resultat(self, res, err: str):
        QApplication.restoreOverrideCursor()
        self.setEnabled(True)
        on_ok, titre = self._reseau_on_ok, self._reseau_titre
        self._reseau_on_ok = None
        if err:
            QMessageBox.warning(self, titre, f"Serveur injoignable :\n{err}")
            return
        if on_ok is not None:
            on_ok(res)

    def _editer_sequences(self):
        """Édition des boucles : locales (autonome) ou personnelles (serveur)."""
        self._seq_stop()
        from .sequence_editor import SequenceEditor
        dlg = SequenceEditor(self._cfg, self)
        accepte = dlg.exec()
        if not dlg.modifie:
            return
        if not accepte:
            self._load_config()              # annulé → on restaure
            return
        from .toast import toast
        if self._remote is None:
            save_config(self._cfg)
            self._peupler_sequences()
            toast(self, "Rondes enregistrées")
            return
        # envoi serveur hors du thread UI (gelait l'interface sur réseau lent)
        sequences = self._cfg.sequences

        def ok(_res):
            self._peupler_sequences()
            toast(self, "Rondes enregistrées")
        self._executer_reseau(
            lambda: self._remote.pousser_boucles(sequences), ok, "Rondes")

    def _ouvrir_admin(self):
        if self._remote is None or not self._remote.admin:
            return
        # récupération de la config d'admin hors du thread UI, puis ouverture
        self._executer_reseau(
            lambda: self._remote.config_admin(),
            self._ouvrir_admin_dialog, "Administration")

    def _ouvrir_admin_dialog(self, cfg_admin):
        from .admin_dialog import AdminDialog
        dlg = AdminDialog(cfg_admin, self._remote, self)
        dlg.exec()
        if dlg.demande_mode == "local":
            self._repasser_autonome()
        elif dlg.demande_mode == "serveur":
            self._basculer_vers_serveur()
        elif dlg.recharger:
            self._load_config()
        if dlg.enregistre:
            from .toast import toast
            toast(self, "Modifications enregistrées sur le serveur")

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
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(6)

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
        self._cap_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._cap_combo.currentIndexChanged.connect(self._selection_changee)
        lay.addWidget(self._cap_combo)

        # navigation manuelle entre pages (visible seulement s'il y en a
        # plusieurs) : avant, seule la rotation automatique les faisait défiler
        self._nav_box = QWidget()
        nav = QHBoxLayout(self._nav_box)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(0)
        self._btn_page_prec = self._tbtn("chevron-left", "", "Page précédente (PgPréc)",
                                         lambda: self._changer_page(-1))
        self._lbl_page = QLabel("")
        self._lbl_page.setObjectName("pageInfo")
        self._btn_page_suiv = self._tbtn("chevron-right", "", "Page suivante (PgSuiv)",
                                         lambda: self._changer_page(+1))
        nav.addWidget(self._btn_page_prec)
        nav.addWidget(self._lbl_page)
        nav.addWidget(self._btn_page_suiv)
        self._nav_box.setVisible(False)
        lay.addWidget(self._nav_box)
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

        # rondes
        self._seq_combo = QComboBox()
        self._seq_combo.setMinimumWidth(120)
        self._seq_combo.setMaximumWidth(220)
        self._seq_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._seq_combo.setToolTip("Ronde à lire")
        lay.addWidget(self._seq_combo)
        self._act_seq = self._tbtn("play", "Lire",
                                   "Lire ou arrêter la ronde sélectionnée",
                                   self._seq_basculee, checkable=True)
        lay.addWidget(self._act_seq)
        self._btn_boucles = self._tbtn("route", "Rondes",
                                       "Créer et modifier vos rondes",
                                       self._editer_sequences)
        lay.addWidget(self._btn_boucles)

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

        # plein écran : deux boutons distincts (action directe + choix de l'écran)
        self._btn_full = self._tbtn("maximize", "Plein écran", "Plein écran (F11)",
                                    self._toggle_fullscreen)
        lay.addWidget(self._btn_full)

        self._btn_ecrans = QToolButton()
        self._btn_ecrans.setIcon(icon("monitor"))
        self._btn_ecrans.setIconSize(QSize(18, 18))
        self._btn_ecrans.setToolTip("Plein écran sur un écran précis")
        self._btn_ecrans.setPopupMode(QToolButton.InstantPopup)
        menu_full = QMenu(self._btn_ecrans)
        menu_full.aboutToShow.connect(lambda: self._peupler_menu_ecrans(menu_full))
        self._btn_ecrans.setMenu(menu_full)
        self._icon_widgets.append((self._btn_ecrans, "monitor"))
        lay.addWidget(self._btn_ecrans)

        self._act_admin = self._tbtn("lock", "Administration",
                                     "Gérer le serveur : utilisateurs, caméras, réglages",
                                     self._ouvrir_admin)
        self._act_admin.setVisible(False)
        lay.addWidget(self._act_admin)

        self._btn_cfg = self._tbtn("settings", "Configuration",
                                   "Préférences et réglages (Ctrl+,)",
                                   self._ouvrir_configuration)
        lay.addWidget(self._btn_cfg)

        # raccourcis clavier (indépendants des boutons)
        for seq, slot in (("Ctrl+G", self._retour_manuel_grille),
                          ("F11", self._toggle_fullscreen),
                          ("Ctrl+,", self._ouvrir_configuration),
                          ("PgUp", lambda: self._changer_page(-1)),
                          ("PgDown", lambda: self._changer_page(+1))):
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
        eh.setContentsMargins(14, 10, 10, 10)
        titre = QLabel("CAMÉRAS")
        titre.setObjectName("sideTitle")
        self._side_count = QLabel("")
        self._side_count.setObjectName("sideCount")
        btn_tout = self._tbtn("check-square", "", "Cocher toutes les caméras",
                              self._tout_cocher)
        btn_rien = self._tbtn("square", "", "Tout décocher", self._tout_decocher)
        for b in (btn_tout, btn_rien):
            b.setIconSize(QSize(15, 15))
        eh.addWidget(titre)
        eh.addStretch(1)
        eh.addWidget(self._side_count)
        eh.addWidget(btn_tout)
        eh.addWidget(btn_rien)
        v.addWidget(entete)

        # recherche : indispensable dès que le parc dépasse quelques sites
        from PySide6.QtWidgets import QLineEdit
        zone = QFrame()
        zone.setObjectName("sideSearch")
        zl = QHBoxLayout(zone)
        zl.setContentsMargins(8, 6, 8, 6)
        self._recherche = QLineEdit()
        self._recherche.setPlaceholderText("Rechercher…")
        self._recherche.setClearButtonEnabled(True)
        self._recherche.textChanged.connect(self._filtrer_arbre)
        zl.addWidget(self._recherche)
        v.addWidget(zone)

        self._tree = QTreeWidget()
        self._tree.setObjectName("cameraTree")
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(14)
        from .widgets import BadgeDelegate
        self._tree.setItemDelegate(BadgeDelegate(self._tree))
        # on coche, on ne « sélectionne » pas : pas de surbrillance persistante
        self._tree.setSelectionMode(QTreeWidget.NoSelection)
        self._tree.setFocusPolicy(Qt.NoFocus)
        self._tree.itemChanged.connect(self._coche_changee)
        self._tree.itemDoubleClicked.connect(self._arbre_double_clic)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._menu_arbre)
        v.addWidget(self._tree, 1)

        self._side_footer = QFrame()
        self._side_footer.setObjectName("sideFooter")
        ph = QVBoxLayout(self._side_footer)
        ph.setContentsMargins(10, 10, 10, 10)
        self._btn_add = QPushButton(icon("plus"), " Ajouter une caméra")
        self._btn_add.setObjectName("addBtn")
        self._btn_add.clicked.connect(self._action_pied_lateral)
        self._icon_widgets.append((self._btn_add, "plus"))
        ph.addWidget(self._btn_add)
        v.addWidget(self._side_footer)

        self._sidebar = panel
        return panel

    # ------------------------------------------------------------------ mur d'images

    def _build_stage(self) -> QWidget:
        self._grid_page = QWidget()
        self._grid_page.setObjectName("gridPage")
        self._grid_layout = QGridLayout(self._grid_page)
        self._grid_layout.setContentsMargins(3, 3, 3, 3)
        self._grid_layout.setSpacing(3)

        from .widgets import EmptyState
        self._placeholder = EmptyState()

        self._mono_page = QWidget()
        self._mono_page.setObjectName("monoPage")
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

        # vérification périodique de la session serveur : détecte un jeton
        # expiré/révoqué (reconnexion) et rafraîchit un jeton bientôt périmé pour
        # que les murs d'images restent connectés sans intervention
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(15 * 60 * 1000)
        self._session_timer.timeout.connect(self._verifier_session)
        self._session_resultat.connect(self._on_session_resultat)
        self._config_resultat.connect(self._on_config_resultat)
        self._reseau_resultat.connect(self._on_reseau_resultat)
        self._reseau_on_ok = None
        self._reseau_titre = ""

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
        # sélecteurs par nom : une feuille sans sélecteur se propagerait aux
        # enfants (le bouton de l'écran vide perdait son style)
        self._grid_page.setStyleSheet(
            f"QWidget#gridPage {{ background-color: {t('video_bg')}; }}")
        self._mono_page.setStyleSheet(
            f"QWidget#monoPage {{ background-color: {t('video_bg')}; }}")
        self._placeholder.restyle()

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
        if self._remote is None:
            try:
                cfg = load_config(self._config_path)
            except Exception as e:
                QMessageBox.critical(self, "Configuration", str(e))
                return
            self._appliquer_config(cfg, initial)
            return

        # mode serveur : la récupération HTTP (jusqu'à ~13 s de timeout) se fait
        # HORS du thread UI — elle gelait toute l'interface à chaque rechargement.
        # La garde est liée à l'objet remote : après une déconnexion/bascule, un
        # chargement de l'ANCIENNE session ne doit pas bloquer celui de la nouvelle.
        if self._cfg_fetch_en_cours and self._remote is getattr(self, "_cfg_remote", None):
            return
        self._cfg_fetch_en_cours = True
        remote = self._remote
        self._cfg_remote = remote               # sur quel objet ce chargement porte
        creds = self._login_differe             # login silencieux à jouer dans le thread
        self.statusBar().showMessage("Chargement de la configuration…")

        def work():
            from ..remote import ErreurServeur, JetonInvalide
            cfg, err = None, ""
            try:
                # login silencieux différé : joué ICI (thread), jamais sur l'UI.
                # Conservé sur network error (retenté au prochain essai) ; sur
                # jeton refusé, _on_config_resultat bascule en interactif.
                if creds and not remote.connecte:
                    remote.login(*creds)
                cfg = remote.config_vue()
            except JetonInvalide:
                err = "jeton"
            except ErreurServeur as e:
                err = str(e) or "serveur injoignable"
            except Exception as e:
                logger.exception("Chargement de la configuration : erreur inattendue")
                err = str(e) or "erreur inattendue"
            try:
                self._config_resultat.emit(cfg, err, initial)
            except RuntimeError:
                pass                             # fenêtre détruite entre-temps
        threading.Thread(target=work, daemon=True, name="config-fetch").start()

    def _on_config_resultat(self, cfg, err: str, initial: bool):
        # la session a pu changer pendant le chargement (déconnexion, bascule…) :
        # résultat obsolète — sans toucher au drapeau d'un chargement plus récent
        if self._remote is None or self._remote is not getattr(self, "_cfg_remote", None):
            return
        self._cfg_fetch_en_cours = False
        if QApplication.activeModalWidget() is not None:
            # un dialogue d'édition est ouvert : appliquer maintenant écraserait
            # la config en cours d'édition — on repasse quand il sera fermé
            self._cfg_fetch_en_cours = True      # bloque un nouveau chargement
            QTimer.singleShot(1500, lambda: self._on_config_resultat(cfg, err, initial))
            return

        if err == "jeton":
            # session expirée (mot de passe changé, serveur redémarré…) OU
            # identifiants mémorisés refusés lors du login différé. Le verrou
            # empêche une relance programmée (backoff, contrôle de session) de
            # partir avec un jeton vide PENDANT la page de connexion — elle
            # rouvrait une seconde page par-dessus la première.
            self._remote.jeton = ""
            self._login_differe = None           # la tentative silencieuse a échoué
            self._session_ui_en_cours = True
            try:
                ok = self._assurer_session(silencieux=False)
            finally:
                self._session_ui_en_cours = False
            if ok:
                self._load_config(initial)
                return
            err = "session expirée"

        if err:
            # on GARDE la configuration et le mur actuels (les tuiles ont leur
            # propre reconnexion) au lieu de tout remplacer par une config vide,
            # et on retente tout seul avec un délai croissant.
            self._cfg_retry += 1
            delay = min(5 * (2 ** (self._cfg_retry - 1)), 60)
            self.statusBar().showMessage(
                f"Serveur injoignable ({err}) — nouvel essai dans {delay}s")
            if not self._cfg.cameras:
                self._selection_appliquee()      # placeholder « non connecté »
            # préserver `initial` à travers la relance : sinon un accroc réseau au
            # démarrage (site 4G) ferait perdre la « ronde au démarrage » du poste
            self._cfg_initial_attente = initial
            QTimer.singleShot(delay * 1000, self._relancer_load_config)
            return

        self._cfg_retry = 0
        self._login_differe = None               # session ouverte : plus rien à différer
        self._appliquer_config(cfg, initial)

    def _relancer_load_config(self):
        if (self._remote is not None and not self._cfg_fetch_en_cours
                and not self._session_ui_en_cours
                and self._remote is getattr(self, "_cfg_remote", None)):
            self._load_config(getattr(self, "_cfg_initial_attente", False))

    def _appliquer_config(self, cfg, initial: bool = False):
        self.statusBar().clearMessage()      # efface un éventuel message persistant
        self._seq_stop()
        self._leave_mono()
        self._vider_grille()
        # revenir explicitement à la page grille : un rechargement reçu en vue
        # mono laissait la pile sur une page mono vidée — écran noir définitif
        self._stack.setCurrentWidget(self._grid_page)
        self._act_grid.setEnabled(False)
        self._cfg = cfg

        self._rot_spin.blockSignals(True)
        self._rot_spin.setValue(cfg.rotation_duree_s)
        self._rot_spin.blockSignals(False)
        self._rotation_timer.setInterval(cfg.rotation_duree_s * 1000)

        self._peupler_arbre()
        self._peupler_sequences()
        self._maj_visibilite_mouvement()
        self._selection_appliquee()

        # si la détection est demandée, (re)créer le moniteur au besoin — il a pu
        # être défait lors d'un changement de session — puis réaligner la liste
        # surveillée (caméras ajoutées/retirées, identifiants modifiés)
        if self._act_motion.isChecked():
            self._motion_assurer()
            self._motion.surveiller(list(self._cfg.cameras))
        self._maj_session_timer()
        # le login différé (identifiants mémorisés) n'ouvre la session que
        # maintenant, dans le thread : rejouer le calcul du bouton Administration
        # qui, à l'ouverture, voyait encore remote.connecte == False
        self._maj_bouton_admin()

        if cfg.warnings:
            QMessageBox.warning(
                self, "Configuration — avertissements",
                "\n".join(cfg.warnings[:20]) + ("\n…" if len(cfg.warnings) > 20 else ""))

        if initial and self._remote is None and not cfg.cameras:
            # premier lancement en mode autonome : ouvrir la configuration
            QTimer.singleShot(200, self._ouvrir_configuration)

        if initial:
            # ronde au démarrage (réglage du poste) : un mur redémarré reprend
            # sa ronde sans intervention — uniquement au premier chargement,
            # jamais sur un simple rechargement de configuration
            nom = self._settings.value("ronde_auto", "", type=str)
            i = next((k for k, s in enumerate(self._cfg.sequences)
                      if s.nom == nom), -1) if nom else -1
            if i >= 0:
                self._seq_combo.setCurrentIndex(i)
                self._act_seq.setChecked(True)      # déclenche la lecture

    def _ouvrir_configuration(self):
        etait_en_seq = self._seq is not None
        self._seq_stop()

        if self._remote is None:
            # mode autonome : configuration locale complète
            dlg = ConfigDialog(self._cfg, self)
            code = dlg.exec()
            if dlg.demande_serveur:
                if dlg.modifie:
                    save_config(self._cfg)
                self._basculer_vers_serveur()
                return
            if code and dlg.modifie:
                save_config(self._cfg)
                self._load_config()
            elif not code and dlg.modifie:
                self._load_config()          # annulé → on repart du fichier
            elif etait_en_seq:
                self._update_status()
            return

        # mode serveur : préférences du poste (compte + déconnexion). La gestion
        # serveur (dont le mode) est dans le panneau Administration.
        from .config_dialogs import PreferencesDialog
        dlg = PreferencesDialog(self._remote, self,
                                noms_rondes=[s.nom for s in self._cfg.sequences])
        dlg.exec()
        if dlg.deconnexion:
            self._deconnecter()
        elif self._remote is not None:
            # un changement de mot de passe a renouvelé le jeton relay : réaligner
            # les URLs RTSP sur le nouveau jeton (les lectures en cours survivent,
            # les reconnexions utiliseront le jeton frais). Sans effet sinon.
            self._remote.maj_jeton_urls(self._cfg)
        elif etait_en_seq:
            self._update_status()

    # ------------------------------------------------- bascule de mode (admin)

    def _basculer_vers_serveur(self, url: str = ""):
        """Passe le poste en mode serveur — exige un compte administrateur."""
        from .login_dialog import LoginDialog
        dlg = LoginDialog(url, "", self, url_editable=True)
        dlg.setWindowTitle("Passer en mode serveur — connexion administrateur")
        if not dlg.exec() or dlg.remote is None:
            return
        if not dlg.remote.admin:
            QMessageBox.warning(
                self, "Mode serveur",
                "Un compte administrateur est nécessaire pour lier ce poste "
                "à un serveur.")
            return
        infos = dlg.infos()
        self._settings.setValue("mode", "serveur")
        self._settings.setValue("serveur_url", infos["url"])
        if infos["memoriser"]:
            self._settings.setValue("serveur_user", infos["username"])
            from ..config import obfusquer
            self._settings.setValue("serveur_pass", obfusquer(infos["password"]))
        else:
            self._settings.remove("serveur_user")
            self._settings.remove("serveur_pass")
        self._remplacer_remote(dlg.remote)
        self._maj_bouton_admin()
        self._load_config()

    def _repasser_autonome(self):
        """Repasse le poste en mode autonome (déclenché par un admin)."""
        if QMessageBox.question(
                self, "Mode autonome",
                "Repasser ce poste en mode autonome ?\n\n"
                "Il n'utilisera plus le serveur ; sa configuration locale "
                "(souvent vide) reprendra la main.") != QMessageBox.Yes:
            return
        self._settings.setValue("mode", "local")
        self._settings.remove("serveur_user")
        self._settings.remove("serveur_pass")
        if self._act_motion.isChecked():
            self._act_motion.setChecked(False)
        if self._motion is not None:
            self._motion.stop()
            self._motion = None
        self._remote = None
        self._maj_bouton_admin()
        self._load_config()

    def _peupler_arbre(self):
        from .widgets import ROLE_BADGES
        cochees = set(self._settings.value("cameras_cochees", [], type=list))
        connus = {c.id for c in self._cfg.cameras}
        if connus and (cochees - connus):
            # caméras disparues (droits modifiés, site supprimé…) : purger, sinon
            # le poste redémarre sur un mur vide sans explication
            cochees &= connus
            self._settings.setValue("cameras_cochees", sorted(cochees))

        # préserve l'état plié/déplié entre deux rechargements de configuration
        premier = self._tree.topLevelItemCount() == 0
        deplies = {self._tree.topLevelItem(i).data(0, Qt.UserRole + 1)
                   for i in range(self._tree.topLevelItemCount())
                   if self._tree.topLevelItem(i).isExpanded()}

        self._tree.blockSignals(True)
        self._tree.clear()
        for site in self._cfg.sites:
            cams = [c for c in self._cfg.cameras if c.site.id == site.id]
            if not cams:
                continue
            site_item = QTreeWidgetItem([site.nom])
            site_item.setData(0, Qt.UserRole + 1, site.id)
            if site.lien == "4g":
                site_item.setData(0, ROLE_BADGES, ["4G"])
            site_item.setFlags(site_item.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            for cam in cams:
                item = QTreeWidgetItem([cam.nom])
                if cam.profil == "eco-extreme":
                    item.setData(0, ROLE_BADGES, ["photo"])
                elif cam.profil == "eco":
                    item.setData(0, ROLE_BADGES, ["éco"])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setData(0, Qt.UserRole, cam.id)
                item.setCheckState(0, Qt.Checked if cam.id in cochees else Qt.Unchecked)
                site_item.addChild(item)
            self._tree.addTopLevelItem(site_item)
            site_item.setExpanded(premier or site_item.data(0, Qt.UserRole + 1) in deplies)
        self._tree.blockSignals(False)
        if self._recherche.text():
            self._filtrer_arbre(self._recherche.text())
        n = len(self._cfg.cameras)
        self._side_count.setText(str(n) if n else "")
        self._side_count.setVisible(bool(n))

    # ------------------------------------------------ actions du panneau caméras

    def _filtrer_arbre(self, texte: str):
        from .camera_picker import _simplifier
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
            if cle:
                si.setExpanded(True)

    def _cocher_tout(self, etat: bool, site_id: str | None = None):
        """Coche/décoche d'un geste (tout le parc ou un site entier)."""
        self._tree.blockSignals(True)
        for i in range(self._tree.topLevelItemCount()):
            si = self._tree.topLevelItem(i)
            if site_id is not None and si.data(0, Qt.UserRole + 1) != site_id:
                continue
            for j in range(si.childCount()):
                si.child(j).setCheckState(0, Qt.Checked if etat else Qt.Unchecked)
        self._tree.blockSignals(False)
        self._coche_changee(None, 0)

    def _tout_cocher(self):
        self._cocher_tout(True)

    def _tout_decocher(self):
        self._cocher_tout(False)

    # ------------------------------------------------------ navigation de pages

    def _changer_page(self, delta: int):
        if self._seq is not None:
            return
        pages = self._pages()
        if len(pages) < 2:
            return
        if self._stack.currentWidget() is self._mono_page:
            self._go_grid()
        self._page = (self._page + delta) % len(pages)
        self._set_grid(pages[self._page])
        self._maj_nav()

    def _maj_nav(self):
        pages = self._pages()
        visible = (len(pages) > 1 and self._seq is None
                   and self._stack.currentWidget() is self._grid_page)
        self._nav_box.setVisible(visible)
        if visible:
            self._lbl_page.setText(f" {self._page % len(pages) + 1}/{len(pages)} ")

    def _a_camera_onvif(self) -> bool:
        return any(c.marque == "onvif" or getattr(c, "remote_onvif", False)
                   for c in self._cfg.cameras)

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
            self._seq_combo.addItem(icon("lock") if s.partagee else icon("route"),
                                    s.nom)
        actif = bool(self._cfg.sequences)
        # sans ronde, une liste vide grisée à côté de « Lire » n'aide personne :
        # on masque, seul le bouton Rondes (création) reste
        self._seq_combo.setVisible(actif)
        self._act_seq.setVisible(actif)
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
            self._maj_nav()
            return
        self._page %= len(pages)
        self._set_grid(pages[self._page])
        self._maj_nav()

    # ----------------------------------------------------------------- grille

    def _make_tile(self, cam, vue: str):
        # profil « photo » en grille : image HTTP périodique (sites 4G contraints)
        if vue == "grille" and cam.profil == "eco-extreme":
            tile = PhotoTile(cam, vue)
        else:
            tile = VideoTile(cam, vue)
        tile.double_clicked.connect(self._tuile_double_clic)
        tile.state_changed.connect(self._update_status)
        from .toast import toast
        tile.snapshot_saved.connect(
            lambda p: toast(self, f"Image enregistrée : {p}"))
        if cam.id in self._motion_ids and hasattr(tile, "set_motion"):
            tile.set_motion(True)
        return tile

    def _vider_grille(self):
        for tile in self._tiles.values():
            self._grid_layout.removeWidget(tile)
            tile.dispose()      # libère mpv hors thread UI puis se détruit
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
                self._grid_layout.removeWidget(tile)
                tile.dispose()

        for cam_id in ids:
            if cam_id not in self._tiles:
                tile = self._make_tile(self._cfg.camera(cam_id), vue)
                self._tiles[cam_id] = tile

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
                self._placeholder.afficher(
                    "motion", "Vue mouvement active",
                    "La grille affichera les caméras dès qu'elles détectent "
                    "une activité.")
            elif not self._cfg.cameras:
                if self._remote is not None and not self._remote.connecte:
                    self._placeholder.afficher(
                        "lock", "Non connecté au serveur",
                        "La connexion sera retentée automatiquement.",
                        "Réessayer maintenant", self._load_config)
                elif self._remote is not None and self._remote.admin:
                    self._placeholder.afficher(
                        "camera", "Aucune caméra configurée",
                        "Ajoutez vos sites et vos enregistreurs depuis le "
                        "panneau d'administration.",
                        "Ouvrir l'administration", self._ouvrir_admin)
                elif self._remote is not None:
                    self._placeholder.afficher(
                        "users", "Aucune caméra attribuée",
                        "Votre compte n'a accès à aucune caméra pour le moment.\n"
                        "Rapprochez-vous de votre administrateur.")
                else:
                    self._placeholder.afficher(
                        "camera", "Aucune caméra configurée",
                        "Ajoutez vos sites et vos enregistreurs pour commencer.",
                        "Ouvrir la configuration", self._ouvrir_configuration)
            else:
                self._placeholder.afficher(
                    "grid", "Aucune caméra affichée",
                    "Cochez des caméras dans le panneau de gauche.\n"
                    "Double-clic : plein écran   ·   Clic droit : options",
                    "Afficher toutes les caméras", self._tout_cocher)
            self._grid_layout.addWidget(self._placeholder, 0, 0)
        else:
            cols = math.ceil(math.sqrt(len(ordered)))
            for idx, tile in enumerate(ordered):
                self._grid_layout.addWidget(tile, idx // cols, idx % cols)
            for c in range(cols):
                self._grid_layout.setColumnStretch(c, 1)
            for r in range(math.ceil(len(ordered) / cols)):
                self._grid_layout.setRowStretch(r, 1)

        # (re)démarre APRÈS l'insertion dans la grille : démarrer avant prenait
        # le winId() d'une fenêtre native créée hors hiérarchie, puis reparentée
        # — identifiant potentiellement recréé sous X11, donc wid mpv périmé.
        # Relance aussi toute tuile conservée à l'arrêt (retour de vue mono).
        # Démarrages ÉCHELONNÉS : 16 mpv qui initialisent VA-API et ouvrent leur
        # flux au même instant se marchent dessus sur un iGPU d'entrée de gamme —
        # les initialisations perdantes retombent DÉFINITIVEMENT en décodage
        # logiciel (constaté sur mur N4020 : 3 tuiles en vaapi, 13 en logiciel).
        if not paused:
            attente = 0
            for tile in self._tiles.values():
                if tile.state == TileState.IDLE:
                    self._start_tuile_differe(tile, attente)
                    attente += ESPACEMENT_DEMARRAGE_MS
        self._grid_dirty = False
        self._update_status()

    def _start_tuile_differe(self, tile, delai_ms: int):
        """Démarre une tuile après delai_ms, si elle est toujours affichée et à
        l'arrêt (la grille a pu être reconstruite entre-temps). Le QTimer est lié
        à la tuile : détruite, le rappel n'est jamais invoqué."""
        if delai_ms <= 0:
            tile.start()
            return

        def go(t=tile):
            if t in self._tiles.values() and t.state == TileState.IDLE:
                t.start()
        QTimer.singleShot(delai_ms, tile, go)

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
            self._mono_layout.removeWidget(self._mono_tile)
            self._mono_tile.dispose()
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
        self._maj_nav()
        self._update_status()

    def _retour_manuel_grille(self):
        self._seq_stop()
        self._go_grid()

    def _tuile_double_clic(self, cam_id: str):
        # mémorise la vue AVANT _seq_stop : pendant une étape mono de boucle,
        # _seq_stop ramène déjà à la grille, et le test après coup re-basculait
        # aussitôt en mono au lieu de rendre la main à l'utilisateur
        en_mono = self._stack.currentWidget() is self._mono_page
        self._seq_stop()
        if en_mono:
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
        """Actions rapides sur l'arbre : affichage pour tous, édition de la
        configuration en mode autonome."""
        item = self._tree.itemAt(pos)
        cam_id = item.data(0, Qt.UserRole) if item else None
        site_id = item.data(0, Qt.UserRole + 1) if item else None

        menu = QMenu(self)
        cam = self._cfg.camera(cam_id) if cam_id else None
        if cam is not None:
            menu.addAction(icon("maximize"), f"Plein écran sur « {cam.nom} »",
                           lambda: (self._seq_stop(), self._set_mono(cam_id)))
            site_id = cam.site.id
        site = self._cfg.site(site_id) if site_id else None
        if site is not None:
            menu.addAction(icon("check-square"), f"Cocher tout « {site.nom} »",
                           lambda: self._cocher_tout(True, site_id))
        menu.addAction(icon("square"), "Tout décocher", self._tout_decocher)
        menu.addSeparator()

        if self._remote is not None:
            # mode serveur : l'édition passe par l'administration / Configuration
            menu.addAction(icon("settings"), "Configuration…",
                           self._ouvrir_configuration)
            menu.exec(self._tree.viewport().mapToGlobal(pos))
            return

        if cam is not None:
            menu.addAction(icon("pencil"), f"Modifier « {cam.nom} »…",
                           lambda: self._modifier_camera(cam_id))
            menu.addAction(icon("trash"), "Supprimer cette caméra",
                           lambda: self._supprimer_camera(cam_id))
            menu.addSeparator()
        if site is not None:
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
        from .toast import toast
        toast(self, "Configuration enregistrée")

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
        if self._remote is None:
            self._save_timer.start()      # enregistrement différé (mode autonome)

    def _rotation_tick(self):
        if self._seq is not None or self._act_pause.isChecked():
            return
        if QApplication.activeModalWidget() is not None:
            return      # ne pas changer de vue (ni ouvrir de mainstream) derrière
                        # un dialogue d'édition ; le tick suivant reprendra
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
            self._maj_nav()
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
        # retour à l'affichage piloté par la sélection — la grille affichée par
        # la dernière étape de la boucle ne correspond pas aux caméras cochées :
        # forcer la reconstruction, sinon on relançait les caméras de la boucle
        self._grid_dirty = True
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
        # ignore les étapes sans caméra (donnée incohérente reçue du serveur :
        # schéma plus ancien, édition manuelle, migration) plutôt que de planter
        # sur un IndexError pendant la lecture de la ronde
        for _ in range(len(etapes)):
            self._seq_idx = (self._seq_idx + 1) % len(etapes)
            if etapes[self._seq_idx].cameras:
                break
        else:
            self._seq_stop()                     # aucune étape exploitable
            return
        etape = etapes[self._seq_idx]

        if etape.mode == "mono":
            self._set_mono(etape.cameras[0])
        else:
            self._leave_mono()
            self._stack.setCurrentWidget(self._grid_page)
            self._act_grid.setEnabled(False)
            self._set_grid(etape.cameras)
        self._maj_nav()
        self.statusBar().showMessage(
            f"Ronde {self._seq.nom} : étape {self._seq_idx + 1}/{len(etapes)}",
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
            if self._remote is not None:
                # mode serveur : les événements arrivent du serveur (SSE)
                from ..remote import EcouteurMouvement
                self._motion = EcouteurMouvement(self._remote, self)
            else:
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
        if actif and not self._act_motion.isChecked():
            return      # événement livré après désactivation (signal queued d'un
                        # thread de tirage encore en vol) : ne pas surligner
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
        # mémorise la géométrie « normale » pour la retrouver intacte en sortie
        # (uniquement au 1er passage : changer d'écran en plein écran ne doit pas
        #  l'écraser)
        if not self.isFullScreen():
            self._geom_normale = self.saveGeometry()
        self._topbar.hide()
        self._sidebar.hide()
        self.statusBar().hide()
        # Cible l'écran voulu en plaçant d'abord la fenêtre BIEN À L'INTÉRIEUR
        # de cet écran (à une taille qui rentre, cadre compris) : ainsi elle est
        # forcément visible sur le bon écran et Windows n'émet pas l'avertissement
        # « Unable to set geometry ». showFullScreen() la déploie ensuite sur cet
        # écran. (Imposer directement 1920×1080 à la fenêtre encadrée la faisait
        # dépasser l'écran ; un simple move() vers le coin pouvait la faire sortir
        # de la zone visible et « disparaître ».)
        if self.isFullScreen():
            self.showNormal()                    # requis pour changer d'écran
        g = ecran.geometry()
        larg = min(self.width(), g.width() - 40)
        haut = min(self.height(), g.height() - 80)
        self.setGeometry(g.x() + (g.width() - larg) // 2,
                         g.y() + (g.height() - haut) // 2, larg, haut)
        # associe explicitement la fenêtre à l'écran cible (fiable en multi-DPI)
        poignee = self.windowHandle()
        if poignee is not None:
            poignee.setScreen(ecran)
        self.showFullScreen()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            geo = getattr(self, "_geom_normale", None)
            if geo is not None:
                self.restoreGeometry(geo)
            self._assurer_visible()
            self._topbar.show()
            self._sidebar.show()
            self.statusBar().show()
        else:
            self._fullscreen_sur(self.screen())

    def _assurer_visible(self):
        """Garantit que la barre de titre reste dans la zone visible d'un écran
        (utile si la configuration d'écrans a changé depuis la dernière fois)."""
        ecran = self.screen() or QGuiApplication.primaryScreen()
        if ecran is None:
            return
        dispo = ecran.availableGeometry()
        cadre = self.frameGeometry()
        if dispo.contains(cadre.topLeft()) and dispo.contains(cadre.topRight()):
            return                               # barre de titre déjà visible
        larg = min(self.width(), dispo.width())
        haut = min(self.height(), dispo.height())
        self.resize(larg, haut)
        self.move(dispo.left() + (dispo.width() - larg) // 2,
                  dispo.top() + (dispo.height() - haut) // 2)

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
        self._session_timer.stop()
        if self._motion is not None:
            self._motion.stop()
        if self._save_timer.isActive():
            self._save_timer.stop()
            if self._remote is None:          # jamais la projection serveur
                save_config(self._cfg)
        # fermeture : libération SYNCHRONE (le processus s'arrête juste après,
        # la libération en arrière-plan de dispose() n'aurait pas le temps)
        if self._mono_tile is not None:
            self._mono_tile.shutdown()
            self._mono_tile = None
        for tile in self._tiles.values():
            tile.shutdown()
        self._tiles.clear()
        # attend (borné) les libérations mpv lancées par des dispose() récents :
        # sortir du process pendant un terminate() laissait mpv en course avec
        # la destruction des fenêtres natives
        from .tile import attendre_liberations
        attendre_liberations(5.0)
        super().closeEvent(event)
