"""Support ONVIF — le standard universel des caméras/NVR IP.

Deux usages :
  1. Découverte réseau (WS-Discovery) : trouve les appareils ONVIF du LAN sans
     rien connaître de leur marque ni de leur schéma d'URL.
  2. Client ONVIF minimal (SOAP sur HTTP, auth WS-UsernameToken PasswordDigest) :
     récupère les URLs RTSP des profils (HD + sub), l'URL snapshot, et pilote le
     PTZ (déplacement continu + stop).

Implémentation sans dépendance lourde (pas de zeep) : SOAP construit à la main,
réponses lues en ignorant les préfixes de namespace (comme snapshot.py).
"""

import base64
import hashlib
import os
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import requests

WSD_ADDR = "239.255.255.250"
WSD_PORT = 3702


def _soap_timeout() -> int:
    """Délai des appels ONVIF, en secondes. Surchargé par
    SENTINELLE_ONVIF_TIMEOUT : sur un site 4G à forte latence ou un DVR qui
    redémarre, 6 s peut être trop court et faire échouer PTZ/détection en
    boucle plutôt qu'attendre un appareil momentanément lent."""
    try:
        return max(2, int(os.environ.get("SENTINELLE_ONVIF_TIMEOUT", "6")))
    except (TypeError, ValueError):
        return 6


SOAP_TIMEOUT = _soap_timeout()

_NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
_NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
_NS_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
_NS_EVENTS = "http://www.onvif.org/ver10/events/wsdl"
_NS_SCHEMA = "http://www.onvif.org/ver10/schema"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(elem, name: str):
    """Premier descendant dont le nom local == name (namespace ignoré)."""
    for e in elem.iter():
        if _local(e.tag) == name:
            return e
    return None


def _findall(elem, name: str):
    return [e for e in elem.iter() if _local(e.tag) == name]


# ---------------------------------------------------------------- découverte

@dataclass
class OnvifDevice:
    xaddr: str                       # URL du service device (ex. http://192.0.2.5/onvif/device_service)
    types: str = ""
    scopes: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.xaddr).hostname or ""

    @property
    def nom(self) -> str:
        # extrait un nom lisible des scopes ONVIF (name/hardware) si présent
        for cle in ("name/", "hardware/"):
            i = self.scopes.find("onvif://www.onvif.org/" + cle)
            if i >= 0:
                bout = self.scopes[i + len("onvif://www.onvif.org/" + cle):].split()[0]
                val = requests.utils.unquote(bout).strip()
                if val:
                    return val
        return self.host


def discover(timeout: float = 4.0) -> list[OnvifDevice]:
    """WS-Discovery : renvoie les appareils ONVIF joignables sur le LAN."""
    msg_id = "urn:uuid:" + _uuid4()
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<e:Header>'
        f'<w:MessageID>{msg_id}</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">'
        'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        '</e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        '</e:Envelope>'
    ).encode()

    trouves: dict[str, OnvifDevice] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.6)
    try:
        # émettre depuis chaque interface locale (hôtes multi-NIC : Wi-Fi+Ethernet,
        # VPN…) — sinon la sonde ne part que par la route par défaut
        for src in _local_ips():
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(src))
            except OSError:
                pass
            for _ in range(2):          # l'UDP multicast se perd facilement
                try:
                    sock.sendto(probe, (WSD_ADDR, WSD_PORT))
                except OSError:
                    pass

        fin = _monotonic() + timeout
        while _monotonic() < fin:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # ex. Windows : une caméra qui ne répond pas au probe multicast
                # provoque un ICMP Port Unreachable, remonté ici en
                # WSAECONNRESET. Ne PAS arrêter la découverte pour autant — les
                # autres appareils répondent encore ; on ignore ce paquet et on
                # continue jusqu'à l'expiration du délai.
                continue
            dev = _parse_probe_match(data)
            if dev and dev.xaddr and dev.xaddr not in trouves:
                trouves[dev.xaddr] = dev
    finally:
        sock.close()
    return list(trouves.values())


