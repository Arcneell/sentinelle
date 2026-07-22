"""Modèle de configuration et lecture/écriture de config.yaml.

Fichier géré par l'application (fenêtre Configuration). Emplacement par défaut
dans le profil utilisateur, ou config.yaml à côté de l'exe (mode portable).
Une entrée invalide est ignorée avec un avertissement, sans bloquer les autres.
"""

import base64
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote

import yaml

logger = logging.getLogger(__name__)

# Les mots de passe ne sont pas stockés en clair dans config.yaml (préfixe "obf:").
# NB : c'est un brouillage local, pas du chiffrement — la clé est embarquée dans
# l'app pour que la config reste déployable telle quelle sur chaque poste. Ça
# empêche la lecture fortuite du fichier, pas un attaquant déterminé.
# NE PAS MODIFIER : cette valeur historique déchiffre les mots de passe déjà
# enregistrés dans les config.yaml existants. La changer les rendrait illisibles.
_OBF_KEY = b"RTSP-TOOL.local.v1"


def _xor(data: bytes) -> bytes:
    return bytes(b ^ _OBF_KEY[i % len(_OBF_KEY)] for i, b in enumerate(data))


def obfusquer(clair: str) -> str:
    if not clair:
        return ""
    return "obf:" + base64.b64encode(_xor(clair.encode("utf-8"))).decode("ascii")


def desobfusquer(valeur: str) -> str:
    if isinstance(valeur, str) and valeur.startswith("obf:"):
        try:
            return _xor(base64.b64decode(valeur[4:])).decode("utf-8")
        except Exception:
            return ""
    return valeur or ""

# Flux demandé selon la vue : sub = substream, main = mainstream.
# En eco-extreme, la grille passe en mode photo ; ce mapping ne vaut que pour le mono.
PROFILS = {
    "normal": {"grille": "sub", "mono": "main"},
    "eco": {"grille": "sub", "mono": "sub"},
    "eco-extreme": {"grille": "sub", "mono": "sub"},
}

PROFIL_LABELS = {
    "normal": "Normal",
    "eco": "Éco",
    "eco-extreme": "Ultra éco",
}

# Gabarits d'URL RTSP par marque : (chemin mainstream, chemin substream).
# Placeholders : {ch} = canal, {ch2} = canal sur 2 chiffres.
BRAND_URL = {
    "hikvision": ("/Streaming/Channels/{ch}01", "/Streaming/Channels/{ch}02"),
    "dahua":     ("/cam/realmonitor?channel={ch}&subtype=0", "/cam/realmonitor?channel={ch}&subtype=1"),
    "amcrest":   ("/cam/realmonitor?channel={ch}&subtype=0", "/cam/realmonitor?channel={ch}&subtype=1"),
    "reolink":   ("/h264Preview_{ch2}_main", "/h264Preview_{ch2}_sub"),
    "uniview":   ("/unicast/c{ch}/s0/live", "/unicast/c{ch}/s1/live"),
    "axis":      ("/axis-media/media.amp?camera={ch}",
                  "/axis-media/media.amp?camera={ch}&resolution=640x360"),
    "vivotek":   ("/live.sdp", "/live2.sdp"),
    "foscam":    ("/videoMain", "/videoSub"),
    "tplink":    ("/stream1", "/stream2"),
}

# Libellés marque pour l'interface (ordre d'affichage)
MARQUE_LABELS = {
    "hikvision": "Hikvision",
    "dahua": "Dahua",
    "amcrest": "Amcrest",
    "reolink": "Reolink",
    "uniview": "Uniview",
    "axis": "Axis",
    "vivotek": "Vivotek",
    "foscam": "Foscam",
    "tplink": "TP-Link / Tapo",
    "onvif": "ONVIF (auto — toutes marques)",
    "custom": "Autre (URLs RTSP libres)",
}

MARQUES = tuple(MARQUE_LABELS)               # toutes les marques connues
MARQUES_URL_LIBRE = ("onvif", "custom")      # pas de gabarit : URLs stockées
LIENS = ("fibre", "4g")


APP_DIR_WIN = "Sentinelle"
APP_DIR_NIX = "sentinelle"
_ANCIENS_WIN = ("RTSP-TOOL",)          # migration depuis l'ancien nom
_ANCIENS_NIX = ("rtsp-tool",)


