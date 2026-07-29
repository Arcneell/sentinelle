# Contribuer

Correctifs, nouvelles marques de caméras, documentation et tests sont bienvenus. Les
échanges suivent le [code de conduite](CODE_OF_CONDUCT.md). Pour une faille de sécurité, pas
d'issue publique : [SECURITY.md](SECURITY.md).

## Signaler un bogue

Ouvrir une [issue](https://github.com/Arcneell/sentinelle/issues) avec le gabarit. Utile :
version, système, mode (autonome ou serveur central), marque et modèle de l'équipement,
journal.

Journal du client : `%APPDATA%\Sentinelle\sentinelle.log` ou
`~/.local/state/sentinelle/sentinelle.log`. Côté serveur : `docker compose logs api`.

Retirer d'abord IP publiques, noms de domaine, identifiants et jetons. Une réponse
`/api/streams` est un secret : elle contient un jeton de relais valide.

## Développement

Python 3.11+ et libmpv (voir *Installation* dans le [README](README.md)).

```bash
git clone https://github.com/Arcneell/sentinelle
cd sentinelle
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-server.txt pytest httpx ruff
python run.py
```

Serveur sans Docker : `python -m sentinelle_server --data ./data-dev`. Serveur complet,
relais compris : `cd deploy && docker compose up -d --build`.

## Tests

```bash
pytest -q
ruff check --select E9,F63,F7,F82 sentinelle sentinelle_server run.py
```

Les tests d'interface construisent de vraies fenêtres Qt en mode hors-écran
(`QT_QPA_PLATFORM=offscreen`) et ne vérifient pas le rendu. Ceux du serveur passent par
`TestClient`, sans Docker ni MediaMTX.

La CI rejoue la suite sur Python 3.11, 3.12 et 3.14, puis construit le `.deb` et vérifie
qu'il s'installe et démarre avec ses seules dépendances strictes.

Une correction de bogue vient avec le test qui échouait avant elle.

## Conventions

- Code, symboles, commentaires, journaux et interface en français.
- PEP 8, lignes de 88 caractères au plus, types sur les signatures publiques. Aucun
  formateur automatique : ne reformater que les lignes touchées.
- Les commentaires expliquent pourquoi. Beaucoup consignent une contrainte de terrain
  (pilote graphique, quota de sessions d'un enregistreur, lien 4G) ; les mettre à jour si la
  contrainte tombe.
- Nouvelle dépendance : à justifier dans la pull request. La surface est volontairement
  mince, l'ONVIF est par exemple implémenté sans `zeep`.
- Aucun appel réseau ni attente bloquante dans le thread Qt.
- `sentinelle_server` n'importe ni Qt ni `sentinelle.ui`. Le code partagé vit dans
  `sentinelle/`.

## Commits et pull requests

Messages en *Conventional Commits*, description en français :

```
feat: ajoute la découverte des canaux Uniview
fix: évite le verrouillage du compte DVR après un échec d'authentification
ci: construit le .deb sur Debian 13
```

Types : `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `ci`, `chore`.

Branche depuis `main`, tests verts en local, puis pull request vers `main` en décrivant le
problème et la vérification faite. Les pull requests sont intégrées en *squash*.

Pour tout ce qui touche au matériel (marque, gabarit d'URL, particularité ONVIF), préciser
l'équipement testé : aucun test automatisé ne couvre cette partie.

## Licence

Le projet est sous [AGPL-3.0 ou ultérieure](LICENSE). Une contribution est distribuée sous
cette même licence. Pas de CLA, chaque contributeur garde son droit d'auteur.
