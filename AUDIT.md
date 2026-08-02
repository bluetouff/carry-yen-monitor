# Audit YCT, 2 août 2026

## Verdict

La production actuelle, le bundle plat `/Users/bluetouff/Desktop/DEV/yct-deploy` et les fichiers correspondants du commit GitHub `6e4260cd635c11104d1c87b14e8d5ccb8417cd05` sont alignés. Cet alignement reproduit toutefois quatre défauts: taux BoJ obsolète, historique CFTC tronqué, qualification trop étroite de la catégorie Legacy et fraîcheur globale trompeuse en cas de repli sur cache.

Le candidat corrigé est validé localement avec les flux réels. Il n'est pas encore publié sur GitHub et n'est pas encore déployé sur `yct.l0g.fr`.

## Preuve d'alignement avant correction

| Artefact | SHA-256 identique local et production |
|---|---|
| `index.html` | `f33cda729cca9740a5106025d6d76ff92d0d902d1c4d50b202eafc67b24f9684` |
| `app.js` | `c0569005f1f85d8c98ca6ca9b20cace839dc0f1ea2a63c006a9010938eb56453` |
| `app.css` | `dd24c9376179573e4b29de61418a3ea7ea9f009f8c38df07be5317acb82c3c49` |

Le snapshot public observé à `2026-08-02T04:15:24Z` contenait 119 semaines CFTC, 260 références FX, `BOJ_RATE=0.75` et aucune métadonnée `sources`.

## Chiffres revérifiés

| Élément | Résultat vérifié | Source et portée |
|---|---:|---|
| Net Legacy non-commercial | `101 271 - 264 683 = -163 412` contrats | [CFTC Futures Only, contrat 097741, 28 juillet 2026](https://www.cftc.gov/dea/futures/deacmesf.htm) |
| Notionnel net CME | `12,7478 Md$` | `163 412 × 12,5 M¥ ÷ 160,2351`; notionnel du futur CME, pas taille mondiale du carry |
| Extrême sur 170 semaines | `-184 223` contrats | CFTC Socrata, même contrat 097741, extrême du 2 juillet 2024 |
| Intensité du short | `88,703 %` | `163 412 ÷ 184 223` |
| USD/JPY de référence | `160,2351`, affiché `160,24` | [BCE, 31 juillet 2026](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html): `184,03 JPY/EUR ÷ 1,1485 USD/EUR` |
| Cible BoJ | `1,00 %` | [BoJ, décision du 31 juillet 2026](https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260731a.pdf), après la hausse décidée le 16 juin |
| Fourchette Fed | `3,50 % à 3,75 %` | [Federal Reserve, décision du 29 juillet 2026](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm) |
| Différentiel | `2,625 points`, soit `262,5 pb` | Milieu de cible Fed `3,625 %` moins cible BoJ `1,00 %` |
| Score heuristique | `54 / 100` | Même formule front, avec les taux corrigés et la série CFTC complète |
| TFF leveraged funds | `76 752 - 178 742 = -101 990` contrats | [CFTC TFF Futures Only, 28 juillet 2026](https://www.cftc.gov/dea/futures/financial_lf.htm); série distincte, non affichée par YCT |

Massive Market Data n'était pas disponible dans cette session et aucune clé Massive n'était exposée au processus. La référence FX retenue est donc exclusivement la publication primaire BCE, ce qui correspond au contrat public de YCT et évite de la présenter comme un spot temps réel.

## Corrections du candidat

- La requête CFTC cible directement `cftc_contract_market_code = '097741'` et exige au moins 156 semaines uniques sur trois ans. Le candidat reçoit 170 semaines, du 2 mai 2023 au 28 juillet 2026.
- Frankfurter est supprimé. Le FX provient directement de l'API Data BCE, séries `EXR.D.USD.EUR.SP00.A` et `EXR.D.JPY.EUR.SP00.A`.
- FRED utilise l'API JSON si une clé est configurée, puis l'export CSV officiel sans clé. Les bornes basse et haute doivent partager la même date.
- La BoJ reste une configuration, car sa cible n'est pas fournie par un flux structuré stable utilisé ici. Elle est désormais datée, sourcée et déclarée à revérifier après 70 jours.
- Chaque source expose `status`, `data_as_of`, `last_checked_at`, `last_success_at` et `source_url`.
- Un bloc conservé après échec passe à `cached`, garde son ancien `last_success_at` et force `health.status=degraded`.
- Le front affiche la fraîcheur des quatre sources et ne montre plus une pastille verte pour un snapshot ancien simplement réécrit.
- La terminologie distingue le rapport Legacy « non-commercial » du rapport TFF « leveraged funds ».
- `verify_snapshot.py` bloque un snapshot incomplet ou trop ancien. `verify_live.py` compare les hashes HTTPS au bundle et contrôle les en-têtes de sécurité.
- L'unité systemd exige `/etc/yct/env` et exécute le validateur après le builder.
- Une CI GitHub minimale et dix tests de régression sont prêts dans le checkout de revue.

## Validation effectuée

- `10/10` tests Python réussis dans le bundle et le checkout du dépôt.
- Compilation Python réussie pour le builder et les deux vérificateurs.
- Syntaxe JavaScript réussie pour le front et la variante standalone.
- Snapshot réel candidat: `health=ok`, CFTC `170`, BCE `259`, Fed `3,625 %`, BoJ `1,00 %`.
- Rendu navigateur du candidat: chiffres attendus, quatre badges de source, aucune erreur console.
- Test mobile `390 × 844`: largeur de page `390`, aucun débordement horizontal.

## Garde de mise en production

La production reste inchangée tant que les étapes suivantes ne sont pas explicitement autorisées et réussies:

1. publier la branche et ouvrir la PR brouillon;
2. obtenir une CI verte sur le SHA exact;
3. sauvegarder `/opt/yct`, `/etc/yct`, `/var/www/html/yct`, `/var/lib/yct/data.json` et l'unité systemd;
4. promouvoir le backend, générer et valider le snapshot;
5. promouvoir les assets avec HTML en dernier;
6. exécuter `verify_live.py` depuis le checkout du SHA déployé;
7. contrôler le rendu, la console, les requêtes et le timer.

La procédure détaillée et le rollback sont dans `RUNBOOK.md`.