def app_data_dir() -> str:
    """Dossier de données de l'application (config, moteurs, shaders)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, APP_DIR_WIN)
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, APP_DIR_NIX)


def app_state_dir() -> str:
    """Dossier d'état (journaux) : ~/.local/state/sentinelle sous Linux (spec
    XDG — un journal qui tourne n'a rien à faire dans ~/.config, qui part dans
    les sauvegardes/synchros), même dossier que la config sous Windows."""
    if sys.platform == "win32":
        return app_data_dir()
    base = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(base, APP_DIR_NIX)


def migrer_ancien_dossier():
    """Renomme l'ancien dossier RTSP-TOOL en Sentinelle (une fois)."""
    nouveau = app_data_dir()
    if os.path.exists(nouveau):
        return
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        anciens = _ANCIENS_WIN
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        anciens = _ANCIENS_NIX
    for a in anciens:
        chemin = os.path.join(base, a)
        if os.path.isdir(chemin):
            try:
                os.rename(chemin, nouveau)
                logger.info(f"Données migrées : {chemin} -> {nouveau}")
            except OSError as e:
                logger.warning(f"Migration impossible ({e})")
            return


def default_config_path() -> str:
    return os.path.join(app_data_dir(), "config.yaml")


def slugify(nom: str) -> str:
    """« Port — Quai 2 » → « port-quai-2 » (ids internes générés par l'UI)."""
    s = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "item"


def mask_url(url: str) -> str:
    """rtsp://user:pass@host → rtsp://user:***@host (pour logs et UI)."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        return f"{scheme}://{creds.split(':', 1)[0]}:***@{host}"
    return url


# ---------------------------------------------------------------- structures

@dataclass
class Site:
    id: str
    nom: str
    lien: str = "fibre"           # fibre | 4g


@dataclass
class Camera:
    id: str
    nom: str
    site: Site
    profil: str = "normal"        # normal | eco | eco-extreme
    marque: str = "hikvision"     # voir MARQUES
    hote: str = ""
    port: int = 554
    canal: int = 1
    user: str = ""
    password: str = ""
    url_substream: str = ""       # marques url-libre (onvif/custom)
    url_mainstream: str = ""      # marques url-libre (onvif/custom)
    url_snapshot: str = ""        # url-libre / profil eco-extreme
    port_http: int = 80           # ISAPI/CGI/ONVIF (mode photo, import, PTZ)
    photo_intervalle_s: int = 10  # rafraîchissement du mode photo
    reconnexion_preventive_s: int = 0   # 0 = désactivé
    ptz: bool = False             # caméra motorisée (ONVIF)
    onvif_profile: str = ""       # token du profil ONVIF principal (PTZ)

    def _auth(self) -> str:
        if not self.user:
            return ""
        return f"{quote(self.user, safe='')}:{quote(self.password, safe='')}@"

    def url(self, flux: str) -> str:
        """URL RTSP pour flux ∈ {sub, main}."""
        main = flux == "main"
        if self.marque in MARQUES_URL_LIBRE:
            u = self.url_mainstream if main else self.url_substream
            u = u or self.url_mainstream or self.url_substream
            if self.marque == "onvif":
                from .onvif import inject_auth       # identifiants pour la lecture
                return inject_auth(u, self.user, self.password)
            return u
        base = f"rtsp://{self._auth()}{self.hote}:{self.port}"
        tmpl = BRAND_URL.get(self.marque, BRAND_URL["hikvision"])
        chemin = tmpl[0] if main else tmpl[1]
        return base + chemin.format(ch=self.canal, ch2=f"{self.canal:02d}")

    def url_pour_vue(self, vue: str) -> str:
        """URL selon la vue ∈ {grille, mono}, en appliquant le profil."""
        return self.url(PROFILS[self.profil][vue])

    def flux_pour_vue(self, vue: str) -> str:
        return PROFILS[self.profil][vue]

    def snapshot_url(self) -> str:
        """URL HTTP d'une image instantanée (mode photo). Vide si non supporté."""
        if self.marque in MARQUES_URL_LIBRE:
            return self.url_snapshot
        base = f"http://{self.hote}:{self.port_http}"
        if self.marque in ("dahua", "amcrest"):
            return f"{base}/cgi-bin/snapshot.cgi?channel={self.canal}"
        if self.marque == "hikvision":
            # image du substream (id x02) = résolution réduite, légère sur un lien 4G
            return f"{base}/ISAPI/Streaming/channels/{self.canal * 100 + 2}/picture"
        return ""     # autres marques : pas de snapshot HTTP standard

    def to_dict(self) -> dict:
        d = {"id": self.id, "nom": self.nom, "site": self.site.id,
             "profil": self.profil, "marque": self.marque}
        if self.marque in MARQUES_URL_LIBRE:
            d.update(url_substream=self.url_substream,
                     url_mainstream=self.url_mainstream,
                     url_snapshot=self.url_snapshot)
            if self.marque == "onvif":
                d.update(hote=self.hote, port_http=self.port_http)
        else:
            d.update(hote=self.hote, port=self.port, canal=self.canal,
                     port_http=self.port_http)
        d.update(user=self.user, password=obfusquer(self.password),
                 photo_intervalle_s=self.photo_intervalle_s)
        if self.reconnexion_preventive_s:
            d["reconnexion_preventive_s"] = self.reconnexion_preventive_s
        if self.ptz:
            d["ptz"] = True
            d["onvif_profile"] = self.onvif_profile
        return d


