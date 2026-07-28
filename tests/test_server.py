"""Tests de non-régression du serveur : authentification, droits par
utilisateur, autorisation du relais, invalidation de session, anti-force-brute,
minimum de mot de passe et validité de session.

N'exige ni Docker ni MediaMTX : l'API est testée via TestClient ; le relais et
le moniteur de mouvement tournent en arrière-plan et échouent silencieusement,
sans gêner les tests.
"""

import os
import time

import yaml
from fastapi.testclient import TestClient

CONFIG = {
    "options": {"rotation_duree_s": 20},
    "sites": [{"id": "s1", "nom": "Site 1", "lien": "fibre"}],
    "cameras": [{
        "id": "cam1", "nom": "Caméra 1", "site": "s1", "profil": "normal",
        "marque": "hikvision", "hote": "127.0.0.1", "port": 554, "canal": 1,
        "port_http": 9, "user": "u", "password": "p",
    }],
}


def _mdp_admin_initial(data_dir) -> str:
    ligne = (data_dir / "admin-initial.txt").read_text(encoding="utf-8")
    for l in ligne.splitlines():
        if "mot de passe" in l:
            return l.split(":", 1)[1].strip()
    raise AssertionError("mot de passe admin introuvable")


def _client(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    os.environ["SENTINELLE_DATA"] = str(tmp_path)
    from sentinelle_server.app import create_app
    return create_app(str(tmp_path))


def test_parcours_complet(tmp_path):
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)

        # santé + rejets d'authentification
        assert c.get("/api/health").json()["ok"] is True
        assert c.post("/api/login", json={"username": "admin", "password": "faux"}).status_code == 401
        assert c.post("/api/login", json={"username": "ghost", "password": "x"}).status_code == 401

        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}

        # validité de session exposée pour le rafraîchissement client
        sess = c.get("/api/session", headers=A).json()
        assert sess["ok"] is True and sess["reste_s"] > 0

        # admin voit tout
        cfg = c.get("/api/config", headers=A).json()
        assert cfg["compte"]["role"] == "admin"
        assert [x["id"] for x in cfg["cameras"]] == ["cam1"]

        # création d'un utilisateur sans aucune caméra (mot de passe >= 8)
        users = c.get("/api/users", headers=A).json()["users"]
        v = {"username": "v", "role": "user", "tout": False,
             "sites": [], "cameras": [], "password": "viewer-1"}
        assert c.put("/api/users", headers=A, json={"users": users + [v]}).status_code == 200

        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}

        # le viewer ne voit aucune caméra et n'accède pas à l'admin
        assert c.get("/api/config", headers=V).json()["cameras"] == []
        assert c.get("/api/users", headers=V).status_code == 403
        assert c.get("/api/config/full", headers=V).status_code == 403

        # autorisation relais : le mot de passe RTSP est le jeton de portée
        # « relay » livré par /api/config, jamais le jeton de session.
        base = {"action": "read", "path": "cam1-sub"}
        rt_admin = c.get("/api/config", headers=A).json()["relay"]["token"]
        rt_viewer = c.get("/api/config", headers=V).json()["relay"]["token"]
        assert c.post("/api/relay-auth", json={**base, "password": rt_viewer}).status_code == 403
        assert c.post("/api/relay-auth", json={**base, "password": rt_admin}).status_code == 200
        assert c.post("/api/relay-auth", json={**base, "password": "bidon"}).status_code == 401
        # cloisonnement des portées : un jeton de SESSION (api) refusé comme mot
        # de passe RTSP — sa capture par écoute du flux ne rouvre pas l'API
        assert c.post("/api/relay-auth", json={**base, "password": tok}).status_code == 401

        # publication : TOUJOURS refusée (aucune source ne publie vers le relais),
        # y compris depuis le réseau interne — empêche l'injection de fausse caméra
        pub = {"action": "publish", "path": "cam1-sub"}
        assert c.post("/api/relay-auth", json={**pub, "ip": "8.8.8.8"}).status_code == 403
        assert c.post("/api/relay-auth", json={**pub, "ip": "127.0.0.1"}).status_code == 403
        assert c.post("/api/relay-auth", json=pub).status_code == 403

        # droit accordé au site -> le viewer voit la caméra
        v["sites"] = ["s1"]
        c.put("/api/users", headers=A, json={"users": users + [v]})
        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}
        vue_v = c.get("/api/config", headers=V).json()
        assert [x["id"] for x in vue_v["cameras"]] == ["cam1"]
        rt_viewer = vue_v["relay"]["token"]
        assert c.post("/api/relay-auth",
                      json={**base, "password": rt_viewer}).status_code == 200

        # boucles personnelles filtrées sur les caméras visibles
        boucles = {"sequences": [
            {"nom": "ok", "etapes": [{"mode": "mono", "cameras": ["cam1"], "duree_s": 5}]},
            {"nom": "ko", "etapes": [{"mode": "mono", "cameras": ["absente"], "duree_s": 5}]},
        ]}
        c.put("/api/account/sequences", headers=V, json=boucles)
        seqs = c.get("/api/config", headers=V).json()["sequences"]
        assert [s["nom"] for s in seqs] == ["ok"]

        # changement de mot de passe -> l'ancienne session est invalidée, et la
        # réponse fournit un relay_token NEUF encore valide contre le relais
        # (l'ancien jeton relay, signé avec l'ancien hash, est mort lui aussi)
        r = c.post("/api/account/password", headers=V,
                   json={"ancien": "viewer-1", "nouveau": "viewer-2"})
        assert r.status_code == 200
        assert c.get("/api/config", headers=V).status_code == 401
        nouveau_relay = r.json()["relay_token"]
        assert nouveau_relay != rt_viewer
        assert c.post("/api/relay-auth",
                      json={**base, "password": rt_viewer}).status_code == 401
        assert c.post("/api/relay-auth",
                      json={**base, "password": nouveau_relay}).status_code == 200


