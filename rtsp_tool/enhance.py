"""Moteur d'amélioration d'image — de la netteté au super-résolution neuronale.

Trois niveaux, appliqués en temps réel par le pipeline GPU de libmpv :

  off    : rendu direct (upscaler ewa_lanczossharp déjà réglé dans player.py)
  leger  : nettoyage anti-artefacts (deband) + accentuation (sharpen). Très peu coûteux.
  sr     : SUPER-RÉSOLUTION NEURONALE — réseau de neurones exécuté en shader GPU.
           Reconstruit contours et détails d'un flux basse qualité.
           · plein écran  : FSRCNNX (photographique, idéal vidéosurveillance) si présent,
                            sinon Anime4K M ;
           · grille       : Anime4K S (plus léger, adapté à plusieurs tuiles).

Honnêteté : la super-résolution reconstruit de façon plausible ce qu'un upscale
classique laisse flou/en blocs ; elle n'invente pas une information jamais captée
(une plaque de 4 pixels de large restera illisible). Sur des substreams CCTV
(CIF/360p bloqués), le gain de lisibilité est néanmoins très net.

Licences : Anime4K (bloc97) est embarqué — MIT. FSRCNNX (igv) est en GPL v3 :
non redistribué ici ; l'utilisateur peut le télécharger localement (bouton dédié),
le fichier reste sur son poste.
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

NIVEAUX = ("off", "leger", "sr")
NIVEAU_LABELS = {
    "off": "Aucune",
    "leger": "Légère — déblocage rapide + netteté",
    "sr": "Maximale — déblocage fort (+ IA si basse résolution)",
}

# Seuil sous lequel un upscale neuronal apporte vraiment quelque chose. Au-delà
# (D1/720p…), la source a assez de pixels : le défaut est le BLOCAGE de
# compression, corrigé par le déblocage, pas par un réseau de super-résolution.
SEUIL_SR_PX = 380

# FSRCNNX : téléchargeable à la demande (GPL, non embarqué)
FSRCNNX_NOM = "FSRCNNX_x2_8-0-4-1.glsl"
FSRCNNX_URL = ("https://github.com/igv/FSRCNN-TensorFlow/releases/download/1.1/"
               "FSRCNNX_x2_8-0-4-1.glsl")


def _bundled_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for rel in ("rtsp_tool/shaders", "shaders"):
        d = base / rel
        if d.is_dir():
            return d
    return Path(__file__).resolve().parent / "shaders"


def user_shaders_dir() -> Path:
    """Dossier des shaders ajoutés par l'utilisateur (FSRCNNX téléchargé…)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        d = Path(base) / "RTSP-TOOL" / "shaders"
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        d = Path(base) / "rtsp-tool" / "shaders"
    return d


def _find(nom: str) -> str:
    """Chemin d'un shader (dossier utilisateur prioritaire, puis embarqué)."""
    for d in (user_shaders_dir(), _bundled_dir()):
        p = d / nom
        if p.is_file():
            return str(p)
    return ""


def fsrcnnx_present() -> bool:
    return bool(_find(FSRCNNX_NOM))


def _chain(*noms) -> list:
    return [c for c in (_find(n) for n in noms) if c]


def resolve(niveau: str, vue: str, src_h: int = 0) -> dict:
    """Réglages mpv pour (niveau, vue, hauteur source) : vf, glsl, deband, sharpen.

    Chaîne (validée en A/B sur substream CCTV réel) :
      1. DÉBLOCAGE (filtre ffmpeg pp7/spp) — supprime les macroblocs de compression,
         qui sont le vrai défaut des flux CCTV ; c'est l'étape qui « dé-pixelise ».
      2. upscale neuronal ×2 UNIQUEMENT si la source est réellement basse résolution
         (≤ 380 px de haut : CIF/QVGA) — sinon inutile voire contre-productif.
      3. accentuation adaptative CAS (shader GPU) — contours nets sans halo.
    """
    if niveau not in NIVEAUX:
        niveau = "off"
    if niveau == "off":
        return {"vf": "", "glsl": [], "deband": False, "sharpen": 0.0}

    cas = _chain("rtsp_tool_cas.glsl")
    if niveau == "leger":
        return {"vf": "lavfi=[pp7=qp=4:mode=medium]", "glsl": cas,
                "deband": True, "sharpen": (0.0 if cas else 0.5)}

    # niveau "sr" — déblocage FORT (+ IA si basse résolution)
    vf = "lavfi=[spp=4:6:1]" if vue == "mono" else "lavfi=[pp7=qp=5:mode=medium]"
    glsl = []
    if src_h and src_h <= SEUIL_SR_PX:            # vraie basse résolution : upscale IA utile
        if vue == "mono" and fsrcnnx_present():
            glsl += _chain(FSRCNNX_NOM)
        elif vue == "mono":
            glsl += _chain("Anime4K_Clamp_Highlights.glsl",
                           "Anime4K_Restore_CNN_M.glsl",
                           "Anime4K_Upscale_CNN_x2_M.glsl")
        else:
            glsl += _chain("Anime4K_Clamp_Highlights.glsl",
                           "Anime4K_Restore_CNN_S.glsl",
                           "Anime4K_Upscale_CNN_x2_S.glsl")
    glsl += cas
    return {"vf": vf, "glsl": glsl, "deband": True,
            "sharpen": (0.0 if cas else 0.6)}


def sr_disponible() -> bool:
    """L'amélioration est opérationnelle (déblocage ffmpeg + shader CAS embarqué)."""
    return True


def apply(player, niveau: str, vue: str):
    """Applique le niveau d'amélioration à une instance mpv (à chaud).

    La hauteur de la source décide de l'upscale neuronal (basse résolution seulement)."""
    if player is None:
        return
    try:
        src_h = int(player.height or 0)
    except Exception:
        src_h = 0
    r = resolve(niveau, vue, src_h)
    try:
        player["vf"] = r["vf"]
        player["glsl-shaders"] = r["glsl"]
        player["deband"] = r["deband"]
        player["sharpen"] = r["sharpen"]
    except Exception as e:
        logger.warning(f"amélioration '{niveau}' non appliquée : {e}")


def download_fsrcnnx(timeout: int = 60) -> tuple[bool, str]:
    """Télécharge FSRCNNX dans le dossier utilisateur. (ok, message)."""
    import requests
    dest_dir = user_shaders_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / FSRCNNX_NOM
    try:
        r = requests.get(FSRCNNX_URL, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        if b"//!HOOK" not in r.content[:4000] and b"gl_FragColor" not in r.content:
            return False, "fichier reçu invalide"
        dest.write_bytes(r.content)
        return True, str(dest)
    except Exception as e:
        return False, str(e)
