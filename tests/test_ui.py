"""Tests de fumée de l'interface (mode hors-écran) : les modules s'importent et
les fenêtres se construisent sans erreur. Ne vérifie pas le rendu, seulement
l'absence de régression à la construction."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
QSettings("Sentinelle", "viewer").setValue("mode", "local")

from sentinelle.ui.theme import apply_theme
apply_theme(app)


def test_imports():
    import sentinelle.ui.main_window          # noqa: F401
    import sentinelle.ui.admin_dialog         # noqa: F401
    import sentinelle.ui.login_dialog         # noqa: F401
    import sentinelle.ui.setup_dialog         # noqa: F401
    import sentinelle.remote                  # noqa: F401


def test_dialogues_se_construisent():
    from sentinelle.config import AppConfig, Camera, Site
    from sentinelle.ui.config_dialogs import (CameraDialog, ConfigDialog,
                                              SiteDialog)
    from sentinelle.ui.login_dialog import LoginDialog
    from sentinelle.ui.sequence_editor import SequenceEditor
    from sentinelle.ui.setup_dialog import SetupDialog

    cfg = AppConfig(path="")
    cfg.sites.append(Site(id="s1", nom="Site", lien="fibre"))
    cfg.cameras.append(Camera(id="c1", nom="Cam", site=cfg.sites[0],
                              marque="hikvision", hote="127.0.0.1", canal=1))
    ConfigDialog(cfg)
    SiteDialog()
    CameraDialog(cfg)
    SequenceEditor(cfg)
    SetupDialog()
    LoginDialog("http://x:8080", "u")


def test_fenetre_principale(tmp_path):
    from sentinelle.ui.main_window import MainWindow
    win = MainWindow(str(tmp_path / "config.yaml"))
    assert win._remote is None                # mode autonome
    assert win.demarrage_annule is False
    win.close()


def test_grille_redemarre_apres_mono(tmp_path):
    """Régression : une tuile conservée d'une étape mono doit repartir quand la
    grille est réaffichée (ne pas rester « en pause »).

    Utilise une fausse tuile (pas de libmpv) : on teste la logique de (re)démarrage
    de _set_grid, pas la lecture réelle."""
    from PySide6.QtWidgets import QWidget
    from sentinelle.config import Camera, Site, save_config
    from sentinelle.ui.main_window import MainWindow
    from sentinelle.ui.tile import TileState

    class FakeTile(QWidget):
        def __init__(self, camera):
            super().__init__()
            self.camera = camera
            self.state = TileState.IDLE
            self.debit_bps = 0.0

        def start(self):
            self.state = TileState.CONNECTING

        def stop(self, message=""):
            self.state = TileState.IDLE

        def shutdown(self):
            self.state = TileState.IDLE

    win = MainWindow(str(tmp_path / "config.yaml"))
    site = Site(id="s1", nom="S", lien="fibre")
    win._cfg.sites.append(site)
    win._cfg.cameras += [
        Camera(id="c1", nom="C1", site=site, marque="hikvision", hote="127.0.0.1", canal=1),
        Camera(id="c2", nom="C2", site=site, marque="hikvision", hote="127.0.0.1", canal=2),
    ]
    save_config(win._cfg)
    win._make_tile = lambda cam, vue: FakeTile(cam)   # pas de flux réel

    win._set_grid(["c1", "c2"])
    assert all(t.state == TileState.CONNECTING for t in win._tiles.values())
    win._set_mono("c1")                       # stoppe les tuiles grille conservées
    win._leave_mono()
    win._set_grid(["c1", "c2"])               # doit les relancer
    for cam_id, tile in win._tiles.items():
        assert tile.state != TileState.IDLE, f"{cam_id} restée à l'arrêt"
    win.close()