def test_rondes_partagees(tmp_path):
    """Rondes gérées par l'admin, attribuées à tous ou à certains comptes,
    filtrées aux caméras visibles, et jamais écrasées par un PUT /api/config."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}

        users = c.get("/api/users", headers=A).json()["users"]
        v = {"username": "v", "role": "user", "tout": False,
             "sites": ["s1"], "cameras": [], "password": "viewer-1"}
        w = {"username": "w", "role": "user", "tout": False,
             "sites": [], "cameras": [], "password": "viewer-2"}
        assert c.put("/api/users", headers=A,
                     json={"users": users + [v, w]}).status_code == 200

        etape = [{"mode": "grille", "cameras": ["cam1"], "duree_s": 10}]
        rondes = {"sequences": [
            {"nom": "Globale", "tous": True, "etapes": etape},
            {"nom": "Ciblée", "utilisateurs": ["v"], "etapes": etape},
            {"nom": "Vide", "etapes": []},                          # ignorée
            {"nom": "Orpheline", "utilisateurs": ["ghost"], "etapes": etape},
        ]}
        r = c.put("/api/rounds", headers=A, json=rondes)
        assert r.status_code == 200
        assert r.json()["sequences"] == 3
        assert r.json()["warnings"]              # ronde vide + compte inconnu

        # lecture admin : attribution complète, compte inconnu retiré
        liste = c.get("/api/rounds", headers=A).json()["sequences"]
        assert [s["nom"] for s in liste] == ["Globale", "Ciblée", "Orpheline"]
        assert liste[0]["tous"] is True
        assert liste[2]["utilisateurs"] == []

        # réservé aux admins
        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}
        assert c.get("/api/rounds", headers=V).status_code == 403
        assert c.put("/api/rounds", headers=V, json=rondes).status_code == 403

        # v voit la globale + la ciblée, marquées partagées, avant ses boucles perso
        c.put("/api/account/sequences", headers=V, json={"sequences": [
            {"nom": "Perso", "etapes": [{"mode": "mono", "cameras": ["cam1"], "duree_s": 5}]}]})
        seqs = c.get("/api/config", headers=V).json()["sequences"]
        assert [s["nom"] for s in seqs] == ["Globale", "Ciblée", "Perso"]
        assert seqs[0]["partagee"] is True and seqs[1]["partagee"] is True
        assert "partagee" not in seqs[2]

        # w n'a aucune caméra visible : aucune ronde ne lui parvient
        tw = c.post("/api/login", json={"username": "w", "password": "viewer-2"}).json()["token"]
        W = {"Authorization": f"Bearer {tw}"}
        assert c.get("/api/config", headers=W).json()["sequences"] == []

        # un PUT /api/config (caméras) ne doit pas écraser les rondes stockées
        assert c.put("/api/config", headers=A, json=CONFIG).status_code == 200
        liste = c.get("/api/rounds", headers=A).json()["sequences"]
        assert [s["nom"] for s in liste] == ["Globale", "Ciblée", "Orpheline"]


def test_dernier_admin_protege(tmp_path):
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        # tenter de ne laisser aucun admin -> refus
        r = c.put("/api/users", headers=A, json={"users": [
            {"username": "u", "role": "user", "tout": False,
             "sites": [], "cameras": [], "password": "unmotdepasse"}]})
        assert r.status_code == 422


def test_mot_de_passe_trop_court_rejete(tmp_path):
    """Un mot de passe < 8 caractères n'est jamais enregistré : le compte n'est
    pas créé et un avertissement est renvoyé."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        users = c.get("/api/users", headers=A).json()["users"]
        faible = {"username": "faible", "role": "user", "tout": False,
                  "sites": [], "cameras": [], "password": "123"}
        r = c.put("/api/users", headers=A, json={"users": users + [faible]})
        assert r.status_code == 200
        assert r.json()["warnings"]                       # avertissement présent
        # le compte n'a pas été créé avec ce mot de passe faible
        assert c.post("/api/login",
                      json={"username": "faible", "password": "123"}).status_code == 401
        # changement de son propre mot de passe : minimum imposé aussi
        assert c.post("/api/account/password", headers=A,
                      json={"ancien": mdp, "nouveau": "court"}).status_code == 422


