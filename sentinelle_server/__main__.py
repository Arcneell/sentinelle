"""Point d'entrée : python -m sentinelle_server [--host H] [--port P] [--data DIR]

Les données (config.yaml central, server.yaml avec les jetons) vivent dans le
dossier --data (ou $SENTINELLE_DATA, défaut : ./data).
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
    # access_log=False : les URLs peuvent porter un jeton en paramètre (mode
    # photo) — on ne les écrit pas dans les journaux
    uvicorn.run(create_app(), host=args.host, port=args.port,
                log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
