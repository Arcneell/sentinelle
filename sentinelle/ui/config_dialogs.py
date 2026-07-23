"""Configuration 100 % par l'interface : sites, caméras, DVR entiers.

- ConfigDialog : le gestionnaire ⚙ (arbre sites/caméras + boutons)
- DvrDialog    : ajoute toutes les caméras d'un DVR d'un coup — interroge
                 l'ISAPI Hikvision pour découvrir canaux et noms, ou génère
                 la liste manuellement (Dahua / ISAPI indisponible)
- CameraDialog : ajout/édition d'une caméra
- SiteDialog   : ajout/édition d'un site

Les modifications sont appliquées à l'AppConfig en mémoire ; la fenêtre
principale enregistre (save_config) si l'utilisateur valide, ou recharge
depuis le disque s'il annule.
"""

import socket
import threading
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..config import (LIENS, MARQUE_LABELS, MARQUES, MARQUES_URL_LIBRE,
                      PROFIL_LABELS, PROFILS, AppConfig, Camera, Site,
                      purger_cameras_sequences)
from ..snapshot import lister_canaux_hikvision
from .icons import icon
from .texte import compte


def _port_ouvert(host: str, port: int, timeout: float = 3.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()

_PROFILS_ORDONNES = list(PROFILS)


def _profil_defaut(site: Site | None) -> str:
    return "eco" if (site and site.lien == "4g") else "normal"


# ------------------------------------------------------------------ SiteDialog

class SiteDialog(QDialog):
    def __init__(self, parent=None, site: Site | None = None):
        super().__init__(parent)
        self.setWindowTitle("Site" if site else "Nouveau site")
        self._nom = QLineEdit(site.nom if site else "")
        self._nom.setPlaceholderText("ex. Le Port")
        self._lien = QComboBox()
        self._lien.addItems(["Fibre / réseau filaire", "4G (bande passante limitée)"])
        if site and site.lien == "4g":
            self._lien.setCurrentIndex(1)

        form = QFormLayout()
        form.addRow("Nom du site :", self._nom)
        form.addRow("Type de lien :", self._lien)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Valider")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(boutons)

    def _valider(self):
        if not self._nom.text().strip():
            QMessageBox.warning(self, "Site", "Le nom est obligatoire.")
            return
        self.accept()

    def valeurs(self) -> tuple[str, str]:
        return self._nom.text().strip(), LIENS[self._lien.currentIndex()]


# ---------------------------------------------------------------- CameraDialog

class CameraDialog(QDialog):
    """Ajout ou édition d'une caméra unique."""

    _test_done = Signal(str)
    _onvif_done = Signal(object, str)       # OnvifResult|None, erreur

    def __init__(self, cfg: AppConfig, parent=None, camera: Camera | None = None,
                 site_defaut: Site | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self._camera = camera
        self._onvif_profile = camera.onvif_profile if camera else ""
        self._onvif_ptz = camera.ptz if camera else False
        self.setWindowTitle("Caméra" if camera else "Nouvelle caméra")
        self.setMinimumWidth(460)

        self._nom = QLineEdit(camera.nom if camera else "")
        self._nom.setPlaceholderText("ex. Parking entrée")

        self._site = QComboBox()
        for s in cfg.sites:
            self._site.addItem(f"{s.nom}{' · 4G' if s.lien == '4g' else ''}", s.id)
        cible = camera.site if camera else site_defaut
        if cible:
            idx = self._site.findData(cible.id)
            if idx >= 0:
                self._site.setCurrentIndex(idx)

        self._profil = QComboBox()
        for p in _PROFILS_ORDONNES:
            self._profil.addItem(PROFIL_LABELS[p], p)
        self._profil.setCurrentIndex(_PROFILS_ORDONNES.index(
            camera.profil if camera else _profil_defaut(cible)))

        self._marque = QComboBox()
        for m in MARQUES:
            self._marque.addItem(MARQUE_LABELS[m], m)
        if camera:
            self._marque.setCurrentIndex(MARQUES.index(camera.marque))

        # bloc DVR (hikvision/dahua)
        self._hote = QLineEdit(camera.hote if camera else "")
        self._hote.setPlaceholderText("IP ou nom d'hôte du DVR")
        self._port = QSpinBox(); self._port.setRange(1, 65535)
        self._port.setValue(camera.port if camera else 554)
        self._canal = QSpinBox(); self._canal.setRange(1, 512)
        self._canal.setValue(camera.canal if camera else 1)
        self._port_http = QSpinBox(); self._port_http.setRange(1, 65535)
        self._port_http.setValue(camera.port_http if camera else 80)
        self._grp_dvr = QGroupBox("Connexion DVR")
        f1 = QFormLayout(self._grp_dvr)
        f1.addRow("Adresse :", self._hote)
        f1.addRow("Port RTSP :", self._port)
        f1.addRow("Canal :", self._canal)
        f1.addRow("Port HTTP (photo) :", self._port_http)

        # bloc custom
        self._url_main = QLineEdit(camera.url_mainstream if camera else "")
        self._url_main.setPlaceholderText("rtsp://…  (flux HD)")
        self._url_sub = QLineEdit(camera.url_substream if camera else "")
        self._url_sub.setPlaceholderText("rtsp://…  (flux léger)")
        self._url_snap = QLineEdit(camera.url_snapshot if camera else "")
        self._url_snap.setPlaceholderText("http://…  (image JPEG, pour le mode photo)")
        self._grp_custom = QGroupBox("URLs directes")
        f2 = QFormLayout(self._grp_custom)
        f2.addRow("URL mainstream :", self._url_main)
        f2.addRow("URL substream :", self._url_sub)
        f2.addRow("URL snapshot :", self._url_snap)

        self._user = QLineEdit(camera.user if camera else "")
        # Le mot de passe n'est JAMAIS réaffiché : à l'édition, le champ reste
        # vide et le mot de passe existant est conservé si on n'en saisit pas
        # un nouveau. Pas de bouton « afficher ».
        self._pwd_existe = bool(camera and camera.password)
        self._pwd = QLineEdit()
        self._pwd.setEchoMode(QLineEdit.Password)
        if self._pwd_existe:
            self._pwd.setPlaceholderText("Laisser vide pour conserver le mot de passe actuel")
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(self._pwd, 1)

        self._photo_int = QSpinBox(); self._photo_int.setRange(2, 600)
        self._photo_int.setSuffix(" s")
        self._photo_int.setValue(camera.photo_intervalle_s if camera else 10)
        self._lbl_photo = QLabel("Rafraîchissement photo :")

        form = QFormLayout()
        form.addRow("Nom :", self._nom)
        form.addRow("Site :", self._site)
        form.addRow("Marque :", self._marque)
        form.addRow("Profil bande passante :", self._profil)
        form.addRow("Utilisateur DVR :", self._user)
        form.addRow("Mot de passe :", pwd_row)
        form.addRow(self._lbl_photo, self._photo_int)

        # actions : résolution ONVIF + test de connexion
        self._btn_onvif = QPushButton(icon("search"), " ONVIF : récupérer les flux")
        self._btn_onvif.clicked.connect(self._resoudre_onvif)
        self._btn_test = QPushButton("Tester la connexion")
        self._btn_test.clicked.connect(self._tester_connexion)
        ligne_actions = QHBoxLayout()
        ligne_actions.addWidget(self._btn_onvif)
        ligne_actions.addWidget(self._btn_test)
        self._statut = QLabel("")
        self._statut.setWordWrap(True)
        self._statut.setObjectName("hint")

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Valider")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self._grp_dvr)
        lay.addWidget(self._grp_custom)
        lay.addLayout(ligne_actions)
        lay.addWidget(self._statut)
        lay.addWidget(boutons)

        self._marque.currentIndexChanged.connect(self._maj_visibilite)
        self._profil.currentIndexChanged.connect(self._maj_visibilite)
        self._site.currentIndexChanged.connect(self._site_change)
        self._test_done.connect(self._on_test_done)
        self._onvif_done.connect(self._on_onvif_done)
        self._maj_visibilite()

    def _marque_cle(self) -> str:
        return self._marque.currentData()

    def _site_change(self):
        # nouveau site choisi → profil par défaut selon son lien (à la création)
        if self._camera is None:
            site = self._cfg.site(self._site.currentData())
            self._profil.setCurrentIndex(_PROFILS_ORDONNES.index(_profil_defaut(site)))

    def _maj_visibilite(self):
        marque = self._marque_cle()
        url_libre = marque in MARQUES_URL_LIBRE
        onvif = marque == "onvif"
        # ONVIF a besoin de l'hôte pour résoudre, mais stocke des URLs
        self._grp_dvr.setVisible(not url_libre or onvif)
        self._grp_custom.setVisible(url_libre)
        self._grp_custom.setTitle("URLs résolues (ONVIF)" if onvif else "URLs directes")
        self._btn_onvif.setVisible(onvif)
        photo = _PROFILS_ORDONNES[self._profil.currentIndex()] == "eco-extreme"
        self._lbl_photo.setVisible(photo)
        self._photo_int.setVisible(photo)
        self.adjustSize()

    # ------------------------------------------------------- test / ONVIF

    def _cible_test(self) -> tuple[str, int]:
        """(host, port) à tester selon la marque."""
        if self._marque_cle() in MARQUES_URL_LIBRE:
            u = self._url_main.text().strip() or self._url_sub.text().strip()
            p = urlparse(u)
            return p.hostname or "", p.port or 554
        return self._hote.text().strip(), self._port.value()

    def _tester_connexion(self):
        host, port = self._cible_test()
        if not host:
            QMessageBox.warning(self, "Test", "Renseignez l'adresse (ou une URL).")
            return
        self._btn_test.setEnabled(False)
        self._statut.setText(f"Test de {host}:{port}…")

        def work():
            ok = _port_ouvert(host, port)
            try:
                self._test_done.emit(
                    f"✓ {host}:{port} joignable (port RTSP ouvert)" if ok
                    else f"✗ {host}:{port} injoignable (IP/port/pare-feu ?)")
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, msg: str):
        self._btn_test.setEnabled(True)
        self._statut.setText(msg)

    def _resoudre_onvif(self):
        host = self._hote.text().strip()
        if not host:
            QMessageBox.warning(self, "ONVIF", "Renseignez l'adresse de la caméra.")
            return
        self._btn_onvif.setEnabled(False)
        self._statut.setText("Interrogation ONVIF…")
        args = (host, self._user.text(), self._pwd.text(), self._port_http.value())

        def work():
            from ..onvif import OnvifCamera
            try:
                res = OnvifCamera(args[0], args[1], args[2], port=args[3]).profils()
            except Exception as e:
                res, err = None, str(e)
            else:
                err = ""
            try:
                self._onvif_done.emit(res, err)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_onvif_done(self, res, erreur: str):
        self._btn_onvif.setEnabled(True)
        if res is None or (res is not None and not res.ok):
            self._statut.setText("ONVIF : " + (erreur or (res.erreur if res else "échec")))
            return
        main, sub = res.principal(), res.secondaire()
        self._url_main.setText(main.rtsp)
        self._url_sub.setText(sub.rtsp if sub else main.rtsp)
        self._url_snap.setText(res.snapshot)
        self._onvif_profile = main.token
        self._onvif_ptz = main.ptz
        ptz_txt = " · PTZ détecté" if main.ptz else ""
        self._statut.setText(
            f"✓ {main.largeur}×{main.hauteur} (HD) + "
            f"{sub.largeur}×{sub.hauteur if sub else ''} (sub){ptz_txt}")

    def _valider(self):
        if not self._nom.text().strip():
            QMessageBox.warning(self, "Caméra", "Le nom est obligatoire.")
            return
        if self._site.currentData() is None:
            QMessageBox.warning(self, "Caméra", "Créez d'abord un site.")
            return
        url_libre = self._marque_cle() in MARQUES_URL_LIBRE
        if url_libre and not (self._url_main.text().strip() or self._url_sub.text().strip()):
            msg = ("ONVIF : cliquez « récupérer les flux » d'abord."
                   if self._marque_cle() == "onvif"
                   else "Renseignez au moins une URL RTSP (main ou sub).")
            QMessageBox.warning(self, "Caméra", msg)
            return
        if not url_libre and not self._hote.text().strip():
            QMessageBox.warning(self, "Caméra", "L'adresse du DVR est obligatoire.")
            return
        self.accept()

    def appliquer(self) -> Camera:
        """Crée ou met à jour la caméra à partir du formulaire."""
        site = self._cfg.site(self._site.currentData())
        cam = self._camera
        if cam is None:
            cam = Camera(id=self._cfg.unique_id(f"{site.id}-{self._nom.text()}"),
                         nom="", site=site)
            self._cfg.cameras.append(cam)
        cam.nom = self._nom.text().strip()
        cam.site = site
        cam.profil = _PROFILS_ORDONNES[self._profil.currentIndex()]
        cam.marque = self._marque_cle()
        cam.hote = self._hote.text().strip()
        cam.port = self._port.value()
        cam.canal = self._canal.value()
        cam.port_http = self._port_http.value()
        cam.user = self._user.text()
        # champ vide à l'édition = on garde le mot de passe existant
        nouveau_mdp = self._pwd.text()
        if nouveau_mdp or not self._pwd_existe:
            cam.password = nouveau_mdp
        cam.url_mainstream = self._url_main.text().strip()
        cam.url_substream = self._url_sub.text().strip()
        cam.url_snapshot = self._url_snap.text().strip()
        cam.photo_intervalle_s = self._photo_int.value()
        cam.ptz = self._onvif_ptz if cam.marque == "onvif" else False
        cam.onvif_profile = self._onvif_profile if cam.marque == "onvif" else ""
        return cam


# ------------------------------------------------------------------- DvrDialog

class DvrDialog(QDialog):
    """Ajoute toutes les caméras d'un DVR en une fois."""

    _canaux_prets = Signal(list, str)    # [(canal, nom)], erreur

    def __init__(self, cfg: AppConfig, parent=None, site_defaut: Site | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self.cameras_creees: list[Camera] = []
        self.setWindowTitle("Ajouter un DVR")
        self.setMinimumSize(560, 560)

        self._site = QComboBox()
        for s in cfg.sites:
            self._site.addItem(f"{s.nom}{' · 4G' if s.lien == '4g' else ''}", s.id)
        if site_defaut:
            idx = self._site.findData(site_defaut.id)
            if idx >= 0:
                self._site.setCurrentIndex(idx)

        self._marque = QComboBox()
        for m in MARQUES:
            if m not in MARQUES_URL_LIBRE:      # marques à gabarit (canal)
                self._marque.addItem(MARQUE_LABELS[m], m)
        self._hote = QLineEdit()
        self._hote.setPlaceholderText("IP ou nom d'hôte du DVR")
        self._port = QSpinBox(); self._port.setRange(1, 65535); self._port.setValue(554)
        self._port_http = QSpinBox(); self._port_http.setRange(1, 65535)
        self._port_http.setValue(80)
        self._user = QLineEdit()
        self._pwd = QLineEdit(); self._pwd.setEchoMode(QLineEdit.Password)
        self._profil = QComboBox()
        for p in _PROFILS_ORDONNES:
            self._profil.addItem(PROFIL_LABELS[p], p)
        self._profil.setCurrentIndex(_PROFILS_ORDONNES.index(
            _profil_defaut(cfg.site(self._site.currentData()))))
        self._site.currentIndexChanged.connect(lambda: self._profil.setCurrentIndex(
            _PROFILS_ORDONNES.index(_profil_defaut(cfg.site(self._site.currentData())))))

        form = QGridLayout()
        form.addWidget(QLabel("Site :"), 0, 0);        form.addWidget(self._site, 0, 1)
        form.addWidget(QLabel("Marque :"), 0, 2);      form.addWidget(self._marque, 0, 3)
        form.addWidget(QLabel("Adresse :"), 1, 0);     form.addWidget(self._hote, 1, 1)
        form.addWidget(QLabel("Port RTSP :"), 1, 2);   form.addWidget(self._port, 1, 3)
        form.addWidget(QLabel("Utilisateur :"), 2, 0); form.addWidget(self._user, 2, 1)
        form.addWidget(QLabel("Mot de passe :"), 2, 2); form.addWidget(self._pwd, 2, 3)
        form.addWidget(QLabel("Port HTTP :"), 3, 0);   form.addWidget(self._port_http, 3, 1)
        form.addWidget(QLabel("Profil :"), 3, 2);      form.addWidget(self._profil, 3, 3)

        self._btn_scan = QPushButton(icon("search"), " Interroger le DVR (canaux + noms)")
        self._btn_scan.clicked.connect(self._scanner)
        self._nb = QSpinBox(); self._nb.setRange(1, 64); self._nb.setValue(8)
        btn_manuel = QPushButton("Générer la liste")
        btn_manuel.clicked.connect(self._generer_manuel)
        ligne_scan = QHBoxLayout()
        ligne_scan.addWidget(self._btn_scan, 2)
        ligne_scan.addSpacing(12)
        ligne_scan.addWidget(QLabel("ou, manuellement :"))
        ligne_scan.addWidget(self._nb)
        ligne_scan.addWidget(QLabel("canaux"))
        ligne_scan.addWidget(btn_manuel, 1)

        self._statut = QLabel("")
        self._statut.setObjectName("hint")

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["", "Canal", "Nom de la caméra"])
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.verticalHeader().hide()
        self._table.setColumnWidth(0, 30)
        self._table.setColumnWidth(1, 60)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Ajouter les caméras cochées")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(ligne_scan)
        lay.addWidget(self._statut)
        lay.addWidget(self._table, 1)
        lay.addWidget(boutons)

        self._canaux_prets.connect(self._afficher_canaux)
        self._marque.currentIndexChanged.connect(
            lambda: self._btn_scan.setEnabled(self._marque.currentData() == "hikvision"))

    # ------------------------------------------------------------- découverte

    def _scanner(self):
        hote = self._hote.text().strip()
        if not hote:
            QMessageBox.warning(self, "DVR", "Renseignez l'adresse du DVR.")
            return
        self._btn_scan.setEnabled(False)
        self._statut.setText("Interrogation du DVR…")
        args = (hote, self._port_http.value(), self._user.text(), self._pwd.text())

        def work():
            canaux, err = lister_canaux_hikvision(*args)
            try:
                self._canaux_prets.emit(canaux, err)
            except RuntimeError:
                pass

        threading.Thread(target=work, daemon=True, name="scan-dvr").start()

    def _afficher_canaux(self, canaux: list, erreur: str):
        self._btn_scan.setEnabled(self._marque.currentData() == "hikvision")
        if erreur:
            self._statut.setText(f"Échec : {erreur}. Utilisez la liste manuelle.")
            return
        self._statut.setText(f"{compte(len(canaux), 'canal trouvé', 'canaux trouvés')}. "
                             "Décochez ceux à ignorer.")
        self._remplir_table(canaux)

    def _generer_manuel(self):
        self._statut.setText("Liste générée. Décochez les canaux inutilisés.")
        self._remplir_table([(i, f"Caméra {i}") for i in range(1, self._nb.value() + 1)])

    def _remplir_table(self, canaux: list):
        self._table.setRowCount(0)
        for canal, nom in canaux:
            r = self._table.rowCount()
            self._table.insertRow(r)
            coche = QTableWidgetItem()
            coche.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            coche.setCheckState(Qt.Checked)
            num = QTableWidgetItem(str(canal))
            num.setFlags(Qt.ItemIsEnabled)
            self._table.setItem(r, 0, coche)
            self._table.setItem(r, 1, num)
            self._table.setItem(r, 2, QTableWidgetItem(nom))

    # ------------------------------------------------------------- validation

    def _valider(self):
        if self._site.currentData() is None:
            QMessageBox.warning(self, "DVR", "Créez d'abord un site.")
            return
        if not self._hote.text().strip():
            QMessageBox.warning(self, "DVR", "L'adresse du DVR est obligatoire.")
            return
        lignes = [r for r in range(self._table.rowCount())
                  if self._table.item(r, 0).checkState() == Qt.Checked]
        if not lignes:
            QMessageBox.warning(self, "DVR", "Aucun canal coché.")
            return

        site = self._cfg.site(self._site.currentData())
        marque = self._marque.currentData()
        taken = {s.id for s in self._cfg.sites} | {c.id for c in self._cfg.cameras}
        for r in lignes:
            canal = int(self._table.item(r, 1).text())
            nom = self._table.item(r, 2).text().strip() or f"Caméra {canal}"
            cam_id = self._cfg.unique_id(f"{site.id}-{nom}", taken)
            taken.add(cam_id)
            cam = Camera(
                id=cam_id, nom=nom, site=site,
                profil=self._profil.currentData(),
                marque=marque,
                hote=self._hote.text().strip(),
                port=self._port.value(),
                canal=canal,
                port_http=self._port_http.value(),
                user=self._user.text(),
                password=self._pwd.text(),
            )
            self._cfg.cameras.append(cam)
            self.cameras_creees.append(cam)
        self.accept()


# --------------------------------------------------------------- OnvifScanDialog

class OnvifScanDialog(QDialog):
    """Découvre les caméras ONVIF du réseau et les ajoute (toutes marques)."""

    _devices_prets = Signal(list)               # [OnvifDevice]
    _import_fini = Signal(list, list)           # [Camera], [messages d'échec]

    def __init__(self, cfg: AppConfig, parent=None, site_defaut: Site | None = None):
        super().__init__(parent)
        self._cfg = cfg
        self.cameras_creees: list[Camera] = []
        self._devices = []
        self._annule = False            # évite d'injecter des caméras après annulation
        self._resolution = False        # évite le double-import
        self.setWindowTitle("Scan réseau ONVIF")
        self.setMinimumSize(560, 520)

        self._site = QComboBox()
        for s in cfg.sites:
            self._site.addItem(f"{s.nom}{' · 4G' if s.lien == '4g' else ''}", s.id)
        if site_defaut:
            i = self._site.findData(site_defaut.id)
            if i >= 0:
                self._site.setCurrentIndex(i)
        self._user = QLineEdit(); self._user.setPlaceholderText("identifiant commun")
        self._pwd = QLineEdit(); self._pwd.setEchoMode(QLineEdit.Password)

        form = QGridLayout()
        form.addWidget(QLabel("Site :"), 0, 0);         form.addWidget(self._site, 0, 1)
        form.addWidget(QLabel("Utilisateur :"), 1, 0);  form.addWidget(self._user, 1, 1)
        form.addWidget(QLabel("Mot de passe :"), 2, 0); form.addWidget(self._pwd, 2, 1)

        self._btn_scan = QPushButton(icon("search"), " Rechercher les caméras ONVIF")
        self._btn_scan.clicked.connect(self._scanner)
        self._statut = QLabel("Recherche des caméras ONVIF présentes sur le réseau local.")
        self._statut.setWordWrap(True)
        self._statut.setObjectName("hint")

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["", "Nom", "Adresse"])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.verticalHeader().hide()
        self._table.setColumnWidth(0, 30)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Ajouter les caméras cochées")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self._btn_scan)
        lay.addWidget(self._statut)
        lay.addWidget(self._table, 1)
        lay.addWidget(boutons)

        self._boutons = boutons
        self._devices_prets.connect(self._afficher_devices)
        self._import_fini.connect(self._on_import_fini)
        QTimer.singleShot(150, self._scanner)   # scan auto à l'ouverture

    def reject(self):
        self._annule = True                     # un import en vol ne sera pas appliqué
        super().reject()

    def _scanner(self):
        self._btn_scan.setEnabled(False)
        self._statut.setText("Recherche ONVIF en cours (WS-Discovery)…")

        def work():
            from ..onvif import discover
            try:
                devs = discover(timeout=4.0)
            except Exception:
                devs = []
            try:
                self._devices_prets.emit(devs)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True, name="onvif-discover").start()

    def _afficher_devices(self, devices: list):
        if self._annule:
            return
        self._btn_scan.setEnabled(True)
        self._devices = devices
        self._table.setRowCount(0)
        if not devices:
            self._statut.setText("Aucune caméra détectée. Vérifiez que l'ONVIF est activé "
                                 "et que vous êtes sur le même réseau.")
            return
        self._statut.setText(f"{compte(len(devices), 'caméra trouvée', 'caméras trouvées')}. "
                             "Renseignez les identifiants puis validez.")
        for dev in devices:
            r = self._table.rowCount()
            self._table.insertRow(r)
            coche = QTableWidgetItem()
            coche.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            coche.setCheckState(Qt.Checked)
            self._table.setItem(r, 0, coche)
            self._table.setItem(r, 1, QTableWidgetItem(dev.nom))
            hote = QTableWidgetItem(dev.host)
            hote.setFlags(Qt.ItemIsEnabled)
            self._table.setItem(r, 2, hote)

    def _valider(self):
        if self._resolution:                 # évite le double-import (double-clic)
            return
        if self._site.currentData() is None:
            QMessageBox.warning(self, "ONVIF", "Créez d'abord un site.")
            return
        choisis = [self._devices[r] for r in range(self._table.rowCount())
                   if self._table.item(r, 0).checkState() == Qt.Checked]
        if not choisis:
            QMessageBox.warning(self, "ONVIF", "Aucune caméra cochée.")
            return
        self._resolution = True
        self._btn_scan.setEnabled(False)
        self._boutons.button(QDialogButtonBox.Ok).setEnabled(False)
        self._statut.setText(f"Résolution des flux de {compte(len(choisis), 'caméra')}…")
        site = self._cfg.site(self._site.currentData())
        user, pwd = self._user.text(), self._pwd.text()
        # noms saisis dans la table (peuvent avoir été édités)
        noms = {self._devices[r].xaddr: self._table.item(r, 1).text().strip()
                for r in range(self._table.rowCount())}

        def work():
            from ..onvif import OnvifCamera
            crees, echecs = [], []
            taken = {s.id for s in self._cfg.sites} | {c.id for c in self._cfg.cameras}
            for dev in choisis:
                nom = noms.get(dev.xaddr) or dev.nom
                try:
                    res = OnvifCamera(dev.host, user, pwd,
                                      device_xaddr=dev.xaddr).profils()
                except Exception as e:
                    echecs.append(f"{nom} : {e}"); continue
                if not res.ok:
                    echecs.append(f"{nom} : {res.erreur}"); continue
                main, sub = res.principal(), res.secondaire()
                cam_id = self._cfg.unique_id(f"{site.id}-{nom}", taken)
                taken.add(cam_id)
                crees.append(Camera(
                    id=cam_id, nom=nom, site=site, marque="onvif",
                    profil=_profil_defaut(site),
                    hote=dev.host, port_http=80, user=user, password=pwd,
                    url_mainstream=main.rtsp,
                    url_substream=sub.rtsp if sub else main.rtsp,
                    url_snapshot=res.snapshot,
                    ptz=main.ptz, onvif_profile=main.token,
                ))
            try:
                self._import_fini.emit(crees, echecs)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True, name="onvif-resolve").start()

    def _on_import_fini(self, crees: list, echecs: list):
        self._resolution = False
        if self._annule:                 # annulé pendant la résolution → ne rien ajouter
            return
        self._btn_scan.setEnabled(True)
        self._boutons.button(QDialogButtonBox.Ok).setEnabled(True)
        self._cfg.cameras.extend(crees)
        self.cameras_creees = crees
        if crees and not echecs:
            self.accept()
            return
        if not crees:
            self._statut.setText("Échec : " + " | ".join(echecs[:4]))
            return
        QMessageBox.warning(self, "ONVIF",
                            f"{len(crees)} caméra(s) ajoutée(s).\n\nÉchecs :\n"
                            + "\n".join(echecs[:10]))
        self.accept()


