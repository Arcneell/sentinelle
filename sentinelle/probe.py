"""Diagnostic des échecs de connexion RTSP.

Deux sources, dans l'ordre :
  1. les logs mpv/ffmpeg collectés par la tuile (toujours disponibles) ;
  2. ffprobe si présent sur la machine (optionnel, plus précis).

Verdicts : auth | timeout | network | other.
'auth' déclenche l'ARRÊT DÉFINITIF des tentatives (pattern vision-ai) : les
DVR Hikvision lockent le compte après quelques échecs d'authentification, et
nos rotations ré-ouvrent des flux en permanence.
"""

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 8

_AUTH_MARKERS = ("401", "unauthorized", "authentication", "auth failed")
_NETWORK_MARKERS = ("refused", "no route", "unreachable", "network is down",
                    "failed to resolve", "name or service not known", "host is down")
_TIMEOUT_MARKERS = ("timed out", "timeout")


def classify_text(text: str) -> str:
    """Classe un texte d'erreur (stderr ffprobe ou logs mpv concaténés)."""
    s = (text or "").lower()
    if any(m in s for m in _AUTH_MARKERS):
        return "auth"
    # réseau avant timeout : « Connection to tcp://… failed: Connection refused »
    # contient les deux, mais c'est bien un refus, pas un délai dépassé
    if any(m in s for m in _NETWORK_MARKERS):
        return "network"
    if any(m in s for m in _TIMEOUT_MARKERS):
        return "timeout"
    return "other"


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


_ffprobe_absent_signale = False


def avertir_ffprobe_absent():
    """Signale UNE fois que le diagnostic fin est indisponible (ffprobe est un
    simple Recommends du .deb : absent après un dpkg -i, sans aucune trace)."""
    global _ffprobe_absent_signale
    if not _ffprobe_absent_signale:
        _ffprobe_absent_signale = True
        logger.warning("ffprobe absent — diagnostic RTSP réduit "
                       "(Debian : apt install ffmpeg)")


def _env_sain() -> dict:
    """Environnement pour lancer un exécutable SYSTÈME depuis l'app PyInstaller.

    Le bootloader PyInstaller préfixe LD_LIBRARY_PATH avec le dossier de l'app :
    le ffprobe du système chargerait alors nos .so embarqués (d'une autre
    version de Debian) et planterait sur un conflit de symboles. On restaure
    la valeur d'origine (LD_LIBRARY_PATH_ORIG), sinon on retire la variable."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
        orig = env.get("LD_LIBRARY_PATH_ORIG")
        if orig:
            env["LD_LIBRARY_PATH"] = orig
    return env


def probe_rtsp(url: str, timeout_s: int = PROBE_TIMEOUT) -> tuple[str, str]:
    """Diagnostic ffprobe d'une URL RTSP. Retourne (kind, detail),
    kind ∈ {ok, auth, timeout, network, other, unavailable}."""
    if not ffprobe_available():
        return "unavailable", "ffprobe absent"
    try:
        r = subprocess.run(
            ["ffprobe", "-rtsp_transport", "tcp", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=timeout_s + 5,
            env=_env_sain(),
        )
        if r.returncode == 0 and r.stdout.strip():
            return "ok", ""
        return classify_text(r.stderr), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "timeout", "ffprobe timeout"
    except Exception as e:
        return "other", str(e)
