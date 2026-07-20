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
    parser = argparse.ArgumentParser(prog="sentinelle",
                                     description="Sentinelle — visionneuse de vidéosurveillance")
    parser.add_argument("--config", "-c", default="",
                        help="chemin du fichier de configuration (défaut : profil utilisateur)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="journalisation détaillée (niveau DEBUG)")
    parser.add_argument("--safe-video", action="store_true",
                        help="sortie vidéo logicielle (vo=x11, sans OpenGL) — "
                             "à utiliser si l'affichage vidéo fait planter le "
                             "système (pilote GPU fragile)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.safe_video:
        # sortie logicielle : évite le chemin OpenGL de mpv, qui peut faire
        # tomber le serveur d'affichage sur certains pilotes GPU Linux.
        os.environ.setdefault("SENTINELLE_MPV_VO", "x11")
        os.environ.setdefault("SENTINELLE_MPV_HWDEC", "no")
        logging.getLogger("sentinelle").info("Mode vidéo sûr : vo=x11, hwdec=no")

    migrer_ancien_dossier()          # reprend les données de l'ancien nom si présent

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import qInstallMessageHandler, QtMsgType

    # Route les messages internes de Qt dans le logging Python (mêmes format et
    # flux que le reste), pour qu'ils restent visibles au débogage.
    _qt_log = logging.getLogger("qt")
    _niveaux = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def _handler_qt(mode, contexte, message):
        niveau = _niveaux.get(mode, logging.INFO)
        # Avertissement bénin en DPI fractionnaire / multi-écrans : Windows rabote
        # la géométrie de quelques pixels, sans effet visible. On le rétrograde en
        # DEBUG (masqué par défaut, réapparaît avec --verbose) au lieu de polluer.
        if "Unable to set geometry" in message:
            niveau = logging.DEBUG
        _qt_log.log(niveau, message)

    qInstallMessageHandler(_handler_qt)

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

    # Premier lancement : choix du mode AVANT d'ouvrir l'application. Fermer
    # l'assistant sans choisir n'entre pas dans l'appli — on quitte, et le choix
    # sera redemandé au prochain lancement.
    from .reglages import reglages
    settings = reglages()
    if not settings.contains("mode"):
        from .ui.setup_dialog import SetupDialog
        dlg = SetupDialog()
        if not dlg.exec() or not dlg.resultat:
            return 0
        # repart d'une base propre (aucun compte hérité d'une install précédente)
        settings.remove("serveur_user")
        settings.remove("serveur_pass")
        settings.setValue("mode", dlg.resultat["mode"])
        # l'adresse du serveur est saisie à la connexion (page de login)

    from .ui.main_window import MainWindow

    win = MainWindow(config_path)
    if getattr(win, "demarrage_annule", False):
        return 0                         # connexion serveur abandonnée
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