# --------------------------------------------------------- CameraManagerWidget

class CameraManagerWidget(QWidget):
    """Arbre sites/caméras + boutons d'ajout, édition, suppression.

    Réutilisé par la Configuration en mode autonome et par le panneau
    d'administration en mode serveur. Opère directement sur l'AppConfig fourni
    et signale toute modification via l'attribut `modifie`."""

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self.modifie = False

        btn_dvr = QPushButton(icon("plus"), " DVR complet")
        btn_dvr.setToolTip("Ajouter d'un coup toutes les caméras d'un enregistreur")
        btn_dvr.clicked.connect(self._ajouter_dvr)
        btn_cam = QPushButton(icon("plus"), " Caméra")
        btn_cam.clicked.connect(self._ajouter_camera)
        btn_site = QPushButton(icon("plus"), " Site")
        btn_site.clicked.connect(self._ajouter_site)
        btn_onvif = QPushButton(icon("search"), " Recherche ONVIF")
        btn_onvif.setToolTip("Détecter les caméras ONVIF présentes sur le réseau")
        btn_onvif.clicked.connect(self._scan_onvif)
        barre = QHBoxLayout()
        barre.setSpacing(6)
        for b in (btn_dvr, btn_cam, btn_site, btn_onvif):
            barre.addWidget(b)
        barre.addStretch(1)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Sites et caméras", "Détails"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._tree.itemDoubleClicked.connect(lambda *_: self._modifier())
        self._tree.itemSelectionChanged.connect(self._maj_boutons)

        self._btn_edit = QPushButton(icon("pencil"), " Modifier")
        self._btn_edit.clicked.connect(self._modifier)
        self._btn_del = QPushButton(icon("trash"), " Supprimer")
        self._btn_del.clicked.connect(self._supprimer)
        edition = QHBoxLayout()
        edition.addStretch(1)
        edition.addWidget(self._btn_edit)
        edition.addWidget(self._btn_del)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(barre)
        lay.addWidget(self._tree, 1)
        lay.addLayout(edition)

        self._maj_boutons()
        self.rafraichir()

    def _maj_boutons(self):
        actif = self._selection() is not None
        self._btn_edit.setEnabled(actif)
        self._btn_del.setEnabled(actif)

    def rafraichir(self, selection: tuple | None = None):
        """Reconstruit l'arbre SANS perdre le contexte de lecture : sites
        pliés/dépliés, position de défilement et sélection sont préservés
        (auparavant tout se redéployait et il fallait re-scroller à chaque
        ajout). `selection` = ("site"|"camera", id) sélectionne et montre un
        élément précis, typiquement celui qui vient d'être créé."""
        premier = self._tree.topLevelItemCount() == 0
        deplies = {self._tree.topLevelItem(i).data(0, Qt.UserRole)[1]
                   for i in range(self._tree.topLevelItemCount())
                   if self._tree.topLevelItem(i).isExpanded()}
        if selection is None:
            selection = self._selection()
        scroll = self._tree.verticalScrollBar().value()

        self._tree.clear()
        a_montrer = None
        for site in self._cfg.sites:
            it = QTreeWidgetItem([site.nom, "site 4G" if site.lien == "4g" else "site fibre"])
            it.setData(0, Qt.UserRole, ("site", site.id))
            if selection == ("site", site.id):
                a_montrer = it
            for cam in [c for c in self._cfg.cameras if c.site.id == site.id]:
                detail = (f"{cam.marque} · {cam.hote or 'URL libre'}"
                          + (f" · canal {cam.canal}" if cam.marque != "custom" else "")
                          + f" · {cam.profil}")
                child = QTreeWidgetItem([cam.nom, detail])
                child.setData(0, Qt.UserRole, ("camera", cam.id))
                it.addChild(child)
                if selection == ("camera", cam.id):
                    a_montrer = child
            self._tree.addTopLevelItem(it)
            it.setExpanded(premier or site.id in deplies)

        self._tree.verticalScrollBar().setValue(scroll)
        if a_montrer is not None:
            parent = a_montrer.parent()
            if parent is not None:
                parent.setExpanded(True)
            self._tree.setCurrentItem(a_montrer)
            self._tree.scrollToItem(a_montrer)

    def _selection(self):
        it = self._tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _site_selectionne(self) -> Site | None:
        sel = self._selection()
        if not sel:
            return None
        if sel[0] == "site":
            return self._cfg.site(sel[1])
        cam = self._cfg.camera(sel[1])
        return cam.site if cam else None

    def _exiger_site(self) -> bool:
        if self._cfg.sites:
            return True
        QMessageBox.information(self, "Configuration",
                                "Commencez par créer un site (bouton « Site »).")
        return False

    def _ajouter_site(self):
        dlg = SiteDialog(self)
        if dlg.exec():
            nom, lien = dlg.valeurs()
            site = Site(id=self._cfg.unique_id(nom), nom=nom, lien=lien)
            self._cfg.sites.append(site)
            self.modifie = True
            self.rafraichir(selection=("site", site.id))

    def _ajouter_dvr(self):
        if not self._exiger_site():
            return
        dlg = DvrDialog(self._cfg, self, site_defaut=self._site_selectionne())
        if dlg.exec():
            self.modifie = True
            crees = dlg.cameras_creees
            self.rafraichir(selection=("camera", crees[0].id) if crees else None)

    def _scan_onvif(self):
        if not self._exiger_site():
            return
        dlg = OnvifScanDialog(self._cfg, self, site_defaut=self._site_selectionne())
        if dlg.exec() and dlg.cameras_creees:
            self.modifie = True
            self.rafraichir(selection=("camera", dlg.cameras_creees[0].id))

    def _ajouter_camera(self):
        if not self._exiger_site():
            return
        dlg = CameraDialog(self._cfg, self, site_defaut=self._site_selectionne())
        if dlg.exec():
            cam = dlg.appliquer()
            self.modifie = True
            self.rafraichir(selection=("camera", cam.id))

    def _modifier(self):
        sel = self._selection()
        if not sel:
            return
        kind, ident = sel
        if kind == "site":
            site = self._cfg.site(ident)
            dlg = SiteDialog(self, site)
            if dlg.exec():
                site.nom, site.lien = dlg.valeurs()
                self.modifie = True
                self.rafraichir()
        else:
            cam = self._cfg.camera(ident)
            dlg = CameraDialog(self._cfg, self, camera=cam)
            if dlg.exec():
                dlg.appliquer()
                self.modifie = True
                self.rafraichir()

    def _supprimer(self):
        sel = self._selection()
        if not sel:
            return
        kind, ident = sel
        if kind == "site":
            site = self._cfg.site(ident)
            nb = len([c for c in self._cfg.cameras if c.site.id == ident])
            if QMessageBox.question(
                    self, "Supprimer",
                    f"Supprimer le site « {site.nom} » et ses {compte(nb, 'caméra')} ?"
            ) != QMessageBox.Yes:
                return
            retirees = {c.id for c in self._cfg.cameras if c.site.id == ident}
            self._cfg.cameras = [c for c in self._cfg.cameras if c.site.id != ident]
            self._cfg.sites = [s for s in self._cfg.sites if s.id != ident]
            purger_cameras_sequences(self._cfg, retirees)
        else:
            cam = self._cfg.camera(ident)
            if QMessageBox.question(self, "Supprimer",
                                    f"Supprimer la caméra « {cam.nom} » ?") != QMessageBox.Yes:
                return
            self._cfg.cameras = [c for c in self._cfg.cameras if c.id != ident]
            purger_cameras_sequences(self._cfg, {ident})
        self.modifie = True
        self.rafraichir()


