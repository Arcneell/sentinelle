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
