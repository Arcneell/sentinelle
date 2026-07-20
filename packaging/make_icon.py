"""Génère le logo de l'application : sentinelle.svg, .png (256) et .ico.

Motif : une tour de garde surmontée d'un œil qui veille (clin d'œil à l'œil de
Sauron au sommet de sa tour), le tout dans un écusson — dégradé sombre + iris
ambré, sur fond arrondi. Les fichiers sont aussi copiés dans sentinelle/ui/
(icône embarquée dans l'application).

Usage : python packaging/make_icon.py
"""

import os
import shutil
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DOSSIER = Path(__file__).parent
UI = DOSSIER.parent / "sentinelle" / "ui"

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="fond" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#20252e"/>
      <stop offset="1" stop-color="#0e1115"/>
    </linearGradient>
    <linearGradient id="bouclier" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2b3542"/>
      <stop offset="1" stop-color="#161c24"/>
    </linearGradient>
    <linearGradient id="tour" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3a4150"/>
      <stop offset="1" stop-color="#10151c"/>
    </linearGradient>
    <radialGradient id="halo" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#ffb347" stop-opacity="0.9"/>
      <stop offset="1" stop-color="#ff6a00" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <rect x="8" y="8" width="240" height="240" rx="56" fill="url(#fond)"/>

  <!-- bouclier -->
  <path d="M128 34 L200 62 V128 C200 174 170 205 128 220
           C86 205 56 174 56 128 V62 Z" fill="url(#bouclier)"
        stroke="#43506180" stroke-width="2"/>

  <!-- tour de garde -->
  <g>
    <!-- corps -->
    <path d="M104 196 L110 116 H146 L152 196 Z" fill="url(#tour)"/>
    <!-- créneaux du sommet -->
    <path d="M104 116 V104 H110 V110 H118 V104 H124 V110 H132 V104 H138 V110 H146 V104 H152 V116 Z"
          fill="#2c3440"/>
    <!-- base -->
    <rect x="98" y="196" width="60" height="10" rx="2" fill="#2c3440"/>
    <!-- meurtrière sombre sous l'œil -->
    <rect x="123" y="150" width="10" height="26" rx="4" fill="#0e1115"/>
  </g>

  <!-- halo de l'œil -->
  <circle cx="128" cy="120" r="40" fill="url(#halo)"/>

  <!-- œil qui veille (amande + iris ambré vertical) -->
  <path d="M96 120 C112 100 144 100 160 120 C144 140 112 140 96 120 Z"
        fill="#fff2d6"/>
  <ellipse cx="128" cy="120" rx="9" ry="15" fill="#ff7a18"/>
  <ellipse cx="128" cy="120" rx="3.4" ry="13" fill="#1a0a00"/>
  <circle cx="124" cy="113" r="2.4" fill="#fff2d6"/>
</svg>
"""


def _ecrire_ico(pngs: dict[int, bytes], chemin: Path):
    """ICO à partir de PNGs (format PNG-in-ICO, supporté depuis Vista)."""
    tailles = sorted(pngs)
    entetes = struct.pack("<HHH", 0, 1, len(tailles))
    offset = 6 + 16 * len(tailles)
    entrees, blobs = b"", b""
    for t in tailles:
        data = pngs[t]
        entrees += struct.pack("<BBBBHHLL", t % 256, t % 256, 0, 0, 1, 32,
                               len(data), offset)
        blobs += data
        offset += len(data)
    chemin.write_bytes(entetes + entrees + blobs)


def main():
    svg_path = DOSSIER / "sentinelle.svg"
    svg_path.write_text(SVG, encoding="utf-8")

    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPixmap

    _app = QGuiApplication(sys.argv)
    pix = QPixmap()
    if not pix.loadFromData(QByteArray(SVG.encode()), "SVG"):
        raise SystemExit("rendu SVG impossible (plugin qsvg absent ?)")

    png_path = DOSSIER / "sentinelle.png"
    pix.save(str(png_path), "PNG")

    # ICO multi-tailles rendues depuis le SVG (net à chaque taille)
    pngs = {}
    for taille in (16, 24, 32, 48, 64, 128, 256):
        p = QPixmap()
        p.loadFromData(QByteArray(SVG.encode()), "SVG")
        img = p.toImage().scaled(taille, taille, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        buf = QBuffer()
        buf.open(QBuffer.WriteOnly)
        QImage(img).save(buf, "PNG")
        pngs[taille] = bytes(buf.data())
    ico_path = DOSSIER / "sentinelle.ico"
    _ecrire_ico(pngs, ico_path)

    # copies embarquées dans l'application
    shutil.copy(png_path, UI / "sentinelle.png")
    shutil.copy(ico_path, UI / "sentinelle.ico")
    print(f"OK : {svg_path.name}, {png_path.name}, {ico_path.name} (+ copies ui/)")


if __name__ == "__main__":
    main()
