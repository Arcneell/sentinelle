"""Lanceur : python run.py [--config cameras.yaml] — sert aussi d'entrée PyInstaller.

Sentinelle — visualiseur de vidéosurveillance multi-sites RTSP / ONVIF.
Copyright (C) 2026 Arcneell

Ce programme est un logiciel libre : vous pouvez le redistribuer et/ou le
modifier selon les termes de la GNU Affero General Public License telle que
publiée par la Free Software Foundation, soit la version 3, soit (à votre
choix) toute version ultérieure. Il est distribué sans aucune garantie ; voir
le fichier LICENSE ou <https://www.gnu.org/licenses/> pour les détails.
"""

from sentinelle.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
