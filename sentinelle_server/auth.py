"""Comptes utilisateurs, mots de passe hachés, jetons de session et droits.

Sécurité :
  - mots de passe hachés en PBKDF2-HMAC-SHA256 (sel aléatoire par compte),
    jamais stockés ni renvoyés en clair ;
  - jeton de session = username + expiration, signé (HMAC-SHA256) avec une clé
    serveur ET l'empreinte du mot de passe : changer le mot de passe invalide
    aussitôt toutes les sessions existantes, et un jeton volé cesse d'être
    valable après SENTINELLE_TOKEN_TTL_H heures — le tout sans stockage de
    session côté serveur ;
  - droits par utilisateur : rôle (admin | user), accès à tout ou à une liste
    de sites / caméras. Le rôle admin donne la gestion + la visibilité totale.

Le fichier users.yaml vit dans le dossier de données du serveur (jamais publié).
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

import yaml

logger = logging.getLogger(__name__)

_ITERATIONS = 200_000

# Longueur minimale imposée à tout nouveau mot de passe (login + admin).
MIN_MDP = 8

# Portées de jeton : un jeton « api » ouvre l'API HTTP (visualisation +
# administration selon le rôle) ; un jeton « relay » ne sert QUE de mot de passe
# RTSP au relais vidéo. Le flux RTSP n'étant pas chiffré, son mot de passe peut
# être capté par écoute réseau : le cloisonner en portée « relay » empêche qu'un
# jeton ainsi sniffé serve contre l'API HTTP.
SCOPE_API = "api"
SCOPE_RELAY = "relay"


def _restreindre(chemin: str):
    """Restreint un fichier de secrets au propriétaire (0600). Sans effet sur
    Windows (dev) ; protège les fichiers montés en volume sur l'hôte Linux."""
    try:
        os.chmod(chemin, 0o600)
    except OSError:
        pass


def _ttl_s() -> int:
    """Durée de vie d'un jeton, en secondes (défaut : 7 jours)."""
    try:
        h = int(os.environ.get("SENTINELLE_TOKEN_TTL_H", "168"))
    except (TypeError, ValueError):
        h = 168
    return max(1, h) * 3600


def hacher(mot_de_passe: str, sel: bytes | None = None) -> tuple[str, str]:
    sel = sel or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode("utf-8"), sel, _ITERATIONS)
    return sel.hex(), h.hex()


def verifier(mot_de_passe: str, sel_hex: str, hash_hex: str) -> bool:
    try:
        sel = bytes.fromhex(sel_hex)
    except ValueError:
        return False
    _, calcule = hacher(mot_de_passe, sel)
    return hmac.compare_digest(calcule, hash_hex)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class User:
    def __init__(self, username: str, role: str = "user", sel: str = "",
                 hash: str = "", tout: bool = False,
                 sites: list | None = None, cameras: list | None = None,
                 sequences: list | None = None, jetons_version: int = 0):
        self.username = username
        self.role = role if role in ("admin", "user") else "user"
        self.sel = sel
        self.hash = hash
        self.tout = bool(tout)
        self.sites = list(sites or [])
        self.cameras = list(cameras or [])
        self.sequences = list(sequences or [])   # boucles personnelles (dicts)
        # incrémenté pour révoquer d'un coup TOUTES les sessions du compte
        # (poste volé, jeton exfiltré) sans changer le mot de passe : le numéro
        # entre dans la signature du jeton, donc les anciens jetons deviennent
        # invalides dès qu'il change.
        self.jetons_version = int(jetons_version)

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    def peut_voir(self, cam) -> bool:
        if self.admin or self.tout:
            return True
        return cam.id in self.cameras or cam.site.id in self.sites

    def to_stored(self) -> dict:
        return {"username": self.username, "role": self.role,
                "sel": self.sel, "hash": self.hash, "tout": self.tout,
                "sites": self.sites, "cameras": self.cameras,
                "sequences": self.sequences, "jetons_version": self.jetons_version}

    def to_public(self) -> dict:
        """Sans secret (sel/hash) — pour l'API d'administration."""
        return {"username": self.username, "role": self.role,
                "tout": self.tout, "sites": self.sites, "cameras": self.cameras}


