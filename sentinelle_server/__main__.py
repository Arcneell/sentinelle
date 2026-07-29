"""Point d'entrée : python -m sentinelle_server [--host H] [--port P] [--data DIR]

Les données (config.yaml central, server.yaml avec les jetons) vivent dans le
dossier --data (ou $SENTINELLE_DATA, défaut : ./data).

Sentinelle — serveur central de vidéosurveillance multi-sites.
Copyright (C) 2026 Arcneell

Ce programme est un logiciel libre : vous pouvez le redistribuer et/ou le
modifier selon les termes de la GNU Affero General Public License telle que
publiée par la Free Software Foundation, soit la version 3, soit (à votre
choix) toute version ultérieure. Il est distribué sans aucune garantie ; voir
le fichier LICENSE ou <https://www.gnu.org/licenses/> pour les détails.
"""

import argparse
import logging
import os


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        prog="sentinelle-server",
        description="Sentinelle Server — configuration centrale et relais de flux")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default="",
                        help="dossier de données (défaut : $SENTINELLE_DATA ou ./data)")
    args = parser.parse_args()
    if args.data:
        os.environ["SENTINELLE_DATA"] = args.data

    import uvicorn

    from .app import create_app
    # access_log=False : pas de journal d'accès par requête (bruit, et défense en
    # profondeur — aucun jeton n'y transite, les jetons ne passent que par en-tête)
    uvicorn.run(create_app(), host=args.host, port=args.port,
                log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
