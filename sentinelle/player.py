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
    basse latence, privilégiant la ROBUSTESSE sur la qualité d'image.

    Rendu par défaut :
      - Linux  : logiciel (vo=x11, sans OpenGL). Les pilotes GPU des postes
                 muraux (NVIDIA sans backend, notamment) font planter le chemin
                 OpenGL de mpv — voire tout le système. Pas d'upscaling.
      - Windows : vo=gpu (Direct3D, fiable sur ce système).

    Tout est surchargeable par l'environnement :
      SENTINELLE_MPV_VO    (ex. gpu si le poste a un pilote fiable)
      SENTINELLE_MPV_HWDEC (ex. vaapi-copy / nvdec-copy pour décharger le
                            décodage sur un GPU SANS utiliser le rendu OpenGL —
                            indispensable pour tenir un mur de flux sur un CPU
                            modeste).

    Décodage allégé (saut du filtre anti-blocs) pour tenir plus de tuiles en
    décodage logiciel : image un peu plus « blocs », charge CPU nettement
    réduite. Ces réglages sont ignorés si le décodage est matériel."""
    if mpv is None:
        raise RuntimeError(f"libmpv indisponible : {MPV_IMPORT_ERROR}")

    if sys.platform == "win32":
        vo_defaut, hwdec_defaut = "gpu", "auto-safe"
        profil = "low-latency"
    else:
        # Linux : décodage MATÉRIEL via VA-API (iGPU Intel/AMD), image copiée en
        # RAM — décharge complètement le CPU sans jamais toucher au rendu OpenGL
        # (qui plante sur ces pilotes et peut couper net les mini-PC). Se
        # rabat tout seul sur le logiciel si VA-API est absent ; rendu x11.
        vo_defaut, hwdec_defaut = "x11", "vaapi-copy"
        # sw-fast : la mise à l'échelle de vo=x11 se fait sur le CPU, à la taille
        # de la fenêtre, pour CHAQUE trame. Sans ce profil, mpv utilise Lanczos +
        # dithering (des Gops/s en plein écran) : c'est ce qui saturait le CPU au
        # passage en plein écran, moment où les mini-PC s'éteignaient net. Ce
        # n'est PAS thermique (machine froide, plante dès l'allumage) : pointe de
        # charge qui fait vraisemblablement décrocher l'alimentation ou le pilote.
        # sw-fast = bilinéaire sans dithering (mpv >= 0.34).
        profil = "low-latency,sw-fast"
    vo = os.environ.get("SENTINELLE_MPV_VO", vo_defaut)
    hwdec = os.environ.get("SENTINELLE_MPV_HWDEC", hwdec_defaut)

    opts = dict(
        wid=str(int(wid)),
        log_handler=log_handler,
        rtsp_transport="tcp",
        network_timeout=15,
        profile=profil,
        vo=vo,
        hwdec=hwdec,
        keepaspect=True,
        audio="no",
        osc=False,
        osd_level=0,
        input_default_bindings=False,
        input_vo_keyboard=False,
        input_cursor=False,
        # mur multi-flux : abandonner les trames en retard plutôt que d'accumuler,
        framedrop="vo",
        # et sauter le filtre anti-blocs (~30 % du coût de décodage en moins) —
        # sans effet en décodage matériel.
        vd_lavc_skiploopfilter="all",
        vd_lavc_fast=True,
    )
    if sys.platform != "win32":
        # low-latency verrouille vd-lavc-threads=1 : correct en décodage matériel,
        # mais quand VA-API manque (pilote absent, mini-PC NVIDIA nu) le repli
        # logiciel décodait un mainstream HD sur UN seul cœur. On rétablit l'auto
        # (l'ordre compte : ces clés, après `profile`, écrasent celles du profil).
        opts["vd_lavc_threads"] = 0
        # borne le pool de mise à l'échelle par instance : sinon chaque tuile crée
        # nproc threads zimg (16 tuiles × nproc sur un mur complet).
        opts["zimg_threads"] = 2
    try:
        return mpv.MPV(**opts)
    except (AttributeError, ValueError, TypeError) as e:
        # option/valeur inconnue de la libmpv chargée (ex. profil sw-fast absent
        # d'un mpv < 0.34) : MPV() lève à la construction. On réessaie en
        # configuration minimale plutôt que de laisser toutes les tuiles mortes.
        for k in ("vd_lavc_threads", "zimg_threads"):
            opts.pop(k, None)
        opts["profile"] = "low-latency"
        import logging
        logging.getLogger(__name__).warning(
            f"Options mpv réduites (libmpv ancienne ? {e}) — "
            "mise à l'échelle rapide indisponible")
        return mpv.MPV(**opts)