def test_cloisonnement_portees_et_jeton(tmp_path):
    """Cloisonnement des portées de jeton et durcissement du transport du jeton :
    - un jeton relay est refusé comme Bearer sur l'API (sens inverse du test relais) ;
    - le login fournit un relay_token distinct du jeton de session ;
    - un jeton en paramètre d'URL (?token=) n'est plus accepté ;
    - un jeton mal formé (ancien format) est rejeté."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        rep = c.post("/api/login", json={"username": "admin", "password": mdp}).json()
        tok, relay_tok = rep["token"], rep["relay_token"]
        assert relay_tok and relay_tok != tok       # relay distinct de l'API

        # un jeton relay présenté en Bearer sur l'API est refusé (portée api attendue)
        assert c.get("/api/config",
                     headers={"Authorization": f"Bearer {relay_tok}"}).status_code == 401
        # le jeton API fonctionne bien, lui
        assert c.get("/api/config", headers={"Authorization": f"Bearer {tok}"}).status_code == 200

        # jeton en paramètre d'URL : plus accepté (fuite via journaux/Referer)
        assert c.get(f"/api/config?token={tok}").status_code == 401
        # jeton mal formé (ancien format 3 parties / bruit) : rejeté proprement
        assert c.get("/api/config",
                     headers={"Authorization": "Bearer a.b.c"}).status_code == 401


def test_snapshot_autorisation(tmp_path):
    """_cam_autorisee : un viewer sans droit sur la caméra reçoit 404 sur le
    snapshot (contrôle d'accès partagé PTZ/snapshot)."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        users = c.get("/api/users", headers=A).json()["users"]
        v = {"username": "v", "role": "user", "tout": False,
             "sites": [], "cameras": [], "password": "viewer-1"}
        c.put("/api/users", headers=A, json={"users": users + [v]})
        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}
        # viewer sans droit → 404 (caméra inconnue ou non autorisée)
        assert c.get("/api/snapshot/cam1", headers=V).status_code == 404
        # caméra inexistante, même pour l'admin → 404
        assert c.get("/api/snapshot/fantome", headers=A).status_code == 404


