"""Point d'entrée : python -m sentinelle [--config chemin/config.yaml]

Sans argument, la configuration vit dans le profil utilisateur
(%APPDATA%\\Sentinelle\\config.yaml) et se gère entièrement dans l'interface.
Usage portable : --config .\\config.yaml à côté de l'exe.
"""

import argparse
import logging
import os
import sys

from .config import default_config_path, migrer_ancien_dossier


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(prog="sentinelle",
                                     description="Sentinelle — visionneuse de vidéosurveillance")
    parser.add_argument("--config", "-c", default="",
                        help="chemin du fichier de configuration (défaut : profil utilisateur)")
    args = parser.parse_args()

    migrer_ancien_dossier()          # reprend les données de l'ancien nom si présent

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("Sentinelle")

    from .ui.theme import apply_theme
    apply_theme(app)          # thème mémorisé (sombre par défaut)
    from .ui.icons import app_icon
    app.setWindowIcon(app_icon())

    config_path = args.config
    if not config_path:
        # portable : un config.yaml posé à côté de l'exe a priorité
        if getattr(sys, "frozen", False):
            portable = os.path.join(os.path.dirname(sys.executable), "config.yaml")
            if os.path.exists(portable):
                config_path = portable
        config_path = config_path or default_config_path()

    from .ui.main_window import MainWindow

    win = MainWindow(config_path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
