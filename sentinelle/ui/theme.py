"""Thème de l'application : palette, typographie, feuille de style globale.

Parti pris — le mur d'images est un instrument, pas un tableau de bord.

1. La couleur est réservée à l'état des caméras. Le châssis (barre du haut,
   panneau latéral, cadres) est entièrement neutre : aucune teinte
   d'« accent ». Une caméra qui lit correctement n'affiche donc AUCUNE
   couleur — tout pixel coloré à l'écran veut dire « regarde ici ».
   Les commandes signalent leur état par inversion (fond clair, texte
   sombre) et par le poids, jamais par la teinte.
2. Le fond du châssis est un graphite, pas un noir : seul le lit vidéo est
   quasi noir, pour que les noirs d'une image de nuit restent lisibles comme
   des noirs et ne se fondent pas dans le cadre.
3. Typographie sobre : la police d'interface du système porte tous les
   libellés, en casse normale. La monospace est réservée aux CHIFFRES qui se
   rafraîchissent (débits, compteurs, numéros de page) — sans elle ils
   sautillent d'une largeur de glyphe à l'autre. Les petites capitales
   espacées ne servent qu'au nom du produit et aux deux intitulés de section
   du panneau latéral.

Les flèches des listes déroulantes et des champs numériques sont dessinées à
partir de petites icônes SVG écrites dans un dossier temporaire, puis
référencées par la feuille de style : c'est net et visible, contrairement aux
flèches natives (parfois invisibles selon la version de Qt).
"""

import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

THEMES = ("dark",)
THEME_LABELS = {"dark": "Sombre"}
_DEFAUT = "dark"

_TOKENS = {
    # ---- lits et surfaces (neutres) ----
    "bg": "#171a1d",            # lit de l'application, fond des interstices
    "surface": "#1f2328",       # barre du haut, panneau latéral, dialogues
    "surface_alt": "#252a30",   # champs, lignes alternées
    "elevated": "#2c3238",      # survol, menus, bulles
    "border": "#353c43",
    "border_soft": "#282e34",
    # ---- texte ----
    "text": "#e7eaec",
    "text_dim": "#949ca5",
    "text_faint": "#69717a",
    # ---- interaction : inversion, pas de teinte ----
    "accent": "#e7eaec",        # « accent » = la craie ; jamais une couleur
    "accent_hover": "#ffffff",
    "on_accent": "#14171a",     # texte posé sur la craie
    "selection": "#e7eaec",     # menus, listes déroulantes (état momentané)
    "selection_text": "#14171a",
    "row_selected": "#2f353c",  # lignes de liste (état durable) : pas d'inversion
    # ---- plan vidéo ----
    "video_bg": "#0b0c0d",      # lit de l'image : le seul quasi-noir
    "tile_bg": "#0b0c0d",
    "tile_header": "#1c2024",   # bandeau d'identité, sous l'image
    "tile_status_text": "#c3c9cf",
    # liseré de la tuile : largeur constante, seule la couleur change — la
    # géométrie de la surface vidéo ne bouge donc jamais d'un état à l'autre
    "bezel": "#39424a",         # lecture en cours : simple cerne discret
    "bezel_idle": "#1f2429",    # rien n'est attendu de cette tuile
    # ---- état des caméras : le seul endroit où la couleur existe ----
    "danger": "#d6504b",        # panne : identifiants refusés, lecteur absent
    "ok": "#43b76b",            # lecture en cours
    "warn": "#d7a03a",          # connexion, nouvelle tentative
}

_current = dict(_TOKENS)
_current_name = _DEFAUT

# ---------------------------------------------------------------- typographie

# Familles préférées. On prend d'abord la police d'INTERFACE du système : c'est
# celle que l'utilisateur lit toute la journée dans ses autres applications, et
# c'est ce qui fait qu'un logiciel a l'air à sa place. Inter n'arrive en tête
# que si elle est réellement installée (ou déposée dans ui/fonts/).
_FAMILLES_UI = ("Inter", "Segoe UI Variable Text", "Segoe UI", "Cantarell",
                "Noto Sans", "Liberation Sans", "DejaVu Sans")
_FAMILLES_DATA = ("JetBrains Mono", "Cascadia Mono", "Consolas",
                  "DejaVu Sans Mono", "Noto Sans Mono", "Monospace")

_ui_family = "Segoe UI"
_data_family = "Consolas"
_polices_chargees = False


def _charger_polices():
    """Enregistre les .ttf embarqués (s'il y en a) et retient les familles
    réellement disponibles.

    Aucune police n'est obligatoire : le dossier ui/fonts/ est facultatif, et
    l'interface se replie proprement sur les familles du système."""
    global _ui_family, _data_family, _polices_chargees
    if _polices_chargees:
        return
    _polices_chargees = True

    dossier = Path(__file__).resolve().parent / "fonts"
    if dossier.is_dir():
        for ttf in sorted(dossier.glob("*.[to]tf")):
            QFontDatabase.addApplicationFont(str(ttf))

    dispo = set(QFontDatabase.families())

    def premiere(candidates, defaut):
        for nom in candidates:
            if nom in dispo:
                return nom
        return defaut

    _ui_family = premiere(_FAMILLES_UI, _ui_family)
    _data_family = premiere(_FAMILLES_DATA, _data_family)


