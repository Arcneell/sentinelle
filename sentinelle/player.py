"""Chargement de libmpv et création des lecteurs vidéo."""

import os
import sys
from pathlib import Path


def _prepare_dll_dirs():
    # Windows : trouver libmpv-2.dll (dossier de l'exe, lib/, puis PATH).
    if sys.platform != "win32":
        return
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent)
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS))
    candidates.append(Path(__file__).resolve().parent.parent / "lib")
    for d in candidates:
        if d.is_dir() and any(d.glob("*mpv*.dll")):
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


mpv = None
MPV_IMPORT_ERROR = None
try:
    _prepare_dll_dirs()
    import mpv as _mpv
    mpv = _mpv
except Exception as e:
    MPV_IMPORT_ERROR = str(e)


def mpv_disponible() -> bool:
    return mpv is not None


def create_player(wid: int, log_handler=None):
    """Instance mpv embarquée dans le widget natif `wid`, réglée pour du RTSP
    basse latence avec un upscaling soigné des substreams.

    Sortie vidéo overridable par l'environnement pour les pilotes GPU fragiles
    (cf. --safe-video) : SENTINELLE_MPV_VO / SENTINELLE_MPV_HWDEC. En mode sûr
    (vo=x11, hwdec=no), on n'active PAS les scalers GPU (ignorés sans OpenGL,
    et sources de crash sur certains pilotes)."""
    if mpv is None:
        raise RuntimeError(f"libmpv indisponible : {MPV_IMPORT_ERROR}")

    vo = os.environ.get("SENTINELLE_MPV_VO", "gpu")
    hwdec = os.environ.get("SENTINELLE_MPV_HWDEC", "auto-safe")

    opts = dict(
        wid=str(int(wid)),
        log_handler=log_handler,
        rtsp_transport="tcp",
        network_timeout=15,
        profile="low-latency",
        vo=vo,
        hwdec=hwdec,
        keepaspect=True,
        audio="no",
        osc=False,
        osd_level=0,
        input_default_bindings=False,
        input_vo_keyboard=False,
        input_cursor=False,
    )
    if vo == "gpu":
        # Upscaling : rend un substream basse résolution nettement plus net une
        # fois étiré sur une grande tuile (scaler à lobes elliptiques + sigmoïde
        # anti-halo + deband pour effacer le blocking JPEG/H.264). Réservé au vo
        # GPU (OpenGL) ; inutile/ignoré avec une sortie logicielle.
        opts.update(
            scale="ewa_lanczossharp",
            cscale="ewa_lanczossharp",
            dscale="mitchell",
            sigmoid_upscaling=True,
            deband=True,
        )
    return mpv.MPV(**opts)
