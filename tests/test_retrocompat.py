"""Compatibilité ascendante d'une mise à jour du serveur.

Simule une installation existante — users.yaml et server.yaml au format
antérieur, jetons émis AVANT la mise à jour — puis démarre le serveur courant
dessus. Une mise à jour ne doit ni déconnecter les postes en service, ni couper
les flux vidéo en cours, ni perdre de droits.

Le point sensible est le schéma de signature des jetons : il n'est stocké nulle
part, un jeton n'est qu'une signature à revalider. Le modifier (ordre des
champs, ajout d'un élément, réécriture de secret_key) invaliderait d'un coup
toutes les sessions du parc au redémarrage du conteneur. Ce test fige ce
schéma : le jeton de référence est fabriqué ici à la main, sans passer par le
code de production, donc une refonte de l'émission le casse.
"""

import base64
import hashlib
import hmac
import os
import time

import yaml
from fastapi.testclient import TestClient

# Paramètres du schéma de jeton figés au format publié (voir auth.py).
ITERATIONS = 200_000
TTL_ANCIEN = 7 * 24 * 3600

CONFIG_ANCIENNE = {
    "options": {"rotation_duree_s": 20},
    "sites": [{"id": "s1", "nom": "Site 1", "lien": "fibre"}],
    "cameras": [{"id": "cam1", "nom": "Caméra 1", "site": "s1",
                 "marque": "hikvision", "hote": "10.0.0.5", "port": 554,
                 "canal": 1, "user": "u", "password": "p"}],
}


def _hacher(mdp: str) -> tuple[str, str]:
    sel = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", mdp.encode(), sel, ITERATIONS)
    return sel.hex(), h.hex()


def _jeton_ancien(secret: str, username: str, scope: str, hash_mdp: str,
                  version: int, ttl: int = TTL_ANCIEN) -> str:
    """Émission d'un jeton telle qu'elle existait avant la mise à jour.
    Volontairement réimplémentée : un test qui appellerait emettre_jeton
    suivrait n'importe quel changement de schéma au lieu de le détecter."""
    exp = int(time.time()) + ttl
    msg = f"{username}:{hash_mdp}:{version}:{scope}:{exp}".encode()
    sig = base64.urlsafe_b64encode(
        hmac.new(bytes.fromhex(secret), msg, hashlib.sha256).digest()
    ).decode().rstrip("=")
    nom = base64.urlsafe_b64encode(username.encode()).decode().rstrip("=")
    return f"{nom}.{scope}.{exp}.{sig}"


def _installation_existante(tmp_path) -> dict:
    """Écrit une installation d'avant la mise à jour et retourne ses jetons."""
    sel_a, hash_a = _hacher("admin-1234")
    sel_m, hash_m = _hacher("operateur-1")
    secret = os.urandom(32).hex()

    # users.yaml : ni rôle « service », ni clé inconnue du format d'alors
    (tmp_path / "users.yaml").write_text(yaml.safe_dump({"users": [
        {"username": "admin", "role": "admin", "sel": sel_a, "hash": hash_a,
         "tout": True, "sites": [], "cameras": [], "sequences": [],
         # non nul : une révocation a déjà eu lieu, elle doit rester effective
         "jetons_version": 3},
        {"username": "mur1", "role": "user", "sel": sel_m, "hash": hash_m,
         "tout": False, "sites": ["s1"], "cameras": [], "jetons_version": 0,
         "sequences": [{"nom": "ronde", "etapes": [
             {"mode": "grille", "cameras": ["cam1"], "duree_s": 30}]}]},
    ]}, allow_unicode=True), encoding="utf-8")

    # server.yaml : sans les clés ajoutées depuis
    (tmp_path / "server.yaml").write_text(
        yaml.safe_dump({"secret_key": secret, "relay_port": 8554}),
        encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(CONFIG_ANCIENNE, allow_unicode=True), encoding="utf-8")

    return {
        "secret": secret,
        "api_mur1": _jeton_ancien(secret, "mur1", "api", hash_m, 0),
        "relay_mur1": _jeton_ancien(secret, "mur1", "relay", hash_m, 0),
        "api_admin": _jeton_ancien(secret, "admin", "api", hash_a, 3),
        "relay_perime": _jeton_ancien(secret, "mur1", "relay", hash_m, 0,
                                      ttl=-60),
    }


def _app(tmp_path):
    os.environ["SENTINELLE_DATA"] = str(tmp_path)
    from sentinelle_server.app import create_app
    return create_app(str(tmp_path))


