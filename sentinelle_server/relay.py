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
import time

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 8


class Relay:
    def __init__(self, api_url: str | None = None):
        self.api = (api_url or os.environ.get("MEDIAMTX_API",
                                              "http://127.0.0.1:9997")).rstrip("/")
        self.pret = False
        self.derniere_erreur = ""

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
        après l'API — ordre de démarrage des conteneurs non garanti)."""
        def run():
            for i in range(tentatives):
                try:
                    self.sync(store)
                    self.pret = True
                    self.derniere_erreur = ""
                    return
                except Exception as e:
                    self.derniere_erreur = str(e)
                    if i % 15 == 0:
                        logger.info(f"Relais pas encore joignable ({e}) — nouvel essai")
                    time.sleep(delai)
            logger.error("Relais vidéo injoignable : les flux ne sont pas publiés")
        threading.Thread(target=run, daemon=True, name="relay-sync").start()

    # ------------------------------------------------------------- diagnostic

    def etat(self) -> dict:
        """État runtime des chemins (lecteurs connectés, source prête…)."""
        r = requests.get(self._url("/v3/paths/list?itemsPerPage=1000"),
                         timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