@dataclass
class Etape:
    mode: str                      # grille | mono
    cameras: list                  # ids ; en mono : un seul élément
    duree_s: int = 30

    def to_dict(self) -> dict:
        return {"mode": self.mode, "cameras": list(self.cameras), "duree_s": self.duree_s}


@dataclass
class Sequence:
    nom: str
    etapes: list = field(default_factory=list)   # [Etape]
    # rondes partagées (mode serveur) : identifiant stable + attribution
    id: str = ""
    tous: bool = False                           # attribuée à tous les comptes
    utilisateurs: list = field(default_factory=list)   # comptes attribués
    # transitoire côté client : ronde reçue du serveur, non modifiable localement
    partagee: bool = False

    def to_dict(self) -> dict:
        d = {"nom": self.nom, "etapes": [e.to_dict() for e in self.etapes]}
        if self.id:
            d["id"] = self.id
        if self.tous:
            d["tous"] = True
        if self.utilisateurs:
            d["utilisateurs"] = list(self.utilisateurs)
        return d


@dataclass
class AppConfig:
    sites: list = field(default_factory=list)      # [Site]
    cameras: list = field(default_factory=list)    # [Camera]
    sequences: list = field(default_factory=list)  # [Sequence]
    warnings: list = field(default_factory=list)   # messages de validation
    path: str = ""
    rotation_duree_s: int = 20

    def site(self, site_id: str) -> Site | None:
        return next((s for s in self.sites if s.id == site_id), None)

    def camera(self, cam_id: str) -> Camera | None:
        return next((c for c in self.cameras if c.id == cam_id), None)

    def unique_id(self, base: str, taken: set | None = None) -> str:
        """Id unique dérivé d'un nom (les ids sont internes, générés par l'UI)."""
        taken = taken or ({s.id for s in self.sites} | {c.id for c in self.cameras})
        slug = slugify(base)
        cand, i = slug, 2
        while cand in taken:
            cand, i = f"{slug}-{i}", i + 1
        return cand


def purger_cameras_sequences(cfg: AppConfig, ids_retires: set):
    """Retire des séquences les caméras supprimées (étapes vides éliminées)."""
    for seq in cfg.sequences:
        for etape in seq.etapes:
            etape.cameras = [c for c in etape.cameras if c not in ids_retires]
        seq.etapes = [e for e in seq.etapes if e.cameras]
    cfg.sequences = [s for s in cfg.sequences if s.etapes]


# -------------------------------------------------------------------- lecture