def _parse_probe_match(data: bytes) -> OnvifDevice | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    xaddrs = _find(root, "XAddrs")
    if xaddrs is None or not (xaddrs.text or "").strip():
        return None
    # XAddrs peut lister plusieurs URLs : on prend la première http(s)
    url = next((u for u in xaddrs.text.split() if u.startswith("http")), "")
    types = _find(root, "Types")
    scopes = _find(root, "Scopes")
    return OnvifDevice(
        xaddr=url,
        types=(types.text or "").strip() if types is not None else "",
        scopes=(scopes.text or "").strip() if scopes is not None else "",
    )


# ------------------------------------------------------------- client ONVIF

@dataclass
class OnvifProfile:
    token: str
    nom: str = ""
    rtsp: str = ""                   # URL RTSP (sans identifiants)
    largeur: int = 0
    hauteur: int = 0
    ptz: bool = False


@dataclass
class OnvifResult:
    profils: list = field(default_factory=list)   # [OnvifProfile], du + défini au - défini
    snapshot: str = ""
    erreur: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.profils) and not self.erreur

    def principal(self) -> OnvifProfile | None:
        return self.profils[0] if self.profils else None

    def secondaire(self) -> OnvifProfile | None:
        return self.profils[-1] if self.profils else None


class OnvifCamera:
    """Client ONVIF minimal pour un appareil (host + identifiants)."""

    def __init__(self, host: str, user: str, password: str, port: int = 80,
                 device_xaddr: str = ""):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self._device_url = device_xaddr or f"http://{host}:{port}/onvif/device_service"
        self._media_url = ""
        self._ptz_url = ""
        self._events_url = ""

    # --- SOAP ---

    def _security_header(self) -> str:
        nonce = os.urandom(16)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        nonce_b64 = base64.b64encode(nonce).decode()
        wsse = ("http://docs.oasis-open.org/wss/2004/01/"
                "oasis-200401-wss-wssecurity-secext-1.0.xsd")
        wsu = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-wssecurity-utility-1.0.xsd")
        pwd_type = ("http://docs.oasis-open.org/wss/2004/01/"
                    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
        enc_type = ("http://docs.oasis-open.org/wss/2004/01/"
                    "oasis-200401-wss-soap-message-security-1.0#Base64Binary")
        return (
            f'<Security s:mustUnderstand="1" xmlns="{wsse}">'
            f'<UsernameToken>'
            f'<Username>{_esc(self.user)}</Username>'
            f'<Password Type="{pwd_type}">{digest}</Password>'
            f'<Nonce EncodingType="{enc_type}">{nonce_b64}</Nonce>'
            f'<Created xmlns="{wsu}">{created}</Created>'
            f'</UsernameToken></Security>'
        )

    def _call(self, url: str, body: str, timeout: int = SOAP_TIMEOUT) -> ET.Element:
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f'<s:Header>{self._security_header()}</s:Header>'
            f'<s:Body>{body}</s:Body></s:Envelope>'
        )
        r = requests.post(url, data=envelope.encode(),
                          headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                          timeout=timeout)
        if r.status_code == 401:
            raise PermissionError("ONVIF : identifiants refusés (401)")
        root = ET.fromstring(r.content)
        fault = _find(root, "Fault")
        if fault is not None:
            reason = _find(fault, "Text") or _find(fault, "faultstring")
            txt = (reason.text if reason is not None else "").strip()
            if "auth" in txt.lower() or "NotAuthorized" in ET.tostring(fault, "unicode"):
                raise PermissionError(f"ONVIF : accès refusé ({txt})")
            raise RuntimeError(f"ONVIF fault : {txt or 'inconnu'}")
        return root

    # --- endpoints (Media / PTZ) ---

    def _resolve_services(self):
        if self._media_url:
            return
        body = (f'<GetCapabilities xmlns="{_NS_DEVICE}">'
                '<Category>All</Category></GetCapabilities>')
        try:
            root = self._call(self._device_url, body)
        except Exception:
            root = None
        if root is not None:
            media = _find(root, "Media")
            if media is not None:
                x = _find(media, "XAddr")
                if x is not None and x.text:
                    self._media_url = x.text.strip()
            ptz = _find(root, "PTZ")
            if ptz is not None:
                x = _find(ptz, "XAddr")
                if x is not None and x.text:
                    self._ptz_url = x.text.strip()
            events = _find(root, "Events")
            if events is not None:
                x = _find(events, "XAddr")
                if x is not None and x.text:
                    self._events_url = x.text.strip()
        # replis usuels si GetCapabilities muet
        if not self._media_url:
            self._media_url = f"http://{self.host}:{self.port}/onvif/Media"
        if not self._ptz_url:
            self._ptz_url = f"http://{self.host}:{self.port}/onvif/PTZ"
        if not self._events_url:
            self._events_url = f"http://{self.host}:{self.port}/onvif/Events"

    # --- API haut niveau ---

    def profils(self) -> OnvifResult:
        """Récupère les profils (URLs RTSP HD + sub), l'URL snapshot, le PTZ."""
        res = OnvifResult()
        try:
            self._resolve_services()
            root = self._call(self._media_url,
                              f'<GetProfiles xmlns="{_NS_MEDIA}"/>')
        except PermissionError as e:
            res.erreur = str(e); return res
        except requests.exceptions.RequestException as e:
            res.erreur = f"appareil injoignable ({type(e).__name__})"; return res
        except (ET.ParseError, RuntimeError) as e:
            res.erreur = str(e); return res

        for prof in _findall(root, "Profiles"):
            token = prof.get("token") or ""
            if not token:
                continue
            p = OnvifProfile(token=token)
            nom = _find(prof, "Name")
            if nom is not None:
                p.nom = (nom.text or "").strip()
            res_el = _find(prof, "Resolution")
            if res_el is not None:
                w, h = _find(res_el, "Width"), _find(res_el, "Height")
                try:
                    p.largeur = int(w.text) if w is not None and w.text else 0
                    p.hauteur = int(h.text) if h is not None and h.text else 0
                except (ValueError, TypeError):
                    p.largeur = p.hauteur = 0
            p.ptz = _find(prof, "PTZConfiguration") is not None
            res.profils.append(p)

        if not res.profils:
            res.erreur = "aucun profil ONVIF exposé"
            return res

        # trie du plus défini au moins défini (HD d'abord)
        res.profils.sort(key=lambda p: p.largeur * p.hauteur, reverse=True)

        for p in res.profils:
            try:
                p.rtsp = self._stream_uri(p.token)
            except Exception:
                p.rtsp = ""
        res.profils = [p for p in res.profils if p.rtsp]
        if not res.profils:
            res.erreur = "profils sans URL RTSP exploitable"
            return res

        try:
            res.snapshot = self._snapshot_uri(res.profils[0].token)
        except Exception:
            res.snapshot = ""
        return res

    def _stream_uri(self, token: str) -> str:
        body = (
            f'<GetStreamUri xmlns="{_NS_MEDIA}"><StreamSetup>'
            f'<Stream xmlns="{_NS_SCHEMA}">RTP-Unicast</Stream>'
            f'<Transport xmlns="{_NS_SCHEMA}"><Protocol>RTSP</Protocol></Transport>'
            f'</StreamSetup><ProfileToken>{_esc(token)}</ProfileToken></GetStreamUri>'
        )
        root = self._call(self._media_url, body)
        uri = _find(root, "Uri")
        return (uri.text or "").strip() if uri is not None else ""

    def _snapshot_uri(self, token: str) -> str:
        body = (f'<GetSnapshotUri xmlns="{_NS_MEDIA}">'
                f'<ProfileToken>{_esc(token)}</ProfileToken></GetSnapshotUri>')
        root = self._call(self._media_url, body)
        uri = _find(root, "Uri")
        return (uri.text or "").strip() if uri is not None else ""

    def ptz_move(self, token: str, pan: float, tilt: float, zoom: float = 0.0,
                 timeout_ptz: str = "PT3S"):
        # Timeout : filet de sécurité — la caméra s'arrête d'elle-même au bout de
        # ce délai si le Stop se perd (sinon risque de mouvement sans fin).
        self._resolve_services()
        body = (
            f'<ContinuousMove xmlns="{_NS_PTZ}">'
            f'<ProfileToken>{_esc(token)}</ProfileToken><Velocity>'
            f'<PanTilt x="{pan:.2f}" y="{tilt:.2f}" xmlns="{_NS_SCHEMA}"/>'
            f'<Zoom x="{zoom:.2f}" xmlns="{_NS_SCHEMA}"/>'
            f'</Velocity><Timeout>{timeout_ptz}</Timeout></ContinuousMove>'
        )
        self._call(self._ptz_url, body)

    def ptz_stop(self, token: str):
        self._resolve_services()
        body = (f'<Stop xmlns="{_NS_PTZ}"><ProfileToken>{_esc(token)}</ProfileToken>'
                f'<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>')
        self._call(self._ptz_url, body)

    # --- Événements de mouvement (ONVIF Events / PullPoint) ---

    def abonner_mouvement(self, duree: str = "PT1M") -> str:
        """Crée un abonnement PullPoint aux événements. Retourne l'URL de tirage."""
        self._resolve_services()
        body = (f'<CreatePullPointSubscription xmlns="{_NS_EVENTS}">'
                f'<InitialTerminationTime>{duree}</InitialTerminationTime>'
                f'</CreatePullPointSubscription>')
        root = self._call(self._events_url, body)
        # SubscriptionReference/Address (WS-Addressing)
        for ref in _findall(root, "SubscriptionReference"):
            adr = _find(ref, "Address")
            if adr is not None and (adr.text or "").strip():
                return adr.text.strip()
        adr = _find(root, "Address")
        return (adr.text or "").strip() if adr is not None else self._events_url

    def desabonner_mouvement(self, endpoint: str):
        """Termine explicitement un abonnement PullPoint (WS-BaseNotification
        Unsubscribe). Best-effort : sans cela l'abonnement n'expire qu'au bout de
        son InitialTerminationTime, et les renouvellements successifs peuvent
        empiler des abonnements sur un DVR à quota de sessions limité."""
        if not endpoint:
            return
        body = '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>'
        try:
            self._call(endpoint, body)
        except Exception:
            pass

    def tirer_mouvement(self, endpoint: str, timeout: str = "PT5S",
                        limite: int = 20) -> list:
        """PullMessages. Retourne [(source, actif: bool)] pour les événements de mouvement."""
        body = (f'<PullMessages xmlns="{_NS_EVENTS}">'
                f'<Timeout>{timeout}</Timeout><MessageLimit>{limite}</MessageLimit>'
                f'</PullMessages>')
        root = self._call(endpoint or self._events_url, body, timeout=12)
        resultats = []
        for msg in _findall(root, "NotificationMessage"):
            topic_el = _find(msg, "Topic")
            topic = (topic_el.text or "") if topic_el is not None else ""
            # état : SimpleItem Name in {IsMotion,State,Motion} Value in {true,false}
            actif = None
            source = ""
            for item in _findall(msg, "SimpleItem"):
                nom = (item.get("Name") or "")
                val = (item.get("Value") or "")
                if nom.lower() in ("ismotion", "state", "motion", "motionalarm"):
                    actif = val.strip().lower() in ("true", "1")
                elif nom.lower() in ("source", "videosourceconfigurationtoken",
                                     "videosourcetoken", "channel", "token"):
                    source = val.strip()
            est_mouvement = ("motion" in topic.lower() or actif is not None)
            if est_mouvement and actif is not None:
                resultats.append((source, actif))
        return resultats

    def a_les_evenements(self) -> bool:
        """Vérifie que le service Events répond (abonnement test)."""
        try:
            self.abonner_mouvement("PT10S")
            return True
        except Exception:
            return False


# ------------------------------------------------------------------- utils

def inject_auth(url: str, user: str, password: str) -> str:
    """rtsp://host/path -> rtsp://user:pass@host/path (identifiants pour la lecture)."""
    if not user or not url or "@" in url.split("://", 1)[-1].split("/", 1)[0]:
        return url
    from urllib.parse import quote
    p = urlparse(url)
    cred = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    netloc = cred + p.netloc
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _uuid4() -> str:
    import uuid
    return str(uuid.uuid4())


def _monotonic() -> float:
    import time
    return time.monotonic()


def _local_ips() -> list:
    """Adresses IPv4 locales (une par interface), pour émettre la sonde partout."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    ips.add("0.0.0.0")          # interface par défaut, toujours tentée
    return list(ips)
