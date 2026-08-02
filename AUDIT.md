# Audit de qualité YCT, 2 août 2026

## Portée

Cet audit couvre la chaîne complète: sources primaires, validation temporelle,
publication, observabilité, interface, artefact Git et activation Debian. La
preuve opérationnelle d'une release est `/opt/yct/SOURCE_SHA` plus la sortie de
`verify_live.py`; elle n'est pas recopiée ici pour éviter un document périmé à
chaque déploiement.

La référence de production avant ce renforcement était le commit
`ff354ed389134487cb9eee6284054a9e37b6e1b2`, activé le 2 août 2026. Les chiffres
publics y étaient exacts, mais la tentative de rafraîchissement pouvait encore
réécrire `data.json` avec un statut dégradé.

## Chiffres de référence revérifiés

| Élément | Résultat | Portée et source |
|---|---:|---|
| Legacy non-commercial | `101 271 - 264 683 = -163 412` | [CFTC Legacy, contrat 097741](https://publicreporting.cftc.gov/resource/6dca-aqww.json), 28 juillet 2026 |
| TFF leveraged funds | `76 752 - 178 742 = -101 990` | [CFTC TFF](https://publicreporting.cftc.gov/resource/gpe5-46if.json), catégorie distincte, 28 juillet 2026 |
| Notionnel net Legacy | `12,7478 Md$` | `163 412 × 12,5 M¥ ÷ 160,2351`; notionnel du futur CME uniquement |
| Extrême Legacy sur 170 semaines | `-184 223` | Même contrat, extrême du 2 juillet 2024 |
| Intensité du short | `88,703 %` | `163 412 ÷ 184 223` |
| USD/JPY BCE | `160,2351` | [BCE](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html), 31 juillet 2026 |
| Cible BoJ | `1,00 %` | [BoJ](https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260731a.pdf), décision du 31 juillet 2026 |
| Milieu de cible Fed | `3,625 %` | FRED `DFEDTARL` et `DFEDTARU`, fourchette `3,50 % à 3,75 %` |
| Différentiel | `2,625 points`, `262,5 pb` | Milieu Fed moins cible BoJ |
| Score `1.0.0` | `54 / 100` | Heuristique inchangée, non présentée comme calibrée |

## Contrats renforcés

### Publication

- `candidate.json` est écrit et validé avant promotion atomique.
- Une source requise en échec ne modifie jamais le dernier `data.json` sain.
- Une tentative sans nouvelle observation laisse `data.json` identique octet pour octet.
- `generated` reste compatible avec l'ancien front et devient l'alias de `published_at`.
- `status.json` porte `checked_at`, résultat, latence, tentatives, volume, statut HTTP, lignes, empreinte et code d'erreur par source.

### Fraîcheur et intégrité

- CFTC Legacy et TFF exigent exactement 170 semaines, le code `097741`, des champs complets, des dates uniques et la semaine attendue par le calendrier officiel 2026.
- BCE exige des paires EUR/USD et EUR/JPY complètes et la dernière journée TARGET attendue après 17 h Europe/Paris.
- FRED exige une date commune récente et une fourchette d'au plus un point.
- BoJ devient automatiquement périmée après la prochaine réunion et une marge de revue de 24 heures.
- Les révisions anciennes sur une zone déjà publiée sont mises en quarantaine; les révisions récentes sont annotées.
- Les valeurs impossibles, sauts FX supérieurs à 15 %, doublons et incohérences avec l'open interest bloquent la promotion.

### Réseau et confidentialité

- Les sources indépendantes sont récupérées en parallèle dans un budget global de 90 secondes.
- Chaque requête est bornée à 15 secondes et 8 Mio, avec trois tentatives maximum.
- Le backoff est exponentiel avec jitter et respecte `Retry-After` pour les erreurs temporaires.
- Les erreurs 4xx déterministes ne sont pas rejouées.
- Les journaux ne contiennent pas les query strings; une clé FRED, Socrata ou Massive ne peut donc pas y être affichée.
- Le navigateur reste same-origin et sans tracker.

### Analyse et interface

- TFF leveraged funds est une série et un sélecteur distincts du Legacy.
- L'appréciation du yen affiche l'opposé de la variation USD/JPY, avec le signe économique correct.
- La formule `1.0.0`, ses poids et son statut non backtesté sont dans le snapshot.
- Massive peut fournir un spot serveur dans `market.json`; il ne remplace jamais la référence BCE et son absence ne dégrade pas `data.json`.
- Le tableau historique statique non relié au snapshot est supprimé; l'interface ne conserve que des chiffres issus du contrat de données ou des explications méthodologiques sourcées.

### Maintenabilité et release

- Le dépôt Git est la seule source. Le bundle plat manuel est supprimé.
- `config/source-calendars.json` est versionné, sourcé et borné par `valid_through`.
- `config/boj-policy.json` est l'unique définition du taux, de la date, de l'URL et de l'empreinte du PDF BoJ pour le builder, le validateur et l'activation.
- La formule et le notionnel du contrat sont définis une seule fois côté Python, incorporés au snapshot et consommés sans valeur de secours silencieuse par le navigateur.
- `tools/release.py` construit une archive à liste blanche, cherche des secrets et vérifie `SOURCE_SHA` plus `SHA256SUMS`.
- Les tests exécutés dans une release extraite désactivent le bytecode puis le manifeste est revérifié, afin qu'aucun `__pycache__` ne puisse atteindre l'activation.
- L'activation sauvegarde et restaure ensemble builder, validateur, calendrier, timer, service, vhost, snapshots et assets.
- `verify_live.py` contrôle désormais `status.json` et, si actif, `market.json` en plus des assets, en-têtes et données.

## Validation reproductible

Le candidat réel du 2 août 2026 a produit:

- Legacy `170` semaines, dernière observation `2026-07-28`, net `-163 412`;
- TFF `170` semaines, dernière observation `2026-07-28`, net `-101 990`;
- BCE `259` références, dernière observation `2026-07-31`, USD/JPY `160,2351`;
- FRED `3,50 % à 3,75 %`, milieu `3,625 %`;
- BoJ `1,00 %`, prochaine revue exigée le `2026-09-18`;
- seconde exécution sans changement: même SHA-256 de `data.json`, nouveau `checked_at` dans `status.json`.

Les commandes de test, création d'artefact, activation, preuve HTTPS et rollback
sont maintenues dans `RUNBOOK.md`.
