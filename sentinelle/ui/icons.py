"""Icônes SVG de l'application (tracés style Feather/Lucide, licence MIT).

Rendues à la volée dans la couleur voulue → nettes à toutes les tailles,
cohérentes avec le thème sombre, aucun emoji dépendant de la police système.
"""

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QIcon, QPixmap

# corps SVG (viewBox 24×24), dessinés au trait sauf mention "fill"
_PATHS = {
    "grid": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>'
            '<rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
    "rotate": '<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    "play": '<polygon points="6 4 20 12 6 20 6 4"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "pause": '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
    "pencil": '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
    "maximize": '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
                '<path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0'
                'l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2'
                'v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83'
                'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09'
                'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0'
                'l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09'
                'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83'
                'l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2'
                'h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
             '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "camera": '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>'
              '<circle cx="12" cy="13" r="4"/>',
    "arrow-up": '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
    "arrow-down": '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/>'
               '<line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "video": '<polygon points="23 7 16 12 23 17 23 7"/>'
             '<rect x="1" y="5" width="15" height="14" rx="2"/>',
    "motion": '<path d="M13 2a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"/>'
              '<path d="M8 8l3 2 1 5 3 3"/><path d="M11 10l-2 4-4 1"/>'
              '<path d="M15 11l4-1"/>',
}

_FILLED = {"play", "stop", "pause"}

_SIZES = (16, 20, 24, 32, 48)


def _svg(name: str, color: str, size: int) -> bytes:
    body = _PATHS[name]
    if name in _FILLED:
        style = f'fill="{color}" stroke="{color}" stroke-width="1"'
    else:
        style = (f'fill="none" stroke="{color}" stroke-width="2" '
                 f'stroke-linecap="round" stroke-linejoin="round"')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 24 24" {style}>{body}</svg>').encode()


@lru_cache(maxsize=None)
def _icon_cached(name: str, color: str) -> QIcon:
    ic = QIcon()
    for size in _SIZES:
        pix = QPixmap()
        pix.loadFromData(QByteArray(_svg(name, color, size)), "SVG")
        if not pix.isNull():
            ic.addPixmap(pix)
    return ic


def icon(name: str, color: str | None = None) -> QIcon:
    """Icône SVG rendue dans la couleur du texte du thème courant par défaut."""
    if color is None:
        from .theme import t
        color = t("text")
    return _icon_cached(name, color)


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    """Icône de l'application (logo dédié, embarqué). Repli sur le glyphe vidéo."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for rel in ("ui/sentinelle.ico", "ui/sentinelle.png",
                "sentinelle/ui/sentinelle.ico", "sentinelle/ui/sentinelle.png"):
        p = base / rel
        if p.is_file():
            ic = QIcon(str(p))
            if not ic.isNull():
                return ic
    return icon("video", "#2a82da")