def test_revocation_sessions(tmp_path):
    """Révocation : /api/account/logout invalide sa propre session ; un admin
    peut couper toutes les sessions d'un autre compte sans son mot de passe."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        users = c.get("/api/users", headers=A).json()["users"]
        v = {"username": "v", "role": "user", "tout": True,
             "sites": [], "cameras": [], "password": "viewer-1"}
        c.put("/api/users", headers=A, json={"users": users + [v]})

        # logout : le viewer coupe sa propre session
        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}
        assert c.get("/api/config", headers=V).status_code == 200
        assert c.post("/api/account/logout", headers=V).status_code == 200
        assert c.get("/api/config", headers=V).status_code == 401

        # une édition admin du compte NE doit PAS invalider ses sessions actives
        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}
        c.put("/api/users", headers=A, json={"users": c.get("/api/users", headers=A).json()["users"]})
        assert c.get("/api/config", headers=V).status_code == 200

        # révocation admin : la session du viewer tombe immédiatement
        assert c.post("/api/users/v/revoke", headers=A).status_code == 200
        assert c.get("/api/config", headers=V).status_code == 401
        # sans changer son mot de passe : il peut se reconnecter
        assert c.post("/api/login",
                      json={"username": "v", "password": "viewer-1"}).status_code == 200
        # compte inconnu -> 404, réservé aux admins
        assert c.post("/api/users/ghost/revoke", headers=A).status_code == 404


def test_anti_force_brute_changement_mdp(tmp_path):
    """Le changement de mot de passe est limité en tentatives : un jeton volé ne
    peut pas deviner sans fin le mot de passe actuel."""
    from sentinelle_server.app import LOGIN_MAX
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        for _ in range(LOGIN_MAX):
            assert c.post("/api/account/password", headers=A,
                          json={"ancien": "faux", "nouveau": "assez-long-1"}).status_code == 403
        r = c.post("/api/account/password", headers=A,
                   json={"ancien": "faux", "nouveau": "assez-long-1"})
        assert r.status_code == 429 and "Retry-After" in r.headers


def test_streams_compte_de_service(tmp_path):
    """Consommateur machine (analyse vidéo) : /api/streams donne des URLs RTSP
    prêtes à l'emploi, limitées aux caméras autorisées, et le jeton relay d'un
    compte de service n'expire pas — son jeton d'API, si."""
    from sentinelle_server.auth import SANS_EXPIRATION, _ttl_s
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}

        users = c.get("/api/users", headers=A).json()["users"]
        svc = {"username": "vision", "role": "service", "tout": False,
               "sites": ["s1"], "cameras": [], "password": "vision-ai-1"}
        aveugle = {"username": "aveugle", "role": "service", "tout": False,
                   "sites": [], "cameras": [], "password": "aveugle-1"}
        r = c.put("/api/users", headers=A, json={"users": users + [svc, aveugle]})
        assert r.status_code == 200

        rep = c.post("/api/login", json={"username": "vision",
                                         "password": "vision-ai-1"}).json()
        assert rep["role"] == "service"
        S = {"Authorization": f"Bearer {rep['token']}"}

        # portée api : durée GLOBALE malgré le rôle (un jeton d'API perpétuel
        # exfiltré resterait exploitable indéfiniment)
        reste = c.get("/api/session", headers=S).json()["reste_s"]
        assert 0 < reste <= _ttl_s()
        # portée relay : perpétuelle
        assert int(rep["relay_token"].split(".")[2]) == SANS_EXPIRATION

        flux = c.get("/api/streams", headers=S).json()
        assert flux["expire_s"] == 0                    # 0 = n'expire pas
        assert flux["relay"]["port"] == 8554
        assert [s["camera"] for s in flux["streams"]] == ["cam1"]
        cam = flux["streams"][0]
        assert cam["site"] == "s1" and cam["nom"] == "Caméra 1"

        # les URLs pointent le relais, portent le jeton relay en mot de passe et
        # le chemin attendu — et elles sont réellement acceptées par le relais
        for vue in ("main", "sub"):
            assert cam[vue].startswith("rtsp://vision:")
            assert cam[vue].endswith(f":8554/cam1-{vue}")
        jeton_url = cam["main"].split(":", 2)[2].split("@")[0]
        for vue in ("main", "sub"):
            assert c.post("/api/relay-auth",
                          json={"action": "read", "path": f"cam1-{vue}",
                                "password": jeton_url}).status_code == 200
        # le jeton des URLs reste cloisonné : refusé en Bearer sur l'API
        assert c.get("/api/streams",
                     headers={"Authorization": f"Bearer {jeton_url}"}).status_code == 401

        # un compte sans droit ne reçoit aucune URL
        ta = c.post("/api/login", json={"username": "aveugle",
                                        "password": "aveugle-1"}).json()["token"]
        assert c.get("/api/streams",
                     headers={"Authorization": f"Bearer {ta}"}).json()["streams"] == []


def test_service_droits_limites(tmp_path):
    """Un compte de service ne voit que ce qu'on lui accorde et n'atteint que
    les points de lecture : ni administration, ni PTZ, ni mot de passe, ni
    config du mur d'images."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        users = c.get("/api/users", headers=A).json()["users"]
        # « tout » demandé pour un compte de service : refusé et signalé
        svc = {"username": "vision", "role": "service", "tout": True,
               "sites": ["s1"], "cameras": [], "password": "vision-ai-1"}
        r = c.put("/api/users", headers=A, json={"users": users + [svc]})
        assert r.status_code == 200
        assert any("service" in w for w in r.json()["warnings"])
        stocke = next(u for u in c.get("/api/users", headers=A).json()["users"]
                      if u["username"] == "vision")
        assert stocke["role"] == "service" and stocke["tout"] is False

        S = {"Authorization": "Bearer " + c.post(
            "/api/login", json={"username": "vision",
                                "password": "vision-ai-1"}).json()["token"]}

        # ouverts : lecture des flux, session, snapshot, événements
        assert c.get("/api/streams", headers=S).status_code == 200
        assert c.get("/api/session", headers=S).status_code == 200
        assert c.get("/api/snapshot/cam1", headers=S).status_code in (200, 502)
        # fermés : tout le reste, y compris ce qu'un simple utilisateur peut faire
        assert c.get("/api/config", headers=S).status_code == 403
        assert c.put("/api/account/sequences", headers=S,
                     json={"sequences": []}).status_code == 403
        assert c.post("/api/account/password", headers=S,
                      json={"ancien": "vision-ai-1",
                            "nouveau": "vision-ai-2"}).status_code == 403
        assert c.post("/api/ptz/cam1/move", headers=S, json={"pan": 1}).status_code == 403
        assert c.get("/api/users", headers=S).status_code == 403
        assert c.put("/api/config", headers=S, json=CONFIG).status_code == 403

        # un rôle inconnu retombe sur « utilisateur » plutôt que d'être accepté
        r = c.put("/api/users", headers=A, json={"users": users + [
            {"username": "bizarre", "role": "root", "tout": False,
             "sites": [], "cameras": [], "password": "bizarre-1"}]})
        assert any("inconnu" in w for w in r.json()["warnings"])
        assert next(u for u in c.get("/api/users", headers=A).json()["users"]
                    if u["username"] == "bizarre")["role"] == "user"