def test_sessions_et_flux_survivent_a_la_mise_a_jour(tmp_path):
    """Les jetons émis avant la mise à jour restent valables : les postes en
    service ne se déconnectent pas et les tuiles vidéo ne tombent pas en 401."""
    j = _installation_existante(tmp_path)
    with TestClient(_app(tmp_path)) as c:
        A = {"Authorization": f"Bearer {j['api_mur1']}"}
        vue = c.get("/api/config", headers=A)
        assert vue.status_code == 200
        vue = vue.json()
        assert [x["id"] for x in vue["cameras"]] == ["cam1"]
        assert [s["nom"] for s in vue["sequences"]] == ["ronde"]

        # le jeton relay déjà distribué ouvre toujours les flux
        lecture = {"action": "read", "path": "cam1-main"}
        assert c.post("/api/relay-auth",
                      json={**lecture, "password": j["relay_mur1"]}).status_code == 200
        # ... et l'expiration reste appliquée (le test précédent ne passe pas
        # parce qu'on aurait cessé de vérifier exp)
        assert c.post("/api/relay-auth",
                      json={**lecture, "password": j["relay_perime"]}).status_code == 401

        # jetons_version non nul : les révocations passées restent honorées
        assert c.get("/api/users",
                     headers={"Authorization": f"Bearer {j['api_admin']}"}).status_code == 200


def test_comptes_existants_inchanges(tmp_path):
    """Mots de passe, rôles et droits d'un compte antérieur sont préservés, et
    un compte normal ne subit aucune des restrictions du rôle service."""
    _installation_existante(tmp_path)
    with TestClient(_app(tmp_path)) as c:
        rep = c.post("/api/login", json={"username": "mur1",
                                         "password": "operateur-1"})
        assert rep.status_code == 200 and rep.json()["role"] == "user"
        N = {"Authorization": f"Bearer {rep.json()['token']}"}

        # reste_s reste une durée positive : -1 est réservé aux jetons
        # perpétuels, que le client interpréterait comme « à rafraîchir »
        assert c.get("/api/session", headers=N).json()["reste_s"] > 0
        assert c.put("/api/account/sequences", headers=N,
                     json={"sequences": []}).status_code == 200

        vue = c.get("/api/config", headers=N).json()
        assert vue["relay"]["port"] == 8554
        # hôte du relais non renseigné : celui par lequel le client a joint l'API
        assert vue["relay"]["host"] == "testserver"


def test_client_anterieur_ne_degrade_pas_les_comptes(tmp_path):
    """Un client d'une version antérieure renvoie la liste des comptes sans les
    clés qu'il ignore : cela ne doit ni changer les rôles, ni couper les
    sessions en cours."""
    j = _installation_existante(tmp_path)
    with TestClient(_app(tmp_path)) as c:
        A = {"Authorization": f"Bearer {j['api_admin']}"}
        users = c.get("/api/users", headers=A).json()["users"]
        assert {u["username"] for u in users} == {"admin", "mur1"}

        connu = ("username", "role", "tout", "sites", "cameras")
        renvoi = [{k: v for k, v in u.items() if k in connu} for u in users]
        r = c.put("/api/users", headers=A, json={"users": renvoi})
        assert r.status_code == 200 and not r.json()["warnings"]

        apres = {u["username"]: u["role"]
                 for u in c.get("/api/users", headers=A).json()["users"]}
        assert apres == {"admin": "admin", "mur1": "user"}
        # les sessions ouvertes survivent à cet enregistrement
        assert c.get("/api/config",
                     headers={"Authorization": f"Bearer {j['api_mur1']}"}).status_code == 200


def test_server_yaml_complete_sans_perdre_les_secrets(tmp_path):
    """L'ajout des nouvelles clés à server.yaml ne doit pas régénérer
    secret_key : ce serait invalider toutes les sessions du parc."""
    j = _installation_existante(tmp_path)
    with TestClient(_app(tmp_path)):
        pass
    apres = yaml.safe_load((tmp_path / "server.yaml").read_text(encoding="utf-8"))
    assert apres["secret_key"] == j["secret"]
    assert apres["relay_port"] == 8554
    assert apres.get("relay_host") == ""

    # deuxième démarrage : le fichier est déjà complet, rien ne bouge
    avant = (tmp_path / "server.yaml").read_text(encoding="utf-8")
    with TestClient(_app(tmp_path)):
        pass
    assert (tmp_path / "server.yaml").read_text(encoding="utf-8") == avant
