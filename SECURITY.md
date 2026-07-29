# Politique de sécurité

## Versions prises en charge

Les correctifs de sécurité sont publiés pour la dernière version en date. Le projet
n'entretient pas de branches de maintenance : mettre à jour vers la
[version la plus récente](https://github.com/Arcneell/sentinelle/releases) est la voie
recommandée.

## Signaler une vulnérabilité

**Ne pas ouvrir d'issue publique.** Utiliser le signalement privé de GitHub :
onglet **Security** du dépôt, puis *Report a vulnerability*
([lien direct](https://github.com/Arcneell/sentinelle/security/advisories/new)). Le rapport
n'est visible que des mainteneurs jusqu'à la publication du correctif.

Un rapport utile contient : la version concernée, le mode de déploiement (autonome ou
serveur central), la description de la faille, les étapes pour la reproduire, et l'impact
estimé. Retirer les identifiants, jetons et adresses réels des traces jointes au rapport.

Réponse sous quelques jours. Le projet est maintenu sur du temps limité : une faille
critique passe avant tout le reste, une faille mineure peut attendre la prochaine version.
Le correctif publié crédite la personne qui a signalé, sauf demande contraire.

## Périmètre

Font partie du périmètre : l'API du serveur central et son modèle de droits, la validation
de l'autorisation au relais, le stockage et la transmission des identifiants
d'enregistreurs, la gestion des jetons de session et de flux, et le client de bureau.

N'en font pas partie : les vulnérabilités des caméras et enregistreurs eux-mêmes, ni celles
de MediaMTX, Qt, mpv ou des autres dépendances — à signaler à leurs projets respectifs,
sauf si Sentinelle les expose d'une manière qui leur est propre.

## Attentes de déploiement connues

Ces points sont documentés et assumés, ce ne sont pas des vulnérabilités :

- **L'API parle HTTP en clair.** Le serveur est prévu pour un réseau de confiance ou un
  VPN ; une surcouche Caddy est fournie pour terminer le TLS
  (`deploy/docker-compose.tls.yml`).
- **Les mots de passe du `config.yaml` client sont obscurcis, pas chiffrés.** La clé est
  embarquée dans l'application : cela empêche une lecture de passage, rien de plus. Un
  compte d'enregistreur en lecture seule est recommandé.
- **La réponse de `GET /api/streams` est un secret** : elle contient un jeton de relais
  valide, en clair, dans les URL RTSP.
- **Les comptes de rôle Service reçoivent un jeton de flux sans expiration**, par
  conception. La révocation (*Déconnecter partout*) est le moyen de le couper.
