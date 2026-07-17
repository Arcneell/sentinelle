"""Reconstruction d'image par IA générative (Real-ESRGAN).

Trop lourd pour la vidéo en direct (~5-20 s par image), mais capable de
RECONSTRUIRE entièrement une image de vidéosurveillance dégradée : le réseau
repeint des détails plausibles là où la compression a tout détruit.

Moteur : realesrgan-ncnn-vulkan (BSD-3-Clause, xinntao) — exécutable portable
Vulkan, fonctionne sur tout GPU (Intel/AMD/NVIDIA). Téléchargé à la demande
dans le profil utilisateur (~45 Mo), jamais redistribué par ce dépôt.

AVERTISSEMENT affiché à l'utilisateur : les détails produits sont INVENTÉS de
façon plausible par le réseau. Utile pour la lisibilité générale d'une scène,
mais une plaque ou un visage « reconstruit » n'a AUCUNE valeur d'identification.
"""

import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION = "v0.2.5.0"
_ASSETS = {
    "win32": "realesrgan-ncnn-vulkan-20220424-windows.zip",
    "linux": "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
}
MODELE = "realesrgan-x4plus"          # modèle photographique (x4)
TIMEOUT_S = 300


def _tools_dir() -> Path:
    from .config import app_data_dir
    return Path(app_data_dir()) / "tools" / "realesrgan"


def _exe_path() -> Path:
    nom = "realesrgan-ncnn-vulkan" + (".exe" if sys.platform == "win32" else "")
    return _tools_dir() / nom


def disponible() -> bool:
    return _exe_path().is_file()


def download_url() -> str:
    asset = _ASSETS.get("win32" if sys.platform == "win32" else "linux")
    return (f"https://github.com/xinntao/Real-ESRGAN/releases/download/"
            f"{VERSION}/{asset}")


def telecharger(timeout: int = 600) -> tuple[bool, str]:
    """Télécharge et extrait le moteur dans le profil utilisateur. (ok, message)."""
    import requests
    dest = _tools_dir()
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / "engine.zip"
    try:
        r = requests.get(download_url(), timeout=timeout, stream=True)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(dest)
        zip_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            _exe_path().chmod(0o755)
        return (True, str(_exe_path())) if disponible() else \
               (False, "archive extraite mais exécutable introuvable")
    except Exception as e:
        return False, str(e)


def reconstruire(src_png: str, dst_png: str) -> tuple[bool, str]:
    """Reconstruit src_png -> dst_png via Real-ESRGAN. (ok, message)."""
    if not disponible():
        return False, "moteur non installé"
    Path(dst_png).parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(_exe_path()), "-i", src_png, "-o", dst_png, "-n", MODELE]
    flags = 0x08000000 if sys.platform == "win32" else 0   # pas de fenêtre console
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, creationflags=flags)
    except subprocess.TimeoutExpired:
        return False, f"délai dépassé ({TIMEOUT_S}s)"
    except OSError as e:
        return False, f"lancement impossible : {e}"
    if not Path(dst_png).is_file():
        err = (r.stderr or r.stdout or "").strip()[-300:]
        return False, err or f"échec (code {r.returncode})"
    return True, dst_png
