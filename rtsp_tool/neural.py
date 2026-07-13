"""Reconstruction neuronale d'image EN TEMPS RÉEL.

Pipeline : OpenCV décode le flux RTSP → chaque frame est réduite puis passée dans
un petit réseau de super-résolution vidéo (Real-ESRGAN « animevideov3 », exécuté
en Vulkan par ncnn) qui reconstruit contours et surfaces → affichage.

Réduire l'entrée AVANT le réseau fait deux choses : ça élimine le bruit de blocs
(moyennage) et ça garde le débit temps réel (le coût du réseau ∝ pixels d'entrée).
Mesuré sur GPU Intel intégré : 320×180→640×360 ≈ 30 fps, 480×270→960×540 ≈ 15 fps.

Dépendances chargées à la demande : `ncnn` (pip) + `opencv-python` + le modèle
(fourni avec le moteur Real-ESRGAN téléchargé par reconstruct.py). Si l'une manque,
disponible() renvoie False et l'appli reste utilisable sans cette option.
"""

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MODELE = "realesr-animevideov3-x2"          # variante vidéo (légère, x2)
CIBLES = {"fluide": 180, "equilibre": 270, "qualite": 360}   # hauteur d'entrée
CIBLE_DEFAUT = "equilibre"

_net = None
_net_lock = threading.Lock()


def _models_dir() -> Path:
    from .reconstruct import _tools_dir
    return _tools_dir() / "models"


def _deps_ok() -> tuple:
    try:
        import cv2  # noqa
        import ncnn  # noqa
    except Exception:
        return (False, None, None)
    d = _models_dir()
    return (True, d / f"{MODELE}.param", d / f"{MODELE}.bin")


def disponible() -> bool:
    ok, p, b = _deps_ok()
    return ok and p.is_file() and b.is_file()


def _get_net():
    global _net
    if _net is not None:
        return _net
    import ncnn
    _, param, binf = _deps_ok()
    net = ncnn.Net()
    net.opt.use_vulkan_compute = True
    net.opt.num_threads = 4
    net.load_param(str(param))
    net.load_model(str(binf))
    _net = net
    return _net


def _super_resolve(rgb):
    """rgb (H,W,3) uint8 -> (2H,2W,3) uint8 reconstruit."""
    import ncnn
    import numpy as np
    h, w, _ = rgb.shape
    m = ncnn.Mat.from_pixels(np.ascontiguousarray(rgb).tobytes(),
                             ncnn.Mat.PixelType.PIXEL_RGB, w, h)
    m.substract_mean_normalize([0., 0., 0.], [1/255., 1/255., 1/255.])
    with _net_lock:                          # une inférence à la fois (GPU partagé)
        ex = _get_net().create_extractor()
        ex.input("data", m)
        _, out = ex.extract("output")
    arr = np.array(out)                       # (3,H,W) float 0..1
    arr = np.clip(arr.transpose(1, 2, 0) * 255, 0, 255).astype(np.uint8)
    # C-contigu obligatoire : QImage refuse un buffer à strides transposés
    return np.ascontiguousarray(arr)


class NeuralWorker(threading.Thread):
    """Décode le flux et reconstruit les frames. Deux threads :
      - un LECTEUR qui décode en continu et ne garde que la DERNIÈRE image
        (indispensable quand plusieurs tuiles se partagent le GPU : sans ça,
        le retard s'accumule — ici la latence reste bornée à ~1 inférence) ;
      - la boucle SR (ce thread) qui traite toujours l'image la plus fraîche.
    Callbacks via signaux Qt fournis par la tuile : on_frame(rgb), on_state(str)."""

    def __init__(self, url: str, cible: str, on_frame, on_state):
        super().__init__(daemon=True, name="neural")
        self.url = url
        self.target_h = CIBLES.get(cible, CIBLES[CIBLE_DEFAUT])
        self._on_frame = on_frame
        self._on_state = on_state
        self._stop = threading.Event()
        self._latest = None
        self._latest_id = 0
        self._verrou = threading.Lock()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------- lecteur

    def _lire(self):
        import cv2
        echecs = 0
        while not self._stop.is_set():
            self._on_state("Connexion…")
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if not cap.isOpened():
                cap.release()
                echecs += 1
                self._on_state("Flux injoignable — nouvel essai"
                               if echecs < 5 else "Flux injoignable")
                self._attendre(min(2 ** echecs, 30))
                continue
            echecs = 0
            vides = 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    vides += 1
                    if vides > 10:
                        break
                    time.sleep(0.05)
                    continue
                vides = 0
                with self._verrou:
                    self._latest = frame          # écrase : on ne garde que la dernière
                    self._latest_id += 1
            cap.release()

    # ------------------------------------------------------------ boucle SR

    def run(self):
        import cv2
        lecteur = threading.Thread(target=self._lire, daemon=True,
                                   name="neural-lecteur")
        lecteur.start()
        traite = 0
        while not self._stop.is_set():
            with self._verrou:
                frame, fid = self._latest, self._latest_id
            if frame is None or fid == traite:
                time.sleep(0.01)
                continue
            traite = fid
            try:
                th = self.target_h
                tw = int(frame.shape[1] * th / frame.shape[0]) // 2 * 2
                small = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                out = _super_resolve(rgb)
            except Exception as e:
                logger.warning(f"neural: frame ignorée ({e})")
                continue
            if not self._stop.is_set():
                self._on_frame(out)

    def _attendre(self, s):
        fin = time.time() + s
        while time.time() < fin and not self._stop.is_set():
            time.sleep(0.1)
