# Serveur central

Le serveur tient en deux conteneurs : une API FastAPI et un relais
[MediaMTX](https://github.com/bluenviron/mediamtx). MediaMTX n'ouvre la connexion vers
l'enregistreur que quand un client lit le chemin, et la ferme peu après le départ du
dernier lecteur. Une caméra ne coûte donc qu'une connexion vers son site, quel que soit le
nombre de spectateurs, ce qui est l'intérêt principal sur les liens 4G.

Ce document couvre le déploiement. Le fonctionnement du client est décrit dans le
[README](../README.md).

## Déploiement

À installer sur une machine Linux qui atteint les enregistreurs et que les postes peuvent
joindre. Prérequis : Docker et le greffon Compose
(`sudo apt install docker.io docker-compose-v2`).

```bash
cd deploy
docker compose up -d --build
```

Au premier démarrage, un compte `admin` est créé. Son mot de passe est écrit dans
`deploy/data/admin-initial.txt` et dans `docker compose logs api`. Se connecter, le changer
dans *Configuration → Mon compte*, puis supprimer le fichier.

Pour repartir d'une installation autonome, copier son `config.yaml` dans `deploy/data/`
avant le premier démarrage : le format est identique.

Mise à jour : `git pull && docker compose up -d --build`. Après modification de
`deploy/mediamtx.yml`, recréer le relais pour qu'il recharge :
`docker compose up -d --force-recreate mediamtx`.

## Postes clients

*Configuration → Connexion*, mode serveur central, URL du serveur (`http://serveur:8080`),
puis connexion. Le mode se verrouille ensuite : le changer, comme changer l'adresse du
serveur, demande un compte administrateur sur ce poste.

« Rester connecté » mémorise les identifiants pour un redémarrage sans intervention. Sur un
mur d'images, utiliser un compte de visionnage dédié.

## Ports et données

`8080/tcp` pour l'API (connexion, configuration, vignettes, PTZ, mouvement en SSE,
autorisation du relais) et `8554/tcp` pour le relais RTSP. Le port de commande de MediaMTX
reste interne au réseau Docker.

`deploy/data/` contient tous les secrets et est exclu du dépôt. L'API tourne sans privilèges
(UID/GID 10001) : ce dossier et son contenu doivent lui être accessibles en écriture,
`sudo chown -R 10001:10001 data`. Un fichier déposé là par root reste en lecture seule pour
le serveur, qui démarre quand même, le signale dans son journal, mais ne peut pas
enregistrer les réglages.

Si le relais est joignable à une autre adresse que l'API, renseigner `relay_host` dans
`deploy/data/server.yaml`. Elle doit être utilisable par tous les consommateurs, postes
compris.

## Sécurité

Mots de passe hachés en PBKDF2, 8 caractères minimum, jamais stockés ni transmis en clair.
Les sessions sont des jetons signés sans état : un changement de mot de passe les invalide
immédiatement, et ils expirent au bout de `SENTINELLE_TOKEN_TTL_H` heures (168 par défaut).
Les clients ayant coché « Rester connecté » les renouvellent avant échéance. Les échecs de
connexion répétés depuis une même IP sont ralentis (HTTP 429).

Les droits par caméra sont appliqués dans l'API et au relais : MediaMTX interroge l'API pour
chaque lecture, et toute publication externe vers le relais est refusée.

L'API parle HTTP en clair. La déployer derrière un VPN, ou terminer le TLS avec la surcouche
Caddy fournie :

```bash
export SENTINELLE_DOMAIN=sentinelle.example.org   # ou « tls internal », voir deploy/Caddyfile
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

## Lecture des flux par un service tiers

Un service d'analyse vidéo ou d'enregistrement doit lire les caméras à travers le relais,
pas contacter les enregistreurs lui-même : une seule connexion par caméra, et les
identifiants restent sur le serveur.

Créer un compte de rôle **Service** (Administration → Utilisateurs), cocher les caméras
nécessaires, puis appeler `/api/streams` :

```bash
TOKEN=$(curl -s -X POST http://serveur:8080/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"vision","password":"…"}' | jq -r .token)

curl -s http://serveur:8080/api/streams -H "Authorization: Bearer $TOKEN"
```

```json
{ "relay": { "host": "serveur", "port": 8554 },
  "expire_s": 0,
  "streams": [ { "camera": "cam1", "nom": "Entrée", "site": "s1", "site_nom": "Site 1",
                 "lien": "4g", "profil": "normal", "ptz": false, "snapshot": true,
                 "main": "rtsp://vision:<jeton>@serveur:8554/cam1-main",
                 "sub":  "rtsp://vision:<jeton>@serveur:8554/cam1-sub" } ] }
```

Les URL sont directement exploitables par ffmpeg ou OpenCV. Elles portent le jeton de relais
comme mot de passe RTSP : **la réponse est un secret**, à ne pas journaliser ni écrire dans
un fichier lisible par tous.

Les flux sont transmis tels quels, donc `-main` a exactement la résolution du flux principal
de l'enregistreur : des zones de détection tracées sur un flux direct restent valides. Si
`snapshot` vaut `true`, `GET /api/snapshot/<camera>` répond, plus pratique qu'une extraction
ffmpeg pour tracer ces zones. `GET /api/events` (SSE) diffuse le mouvement, pour un
consommateur qui préfère se réveiller sur événement.

Un compte de rôle Service :

- reçoit un jeton de flux sans expiration (`expire_s: 0`). Les services d'analyse tournent
  des mois sans supervision et leur bibliothèque RTSP traite souvent un 401 comme un échec
  définitif. Son jeton d'API, lui, expire normalement ;
- ne voit jamais tout le parc : l'option « tout » est forcée à l'arrêt ;
- n'atteint que `/api/streams`, `/api/session`, `/api/snapshot/…` et `/api/events`.
  Administration, PTZ, boucles et changement de son propre mot de passe répondent 403.

Le jeton de flux n'expirant pas, la révocation est le moyen de le couper : **Déconnecter
partout** sur le compte, ou `POST /api/users/<nom>/revoke`. Un changement de mot de passe a
le même effet, et retirer le rôle Service tue les jetons perpétuels déjà émis.
