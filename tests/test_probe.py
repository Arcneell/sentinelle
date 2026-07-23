"""Classification des échecs RTSP (probe.classify_text).

Chemin critique : « auth » déclenche l'ARRÊT DÉFINITIF des tentatives (les DVR
Hikvision verrouillent le compte après quelques échecs). Une fausse détection
« auth » abandonnerait une caméra qui aurait pu se rétablir — d'où les cas de
non-régression sur le code 401 en sous-chaîne fortuite.
"""

from sentinelle.probe import classify_text


def test_auth_marqueurs_explicites():
    assert classify_text("method DESCRIBE failed: 401 Unauthorized") == "auth"
    assert classify_text("RTSP/1.0 401 Unauthorized") == "auth"
    assert classify_text("Authentication failed") == "auth"
    assert classify_text("auth failed for user") == "auth"


def test_401_en_jeton_isole_uniquement():
    # « 401 » noyé dans un autre nombre ne doit PAS être classé auth
    assert classify_text("bitrate 14012 kbps") != "auth"
    assert classify_text("frame 40100 dropped") != "auth"
    # mais un 401 isolé (bordé de non-chiffres) reste auth
    assert classify_text("status=401 rejected") == "auth"


def test_reseau_avant_timeout():
    # « Connection refused » contient parfois aussi « timed out » : c'est un refus
    assert classify_text("Connection to tcp://x failed: Connection refused") == "network"
    assert classify_text("No route to host") == "network"
    assert classify_text("Name or service not known") == "network"


def test_timeout_et_autre():
    assert classify_text("Operation timed out") == "timeout"
    assert classify_text("") == "other"
    assert classify_text("codec parameters not found") == "other"