def famille_ui() -> str:
    _charger_polices()
    return _ui_family


def famille_data() -> str:
    _charger_polices()
    return _data_family


def police_ui(taille: int = 13, gras: bool = False) -> QFont:
    """Police d'interface : tous les libellés, en casse normale."""
    f = QFont(famille_ui(), taille)
    f.setPixelSize(taille)
    f.setWeight(QFont.DemiBold if gras else QFont.Normal)
    return f


def police_data(taille: int = 12, gras: bool = False) -> QFont:
    """Monospace, pour les valeurs qui se rafraîchissent.

    Réservée aux chiffres et aux identifiants techniques : débits, compteurs,
    numéros de page, adresses. En largeur fixe, un débit qui passe de 1.4 à
    0.9 Mb/s ne fait plus bouger le texte autour de lui."""
    f = QFont(famille_data(), taille)
    f.setPixelSize(taille)
    f.setWeight(QFont.DemiBold if gras else QFont.Normal)
    return f


def police_etiquette(taille: int = 11, suivi: float = 14.0) -> QFont:
    """Petites capitales espacées — le nom du produit et les intitulés de
    section du panneau latéral, rien d'autre.

    L'interlettrage passe par la police : Qt n'implémente pas la propriété
    ``letter-spacing`` des feuilles de style."""
    f = QFont(famille_ui(), taille)
    f.setPixelSize(taille)
    f.setWeight(QFont.DemiBold)
    f.setCapitalization(QFont.AllUppercase)
    f.setLetterSpacing(QFont.PercentageSpacing, 100.0 + suivi)
    return f


