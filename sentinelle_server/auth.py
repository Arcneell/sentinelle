"""Comptes utilisateurs, mots de passe hachés, jetons de session et droits.

Sécurité :
  - mots de passe hachés en PBKDF2-HMAC-SHA256 (sel aléatoire par compte),
    jamais stockés ni renvoyés en clair ;
  - jeton de session = username signé (HMAC-SHA256) avec une clé serveur ET
    l'empreinte du mot de passe : changer le mot de passe invalide aussitôt
    toutes les sessions existantes, sans stockage de session ;
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

import yaml

logger = logging.getLogger(__name__)

_ITERATIONS = 200_000


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
                 sequences: list | None = None):
        self.username = username
        self.role = role if role in ("admin", "user") else "user"
        self.sel = sel
        self.hash = hash
        self.tout = bool(tout)
        self.sites = list(sites or [])
        self.cameras = list(cameras or [])
        self.sequences = list(sequences or [])   # boucles personnelles (dicts)

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
                "sequences": self.sequences}

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
                        sequences=list(u.get("sequences") or []))
                except (KeyError, TypeError):
                    continue

    def _sauver(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# Sentinelle Server — comptes utilisateurs.\n"
                    "# Mots de passe hachés (PBKDF2) ; ne pas éditer à la main.\n")
            yaml.safe_dump({"users": [u.to_stored() for u in self.users.values()]},
                           f, allow_unicode=True, sort_keys=False)
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

    def _signature(self, user: User) -> str:
        msg = f"{user.username}:{user.hash}".encode()
        return _b64(hmac.new(self.secret, msg, hashlib.sha256).digest())

    def emettre_jeton(self, user: User) -> str:
        return _b64(user.username.encode()) + "." + self._signature(user)

    def user_du_jeton(self, jeton: str) -> User | None:
        if not jeton or "." not in jeton:
            return None
        nom_b64, sig = jeton.split(".", 1)
        try:
            username = _unb64(nom_b64).decode("utf-8")
        except Exception:
            return None
        user = self.users.get(username)
        if user is None:
            return None
        if not hmac.compare_digest(sig, self._signature(user)):
            return None
        return user

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
            if mdp:
                sel, h = hacher(mdp)
            elif ancien is not None:
                sel, h = ancien.sel, ancien.hash
            else:
                avertissements.append(f"'{nom}' sans mot de passe ignoré")
                continue
            nouveaux[nom] = User(
                username=nom, role=role, sel=sel, hash=h,
                tout=bool(e.get("tout", False)),
                sites=[str(x) for x in (e.get("sites") or [])],
                cameras=[str(x) for x in (e.get("cameras") or [])],
                # les boucles personnelles ne transitent pas par l'admin :
                # celles du compte existant sont conservées
                sequences=(ancien.sequences if ancien is not None else []))
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