class Users:
    def __init__(self, data_dir: str, secret: bytes):
        self.path = os.path.join(data_dir, "users.yaml")
        self.secret = secret
        self.lock = threading.RLock()
        self.users: dict[str, User] = {}
        self._charger()

    # ------------------------------------------------------------- persistance

    def _charger(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for u in data.get("users") or []:
                try:
                    self.users[u["username"]] = User(
                        username=str(u["username"]), role=str(u.get("role", "user")),
                        sel=str(u.get("sel", "")), hash=str(u.get("hash", "")),
                        tout=bool(u.get("tout", False)),
                        sites=list(u.get("sites") or []),
                        cameras=list(u.get("cameras") or []),
                        sequences=list(u.get("sequences") or []),
                        jetons_version=int(u.get("jetons_version", 0) or 0))
                except (KeyError, TypeError):
                    continue

    def _sauver(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# Sentinelle Server — comptes utilisateurs.\n"
                    "# Mots de passe hachés (PBKDF2) ; ne pas éditer à la main.\n")
            yaml.safe_dump({"users": [u.to_stored() for u in self.users.values()]},
                           f, allow_unicode=True, sort_keys=False)
        _restreindre(tmp)                       # 0600 avant publication du fichier
        os.replace(tmp, self.path)

    def vide(self) -> bool:
        return not self.users

    # ---------------------------------------------------------------- bootstrap

    def creer_admin_initial(self) -> str:
        """Crée le compte admin par défaut, retourne son mot de passe généré."""
        mdp = secrets.token_urlsafe(12)
        sel, h = hacher(mdp)
        with self.lock:
            self.users["admin"] = User("admin", role="admin", sel=sel, hash=h, tout=True)
            self._sauver()
        return mdp

    # ------------------------------------------------------------------- jetons
    #
    # Format : b64(username).scope.exp.signature  où exp est un horodatage Unix
    # et la signature couvre username + empreinte du mot de passe + version de
    # session + portée + exp. Rend le jeton invalide : changer le mot de passe
    # (hash), incrémenter jetons_version (révocation), dépasser exp, ou présenter
    # le jeton dans une portée qui n'est pas la sienne.

    def _signature(self, user: User, scope: str, exp: int) -> str:
        msg = f"{user.username}:{user.hash}:{user.jetons_version}:{scope}:{exp}".encode()
        return _b64(hmac.new(self.secret, msg, hashlib.sha256).digest())

    def emettre_jeton(self, user: User, scope: str = SCOPE_API) -> str:
        exp = int(time.time()) + _ttl_s()
        return (f"{_b64(user.username.encode())}.{scope}.{exp}."
                f"{self._signature(user, scope, exp)}")

    def user_du_jeton(self, jeton: str, scope: str = SCOPE_API) -> User | None:
        if not jeton or jeton.count(".") != 3:
            return None
        nom_b64, scope_j, exp_s, sig = jeton.split(".", 3)
        if scope_j != scope:
            return None                         # jeton présenté hors de sa portée
        try:
            username = _unb64(nom_b64).decode("utf-8")
            exp = int(exp_s)
        except Exception:
            return None
        if exp < time.time():
            return None                         # jeton expiré
        user = self.users.get(username)
        if user is None:
            return None
        if not hmac.compare_digest(sig, self._signature(user, scope, exp)):
            return None
        return user

    def reste_jeton(self, jeton: str) -> int:
        """Secondes restant avant expiration (0 si absent/illisible). Ne
        revérifie pas la signature : à n'appeler qu'après user_du_jeton."""
        try:
            exp = int(jeton.split(".", 3)[2])
        except (IndexError, ValueError):
            return 0
        return max(0, exp - int(time.time()))

    def revoquer_sessions(self, username: str) -> bool:
        """Invalide immédiatement toutes les sessions (api + relay) du compte en
        incrémentant sa version de jeton. Utilisé pour couper un poste volé sans
        toucher au mot de passe."""
        with self.lock:
            user = self.users.get(username)
            if user is None:
                return False
            user.jetons_version += 1
            self._sauver()
            return True

    # ------------------------------------------------------------------- login

    def authentifier(self, username: str, mot_de_passe: str) -> User | None:
        user = self.users.get(username)
        if user is None:
            # comparaison factice : évite de révéler l'existence du compte au timing
            hacher(mot_de_passe)
            return None
        if verifier(mot_de_passe, user.sel, user.hash):
            return user
        return None

    # -------------------------------------------------------------------- CRUD

    def liste_publique(self) -> list[dict]:
        with self.lock:
            return [u.to_public() for u in self.users.values()]

    def definir_mot_de_passe(self, username: str, mot_de_passe: str) -> bool:
        with self.lock:
            user = self.users.get(username)
            if user is None:
                return False
            user.sel, user.hash = hacher(mot_de_passe)
            self._sauver()
            return True

    def remplacer(self, entrees: list[dict]) -> list[str]:
        """Remplace la liste des utilisateurs (administration).

        Chaque entrée : username, role, tout, sites, cameras, et password
        optionnel (vide = conserver le mot de passe existant). Retourne les
        avertissements de validation. Garantit au moins un admin subsistant."""
        avertissements = []
        nouveaux: dict[str, User] = {}
        for e in entrees:
            nom = str(e.get("username", "")).strip()
            if not nom:
                avertissements.append("compte sans nom ignoré")
                continue
            if nom in nouveaux:
                avertissements.append(f"doublon '{nom}' ignoré")
                continue
            role = str(e.get("role", "user"))
            ancien = self.users.get(nom)
            mdp = e.get("password") or ""
            if mdp and len(mdp) < MIN_MDP:
                # ne jamais enregistrer un mot de passe faible : on conserve
                # l'ancien s'il existe, sinon on ignore le compte
                avertissements.append(
                    f"'{nom}' : mot de passe trop court (min {MIN_MDP}) — non modifié")
                mdp = ""
            if mdp:
                sel, h = hacher(mdp)
            elif ancien is not None:
                sel, h = ancien.sel, ancien.hash
            else:
                avertissements.append(f"'{nom}' sans mot de passe (valide) ignoré")
                continue
            nouveaux[nom] = User(
                username=nom, role=role, sel=sel, hash=h,
                tout=bool(e.get("tout", False)),
                sites=[str(x) for x in (e.get("sites") or [])],
                cameras=[str(x) for x in (e.get("cameras") or [])],
                # les boucles personnelles ne transitent pas par l'admin :
                # celles du compte existant sont conservées
                sequences=(ancien.sequences if ancien is not None else []),
                # préserver la version de session : sinon éditer un compte
                # déconnecterait toutes ses sessions actives à chaque fois
                jetons_version=(ancien.jetons_version if ancien is not None else 0))
        if not any(u.admin for u in nouveaux.values()):
            raise ValueError("il doit rester au moins un administrateur")
        with self.lock:
            self.users = nouveaux
            self._sauver()
        return avertissements

    def definir_sequences(self, username: str, sequences: list) -> bool:
        """Enregistre les boucles personnelles d'un compte."""
        with self.lock:
            user = self.users.get(username)
            if user is None:
                return False
            user.sequences = list(sequences)
            self._sauver()
            return True

    def cameras_visibles(self, user: User, cfg) -> set[str]:
        return {c.id for c in cfg.cameras if user.peut_voir(c)}
