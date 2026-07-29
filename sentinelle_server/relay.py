"""Pilotage du relais vidéo (MediaMTX) via son API de configuration.

Le serveur déclare un chemin par flux (<camera>-main, <camera>-sub) dont la
source est l'URL RTSP du DVR, en mode « à la demande » : MediaMTX n'ouvre la
connexion vers le DVR que quand au moins un client lit le chemin, et la ferme
quelques secondes après le départ du dernier lecteur. Quel que soit le nombre
de spectateurs, chaque caméra ne consomme qu'UNE connexion vers son site.

La lecture sur le relais est protégée par les identifiants relay_user/relay_pass
(poussés ici dans la config d'auth de MediaMTX) ; les identifiants DVR, eux,
ne quittent jamais le serveur.
"""

import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 8


class Relay:
    def __init__(self, api_url: str | None = None):
        self.api = (api_url or os.environ.get("MEDIAMTX_API",
                                              "http://127.0.0.1:9997")).rstrip("/")
        self.pret = False
        self.derniere_erreur = ""
        # une seule synchronisation de fond à la fois : chaque appel remplace la
        # précédente (voir sync_fond)
        self._sync_lock = threading.Lock()
        self._sync_arret = threading.Event()
        self._sync_th: threading.Thread | None = None

    def _url(self, chemin: str) -> str:
        return self.api + chemin

    # ---------------------------------------------------------------- synchro

    def sync(self, store) -> None:
        """Aligne MediaMTX sur la configuration : un chemin par flux.

        L'autorisation de lecture est déléguée à l'API (auth externe MediaMTX,
        voir mediamtx.yml) : chaque lecture est validée par jeton + droits de
        l'utilisateur. Rien à pousser ici côté comptes."""
        voulus: dict[str, str] = {}
        for cam in store.cfg.cameras:
            for suffixe, flux in (("main", "main"), ("sub", "sub")):
                u = cam.url(flux)
                if u:
                    voulus[f"{cam.id}-{suffixe}"] = u

        r = requests.get(self._url("/v3/config/paths/list?itemsPerPage=1000"),
                         timeout=TIMEOUT)
        r.raise_for_status()
        existants = {item.get("name") for item in r.json().get("items", [])}
        existants.discard(None)

        for nom in existants - set(voulus):
            if nom in ("all", "all_others"):
                continue
            requests.delete(self._url(f"/v3/config/paths/delete/{nom}"),
                            timeout=TIMEOUT)

        for nom, source in voulus.items():
            conf = {
                "source": source,
                "sourceOnDemand": True,
                "sourceOnDemandStartTimeout": "12s",
                # source gardée ouverte après le départ du dernier lecteur :
                # les rotations / changements de page réutilisent la connexion
                "sourceOnDemandCloseAfter": "60s",
                # tirage DVR en TCP : fiable sur VPN et liens 4G
                "rtspTransport": "tcp",
            }
            rp = self._poser_chemin(nom, conf, nom in existants)
            if rp.status_code == 400 and "rtspTransport" in conf:
                # version de MediaMTX sans ce paramètre → repli sans lui
                conf.pop("rtspTransport")
                rp = self._poser_chemin(nom, conf, nom in existants)
            rp.raise_for_status()
        logger.info(f"Relais synchronisé : {len(voulus)} flux déclarés")

    def _poser_chemin(self, nom: str, conf: dict, existe: bool):
        if existe:
            return requests.patch(self._url(f"/v3/config/paths/patch/{nom}"),
                                  json=conf, timeout=TIMEOUT)
        return requests.post(self._url(f"/v3/config/paths/add/{nom}"),
                             json=conf, timeout=TIMEOUT)

    def sync_fond(self, store, tentatives: int = 90, delai: float = 2.0):
        """Synchronisation en arrière-plan avec retries (MediaMTX peut démarrer
        après l'API — ordre de démarrage des conteneurs non garanti).

        UNE seule tentative de fond à la fois : un nouvel appel (sauvegarde de
        configuration, rechargement) annule la précédente. Sinon, relais
        injoignable, chaque appel empilait un thread qui vivait
        tentatives × delai — soit trois minutes de threads concurrents poussant
        des configurations différentes au même relais."""
        def run(arret: threading.Event):
            for i in range(tentatives):
                if arret.is_set():
                    return
                try:
                    self.sync(store)
                    self.pret = True
                    self.derniere_erreur = ""
                    return
                except Exception as e:
                    self.derniere_erreur = str(e)
                    if i % 15 == 0:
                        logger.info(f"Relais pas encore joignable ({e}) — nouvel essai")
                    if arret.wait(delai):
                        return
            logger.error("Relais vidéo injoignable : les flux ne sont pas publiés")

        with self._sync_lock:
            self._sync_arret.set()             # arrête la tentative précédente
            self._sync_arret = threading.Event()
            self._sync_th = threading.Thread(target=run, args=(self._sync_arret,),
                                             daemon=True, name="relay-sync")
            self._sync_th.start()

    def stop(self):
        """Arrête la synchronisation de fond (arrêt du serveur, fin de test).

        Sans cela, le thread de retries survivait à l'application : en test,
        chaque instance en laissait un tourner trois minutes."""
        with self._sync_lock:
            self._sync_arret.set()
            th = self._sync_th
        if th is not None:
            th.join(timeout=5)

    # ------------------------------------------------------------- diagnostic

    def etat(self) -> dict:
        """État runtime des chemins (lecteurs connectés, source prête…)."""
        r = requests.get(self._url("/v3/paths/list?itemsPerPage=1000"),
                         timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