def test_service_revocation_coupe_le_jeton_perpetuel(tmp_path):
    """Un jeton relay qui n'expire pas doit rester coupable : la révocation et
    le changement de mot de passe l'invalident immédiatement. Et un compte
    rétrogradé perd la perpétuité de ses jetons déjà émis."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        users = c.get("/api/users", headers=A).json()["users"]
        svc = {"username": "vision", "role": "service", "tout": False,
               "sites": ["s1"], "cameras": [], "password": "vision-ai-1"}
        c.put("/api/users", headers=A, json={"users": users + [svc]})

        def _relay():
            return c.post("/api/login", json={"username": "vision",
                                              "password": "vision-ai-1"}).json()["relay_token"]

        lecture = {"action": "read", "path": "cam1-main"}
        rt = _relay()
        assert c.post("/api/relay-auth", json={**lecture, "password": rt}).status_code == 200

        # révocation : le jeton perpétuel tombe tout de suite
        assert c.post("/api/users/vision/revoke", headers=A).status_code == 200
        assert c.post("/api/relay-auth", json={**lecture, "password": rt}).status_code == 401

        # un jeton perpétuel reste cloisonné : refusé en Bearer sur l'API
        rt = _relay()
        assert c.get("/api/streams",
                     headers={"Authorization": f"Bearer {rt}"}).status_code == 401

        # compte rétrogradé en utilisateur : ses jetons perpétuels meurent
        svc["role"], svc["password"] = "user", ""
        c.put("/api/users", headers=A, json={"users": users + [svc]})
        assert c.post("/api/relay-auth", json={**lecture, "password": rt}).status_code == 401
        # et le compte redevenu simple utilisateur reçoit un jeton qui expire
        rep = c.post("/api/login", json={"username": "vision",
                                         "password": "vision-ai-1"}).json()
        assert int(rep["relay_token"].split(".")[2]) > int(time.time())
        assert c.post("/api/relay-auth",
                      json={**lecture, "password": rep["relay_token"]}).status_code == 200
        assert c.get("/api/config",
                     headers={"Authorization": f"Bearer {rep['token']}"}).status_code == 200


def test_relay_host_annonce(tmp_path):
    """relay_host de server.yaml : annoncé dans /api/config et /api/streams ;
    vide, les clients emploient l'hôte de l'API."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/config", headers=A).json()["relay"]["host"] == "testserver"

    params = yaml.safe_load((tmp_path / "server.yaml").read_text(encoding="utf-8"))
    assert params["relay_host"] == ""            # clé créée au premier démarrage
    params["relay_host"] = "video.exemple.lan"
    (tmp_path / "server.yaml").write_text(yaml.safe_dump(params), encoding="utf-8")

    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/config", headers=A).json()["relay"]["host"] == "video.exemple.lan"
        flux = c.get("/api/streams", headers=A).json()
        assert flux["relay"]["host"] == "video.exemple.lan"
        assert "@video.exemple.lan:8554/cam1-main" in flux["streams"][0]["main"]


