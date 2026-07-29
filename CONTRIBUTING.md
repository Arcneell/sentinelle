# Contribuer à Sentinelle

Merci de l'intérêt porté au projet. Correctifs, prise en charge de nouvelles marques de
caméras, documentation et tests sont tous bienvenus.

Les échanges suivent le [code de conduite](CODE_OF_CONDUCT.md). Pour une faille de
sécurité, ne pas ouvrir d'*issue* publique : suivre [SECURITY.md](SECURITY.md).

## Signaler un bogue ou proposer une évolution

Ouvrir une [issue](https://github.com/Arcneell/sentinelle/issues) en utilisant le gabarit
proposé. Pour un bogue, les informations qui font gagner le plus de temps sont : la version
de Sentinelle, le système et le mode (autonome ou serveur central), la marque et le modèle
de l'équipement concerné, et le journal de l'application.

Le journal du client se trouve dans `%APPDATA%\Sentinelle\sentinelle.log` (Windows) ou
`~/.local/state/sentinelle/sentinelle.log` (Linux). Celui du serveur s'obtient par
`docker compose logs api`.

**Retirer les éléments sensibles avant de publier** : adresses IP publiques, noms de
domaine, identifiants d'enregistreurs, jetons. Le contenu d'une réponse `/api/streams` est
un secret, il contient un jeton de relais valide.

## Environnement de développement

Prérequis : Python 3.11 ou plus récent, et **libmpv** (voir la section *Démarrage rapide* du
[README](README.md) pour l'installer selon le système).

```bash
git clone https://github.com/Arcneell/sentinelle
cd sentinelle
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-server.txt
pip install pytest httpx ruff
python run.py
```

Pour travailler sur le serveur sans Docker :

```bash
python -m sentinelle_server --data ./data-dev
```

Le serveur complet, relais compris, se lance par `cd deploy && docker compose up -d --build`.

## Tests

```bash
pytest -q                        # suite complète
QT_QPA_PLATFORM=offscreen pytest -q tests/test_ui.py   # tests d'interface sans écran
```

Les tests d'interface construisent de vraies fenêtres Qt en mode hors-écran ; ils ne
vérifient pas le rendu, seulement l'absence de régression à la construction. Les tests du
serveur passent par `TestClient` et n'exigent ni Docker ni MediaMTX.

L'intégration continue rejoue la suite sur Python 3.11, 3.12 et 3.14, ajoute
`ruff check --select E9,F63,F7,F82` et `python -m compileall`, puis construit le paquet
Debian et vérifie qu'il s'installe et démarre avec ses seules dépendances strictes. Ces
mêmes commandes se lancent en local avant d'ouvrir une *pull request*.

Toute correction de bogue devrait venir avec le test qui échouait avant elle.

## Conventions de code

- **Langue** : le code, les noms de symboles, les commentaires, les messages de journal et
  l'interface sont en français. S'y tenir, y compris dans les nouveaux fichiers.
- **Style** : PEP 8, lignes de 88 caractères au plus, annotations de types sur les
  signatures publiques. Pas de formatage automatique imposé sur l'ensemble du dépôt : ne
  reformater que les lignes touchées, pour garder les diffs lisibles.
- **Commentaires** : expliquer *pourquoi*, pas *quoi*. Le dépôt documente délibérément les
  contraintes de terrain (pilotes graphiques défaillants, quotas de sessions des
  enregistreurs, liens 4G) à l'endroit du code qu'elles justifient ; un correctif qui lève
  une de ces contraintes doit mettre le commentaire à jour.
- **Dépendances** : en ajouter une se justifie dans la *pull request*. Le client est
  installé sur des postes verrouillés et le serveur tourne sans privilèges ; la surface est
  volontairement mince, et l'ONVIF est par exemple implémenté directement plutôt que via
  `zeep`.
- **Qt** : aucun appel réseau ni aucune attente bloquante dans le thread d'interface. Le
  travail long part dans un `QThread` ou un thread Python qui renvoie son résultat par
  signal.
- **Serveur** : `sentinelle_server` ne doit jamais importer Qt ni `sentinelle.ui`. Le code
  partagé avec le client (configuration, ONVIF) vit dans `sentinelle/`.

## Commits et pull requests

Les messages de commit suivent la convention *Conventional Commits*, avec une description en
français :

```
feat: ajoute la découverte des canaux Uniview
fix: évite le verrouillage du compte DVR après un échec d'authentification
docs: précise les prérequis VA-API sous Fedora
ci: construit le .deb sur Debian 13
```

Types utilisés : `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `ci`, `chore`. Le corps
du message explique la raison du changement et ses conséquences visibles.

Parcours d'une contribution :

1. Créer une branche depuis `main`.
2. Committer, en gardant les changements sans rapport dans des commits séparés.
3. Vérifier en local : `pytest -q` et `ruff check --select E9,F63,F7,F82 sentinelle sentinelle_server run.py`.
4. Ouvrir la *pull request* vers `main`, en décrivant le problème résolu et la manière dont
   le changement a été vérifié — matériel réel utilisé le cas échéant, marque et modèle
   compris.
5. L'intégration continue doit être verte. Les *pull requests* sont intégrées en *squash*,
   l'historique de `main` garde donc un commit par contribution.

Une modification qui touche au matériel (nouvelle marque, gabarit d'URL, particularité
ONVIF) gagne beaucoup à préciser sur quel équipement elle a été testée : c'est la partie du
projet qu'aucun test automatisé ne couvre.

## Licence des contributions

Le projet est sous [GNU Affero General Public License v3.0 ou ultérieure](LICENSE). En
proposant une contribution, vous acceptez qu'elle soit distribuée sous cette même licence.
Il n'y a pas d'accord de contribution (CLA) à signer, et chaque contributeur conserve le
droit d'auteur sur son travail.
