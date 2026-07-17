"""Thème sombre de l'application : palette + feuille de style globale.

Une seule apparence (sombre), adaptée à un mur d'images. Les widgets lisent les
couleurs via ``t("clé")`` plutôt que par des valeurs codées en dur, ce qui
garantit la cohérence de l'habillage.

Les flèches des listes déroulantes et des champs numériques sont dessinées à
partir de petites icônes SVG écrites dans un dossier temporaire, puis
référencées par la feuille de style : c'est net et visible, contrairement aux
flèches natives (parfois invisibles selon la version de Qt).
"""

import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QPalette

THEMES = ("dark",)
THEME_LABELS = {"dark": "Sombre"}
_DEFAUT = "dark"

_TOKENS = {
    "bg": "#14161a",
    "surface": "#1b1e24",
    "surface_alt": "#22262e",
    "elevated": "#2b303a",
    "border": "#333a45",
    "border_soft": "#2a2f38",
    "text": "#e4e6ea",
    "text_dim": "#9aa0aa",
    "text_faint": "#6b7280",
    "accent": "#3d8bfd",
    "accent_hover": "#5b9dff",
    "on_accent": "#ffffff",
    "selection": "#3d8bfd",
    "selection_text": "#ffffff",
    "video_bg": "#0a0b0d",
    "tile_bg": "#101216",
    "tile_header": "#1b1e24",
    "tile_status_text": "#c4c8ce",
    "danger": "#e0524d",
    "ok": "#46c46e",
    "warn": "#e0a636",
}

_current = dict(_TOKENS)
_current_name = _DEFAUT


def t(cle: str) -> str:
    return _current[cle]


def nom_courant() -> str:
    return _current_name


def theme_enregistre() -> str:
    return _DEFAUT


def enregistrer_theme(nom: str):
    pass                       # thème unique : rien à mémoriser


