"""Génère le logo de l'application : sentinelle.svg, .png (256) et .ico.

Motif : un œil de veille stylisé (la sentinelle qui surveille) avec un balayage
radar, sur fond sombre arrondi aux couleurs de l'appli.

Usage : python packaging/make_icon.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DOSSIER = Path(__file__).parent

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <rect x="8" y="8" width="240" height="240" rx="52" fill="#14171c"/>
  <rect x="12" y="12" width="232" height="232" rx="48" fill="none"
        stroke="#2a82da" stroke-width="4" opacity="0.30"/>
  <!-- œil -->
  <path d="M40 128 C 80 74, 176 74, 216 128 C 176 182, 80 182, 40 128 Z"
        fill="none" stroke="#2a82da" stroke-width="12" stroke-linejoin="round"/>
  <circle cx="128" cy="128" r="34" fill="#2a82da"/>
  <circle cx="128" cy="128" r="15" fill="#14171c"/>
  <!-- reflet / balayage -->
  <circle cx="118" cy="117" r="6" fill="#eaf2fb"/>
  <path d="M128 128 L128 92" stroke="#3fbf5f" stroke-width="5"
        stroke-linecap="round" opacity="0.9"/>
</svg>
"""


def main():
    svg_path = DOSSIER / "sentinelle.svg"
    svg_path.write_text(SVG, encoding="utf-8")

    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QGuiApplication, QPixmap

    _app = QGuiApplication(sys.argv)
    pix = QPixmap()
    if not pix.loadFromData(QByteArray(SVG.encode()), "SVG"):
        raise SystemExit("rendu SVG impossible (plugin qsvg absent ?)")

    png_path = DOSSIER / "sentinelle.png"
    pix.save(str(png_path), "PNG")

    from PIL import Image
    ico_path = DOSSIER / "sentinelle.ico"
    Image.open(png_path).save(
        ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                         (128, 128), (256, 256)])
    print(f"OK : {svg_path.name}, {png_path.name}, {ico_path.name}")


if __name__ == "__main__":
    main()