def test_anti_force_brute_login(tmp_path):
    """Au-delà de LOGIN_MAX échecs depuis la même IP, le login est temporairement
    refusé (429) sans même vérifier le mot de passe."""
    from sentinelle_server.app import LOGIN_MAX
    app = _client(tmp_path)
    with TestClient(app) as c:
        for _ in range(LOGIN_MAX):
            assert c.post("/api/login",
                          json={"username": "admin", "password": "faux"}).status_code == 401
        r = c.post("/api/login", json={"username": "admin", "password": "faux"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers


def test_conduite_serveur_reservee_aux_admins(tmp_path):
    """État, journal, rechargement et redémarrage : administrateurs seulement.

    Le redémarrage n'est pas déclenché ici (il tuerait le processus de test) :
    on vérifie qu'un simple utilisateur est refusé sur les quatre points."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}

        users = c.get("/api/users", headers=A).json()["users"]
        v = {"username": "v", "role": "user", "tout": False,
             "sites": ["s1"], "cameras": [], "password": "viewer-1"}
        assert c.put("/api/users", headers=A, json={"users": users + [v]}).status_code == 200
        tv = c.post("/api/login", json={"username": "v", "password": "viewer-1"}).json()["token"]
        V = {"Authorization": f"Bearer {tv}"}

        for methode, chemin in (("get", "/api/server/status"),
                                ("get", "/api/server/logs"),
                                ("post", "/api/server/reload"),
                                ("post", "/api/server/restart")):
            r = getattr(c, methode)(chemin, headers=V)
            assert r.status_code == 403, f"{chemin} ouvert à un simple utilisateur"

        etat = c.get("/api/server/status", headers=A).json()
        assert etat["cameras"] == 1 and etat["sites"] == 1
        assert etat["comptes"] == {"admin": 1, "user": 1, "service": 0}
        assert etat["uptime_s"] >= 0 and etat["version"]

        journal = c.get("/api/server/logs", headers=A, params={"lignes": 50}).json()
        assert isinstance(journal["lignes"], list)


def test_rechargement_prend_en_compte_le_fichier_modifie(tmp_path):
    """Une caméra ajoutée dans config.yaml sur le serveur apparaît après
    /api/server/reload, sans redémarrage."""
    app = _client(tmp_path)
    with TestClient(app) as c:
        mdp = _mdp_admin_initial(tmp_path)
        tok = c.post("/api/login", json={"username": "admin", "password": mdp}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        assert len(c.get("/api/config", headers=A).json()["cameras"]) == 1

        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        cfg["cameras"].append({
            "id": "cam2", "nom": "Caméra 2", "site": "s1", "profil": "eco",
            "marque": "dahua", "hote": "127.0.0.2", "port": 554, "canal": 2,
            "port_http": 9, "user": "u", "password": "p",
        })
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

        r = c.post("/api/server/reload", headers=A).json()
        assert r["ok"] is True and r["cameras"] == 2
        assert [x["id"] for x in c.get("/api/config", headers=A).json()["cameras"]] \
            == ["cam1", "cam2"]
