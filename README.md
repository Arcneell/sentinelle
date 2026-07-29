<p align="center">
  <img src="packaging/sentinelle.png" alt="Sentinelle" width="120"/>
</p>

<h1 align="center">Sentinelle</h1>

<p align="center">
  <strong>Mur d'images multi-sites pour caméras et enregistreurs RTSP / ONVIF.</strong><br/>
  Vues grille et plein écran, détection de mouvement ONVIF, profils de bande passante —<br/>
  en poste autonome, ou adossé à un serveur central avec comptes et droits par caméra.
</p>

<p align="center">
  <a href="https://github.com/Arcneell/sentinelle/releases"><img src="https://img.shields.io/github/v/release/Arcneell/sentinelle?color=ff7a18&label=version" alt="Version"/></a>
  <a href="https://github.com/Arcneell/sentinelle/actions/workflows/ci.yml"><img src="https://github.com/Arcneell/sentinelle/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776ab.svg" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/plateformes-Windows%20%7C%20Linux-informational.svg" alt="Plateformes"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-AGPL--3.0-blue.svg" alt="Licence AGPL-3.0"/></a>
</p>

---

Sentinelle transforme un poste de travail en mur d'images pour caméras et enregistreurs
RTSP / ONVIF. Les marques **Hikvision** et **Dahua** sont gérées nativement, plusieurs
autres via des gabarits d'URL, et n'importe quel équipement **ONVIF** par découverte
automatique. Chaque caméra reçoit un profil de bande passante, ce qui garde les grandes
grilles exploitables même derrière un lien 4G.

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Modes de déploiement](#modes-de-déploiement)
- [Démarrage rapide](#démarrage-rapide)
- [Configuration](#configuration)
- [Fonctionnement détaillé](#fonctionnement-détaillé)
- [Serveur central](#serveur-central)
- [Construction des paquets](#construction-des-paquets)
- [Architecture](#architecture)
- [Contribuer](#contribuer)
- [Sécurité](#sécurité)
- [Licence](#licence)

## Fonctionnalités

- **Vues grille (jusqu'à 4×4) et plein écran** — double-clic sur une tuile pour basculer.
- **Détection de mouvement ONVIF** — les tuiles qui bougent sont cerclées de rouge, et une
  *vue mouvement* remplit la grille en direct avec les seules caméras actives.
- **Profils de bande passante** — flux principal, secondaire ou vignette JPEG selon la vue.
  Une caméra hors écran ne tient aucune connexion, et rien n'est réencodé.
- **Large compatibilité et découverte réseau** — un balayage du réseau local suffit :
  URL de flux, URL de vignette et présence du PTZ sont résolues automatiquement.
- **Commande PTZ** et zoom numérique, mode d'affichage par tuile, plein écran multi-écrans.
- **Rotations et boucles** — séquences de vues ordonnées jouées en continu, avec éditeur.
- **Serveur central facultatif** — configuration partagée, comptes utilisateurs avec droits
  par caméra, et relais de flux qui ne tire chaque caméra **qu'une fois**, quel que soit le
  nombre de spectateurs.

## Modes de déploiement

Le mode se choisit au premier lancement de chaque poste, puis se verrouille : le modifier,
comme changer l'adresse du serveur, demande la connexion d'un compte administrateur sur ce
poste.

|                        | **Autonome** (par défaut)   | **Serveur central**                                                 |
| ---------------------- | --------------------------- | ------------------------------------------------------------------- |
| Infrastructure         | aucune                      | un hôte Docker (VM, NAS, mini-PC)                                   |
| Configuration          | locale à chaque poste       | centralisée : une caméra ajoutée une fois est visible par tous      |
| Identifiants des DVR   | sur chaque poste            | **ne quittent jamais le serveur** (les clients reçoivent un jeton)  |
| Contrôle d'accès       | —                           | sites et caméras par utilisateur, appliqués côté API *et* au relais |
| Bande passante         | un tirage par spectateur    | **un tirage par caméra**, seulement pendant le visionnage           |
| Administration         | —                           | panneau Administration intégré (comptes, caméras, boucles)          |

## Démarrage rapide

Prérequis : **Python 3.11+** et **libmpv**.

- Windows : placer `libmpv-2.dll` dans un dossier `lib/` à la racine du projet.
- Debian / Ubuntu : `sudo apt install libmpv2 libxcb-cursor0 va-driver-all`
  Fedora : `sudo dnf install mpv-libs xcb-util-cursor libva-utils`
  `libxcb-cursor0` est requis par le backend X11 de Qt, utilisé pour l'incrustation vidéo —
  y compris sous Wayland via XWayland. `va-driver-all` active le **décodage vidéo
  matériel** (VA-API), ce qui permet à un mini-PC de faible puissance d'afficher de
  nombreux flux sans saturer le processeur.
- Facultatif : `ffprobe` (paquet `ffmpeg`) améliore le diagnostic des échecs.

```bash
pip install -r requirements.txt
python run.py
```

La fenêtre de configuration s'ouvre au premier lancement.

> **Installation par paquet.** Un `.deb` est joint à chaque
> [version publiée](https://github.com/Arcneell/sentinelle/releases) :
> `sudo apt install ./sentinelle_<version>_amd64.deb`. C'est le chemin d'installation
> pris en charge : il installe `libmpv2`, les bibliothèques Qt/xcb et les pilotes VA-API
> (dépendances strictes) ainsi que `ffmpeg` (recommandé). `dpkg -i` ne résout pas les
> dépendances ; le cas échéant, enchaîner avec `apt -f install`.

## Configuration

Tout se gère dans l'interface : ajouter un site (fibre ou 4G), ajouter un enregistreur
(adresse et identifiants, puis découverte des canaux ou liste manuelle), puis cocher les
caméras à afficher.

Le fichier est écrit dans `%APPDATA%\Sentinelle\config.yaml` (Windows) ou
`~/.config/sentinelle/config.yaml` (Linux) ; un `config.yaml` placé à côté de l'exécutable
a la priorité. Les mots de passe y sont obscurcis, non chiffrés : la clé est embarquée dans
l'application, ce qui n'empêche qu'une lecture de passage. **Utiliser un compte DVR en
lecture seule.**

## Fonctionnement détaillé

### Détection de mouvement (ONVIF)

Le bouton **Mouvement** abonne l'application au flux d'événements ONVIF de chaque caméra
(PullPoint). Quand une caméra signale un mouvement, sa tuile est cerclée de rouge. Le
bouton **Vue mouvement** remplace la sélection manuelle par les seules caméras en train de
bouger, en direct : un mur sans intervention qui fait remonter l'activité de tous les
sites.

L'ONVIF et sa règle de mouvement doivent être activés sur l'équipement ; une caméra sans
service d'événements est simplement ignorée. Le mouvement retombe sur l'événement « off »
de la caméra, ou après quelques secondes sans nouvel événement.

### Profils de bande passante

Seul le flux demandé à l'enregistreur détermine le débit : il n'y a aucun réencodage, et
une caméra hors écran ne tient aucune connexion.

| Profil       | Grille                          | Plein écran            |
| ------------ | ------------------------------- | ---------------------- |
| Normal       | flux secondaire                 | flux principal (HD)    |
| Éco          | flux secondaire                 | flux secondaire        |
| Éco extrême  | vignette JPEG toutes les N s    | flux secondaire        |

Les rotations et les boucles ferment les flux en cours avant d'ouvrir les suivants. Le RTSP
passe en TCP. Le rendu privilégie la **robustesse sur la finesse** : sous Linux, la vidéo
est décodée en **matériel** (VA-API) puis rendue en logiciel, sans OpenGL. Ce chemin
fonctionne sur n'importe quel matériel, y compris les mini-PC fanless utilisés comme murs
d'images, dont les pilotes graphiques font souvent tomber le chemin OpenGL de mpv. Les
variables `SENTINELLE_MPV_VO` et `SENTINELLE_MPV_HWDEC` permettent de forcer un autre
réglage machine par machine.

### Matériel pris en charge

Hikvision, Dahua, Amcrest, Reolink, Uniview, Axis, Vivotek, Foscam et TP-Link/Tapo par
gabarits d'URL intégrés, plus **ONVIF** pour tout le reste. La découverte réseau ONVIF
balaie le réseau local et résout pour chaque caméra ses URL de flux (principal et
secondaire), son URL de vignette et sa capacité PTZ. L'import d'un enregistreur complet
découvre les canaux et leurs noms via l'ISAPI Hikvision, ou les liste manuellement pour les
autres marques.

La reconnexion applique un délai exponentiel, et les tentatives s'arrêtent sur un échec
d'authentification afin de ne pas verrouiller le compte de l'enregistreur.

## Serveur central

Le serveur tient en deux conteneurs : une API FastAPI et un relais de flux
[MediaMTX](https://github.com/bluenviron/mediamtx). Les flux sont relayés **à la demande**
sans réencodage (H.264 en passthrough), donc la charge processeur reste négligeable.

À déployer sur une machine Linux qui atteint les enregistreurs et que les postes peuvent
joindre. Prérequis : Docker et le greffon Compose
(`sudo apt install docker.io docker-compose-v2` sur Debian / Ubuntu). Depuis un clone de ce
dépôt :

```bash
cd deploy
docker compose up -d --build     # construit l'image de l'API et lance les deux conteneurs
```

- Au premier démarrage, un compte **admin** est créé ; son mot de passe initial est écrit
  dans les journaux de l'API (`docker compose logs api`) et dans
  `deploy/data/admin-initial.txt`. Se connecter avec, le changer (*Configuration → Mon
  compte*), puis supprimer ce fichier.
- Pour repartir d'une installation autonome existante, copier son `config.yaml` dans
  `deploy/data/` avant le premier démarrage : le format de fichier est identique.
- Tout s'administre depuis l'application, connecté en admin, via **Administration** : créer
  des comptes, leur accorder des sites entiers ou des caméras précises, modifier caméras,
  sites, boucles et réglages.
- Sur chaque poste : *Configuration → Connexion*, mode **Serveur central**, puis l'URL du
  serveur (`http://serveur:8080`) et la connexion. « Rester connecté » mémorise les
  identifiants pour un redémarrage sans intervention — utiliser un compte de visionnage
  dédié sur les murs d'images.
- **Ports** : `8080/tcp` (API : connexion, configuration, vignettes, PTZ, mouvement en SSE,
  autorisation du relais) et `8554/tcp` (relais RTSP). Le port de commande de MediaMTX
  reste interne au réseau Docker.
- **Propriétaire du dossier de données** : l'API tourne sans privilèges (UID/GID 10001),
  donc `deploy/data/` *et tout son contenu* doivent être accessibles en écriture à cet UID
  (`sudo chown -R 10001:10001 data`). Un fichier déposé là par root — amorçage manuel,
  ancienne installation — reste en lecture seule pour le serveur : il démarre quand même et
  le signale dans le journal, mais ne peut pas enregistrer les réglages.
- **Mise à jour** : `git pull && docker compose up -d --build`. Après modification de
  `deploy/mediamtx.yml`, recréer le relais pour qu'il recharge sa configuration :
  `docker compose up -d --force-recreate mediamtx`.

### Modèle de sécurité

Les mots de passe sont hachés en PBKDF2, jamais stockés ni transmis en clair, avec un
minimum de 8 caractères. Les sessions sont des jetons signés sans état, qu'un changement de
mot de passe invalide immédiatement et qui **expirent** au bout de
`SENTINELLE_TOKEN_TTL_H` heures (168 par défaut, soit 7 jours ; les clients ayant coché
« Rester connecté » les renouvellent silencieusement avant échéance, et les comptes de rôle
Service reçoivent à la place un jeton de flux sans expiration, voir plus bas). Les échecs
de connexion répétés depuis une même IP sont ralentis (HTTP 429). Les droits par caméra sont
appliqués à la fois dans l'API et au relais : MediaMTX interroge l'API pour chaque lecture
via son autorisation HTTP externe, et toute publication externe vers le relais est refusée.
Les identifiants des enregistreurs ne vivent que sur le serveur.

L'API parle HTTP en clair : la déployer sur un réseau de confiance (VPN), ou terminer le
TLS avec la surcouche Caddy fournie.

```bash
export SENTINELLE_DOMAIN=sentinelle.example.org   # ou « tls internal », voir deploy/Caddyfile
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

`deploy/data/` contient tous les secrets et est exclu du dépôt.

### Consommateurs de flux tiers

Tout autre programme ayant besoin des caméras — analyse vidéo, enregistrement — doit les
lire **à travers le relais** plutôt que de contacter les enregistreurs lui-même. Chaque
caméra ne coûte alors qu'une connexion vers son site quel que soit le nombre de
consommateurs, ce qui compte sur les liens 4G, et les identifiants des enregistreurs restent
sur le serveur.

Créer un compte de rôle **Service** (Administration → Utilisateurs), ne cocher que les
caméras nécessaires, puis appeler `GET /api/streams` avec son jeton de session :

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

Les URL sont directement exploitables par ffmpeg ou OpenCV et portent le jeton de relais
comme mot de passe RTSP : **la réponse est un secret**, à ne pas journaliser ni écrire dans
un fichier lisible par tous. Les flux sont transmis tels quels, donc `-main` a exactement la
résolution du flux principal de l'enregistreur : des zones de détection tracées sur un flux
direct restent valides. `snapshot: true` signale que `GET /api/snapshot/<camera>` répond, ce
qui est plus pratique qu'une extraction d'image par ffmpeg pour tracer ces zones. Le
mouvement est disponible sur `GET /api/events` (SSE) pour un consommateur qui préfère se
réveiller sur événement plutôt que scruter les images.

Le **rôle Service** est ce qui rend cet usage tenable sans surveillance. Un tel compte :

- reçoit un **jeton de flux sans expiration** (`expire_s: 0`). Les services d'analyse
  tournent des mois sans supervision et leur bibliothèque RTSP traite souvent un 401 comme
  un échec définitif : un jeton expirant les rendrait aveugles en silence. Son jeton *d'API*
  expire normalement, si bien qu'une fuite ne coûte qu'une reconnexion, pas un accès
  perpétuel à l'API ;
- **ne voit jamais tout** : l'option « tout » est forcée à l'arrêt, il lit exactement les
  sites et caméras cochés, rien de plus ;
- **n'atteint que les points d'accès de lecture** : `/api/streams`, `/api/session`,
  `/api/snapshot/…` et `/api/events`. L'administration, le PTZ, les boucles et même le
  changement de son propre mot de passe répondent 403.

Le jeton de flux n'expirant pas, la révocation est le moyen de le couper : sélectionner le
compte et cliquer **Déconnecter partout** (ou `POST /api/users/<nom>/revoke`), ce qui
invalide immédiatement tous ses jetons. Un changement de mot de passe a le même effet.
Retirer le rôle Service au compte tue également les jetons perpétuels déjà émis.

Si le relais est joignable à une autre adresse que l'API, renseigner `relay_host` dans
`deploy/data/server.yaml` — elle doit être utilisable par tous les consommateurs, postes de
travail compris.

## Construction des paquets

Des paquets Linux sont joints à chaque
[version publiée](https://github.com/Arcneell/sentinelle/releases). Pour les construire
soi-même :

```bash
# .deb Linux (fonctionne aussi depuis Windows, via Docker) -> dist/sentinelle_<version>_amd64.deb
# Construire sur la MÊME version de Debian que les machines cibles (Debian 13 / trixie).
docker run --rm -v "${PWD}:/src" -w /src debian:13 bash packaging/build_deb.sh
```

```powershell
# Exécutable Windows (PyInstaller) -> dist/Sentinelle/Sentinelle.exe
pwsh packaging/build_windows.ps1
```

Le script signe l'exécutable si un certificat de signature de code est fourni
(`$env:SENTINELLE_PFX` et `$env:SENTINELLE_PFX_PW`), ce qui est **recommandé** : les binaires
non signés sont régulièrement bloqués par les protections de poste. Sans certificat, la
construction aboutit quand même, non signée.

Après installation du `.deb`, lancer **Sentinelle** depuis le menu des applications ou par
la commande `sentinelle` (Debian 13, Ubuntu 24.04+). En session Wayland — le défaut de
GNOME — l'application demande automatiquement XWayland
(`QT_QPA_PLATFORM=xcb;wayland`, soit xcb avec repli wayland) pour que la vidéo s'affiche ;
ne définir cette variable soi-même que pour contourner cette détection. Si l'application se
retrouve en Wayland natif, donc sans vidéo, elle le signale au démarrage. Si le pilote
graphique fait tomber la machine, un clic droit sur l'icône de lancement propose **Mode
vidéo sûr**, également disponible par `sentinelle --safe-video`.

## Architecture

```
sentinelle/                  Client de bureau (PySide6 / Qt 6)
├── config.py                Modèle de données, gabarits d'URL, lecture/écriture config.yaml
├── probe.py                 Classement des échecs RTSP (auth / délai / réseau)
├── snapshot.py              Vignettes JPEG (ISAPI/CGI) et découverte des canaux Hikvision
├── onvif.py                 ONVIF : WS-Discovery, URI de flux et vignette, PTZ, événements
├── motion.py                Moniteur de mouvement ONVIF (un thread d'abonnement par caméra)
├── player.py                Chargement de libmpv, réglages RTSP, décodage matériel VA-API
├── remote.py                Mode serveur : client API, session, écoute SSE du mouvement
└── ui/                      Barre de titre, panneau latéral, vues, tuiles, dialogues, thème
sentinelle_server/           Serveur (aucune dépendance Qt)
├── app.py                   API FastAPI : connexion, config, vignettes, PTZ, SSE, auth relais
├── auth.py                  Comptes, hachage PBKDF2, jetons signés, droits
├── store.py                 Configuration centrale (même format YAML) + amorçage admin
├── relay.py                 Orchestration de MediaMTX (un chemin à la demande par flux)
└── motion.py                Moniteur de mouvement ONVIF côté serveur + bus d'événements
deploy/                      docker-compose.yml, Dockerfile.server, mediamtx.yml
packaging/                   Script de construction du .deb, génération des icônes
tests/                       Tests unitaires et de fumée (pytest)
```

L'ONVIF est implémenté directement sur SOAP/HTTP (authentification WS-UsernameToken en
digest), sans dépendance `zeep` ou `onvif-zeep`. La découverte réseau utilise le multicast
WS-Discovery, qui ne franchit pas les frontières de VLAN ou de VPN : les caméras sur des
sous-réseaux routés s'ajoutent par IP directe. Chaque tuile fait tourner sa propre instance
de libmpv sur un thread distinct, de sorte qu'un flux en échec n'affecte jamais les autres.

**Pile technique.** Client : Python 3.11+, [PySide6](https://doc.qt.io/qtforpython/) (Qt 6),
[python-mpv](https://github.com/jaseg/python-mpv), PyYAML, requests. Serveur : FastAPI,
uvicorn et [MediaMTX](https://github.com/bluenviron/mediamtx) en conteneurs.

## Contribuer

Les contributions sont bienvenues : correctifs, prise en charge de nouvelles marques,
traductions, documentation. Le guide [CONTRIBUTING.md](CONTRIBUTING.md) décrit
l'environnement de développement, les conventions de code et de commit, et le parcours
d'une *pull request*. Les échanges suivent le [code de conduite](CODE_OF_CONDUCT.md).

L'interface et les messages sont pour l'instant entièrement en français ; le code et les
commentaires le sont également.

## Sécurité

Pour signaler une vulnérabilité, suivre [SECURITY.md](SECURITY.md) — pas d'*issue*
publique.

## Licence

Sentinelle est un logiciel libre distribué sous licence
[GNU Affero General Public License v3.0 ou ultérieure](LICENSE).
