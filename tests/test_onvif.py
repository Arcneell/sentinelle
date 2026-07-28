"""ONVIF : parsing de la découverte WS-Discovery, injection d'identifiants,
masquage d'URL. Logique réseau/parsing critique, jusque-là non couverte."""

from sentinelle.config import mask_url
from sentinelle.onvif import _parse_probe_match, inject_auth

_PROBE_MATCH = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
    '<e:Body><d:ProbeMatches><d:ProbeMatch>'
    '<d:Types>dn:NetworkVideoTransmitter</d:Types>'
    '<d:Scopes>onvif://www.onvif.org/name/CAM-1 onvif://www.onvif.org/hardware/X</d:Scopes>'
    '<d:XAddrs>http://192.0.2.5/onvif/device_service</d:XAddrs>'
    '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
).encode()


def test_parse_probe_match():
    dev = _parse_probe_match(_PROBE_MATCH)
    assert dev is not None
    assert dev.xaddr == "http://192.0.2.5/onvif/device_service"
    assert dev.host == "192.0.2.5"
    assert dev.nom == "CAM-1"                       # extrait du scope name/


def test_parse_probe_match_invalide():
    assert _parse_probe_match(b"pas du xml") is None
    # une réponse sans XAddrs n'est pas exploitable
    assert _parse_probe_match(b"<a><b/></a>") is None


def test_inject_auth():
    out = inject_auth("rtsp://192.0.2.5:554/stream", "user", "p@ss")
    assert out == "rtsp://user:p%40ss@192.0.2.5:554/stream"
    # déjà des identifiants → inchangé ; pas d'utilisateur → inchangé
    assert inject_auth("rtsp://u:x@h/s", "user", "p") == "rtsp://u:x@h/s"
    assert inject_auth("rtsp://h/s", "", "p") == "rtsp://h/s"


def test_mask_url():
    assert mask_url("rtsp://user:secret@192.0.2.5:554/live") == \
        "rtsp://user:***@192.0.2.5:554/live"
    # un « @ » dans le chemin ne doit pas fausser l'hôte affiché
    assert mask_url("rtsp://user:secret@192.0.2.5/path@2") == \
        "rtsp://user:***@192.0.2.5/path@2"
    # pas d'identifiants → inchangé
    assert mask_url("rtsp://192.0.2.5/live") == "rtsp://192.0.2.5/live"
