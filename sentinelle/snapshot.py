"""Accès HTTP aux DVR : snapshots JPEG (mode photo) et interrogation ISAPI.

Auth : Digest d'abord (standard Hikvision/Dahua), repli Basic.
Verdicts d'erreur alignés sur probe.py : auth → arrêt définitif côté tuile.
"""

import logging
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

logger = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = 8


def _get(url: str, user: str, password: str, timeout: int):
    """GET avec Digest puis repli Basic. Retourne la Response."""
    r = requests.get(url, auth=HTTPDigestAuth(user, password), timeout=timeout)
    if r.status_code == 401 and user:
        r = requests.get(url, auth=HTTPBasicAuth(user, password), timeout=timeout)
    return r


def fetch_snapshot(url: str, user: str, password: str,
                   timeout: int = SNAPSHOT_TIMEOUT) -> tuple[bytes | None, str, str]:
    """Récupère une image JPEG. Retourne (data, kind, detail),
    kind ∈ {ok, auth, network, timeout, other}."""
    try:
        r = _get(url, user, password, timeout)
        if r.status_code == 401:
            return None, "auth", "401 Unauthorized"
        if r.status_code == 403:
            return None, "auth", "403 Forbidden (droits du compte DVR)"
        if r.status_code != 200:
            return None, "other", f"HTTP {r.status_code}"
        data = r.content
        if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")):
            return None, "other", "réponse non-image (endpoint snapshot indisponible ?)"
        return data, "ok", ""
    except requests.exceptions.Timeout:
        return None, "timeout", "timeout HTTP"
    except requests.exceptions.ConnectionError as e:
        return None, "network", str(e)[:200]
    except requests.exceptions.RequestException as e:
        return None, "other", str(e)[:200]


def lister_canaux_hikvision(hote: str, port_http: int, user: str, password: str,
                            timeout: int = 10) -> tuple[list[tuple[int, str]], str]:
    """Interroge l'ISAPI d'un DVR Hikvision et retourne ([(canal, nom)], erreur).

    Essaie /ISAPI/System/Video/inputs/channels (canaux analogiques, avec noms)
    puis /ISAPI/ContentMgmt/InputProxy/channels (caméras IP d'un NVR),
    puis /ISAPI/Streaming/channels en dernier recours.
    """
    base = f"http://{hote}:{port_http}"
    canaux: dict[int, str] = {}

    def _xml(url):
        r = _get(url, user, password, timeout)
        if r.status_code == 401:
            raise PermissionError("identifiants refusés (401)")
        if r.status_code != 200:
            return None
        return ET.fromstring(r.content)

    def _local(tag):
        return tag.rsplit("}", 1)[-1]

    def _int(el):
        return int(el.text) if el is not None and el.text and el.text.strip() else None

    try:
        # DVR analogiques / turbo HD
        root = _xml(f"{base}/ISAPI/System/Video/inputs/channels")
        if root is not None:
            for ch in root.iter():
                if _local(ch.tag) != "VideoInputChannel":
                    continue
                cid = nom = None
                for sub in ch:
                    if _local(sub.tag) == "id":
                        cid = _int(sub)
                    elif _local(sub.tag) == "name":
                        nom = (sub.text or "").strip()
                if cid is not None:
                    canaux[cid] = nom or f"Canal {cid}"

        # caméras IP raccordées à un NVR (canaux 33+, D1, D2…)
        root = _xml(f"{base}/ISAPI/ContentMgmt/InputProxy/channels")
        if root is not None:
            for ch in root.iter():
                if _local(ch.tag) != "InputProxyChannel":
                    continue
                cid = nom = None
                for sub in ch:
                    if _local(sub.tag) == "id":
                        cid = _int(sub)
                    elif _local(sub.tag) == "name":
                        nom = (sub.text or "").strip()
                if cid is not None:
                    canaux[cid] = nom or f"Caméra {cid}"

        # dernier recours : ids de streaming (101, 201…) sans nom
        if not canaux:
            root = _xml(f"{base}/ISAPI/Streaming/channels")
            if root is not None:
                for ch in root.iter():
                    if _local(ch.tag) != "StreamingChannel":
                        continue
                    for sub in ch:
                        if _local(sub.tag) == "id":
                            sid = _int(sub)
                            if sid is not None and sid % 100 == 1:   # mainstream → 1 canal
                                canaux[sid // 100] = f"Canal {sid // 100}"

        if not canaux:
            return [], "aucun canal trouvé (ISAPI indisponible sur ce DVR ?)"
        return sorted(canaux.items()), ""
    except PermissionError as e:
        return [], str(e)
    except requests.exceptions.Timeout:
        return [], "délai dépassé — DVR injoignable"
    except requests.exceptions.ConnectionError:
        return [], "connexion impossible (IP/port HTTP à vérifier)"
    except requests.exceptions.RequestException as e:
        return [], f"erreur réseau : {type(e).__name__}"
    except (ET.ParseError, ValueError, TypeError) as e:
        return [], f"réponse ISAPI illisible : {e}"
