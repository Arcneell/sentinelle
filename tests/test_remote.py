"""Client serveur (remote.py) : robustesse du décodage JSON et cloisonnement du
jeton relais (mot de passe RTSP distinct du jeton de session)."""

import pytest

from sentinelle.remote import ErreurServeur, ServeurDistant, _json


class _Reponse:
    def __init__(self, payload=None, boom=False, non_dict=False):
        self._payload = payload
        self._boom = boom
        self._non_dict = non_dict

    def json(self):
        if self._boom:
            raise ValueError("pas du JSON")     # cf. requests JSONDecodeError
        return ["liste"] if self._non_dict else (self._payload or {})


def test_json_leve_sur_reponse_non_json():
    # un portail captif 4G renvoie 200 + HTML : _json doit lever ErreurServeur
    # (typé) plutôt que laisser un JSONDecodeError tuer le thread appelant
    with pytest.raises(ErreurServeur):
        _json(_Reponse(boom=True))
    with pytest.raises(ErreurServeur):
        _json(_Reponse(non_dict=True))
    assert _json(_Reponse({"token": "x"})) == {"token": "x"}


def test_base_rtsp_utilise_jeton_relais():
    srv = ServeurDistant("http://192.0.2.9:8080", jeton="JETON-API")
    srv._relay_port = 8554
    srv._relay_jeton = "JETON-RELAY"
    url = srv._base_rtsp()
    # c'est le jeton RELAIS qui sert de mot de passe RTSP, pas le jeton de session
    assert url == "rtsp://sentinelle:JETON-RELAY@192.0.2.9:8554/"
    assert "JETON-API" not in url


def test_base_rtsp_jamais_de_repli_sur_jeton_session():
    # sans jeton relais, le mot de passe RTSP reste VIDE — jamais le jeton de
    # session (l'employer le ferait fuiter en clair sur le fil RTSP)
    srv = ServeurDistant("http://192.0.2.9:8080", jeton="JETON-API")
    srv._relay_port = 8554
    url = srv._base_rtsp()
    assert url == "rtsp://sentinelle:@192.0.2.9:8554/"
    assert "JETON-API" not in url


def test_login_met_a_jour_le_jeton_relais(monkeypatch):
    # login() doit rafraîchir _relay_jeton à partir de la réponse serveur
    srv = ServeurDistant("http://192.0.2.9:8080")

    class _R:
        status_code = 200
        def json(self):
            return {"token": "API-1", "relay_token": "RELAY-1",
                    "username": "u", "role": "user"}

    monkeypatch.setattr("requests.post", lambda *a, **k: _R())
    srv.login("u", "p")
    assert srv.jeton == "API-1"
    assert srv._relay_jeton == "RELAY-1"


def test_changer_mdp_met_a_jour_le_jeton_relais(monkeypatch):
    # le changement de mot de passe invalide TOUS les jetons (signature liée au
    # hash) : le client doit adopter le nouveau relay_token de la réponse
    srv = ServeurDistant("http://192.0.2.9:8080", jeton="API-1")
    srv._relay_jeton = "RELAY-1"

    class _R:
        status_code = 200
        def json(self):
            return {"ok": True, "token": "API-2", "relay_token": "RELAY-2"}

    monkeypatch.setattr("requests.request", lambda *a, **k: _R())
    srv.changer_mot_de_passe("ancien", "nouveau-mdp")
    assert srv.jeton == "API-2"
    assert srv._relay_jeton == "RELAY-2"


def test_maj_jeton_urls_reecrit_en_place(monkeypatch):
    # après rafraîchissement du jeton relay, les URLs RTSP des caméras du mode
    # serveur sont réécrites EN PLACE (sans reconstruire le mur)
    from sentinelle.config import AppConfig, Camera, Site

    srv = ServeurDistant("http://192.0.2.9:8080", jeton="API-1")
    srv._relay_port = 8554
    srv._relay_jeton = "VIEUX"

    cfg = AppConfig(path="")
    site = Site(id="s1", nom="S1")
    cfg.sites.append(site)
    cam = Camera(id="c1", nom="C1", site=site, marque="custom",
                 url_mainstream="rtsp://sentinelle:VIEUX@192.0.2.9:8554/c1-main",
                 url_substream="rtsp://sentinelle:VIEUX@192.0.2.9:8554/c1-sub")
    cam.remote = srv
    cam._relay_main, cam._relay_sub = "c1-main", "c1-sub"
    cam._relay_snapshot = False
    cfg.cameras.append(cam)

    srv._relay_jeton = "NEUF"
    srv.maj_jeton_urls(cfg)
    assert cam.url_mainstream == "rtsp://sentinelle:NEUF@192.0.2.9:8554/c1-main"
    assert cam.url_substream == "rtsp://sentinelle:NEUF@192.0.2.9:8554/c1-sub"
