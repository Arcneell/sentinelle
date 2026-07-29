# Sécurité

## Signaler une vulnérabilité

Pas d'issue publique. Utiliser le signalement privé de GitHub : onglet Security du dépôt,
*Report a vulnerability*
([lien direct](https://github.com/Arcneell/sentinelle/security/advisories/new)). Le rapport
n'est visible que des mainteneurs jusqu'au correctif.

Indiquer la version, le mode de déploiement, la description de la faille, les étapes de
reproduction et l'impact estimé. Retirer les identifiants, jetons et adresses réels des
traces jointes.

Réponse sous quelques jours. Les correctifs sortent dans la dernière version en date ; il n'y
a pas de branche de maintenance. Le correctif crédite la personne qui a signalé, sauf demande
contraire.

## Périmètre

Concerné : l'API du serveur et son modèle de droits, l'autorisation au relais, le stockage et
la transmission des identifiants d'enregistreurs, les jetons de session et de flux, le client
de bureau.

Hors périmètre : les failles des caméras et enregistreurs, et celles de MediaMTX, Qt, mpv ou
des autres dépendances, à signaler à leurs projets respectifs.

## Comportements assumés

Documentés, ce ne sont pas des vulnérabilités :

- l'API parle HTTP en clair, à déployer derrière un VPN ou la surcouche Caddy fournie
  (`deploy/docker-compose.tls.yml`) ;
- les mots de passe du `config.yaml` client sont obscurcis avec une clé embarquée, pas
  chiffrés : préférer un compte d'enregistreur en lecture seule ;
- la réponse de `GET /api/streams` contient un jeton de relais en clair dans les URL RTSP ;
- les comptes de rôle Service reçoivent un jeton de flux sans expiration ; la révocation
  (*Déconnecter partout*) est le moyen de le couper.
