# Carry Yen (YCT)

Langue: **Français** · [English](README.md)

Un tableau de bord web qui surveille le carry trade sur le yen et répond, en un coup d'oeil, à une question simple: les acteurs non commerciaux suivis par la CFTC parient-ils toujours contre le yen, et le risque d'un retournement brutal monte-t-il?

Démo en ligne: https://yct.l0g.fr

![Aperçu du tableau de bord](docs/preview.png)

## En bref, pour comprendre sans être expert

Le carry trade, c'est emprunter dans une monnaie où les taux sont très bas (le yen japonais, proche de 0) pour placer dans une monnaie où ils rapportent davantage (le dollar). Tant que le yen reste faible, on encaisse la différence de taux, mois après mois. C'est une machine à cash très populaire chez les grands fonds.

Le piège: cette différence s'encaisse à crédit et avec du levier. Si le yen se met soudain à monter, tout le monde veut rembourser en même temps, le yen grimpe encore plus vite, et les positions sautent en cascade. C'est ce qui s'est produit en août 2024: en trois semaines le yen a violemment repris de la valeur et les bourses ont dévissé. On appelle ça un débouclage.

Ce tableau de bord mesure trois choses pour estimer où on en est:

- À quel point le pari contre le yen est rentable aujourd'hui (l'écart de taux entre les États-Unis et le Japon).
- Quelle est l'ampleur de la position de la catégorie large « non-commercial » du rapport Legacy, et si elle est déjà extrême.
- Si le yen commence à se renforcer, premier signe d'un possible débouclage.

Il en tire un indicateur de risque de 0 à 100 et un verdict en clair. Ce n'est pas un conseil d'investissement, c'est un thermomètre.

## Comment lire le tableau de bord

- USD/JPY: le prix du dollar en yens. Quand il monte, le yen s'affaiblit, le carry trade fonctionne. Quand il baisse, le yen se renforce, attention.
- Différentiel Fed contre BoJ: l'écart de taux. Plus il est large, plus le pari rapporte.
- Net non-commercial JPY: longs moins shorts dans la catégorie large du rapport CFTC Legacy. Ce n'est pas la catégorie plus étroite « leveraged funds » du rapport TFF.
- TFF leveraged funds: seconde lecture du même contrat CME, affichée séparément et jamais fusionnée avec le Legacy.
- Risque de débouclage: la synthèse. Faible, Modéré, Élevé ou Critique.
- La jauge carry contre débouclage: l'aiguille à gauche, le carry domine, tranquille. À droite, le terrain devient dangereux.

## D'où viennent les chiffres

Tout provient de sources officielles, publiques et gratuites. On préfère toujours la source d'origine à un site qui la recopie.

| Donnée | Source officielle |
|---|---|
| Qui parie contre le yen | CFTC, le régulateur américain des marchés à terme (rapport hebdomadaire des positions) |
| Position des leveraged funds | CFTC, rapport TFF Futures Only, série distincte |
| Prix USD/JPY | Banque centrale européenne (taux de référence quotidiens) |
| Taux directeur américain | Réserve fédérale via la base FRED |
| Taux directeur japonais | Configuration datée et vérifiée après chaque décision de la Banque du Japon |

## Confiance et confidentialité

Le tableau de bord a été pensé pour être sobre et respectueux:

- Aucune publicité, aucun pisteur, aucun cookie.
- Votre navigateur ne contacte aucun service tiers. Le serveur prépare les JSON de données et d'état, puis le navigateur les lit sur le domaine YCT. Les fournisseurs de données ne voient donc jamais votre adresse IP.
- Aucune clé ni aucun secret n'est exposé.
- Tout le code est ouvert, vous pouvez le lire et le vérifier.

## Architecture, pour les techniciens

Conception transactionnelle. Un job contrôle les sources plusieurs fois par jour. Il ne remplace `data.json` qu'après validation complète; `status.json` décrit chaque tentative et `market.json` reçoit, si elle est configurée, une cotation Massive séparée.

