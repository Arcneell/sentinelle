"""Optimisation du substream côté DVR (marques Dahua).

Le vrai levier de qualité d'un flux de vidéosurveillance est l'encodage réglé
DANS le DVR, pas le post-traitement côté client. Beaucoup de DVR Dahua sortent
d'usine leur substream en MJPEG (chaque image = un JPEG indépendant) à bas débit,
ce qui donne une image très « blocs ». Passer le substream en H.264 au même débit
transforme l'image à la source.

Ce module lit et modifie la config d'encodage du substream via l'API HTTP CGI
Dahua (auth Digest). Il ne touche QUE l'ExtraFormat (substream) : ni le flux
principal, ni l'enregistrement.
"""

import logging

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

logger = logging.getLogger(__name__)

TIMEOUT = 8


def _get(base, path, user, pwd):
    url = base + path
    r = requests.get(url, auth=HTTPDigestAuth(user, pwd), timeout=TIMEOUT)
    if r.status_code == 401 and user:
        r = requests.get(url, auth=HTTPBasicAuth(user, pwd), timeout=TIMEOUT)
    return r


def lire_substream(cam) -> dict:
    """Retourne l'état du substream : {ok, compression, bitrate, fps, w, h, erreur}."""
    base = f"http://{cam.hote}:{cam.port_http}"
    res = {"ok": False, "erreur": ""}
    try:
        r = _get(base, "/cgi-bin/configManager.cgi?action=getConfig&name=Encode",
                 cam.user, cam.password)
        if r.status_code == 401:
            res["erreur"] = "identifiants refusés"; return res
        if r.status_code != 200:
            res["erreur"] = f"HTTP {r.status_code}"; return res
        vals = {}
        for l in r.text.splitlines():
            if "].ExtraFormat[0].Video." in l and "=" in l:
                cle, v = l.split("=", 1)
                vals[cle.split(".Video.")[-1].strip()] = v.strip()
        if not vals:
            res["erreur"] = "réponse inattendue (DVR non Dahua ?)"; return res
        res.update(ok=True, compression=vals.get("Compression", "?"),
                   bitrate=vals.get("BitRate", "?"), fps=vals.get("FPS", "?"),
                   w=vals.get("Width", "?"), h=vals.get("Height", "?"))
        return res
    except requests.exceptions.RequestException as e:
        res["erreur"] = f"injoignable ({type(e).__name__})"
        return res


def optimiser_substream(cam, bitrate: int = 1024, fps: int = 15) -> tuple[bool, str]:
    """Passe le substream en H.264 (bitrate/fps donnés). (ok, message)."""
    base = f"http://{cam.hote}:{cam.port_http}"
    params = [
        "Encode[0].ExtraFormat[0].Video.Compression=H.264",
        f"Encode[0].ExtraFormat[0].Video.FPS={fps}",
        f"Encode[0].ExtraFormat[0].Video.BitRate={bitrate}",
        "Encode[0].ExtraFormat[0].Video.BitRateControl=CBR",
    ]
    try:
        r = _get(base, "/cgi-bin/configManager.cgi?action=setConfig&" + "&".join(params),
                 cam.user, cam.password)
        if r.status_code == 401:
            return False, "identifiants refusés"
        if r.status_code == 200 and "OK" in r.text:
            logger.info(f"[{cam.id}] substream -> H.264 {bitrate}k/{fps}ips")
            return True, "H.264 activé"
        return False, f"refusé (HTTP {r.status_code})"
    except requests.exceptions.RequestException as e:
        return False, f"injoignable ({type(e).__name__})"