# ---------------------------------------------------------------- ConfigDialog

class ConfigDialog(QDialog):
    """Configuration en mode autonome : caméras et réglages du poste.

    Le passage en mode serveur est protégé : il exige la connexion d'un compte
    administrateur (voir la fenêtre principale)."""

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self.modifie = False
        self.demande_serveur = False          # l'utilisateur veut passer en serveur
        self.setWindowTitle("Configuration")
        self.setWindowIcon(icon("settings"))
        self.setMinimumSize(720, 600)

        self._manager = CameraManagerWidget(cfg, self)

        self._rot = QSpinBox()
        self._rot.setRange(3, 3600)
        self._rot.setSuffix(" s")
        self._rot.setValue(cfg.rotation_duree_s)
        self._rot.setMaximumWidth(140)
        reglages = QGroupBox("Réglages généraux")
        rg = QFormLayout(reglages)
        rg.setContentsMargins(12, 8, 12, 8)
        rg.setHorizontalSpacing(18)
        rg.setLabelAlignment(Qt.AlignLeft)
        rg.addRow("Durée de rotation :", self._rot)

        # ronde lancée automatiquement à l'ouverture (réglage de CE poste) :
        # un mur d'images redémarré reprend sa ronde sans intervention
        from ..reglages import reglages as _reglages
        self._settings = _reglages()
        self._ronde_auto = QComboBox()
        self._ronde_auto.addItem("(aucune)", "")
        for s in cfg.sequences:
            self._ronde_auto.addItem(s.nom, s.nom)
        i = self._ronde_auto.findData(self._settings.value("ronde_auto", "", type=str))
        self._ronde_auto.setCurrentIndex(max(0, i))
        rg.addRow("Ronde au démarrage :", self._ronde_auto)

        # mode de fonctionnement (verrouillé : bascule réservée à un admin)
        mode = QGroupBox("Mode de fonctionnement")
        ml = QVBoxLayout(mode)
        ml.setContentsMargins(12, 8, 12, 8)
        btn_srv = QPushButton(icon("lock"), " Passer en mode serveur…")
        btn_srv.setToolTip("Nécessite un compte administrateur du serveur")
        btn_srv.clicked.connect(self._demander_serveur)
        ml.addWidget(btn_srv)

        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Enregistrer")
        boutons.button(QDialogButtonBox.Cancel).setText("Annuler")
        boutons.accepted.connect(self._terminer)
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(self._manager, 1)
        lay.addWidget(reglages)
        lay.addWidget(mode)
        lay.addWidget(boutons)

    def _demander_serveur(self):
        # la bascule elle-même (login admin) est gérée par la fenêtre principale
        self.demande_serveur = True
        self.done(2)                          # code distinct de Ok/Cancel

    def reject(self):
        # ne jamais jeter des modifications sans prévenir
        if self._manager.modifie or self._rot.value() != self._cfg.rotation_duree_s:
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

    def _terminer(self):
        if self._rot.value() != self._cfg.rotation_duree_s:
            self._cfg.rotation_duree_s = self._rot.value()
            self.modifie = True
        if self._manager.modifie:
            self.modifie = True
        self._settings.setValue("ronde_auto", self._ronde_auto.currentData())
        self.accept()


