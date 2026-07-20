"""Lanceur : python run.py [--config cameras.yaml] — sert aussi d'entrée PyInstaller."""

from sentinelle.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