# ------------------------------------------------------------------ palette


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
    p.setColor(QPalette.Highlight, q("row_selected"))
    p.setColor(QPalette.HighlightedText, q("text"))
    p.setColor(QPalette.Link, q("text"))
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
    ui = famille_ui()
    mono = famille_data()
    return f"""
    /* Angles droits sur tout ce qui encadre de la vidéo ou structure la
       fenêtre ; 3 px sur les commandes seulement, pour qu'elles restent des
       objets manipulables sans amollir le châssis. */
    QWidget {{ font-family: "{ui}"; font-size: 13px; }}
    QMainWindow, QDialog, QWidget#root {{ background: {c['bg']}; }}

    /* ---- barre du haut ---- */
    QFrame#topbar {{
        background: {c['surface']};
        border-bottom: 1px solid {c['border']};
    }}
    QLabel#brand {{
        color: {c['text']};
        padding-right: 2px;
    }}

    QToolButton {{
        color: {c['text_dim']};
        background: transparent;
        border: none;
        border-radius: 3px;
        padding: 6px 10px;
        margin: 0;
        font-size: 13px;
    }}
    QToolButton:hover {{ background: {c['elevated']}; color: {c['text']}; }}
    QToolButton:pressed {{ background: {c['border']}; }}
    QToolButton:checked {{
        background: {c['accent']}; color: {c['on_accent']}; font-weight: 600;
    }}
    QToolButton:checked:hover {{ background: {c['accent_hover']}; }}
    QToolButton:disabled {{ color: {c['text_faint']}; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    /* numéro de page : monospace, il change en cours de défilement */
    QLabel#pageInfo {{
        color: {c['text']}; font-family: "{mono}"; font-size: 12px;
        padding: 0 4px;
    }}

    /* ---- champs ---- */
    QComboBox, QSpinBox, QLineEdit {{
        background: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border_soft']};
        border-radius: 3px;
        padding: 5px 8px;
        min-height: 20px;
        selection-background-color: {c['selection']};
        selection-color: {c['selection_text']};
    }}
    QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: {c['border']}; }}
    QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {c['text_dim']}; }}
    QSpinBox {{ font-family: "{mono}"; }}     /* une durée, un port : des chiffres */
    QComboBox QAbstractItemView {{
        background: {c['surface']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 3px;
        padding: 2px;
        selection-background-color: {c['selection']};
        selection-color: {c['selection_text']};
        outline: none;
    }}

    /* flèche de liste déroulante (icône SVG, nette et visible) */
    QComboBox::drop-down {{
        subcontrol-origin: padding; subcontrol-position: center right;
        border: none; width: 22px;
    }}
    QComboBox::down-arrow {{ image: url({fl['down']}); width: 12px; height: 12px; }}

    /* boutons +/- des champs numériques */
    QSpinBox::up-button, QSpinBox::down-button {{
        subcontrol-origin: border; width: 18px;
        background: {c['surface_alt']};
        border-left: 1px solid {c['border_soft']};
    }}
    QSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 3px; }}
    QSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 3px; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {c['elevated']}; }}
    QSpinBox::up-arrow {{ image: url({fl['up']}); width: 11px; height: 11px; }}
    QSpinBox::down-arrow {{ image: url({fl['down']}); width: 11px; height: 11px; }}

    /* ---- boutons ---- */
    /* pas de « text-align » ici : c'est déjà le centrage par défaut d'un
       bouton, et combiné à « padding » il décalait le libellé vers la gauche
       jusqu'à rogner son premier caractère (visible sur les libellés longs) */
    QPushButton {{
        background: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 3px;
        padding: 8px 14px;
    }}
    QPushButton:hover {{ background: {c['elevated']}; border-color: {c['text_faint']}; }}
    QPushButton:pressed {{ background: {c['border']}; }}
    QPushButton:disabled {{ color: {c['text_faint']}; border-color: {c['border_soft']}; }}
    QPushButton:default {{
        background: {c['accent']}; color: {c['on_accent']};
        border-color: {c['accent']}; font-weight: 600;
    }}
    QPushButton:default:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
    QPushButton#compact {{
        padding: 4px 2px; min-width: 0; font-size: 12px;
    }}
    QPushButton#addBtn {{
        background: transparent; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 3px;
        padding: 9px 12px;
    }}
    QPushButton#addBtn:hover {{ background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent']}; }}
    /* largeur plancher pour que « Valider » / « Annuler » ne soient pas des
       vignettes ; la largeur réelle suit le libellé au-delà */
    QDialogButtonBox QPushButton {{ min-width: 92px; }}

    /* ---- panneau latéral ---- */
    QFrame#sidebar {{ background: {c['surface']}; border-right: 1px solid {c['border']}; }}
    QFrame#sideHeader {{ border-bottom: 1px solid {c['border_soft']}; }}
    QFrame#sideSearch {{ border-bottom: 1px solid {c['border_soft']}; }}
    QFrame#sideFooter {{ border-top: 1px solid {c['border_soft']}; }}
    QLabel#sideTitle {{ color: {c['text_dim']}; }}
    QLabel#sideCount {{
        color: {c['text']}; font-family: "{mono}"; font-size: 12px;
        padding: 0 2px;
    }}
    QSplitter#workspace::handle {{ background: {c['border']}; }}

    /* ---- arbre des caméras ---- */
    QTreeWidget#cameraTree {{
        background: {c['surface']}; color: {c['text']};
        border: none; outline: none; padding: 4px 2px;
    }}
    QTreeWidget, QListWidget, QTableWidget {{
        background: {c['surface']};
        alternate-background-color: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border_soft']};
        border-radius: 3px;
        outline: none;
    }}
    QTreeWidget::item, QListWidget::item, QTableWidget::item {{ padding: 6px 4px; }}
    /* état durable : pas d'inversion, elle fatigue sur une liste entière */
    QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected {{
        background: {c['row_selected']}; color: {c['text']};
    }}
    QTreeWidget#cameraTree::item:hover {{ background: {c['surface_alt']}; }}
    QHeaderView::section {{
        background: {c['surface_alt']}; color: {c['text_dim']};
        border: none; border-bottom: 1px solid {c['border_soft']};
        padding: 7px 8px; font-size: 12px;
    }}

    /* ---- onglets (panneau d'administration) ---- */
    /* la marge du volet est indispensable : sans elle, la première rangée de
       boutons d'une page se retrouve collée sous la barre d'onglets */
    QTabWidget::pane {{
        border: 1px solid {c['border_soft']}; border-radius: 0; top: -1px;
        padding: 16px;
    }}
    QTabBar::tab {{
        background: transparent; color: {c['text_dim']};
        border: none; border-bottom: 2px solid transparent;
        padding: 9px 18px; margin-right: 2px; font-size: 13px;
    }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabBar::tab:selected {{
        color: {c['text']}; border-bottom: 2px solid {c['text']};
    }}

    /* ---- groupes, menus, bulles ---- */
    QGroupBox {{
        border: 1px solid {c['border_soft']}; border-radius: 3px;
        margin-top: 14px; padding: 12px 10px 10px 10px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin; left: 12px; padding: 0 5px;
        color: {c['text_dim']}; font-size: 12px;
    }}
    QMenu {{
        background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 3px; padding: 4px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 2px; }}
    QMenu::item:selected {{ background: {c['selection']}; color: {c['selection_text']}; }}
    QMenu::separator {{ height: 1px; background: {c['border_soft']}; margin: 5px 8px; }}
    QToolTip {{
        background: {c['elevated']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 3px; padding: 5px 8px;
    }}

    /* ---- barre d'état ---- */
    QStatusBar {{
        background: {c['surface']}; color: {c['text_dim']};
        border-top: 1px solid {c['border_soft']};
    }}
    QStatusBar::item {{ border: none; }}
    /* compteurs de flux et débit cumulé : ils défilent en continu */
    QStatusBar QLabel {{ font-family: "{mono}"; font-size: 12px; }}

    /* ---- ascenseurs ---- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 0; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['text_faint']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 0; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ---- indications discrètes ---- */
    QLabel#hint {{ color: {c['text_dim']}; }}
    """


def apply_theme(app, nom: str | None = None):
    """Applique le thème à l'application entière."""
    app.setStyle("Fusion")
    _charger_polices()
    app.setFont(police_ui(13))
    app.setPalette(_palette())
    app.setStyleSheet(_qss())


# rétrocompatibilité
def apply_dark_theme(app):
    apply_theme(app)