```
  timer systemd (4 fois par jour)
        |
        v
  build_snapshot.py  --(HTTPS)-->  CFTC Legacy/TFF + API Data BCE + FRED
        |
        +--> candidate.json --> validation --> data.json
        +--> status.json
        +--> market.json (Massive optionnel)
        ^
        | Alias JSON en lecture seule
  Apache 443  -->  web/index.html + app.css + app.js
        ^
        | HTTPS, CSP default-src 'none'
     visiteur (ne lit que le domaine YCT)
```

Si une source requise tombe, `data.json` ne change pas. La page conserve le dernier snapshot intégralement sain et montre l'échec du dernier contrôle à partir de `status.json`. Si les observations sont identiques, `data.json` reste identique octet pour octet.

### Précision sur la donnée CFTC

Les requêtes Legacy et TFF ciblent directement le code CFTC `097741`, le futur yen standard du CME. Chacune exige exactement 170 observations hebdomadaires complètes et la semaine attendue d'après le calendrier officiel CFTC. Les catégories sont conservées séparément.

### Indicateur de risque, la formule

Score de 0 (carry dominant) à 100 (débouclage), recalculé dans le navigateur:

- Surcharge des shorts, poids 0,45: position nette courante rapportée à son extrême sur environ trois ans.
- Appréciation du yen sur quatre semaines, poids 0,35: un yen qui se renforce augmente le risque.
- Compression de l'écart de taux, poids 0,20: plus l'écart se réduit, plus le coussin est mince.

Bandes: Faible (moins de 30), Modéré (30 à 55), Élevé (55 à 78), Critique (78 et plus). Le notionnel est estimé par contrats nets multipliés par 12 500 000 yens, converti avec la référence BCE. La formule est versionnée `1.0.0` et qualifiée d'heuristique non backtestée.

## Contenu du dépôt

```
web/index.html web/app.css web/app.js   application web servie par Apache
web/data.json                            echantillon de demonstration (synthetique)
build_snapshot.py                        builder du snapshot, Python stdlib uniquement
yct_quality.py                           calendriers et contrats de qualité partagés
config/source-calendars.json             calendriers officiels CFTC et BoJ versionnés
config/boj-policy.json                   taux, date et décision BoJ, contrat public unique
verify_snapshot.py                       valide schema, grain et fraicheur
verify_live.py                           compare les artefacts HTTPS au bundle local
gen_sample.py                            regenere l'echantillon de demo
env.example                              modele des seules options locales (sans secret)
deploy/yct-snapshot.service              unite systemd durcie
deploy/yct-snapshot.timer                declencheur planifie
deploy/yct.l0g.fr.conf                   exemple de vhost Apache durci
tools/release.py                         artefact exact, checksums et détection de secrets
RUNBOOK.md                               procedure de deploiement detaillee
tests/test_yct.py                         tests de regression des contrats de source
.github/workflows/test.yml               CI minimale des contrats
```

## Essayer en local, sans serveur

```bash
python3 gen_sample.py          # genere web/data.json (echantillon)
cd web && python3 -m http.server 8000
# puis ouvrir http://localhost:8000
```

La version publique garde toutes les requêtes vers les fournisseurs côté serveur. Il n'existe pas de variante qui contacte automatiquement ces services depuis le navigateur.

## Déploiement en production

Procédure complète pas à pas dans [RUNBOOK.md](RUNBOOK.md). En résumé: un utilisateur système dédié, le builder sous timer systemd durci, un vhost Apache statique avec CSP stricte et certificat Let's Encrypt. Prérequis: Debian, Apache 2 (ssl, headers, rewrite), Python 3, un enregistrement DNS.

## Avertissement

Outil d'information et d'analyse. Rien ici n'est un conseil en investissement, financier ou fiscal. Le carry trade comporte un risque de perte, amplifié par le levier et par le change. Faites vos propres recherches.

## Licence

MIT. Voir [LICENSE](LICENSE).
