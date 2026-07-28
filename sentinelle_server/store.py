"""Stockage serveur : jetons d'accès + configuration centrale.

La configuration centrale utilise exactement le même format (et le même code de
lecture/écriture) que le config.yaml du client autonome : un fichier existant
peut être déposé tel quel dans le dossier de données pour amorcer le serveur.

server.yaml contient les secrets du serveur, générés au premier démarrage :
  - secret_key : clé de signature des jetons de session
  - relay_port : port RTSP du relais vidéo (les accès sont autorisés par
    l'API à chaque lecture — voir auth externe MediaMTX)
  - relay_host : nom ou IP du relais annoncé aux clients. Vide (défaut) = les
    clients emploient l'hôte par lequel ils ont joint l'API, ce qui convient
    dès que l'API et le relais sont sur la même machine. À ne renseigner que si
    le relais est joignable à une autre adresse — et il faut alors qu'elle soit
    valable pour TOUS les consommateurs (murs d'images et analyse vidéo).
"""

import logging
import os
import secrets
import tempfile
import threading

import yaml

from sentinelle.config import load_config, save_config

from .auth import Users, _restreindre

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or os.environ.get("SENTINELLE_DATA", "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.params = self._charger_params()
        self.config_path = os.path.join(self.data_dir, "config.yaml")
        self.cfg = load_config(self.config_path)
        self.users = Users(self.data_dir, bytes.fromhex(self.params["secret_key"]))
        if self.users.vide():
            mdp = self.users.creer_admin_initial()
            logger.warning("=" * 60)
            logger.warning("COMPTE ADMIN CRÉÉ  —  identifiant : admin")
            logger.warning(f"Mot de passe initial : {mdp}")
            logger.warning("À changer à la première connexion (Configuration → "
                           "Mon compte).")
            logger.warning("=" * 60)
            # trace persistante, lisible une fois puis à supprimer par l'admin
            try:
                chemin_admin = os.path.join(self.data_dir, "admin-initial.txt")
                with open(chemin_admin, "w", encoding="utf-8") as f:
                    f.write(f"identifiant: admin\nmot de passe initial: {mdp}\n")
                _restreindre(chemin_admin)
            except OSError:
                pass

    # ------------------------------------------------------------- paramètres

    def _charger_params(self) -> dict:
        chemin = os.path.join(self.data_dir, "server.yaml")
        params = {}
        if os.path.exists(chemin):
            _restreindre(chemin)               # durcit aussi les installs antérieures au correctif
            with open(chemin, encoding="utf-8") as f:
                params = yaml.safe_load(f) or {}
        defauts = {
            "secret_key": lambda: secrets.token_hex(32),
            "relay_port": lambda: 8554,
        }
        manquants = [k for k in defauts if not params.get(k)]
        for k in manquants:
            params[k] = defauts[k]()
        # clés dont la valeur vide est légitime : on teste l'ABSENCE, sinon le
        # fichier serait réécrit à chaque démarrage
        optionnels = {"relay_host": ""}
        absents = [k for k in optionnels if k not in params]
        for k in absents:
            params[k] = optionnels[k]
        if manquants or absents:
            try:
                with open(chemin, "w", encoding="utf-8") as f:
                    f.write("# Sentinelle Server — secrets générés au premier démarrage.\n"
                            "# secret_key : signature des jetons de session (ne pas partager).\n"
                            "# relay_host : hôte du relais annoncé aux clients "
                            "(vide = hôte de l'API).\n")
                    yaml.safe_dump(params, f, sort_keys=False)
                _restreindre(chemin)           # 0600 : secret de signature des jetons
                logger.info(f"Paramètres serveur écrits dans {chemin} "
                            f"({', '.join(manquants + absents)})")
            except OSError as e:
                if manquants:
                    # secret_key ou relay_port à générer : ne pas pouvoir les
                    # persister est bloquant, sinon la clé de signature change à
                    # chaque redémarrage et tout le parc se déconnecte sans
                    # explication. Message explicite plutôt qu'une trace.
                    raise RuntimeError(
                        f"impossible d'écrire {chemin} ({e}). Le dossier de "
                        f"données doit être accessible en écriture par "
                        f"l'utilisateur du serveur (UID/GID 10001 dans l'image "
                        f"Docker) :  sudo chown -R 10001:10001 <dossier>") from None
                # seules des clés facultatives manquaient (mise à jour d'une
                # installation existante) : leur valeur par défaut tient en
                # mémoire, le serveur démarre normalement. Une écriture ratée
                # ici ne doit JAMAIS empêcher le démarrage.
                logger.warning(
                    f"{chemin} non modifiable ({e}) — valeurs par défaut "
                    f"appliquées en mémoire pour : {', '.join(absents)}. "
                    f"Pour les rendre configurables : "
                    f"sudo chown -R 10001:10001 <dossier de données>")
        return params

    # ----------------------------------------------------------- configuration

    def remplacer_config(self, data: dict) -> list[str]:
        """Valide puis applique une configuration complète (dict au format YAML
        du client). Un mot de passe vide conserve la valeur déjà stockée pour la
        même caméra. Retourne les avertissements de validation."""
        with self.lock:
            fd, tmp = tempfile.mkstemp(suffix=".yaml", dir=self.data_dir)
            os.close(fd)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
                nouveau = load_config(tmp)
            finally:
                os.remove(tmp)
            for cam in nouveau.cameras:
                if not cam.password:
                    ancien = self.cfg.camera(cam.id)
                    if ancien is not None:
                        cam.password = ancien.password
            # les rondes partagées se gèrent par /api/rounds : la config poussée
            # par un client n'écrase jamais celles déjà stockées (une session
            # admin qui enregistre les caméras ne doit pas remettre un instantané
            # périmé des rondes modifiées entre-temps)
            nouveau.sequences = self.cfg.sequences
            nouveau.path = self.config_path
            save_config(nouveau)
            self.cfg = nouveau
            return list(nouveau.warnings)

    def remplacer_rondes(self, sequences: list):
        """Remplace les rondes partagées ([Sequence] déjà validées) et persiste."""
        with self.lock:
            self.cfg.sequences = list(sequences)
            save_config(self.cfg)