# ------------------------------------------------------------ PreferencesDialog

class PreferencesDialog(QDialog):
    """Préférences d'un poste connecté à un serveur : compte + déconnexion.

    La gestion du serveur (caméras, utilisateurs, mode) se fait dans le panneau
    d'administration, réservé aux comptes admin. Les boucles se modifient depuis
    le bouton « Boucles » de la barre du haut."""

    _mdp_termine = Signal(str)               # erreur "" | message — thread → UI

    def __init__(self, remote, parent=None, noms_rondes: list | None = None):
        super().__init__(parent)
        self._remote = remote
        self.deconnexion = False
        self._mdp_en_cours = False
        self._mdp_termine.connect(self._on_mdp_termine)
        self.setWindowTitle("Configuration")
        self.setWindowIcon(icon("settings"))
        self.setMinimumWidth(460)

        # réglage de CE poste : ronde lancée automatiquement à l'ouverture
        from ..reglages import reglages as _reglages
        self._settings = _reglages()
        poste = QGroupBox("Ce poste")
        pf = QFormLayout(poste)
        pf.setContentsMargins(12, 8, 12, 8)
        pf.setHorizontalSpacing(18)
        self._ronde_auto = QComboBox()
        self._ronde_auto.addItem("(aucune)", "")
        for nom in (noms_rondes or []):
            self._ronde_auto.addItem(nom, nom)
        i = self._ronde_auto.findData(self._settings.value("ronde_auto", "", type=str))
        self._ronde_auto.setCurrentIndex(max(0, i))
        self._ronde_auto.currentIndexChanged.connect(
            lambda: self._settings.setValue("ronde_auto", self._ronde_auto.currentData()))
        pf.addRow("Ronde au démarrage :", self._ronde_auto)

        compte = QGroupBox("Mon compte")
        cf = QFormLayout(compte)
        cf.setContentsMargins(12, 8, 12, 8)
        cf.setHorizontalSpacing(18)
        role = "administrateur" if remote.admin else "utilisateur"
        cf.addRow("Connecté en tant que :", QLabel(f"{remote.username} ({role})"))
        self._mdp_ancien = QLineEdit(); self._mdp_ancien.setEchoMode(QLineEdit.Password)
        self._mdp_nouv = QLineEdit(); self._mdp_nouv.setEchoMode(QLineEdit.Password)
        self._mdp_conf = QLineEdit(); self._mdp_conf.setEchoMode(QLineEdit.Password)
        cf.addRow("Mot de passe actuel :", self._mdp_ancien)
        cf.addRow("Nouveau mot de passe :", self._mdp_nouv)
        cf.addRow("Confirmer :", self._mdp_conf)
        btn_mdp = QPushButton("Changer mon mot de passe")
        btn_mdp.clicked.connect(self._changer_mdp)
        cf.addRow("", btn_mdp)

        btn_logout = QPushButton(icon("lock"), " Se déconnecter")
        btn_logout.clicked.connect(self._se_deconnecter)

        boutons = QDialogButtonBox(QDialogButtonBox.Close)
        boutons.button(QDialogButtonBox.Close).setText("Fermer")
        boutons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(poste)
        lay.addWidget(compte)
        lay.addWidget(btn_logout)
        lay.addStretch(1)
        lay.addWidget(boutons)

    def reject(self):
        # ne pas fermer pendant un changement de mot de passe en vol : sinon
        # _on_mdp_termine afficherait sa boîte sur un dialogue déjà fermé et les
        # champs ne seraient jamais vidés
        if self._mdp_en_cours:
            return
        super().reject()

    def _changer_mdp(self):
        if self._mdp_en_cours:               # ré-entrance : un envoi tourne déjà
            return
        ancien = self._mdp_ancien.text()
        nouveau = self._mdp_nouv.text()
        if len(nouveau) < 8:                 # même minimum que le serveur (MIN_MDP)
            QMessageBox.warning(self, "Mot de passe",
                                "Le nouveau mot de passe doit faire au moins 8 caractères.")
            return
        if nouveau != self._mdp_conf.text():
            QMessageBox.warning(self, "Mot de passe", "La confirmation ne correspond pas.")
            return
        # envoi hors du thread UI (gelait la fenêtre sur réseau lent)
        self._mdp_en_cours = True
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        remote = self._remote

        def work():
            from ..remote import ErreurServeur
            err, ok = "", False
            try:
                remote.changer_mot_de_passe(ancien, nouveau)
                ok = True
            except ErreurServeur as e:
                err = str(e) or "serveur injoignable"
            except Exception as e:
                err = str(e) or "erreur inattendue"
            finally:
                if not ok and not err:           # erreur hors Exception
                    err = "erreur inattendue"
                try:
                    self._mdp_termine.emit(err)  # dans finally : réactive toujours l'UI
                except RuntimeError:
                    pass
        threading.Thread(target=work, daemon=True, name="mdp").start()

    def _on_mdp_termine(self, err: str):
        QApplication.restoreOverrideCursor()
        self.setEnabled(True)
        self._mdp_en_cours = False
        if err:
            QMessageBox.warning(self, "Mot de passe", f"Échec : {err}")
            return
        for champ in (self._mdp_ancien, self._mdp_nouv, self._mdp_conf):
            champ.clear()
        QMessageBox.information(self, "Mot de passe", "Mot de passe modifié.")

    def _se_deconnecter(self):
        if self._mdp_en_cours:               # changement de mot de passe en vol
            return
        self.deconnexion = True
        self.accept()