def load_config(path: str) -> AppConfig:
    """Charge config.yaml. Fichier absent = config vide (premier lancement) ;
    les entrées invalides sont collectées dans AppConfig.warnings."""
    cfg = AppConfig(path=path)
    if not os.path.exists(path):
        logger.info(f"Pas de config existante ({path}) — démarrage vide")
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("racine YAML inattendue")
    except (OSError, yaml.YAMLError, ValueError) as e:
        # config illisible/corrompue (ex. écriture interrompue par un crash) :
        # on n'empêche jamais l'ouverture — on repart vide en le signalant.
        logger.error(f"Config illisible ({path}) : {e} — démarrage vide")
        cfg.warnings.append(f"Config illisible ({e}) — repartie de zéro")
        return cfg

    options = raw.get("options") or {}
    try:
        cfg.rotation_duree_s = max(3, int(options.get("rotation_duree_s", 20)))
    except (TypeError, ValueError):
        cfg.warnings.append("[options] rotation_duree_s invalide → 20")

    for s in raw.get("sites") or []:
        try:
            lien = str(s.get("lien", "fibre")).lower()
            if lien not in LIENS:
                cfg.warnings.append(f"[site {s.get('id')}] lien '{lien}' inconnu → fibre")
                lien = "fibre"
            cfg.sites.append(Site(id=str(s["id"]), nom=str(s.get("nom") or s["id"]), lien=lien))
        except (KeyError, TypeError) as e:
            cfg.warnings.append(f"[site ?] entrée invalide ({e}) — ignorée")

    ids_vus = set()
    for c in raw.get("cameras") or []:
        nom = c.get("id") or c.get("nom") or "<sans id>"
        try:
            cam_id = str(c["id"])
            if cam_id in ids_vus:
                raise ValueError(f"id '{cam_id}' en double")
            site = cfg.site(str(c.get("site")))
            if site is None:
                raise ValueError(f"site '{c.get('site')}' inconnu")
            marque = str(c.get("marque", "hikvision")).lower()
            if marque not in MARQUES:
                raise ValueError(f"marque '{marque}' inconnue")
            profil = str(c.get("profil") or ("eco" if site.lien == "4g" else "normal")).lower()
            if profil not in PROFILS:
                cfg.warnings.append(f"[{cam_id}] profil '{profil}' inconnu → normal")
                profil = "normal"
            if marque in MARQUES_URL_LIBRE:
                if not (c.get("url_substream") or c.get("url_mainstream")):
                    raise ValueError(f"marque '{marque}' : url_substream ou url_mainstream requis")
            elif not c.get("hote"):
                raise ValueError("champ 'hote' requis")

            cfg.cameras.append(Camera(
                id=cam_id,
                nom=str(c.get("nom") or cam_id),
                site=site,
                profil=profil,
                marque=marque,
                hote=str(c.get("hote", "")),
                port=int(c.get("port", 554)),
                canal=int(c.get("canal", 1)),
                user=str(c.get("user", "")),
                password=desobfusquer(str(c.get("password", ""))),
                url_substream=str(c.get("url_substream", "")),
                url_mainstream=str(c.get("url_mainstream", "")),
                url_snapshot=str(c.get("url_snapshot", "")),
                port_http=int(c.get("port_http", 80)),
                photo_intervalle_s=max(2, int(c.get("photo_intervalle_s", 10))),
                reconnexion_preventive_s=max(0, int(c.get("reconnexion_preventive_s", 0))),
                ptz=bool(c.get("ptz", False)),
                onvif_profile=str(c.get("onvif_profile", "")),
            ))
            ids_vus.add(cam_id)
        except (KeyError, ValueError, TypeError) as e:
            cfg.warnings.append(f"[{nom}] config invalide : {e} — caméra ignorée")

    cam_ids = {c.id for c in cfg.cameras}
    for s in raw.get("sequences") or []:
        nom = s.get("nom") or "<sans nom>"
        try:
            etapes = []
            for e in s.get("etapes") or []:
                mode = str(e.get("mode", "grille"))
                if mode not in ("grille", "mono"):
                    raise ValueError(f"mode '{mode}' inconnu")
                cams = [str(x) for x in (e.get("cameras") or []) if str(x) in cam_ids]
                if not cams:
                    raise ValueError("étape sans caméra valide")
                if mode == "mono":
                    cams = cams[:1]
                etapes.append(Etape(mode=mode, cameras=cams,
                                    duree_s=max(3, int(e.get("duree_s", 30)))))
            if not etapes:
                raise ValueError("aucune étape valide")
            cfg.sequences.append(Sequence(
                nom=str(nom), etapes=etapes,
                id=str(s.get("id", "")),
                tous=bool(s.get("tous", False)),
                utilisateurs=[str(x) for x in (s.get("utilisateurs") or [])]))
        except (KeyError, ValueError, TypeError) as e:
            cfg.warnings.append(f"[séquence {nom}] invalide : {e} — ignorée")

    # identifiant stable pour chaque séquence (rondes partagées côté serveur) ;
    # les fichiers existants n'en ont pas : on en génère un depuis le nom
    seq_ids = {s.id for s in cfg.sequences if s.id}
    for s in cfg.sequences:
        if not s.id:
            base = slugify(s.nom)
            cand, i = base, 2
            while cand in seq_ids:
                cand, i = f"{base}-{i}", i + 1
            s.id = cand
            seq_ids.add(cand)

    for w in cfg.warnings:
        logger.warning(w)
    logger.info(f"Config : {len(cfg.cameras)} caméra(s), {len(cfg.sites)} site(s), "
                f"{len(cfg.sequences)} séquence(s) depuis {path}")
    return cfg


# ------------------------------------------------------------------- écriture

def save_config(cfg: AppConfig):
    """Réécrit config.yaml (écriture atomique : tmp puis remplacement)."""
    data = {
        "options": {"rotation_duree_s": cfg.rotation_duree_s},
        "sites": [{"id": s.id, "nom": s.nom, "lien": s.lien} for s in cfg.sites],
        "cameras": [c.to_dict() for c in cfg.cameras],
        "sequences": [s.to_dict() for s in cfg.sequences],
    }
    os.makedirs(os.path.dirname(os.path.abspath(cfg.path)), exist_ok=True)
    tmp = cfg.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Sentinelle — fichier géré par l'application (fenêtre Configuration).\n")
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())          # forcer l'écriture disque avant le remplacement
    os.replace(tmp, cfg.path)
    logger.info(f"Config enregistrée : {cfg.path}")