def _palette() -> QPalette:
    c = _TOKENS
    q = lambda k: QColor(c[k])
    p = QPalette()
    p.setColor(QPalette.Window, q("bg"))
    p.setColor(QPalette.WindowText, q("text"))
    p.setColor(QPalette.Base, q("surface"))
    p.setColor(QPalette.AlternateBase, q("surface_alt"))
    p.setColor(QPalette.Text, q("text"))
    p.setColor(QPalette.Button, q("surface_alt"))
    p.setColor(QPalette.ButtonText, q("text"))
    p.setColor(QPalette.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, q("elevated"))
    p.setColor(QPalette.ToolTipText, q("text"))
    p.setColor(QPalette.Highlight, q("selection"))
    p.setColor(QPalette.HighlightedText, q("selection_text"))
    p.setColor(QPalette.Link, q("accent"))
    p.setColor(QPalette.PlaceholderText, q("text_faint"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, q("text_faint"))
    return p


def _fleches() -> dict:
    """Écrit les icônes de flèche (chevrons) et renvoie leurs URL QSS."""
    dossier = Path(tempfile.gettempdir()) / "sentinelle-ui"
    dossier.mkdir(parents=True, exist_ok=True)
    col = _TOKENS["text"]
    formes = {
        "down": "M3 5 L7 9 L11 5",
        "up": "M3 9 L7 5 L11 9",
    }
    urls = {}
    for nom, d in formes.items():
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" '
               f'viewBox="0 0 14 14"><path d="{d}" fill="none" stroke="{col}" '
               f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')
        p = dossier / f"caret-{nom}.svg"
        p.write_text(svg, encoding="utf-8")
        urls[nom] = p.as_posix()
    return urls


def _qss() -> str:
    c = _current
    fl = _fleches()
    return f"""
    /* Style plat avec arrondis légers ; surfaces structurelles à angles droits. */
    QWidget {{ font-size: 13px; }}
    QMainWindow, QDialog, QWidget#root {{ background: {c['bg']}; }}

    /* ---- barre de titre applicative ---- */
    QFrame#topbar {{
        background: {c['surface']};
        border-bottom: 1px solid {c['border']};
    }}
    QLabel#brand {{
        color: {c['accent']};
        font-size: 16px;
        font-weight: 700;
        letter-spacing: 1px;
        padding-right: 4px;
    }}
    QFrame#vsep {{ background: {c['border_soft']}; border: none; max-width: 1px; }}

    QToolButton {{
        color: {c['text']};
        background: transparent;
        border: none;
        border-radius: 6px;
        padding: 7px 11px;
        margin: 0;
    }}
    QToolButton:hover {{ background: {c['surface_alt']}; }}
    QToolButton:pressed {{ background: {c['elevated']}; }}
    QToolButton:checked {{ background: {c['accent']}; color: {c['on_accent']}; }}
    QToolButton:disabled {{ color: {c['text_faint']}; }}
    QToolButton::menu-button {{ border: none; width: 14px; }}

    /* ---- champs ---- */
    QComboBox, QSpinBox, QLineEdit {{
        background: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        padding: 5px 8px;
        min-height: 20px;
        selection-background-color: {c['selection']};
        selection-color: {c['selection_text']};
    }}
    QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: {c['text_faint']}; }}
    QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {c['accent']}; }}
    QComboBox QAbstractItemView {{
        background: {c['surface']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        padding: 2px;
        selection-background-color: {c['selection']};
        selection-color: {c['selection_text']};
        outline: none;
    }}

    /* flèche de liste déroulante (icône SVG, nette et visible) */
    QComboBox::drop-down {{
        subcontrol-origin: padding; subcontrol-position: center right;
        border: none; width: 24px;
    }}
    QComboBox::down-arrow {{ image: url({fl['down']}); width: 12px; height: 12px; }}

    /* boutons +/- des champs numériques */
    QSpinBox::up-button, QSpinBox::down-button {{
        subcontrol-origin: border; width: 20px;
        background: {c['surface_alt']};
        border-left: 1px solid {c['border']};
    }}
    QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 6px; }}
    QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 6px; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {c['elevated']}; }}
    QSpinBox::up-arrow {{ image: url({fl['up']}); width: 11px; height: 11px; }}
    QSpinBox::down-arrow {{ image: url({fl['down']}); width: 11px; height: 11px; }}

    /* ---- boutons ---- */
    QPushButton {{
        background: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        padding: 8px 14px;
        text-align: left;
    }}
    QPushButton:hover {{ background: {c['elevated']}; border-color: {c['text_faint']}; }}
    QPushButton:pressed {{ background: {c['elevated']}; }}
    QPushButton:disabled {{ color: {c['text_faint']}; border-color: {c['border_soft']}; }}
    QPushButton:default {{
        background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent']};
    }}
    QPushButton:default:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton#compact {{ padding: 4px 2px; min-width: 0; text-align: center; }}
    QPushButton#addBtn {{
        background: {c['accent']}; color: {c['on_accent']};
        border: none; border-radius: 6px; padding: 9px 12px; font-weight: 600; text-align: center;
    }}
    QPushButton#addBtn:hover {{ background: {c['accent_hover']}; }}
    QDialogButtonBox QPushButton {{ text-align: center; min-width: 92px; }}

    /* ---- panneau latéral (structurel : angles droits) ---- */
    QFrame#sidebar {{ background: {c['surface']}; border-right: 1px solid {c['border']}; }}
    QFrame#sideHeader {{ border-bottom: 1px solid {c['border_soft']}; }}
    QFrame#sideFooter {{ border-top: 1px solid {c['border_soft']}; }}
    QLabel#sideTitle {{
        color: {c['text_dim']}; font-weight: 700; font-size: 11px; letter-spacing: 1.5px;
    }}
    QLabel#sideCount {{
        color: {c['text_dim']}; background: {c['surface_alt']};
        border-radius: 9px; padding: 1px 9px; font-size: 11px; font-weight: 600;
    }}
    QSplitter#workspace::handle {{ background: {c['border']}; }}

    /* ---- arbre des caméras ---- */
    QTreeWidget#cameraTree {{
        background: {c['surface']}; color: {c['text']};
        border: none; outline: none; padding: 4px;
    }}
    QTreeWidget, QListWidget, QTableWidget {{
        background: {c['surface']};
        alternate-background-color: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border_soft']};
        border-radius: 6px;
        outline: none;
    }}
    QTreeWidget::item, QListWidget::item, QTableWidget::item {{ padding: 6px 4px; border-radius: 4px; }}
    QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected {{
        background: {c['selection']}; color: {c['selection_text']};
    }}
    QTreeWidget#cameraTree::item:hover {{ background: {c['surface_alt']}; }}
    QHeaderView::section {{
        background: {c['surface_alt']}; color: {c['text_dim']};
        border: none; border-bottom: 1px solid {c['border_soft']}; padding: 6px 8px;
    }}

    /* ---- groupes, menus, bulles ---- */
    QGroupBox {{
        border: 1px solid {c['border_soft']}; border-radius: 8px;
        margin-top: 14px; padding: 12px 10px 10px 10px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; left: 12px; padding: 0 5px;
        color: {c['text_dim']}; font-weight: 600;
    }}
    QMenu {{
        background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 5px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
    QMenu::separator {{ height: 1px; background: {c['border_soft']}; margin: 5px 8px; }}
    QToolTip {{
        background: {c['elevated']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 6px; padding: 5px 8px;
    }}

    /* ---- barre d'état ---- */
    QStatusBar {{ background: {c['surface']}; color: {c['text_dim']}; border-top: 1px solid {c['border_soft']}; }}
    QStatusBar::item {{ border: none; }}

    /* ---- ascenseurs ---- */
    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['elevated']}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['text_faint']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {c['elevated']}; border-radius: 5px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ---- indications discrètes ---- */
    QLabel#hint {{ color: {c['text_dim']}; }}
    """


def apply_theme(app, nom: str | None = None):
    """Applique le thème sombre à l'application entière."""
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setStyleSheet(_qss())


# rétrocompatibilité
def apply_dark_theme(app):
    apply_theme(app)
