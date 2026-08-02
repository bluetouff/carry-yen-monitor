# Déploiement sûr de Carry Yen sur yct.l0g.fr

`yct.l0g.fr` reste la référence de production. La page est statique et privacy-first: le navigateur ne contacte que le domaine YCT. Un service non privilégié prépare puis valide `data.json` à partir de sources primaires.

```
systemd timer
     |
     v
build_snapshot.py --> CFTC Socrata + API Data BCE + FRED
     |                         |
     |                         +-- BoJ: valeur configurée, datée et sourcée
     v
verify_snapshot.py --> /var/lib/yct/data.json (écriture atomique)
                              |
Apache 443 -------------------+--> navigateur, même origine uniquement
```

Le dépôt source place les assets dans `web/` et les unités dans `deploy/`. Le bundle plat `yct-deploy` contient les mêmes fichiers à sa racine; dans ce cas, retirer les préfixes `web/` et `deploy/`, et utiliser `yct.env.example` à la place de `env.example`.

## 1. Contrats à ne pas casser

- URL publique inchangée: `https://yct.l0g.fr/` et `https://yct.l0g.fr/data.json`.
- Aucun script, pixel, police, image ou appel automatique tiers dans le navigateur.
- `data.json` reste compatible avec l'ancien front (`generated`, `rates`, `fx`, `cot`, `spot`) et ajoute `schema_version`, `sources` et `health`.
- CFTC: rapport Legacy Futures Only, seul contrat CME JPY `097741`, au moins 156 semaines uniques.
- FX: référence BCE quotidienne, USD/JPY dérivé de EUR/JPY divisé par EUR/USD; ce n'est pas un spot temps réel.
- Une source conservée en cache garde son ancien `last_success_at` et force `health.status=degraded`.

## 2. Préflight sur le poste de travail

Depuis un checkout propre du commit à déployer:

```bash
python3 -m py_compile build_snapshot.py verify_snapshot.py verify_live.py tests/test_yct.py
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Générer un candidat réel hors production:

```bash
mkdir -p /private/tmp/yct-candidate
OUT_PATH=/private/tmp/yct-candidate/data.json \
BOJ_RATE=1.00 BOJ_RATE_AS_OF=2026-07-31 \
FED_RATE=3.625 FED_RATE_AS_OF=2026-07-29 \
python3 build_snapshot.py
python3 verify_snapshot.py /private/tmp/yct-candidate/data.json
```

Le résultat attendu est `health=ok`, 170 semaines CFTC, une dernière date CFTC récente, au moins 240 références BCE et les quatre sources renseignées. Un code retour non nul bloque le déploiement.

Noter le SHA exact et vérifier l'état GitHub Actions avant transfert:

```bash
git rev-parse HEAD
git status --short
gh run list --branch main --limit 5
```

## 3. Transfert non privilégié

Créer sur le serveur un répertoire de release appartenant à `bluetouff`, par exemple `/home/bluetouff/yct-release-<SHA>`, puis y transférer le checkout ou une archive du SHA contrôlé. Ne jamais envoyer de secret: `/etc/yct/env` reste uniquement sur le serveur.

Sur le serveur, vérifier avant toute installation:

```bash
cd /home/bluetouff/yct-release-<SHA>
git rev-parse HEAD
git status --short
python3 -m unittest discover -s tests -v
```

Le SHA doit être strictement identique à celui validé sur le poste de travail.

## 4. Sauvegarde obligatoire de la production

Créer un répertoire horodaté explicite sous `/var/backups/yct/`, puis sauvegarder les quatre surfaces avant mutation:

```bash
sudo mkdir -p /var/backups/yct/2026-08-02T0700Z
sudo cp -a /opt/yct /var/backups/yct/2026-08-02T0700Z/opt-yct
sudo cp -a /etc/yct /var/backups/yct/2026-08-02T0700Z/etc-yct
sudo cp -a /var/www/html/yct /var/backups/yct/2026-08-02T0700Z/web-yct
sudo cp -a /var/lib/yct/data.json /var/backups/yct/2026-08-02T0700Z/data.json
sudo cp -a /etc/systemd/system/yct-snapshot.service /var/backups/yct/2026-08-02T0700Z/yct-snapshot.service
```

Conserver ce chemin: il est le point de rollback. Vérifier qu'il contient bien les fichiers avant de poursuivre.

## 5. Promotion backend avant le front

L'ancien front sait lire le nouveau schéma. On met donc d'abord à niveau le builder et sa validation, sans toucher aux assets web.

```bash
sudo install -o root -g root -m 0755 build_snapshot.py /opt/yct/build_snapshot.py
sudo install -o root -g root -m 0755 verify_snapshot.py /opt/yct/verify_snapshot.py
sudo install -o root -g root -m 0644 deploy/yct-snapshot.service /etc/systemd/system/yct-snapshot.service
sudo systemctl daemon-reload
```

Mettre à jour `/etc/yct/env` avec `sudoedit` sans remplacer les secrets existants:

```text
BOJ_RATE=1.00
BOJ_RATE_AS_OF=2026-07-31
BOJ_SOURCE_URL=https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260731a.pdf
FED_RATE=3.625
FED_RATE_AS_OF=2026-07-29
FED_RATE_SOURCE_URL=https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm
```

Après chaque réunion BoJ ou FOMC, actualiser le taux, la date et l'URL officielle même si la décision maintient le niveau. L'unité exige désormais la présence de `/etc/yct/env`.

Lancer une génération complète. `ExecStartPost` valide automatiquement le fichier écrit:

```bash
sudo systemctl start yct-snapshot.service
systemctl status yct-snapshot.service --no-pager
sudo journalctl -u yct-snapshot.service -n 50 --no-pager
sudo -u yct /usr/bin/python3 /opt/yct/verify_snapshot.py /var/lib/yct/data.json
```

Ne pas promouvoir le front si le service ou le vérificateur n'est pas vert.

## 6. Promotion web avec HTML en dernier

Préparer les trois fichiers dans le répertoire final, puis renommer CSS et JavaScript avant HTML. Chaque `mv` est atomique sur le même système de fichiers, et l'ancien front reste compatible pendant la courte transition.

```bash
sudo install -o root -g root -m 0644 web/app.css /var/www/html/yct/app.css.next
sudo install -o root -g root -m 0644 web/app.js /var/www/html/yct/app.js.next
sudo install -o root -g root -m 0644 web/index.html /var/www/html/yct/index.html.next
sudo mv /var/www/html/yct/app.css.next /var/www/html/yct/app.css
sudo mv /var/www/html/yct/app.js.next /var/www/html/yct/app.js
sudo mv /var/www/html/yct/index.html.next /var/www/html/yct/index.html
```

Apache ne nécessite pas de reload pour ces fichiers statiques.

## 7. Preuve post-déploiement

Depuis le checkout exact utilisé pour la release:

```bash
python3 verify_live.py --base-url https://yct.l0g.fr/ --bundle-dir .
```

Le script échoue si un des trois assets HTTPS diffère du bundle, si la CSP ou HSTS régresse, si le cache de `data.json` change, si le snapshot est dégradé ou si les dates deviennent trop anciennes.

Contrôles manuels complémentaires:

```bash
curl -sI https://yct.l0g.fr/data.json | grep -iE 'HTTP/|content-type|cache-control'
curl -sI https://yct.l0g.fr/ | grep -iE 'strict-transport|content-security|x-frame|x-content|referrer'
systemctl list-timers yct-snapshot.timer --no-pager
```

Dans un vrai navigateur, vérifier:

- `USD/JPY 160,24` pour la référence BCE du 31 juillet 2026;
- différentiel `2,625 pts` et `262,5 pb`;
- net CFTC `-163 412` au 28 juillet et historique débutant en mai 2023 environ;
- `89 % de l'extrême`, notionnel CME `≈ 12,7 Md$`, score `54`;
- quatre badges de source, aucun badge en cache;
- aucune erreur console et aucune requête vers un domaine tiers.

## 8. Rollback

Si une vérification échoue, restaurer le répertoire de sauvegarde choisi à l'étape 4, puis relancer le service et revalider. Exemple avec le chemin ci-dessus:

```bash
sudo cp -a /var/backups/yct/2026-08-02T0700Z/opt-yct/. /opt/yct/
sudo cp -a /var/backups/yct/2026-08-02T0700Z/etc-yct/. /etc/yct/
sudo cp -a /var/backups/yct/2026-08-02T0700Z/web-yct/. /var/www/html/yct/
sudo cp -a /var/backups/yct/2026-08-02T0700Z/data.json /var/lib/yct/data.json
sudo cp -a /var/backups/yct/2026-08-02T0700Z/yct-snapshot.service /etc/systemd/system/yct-snapshot.service
sudo systemctl daemon-reload
sudo systemctl start yct-snapshot.service
```

Le rollback est terminé seulement après vérification de la page HTTPS, de `data.json` et du timer.

## 9. Installation initiale

Pour une nouvelle machine uniquement:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin yct
sudo mkdir -p /var/lib/yct /opt/yct /etc/yct /var/www/html/yct
sudo chown yct:yct /var/lib/yct
sudo chmod 755 /var/lib/yct
sudo install -o root -g root -m 0755 build_snapshot.py verify_snapshot.py verify_live.py /opt/yct/
sudo install -o root -g root -m 0600 env.example /etc/yct/env
sudo install -o root -g root -m 0644 deploy/yct-snapshot.service deploy/yct-snapshot.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 web/index.html web/app.css web/app.js /var/www/html/yct/
sudo systemctl daemon-reload
sudo systemctl enable --now yct-snapshot.timer
sudo systemctl start yct-snapshot.service
```

Configurer ensuite le vhost `deploy/yct.l0g.fr.conf`, tester avec `apache2ctl configtest`, obtenir ou renouveler le certificat Let's Encrypt et ne recharger Apache qu'après succès du configtest.

## 10. Sécurité et maintenance

- Pas de clé côté navigateur; FRED fonctionne aussi via son export CSV officiel sans clé.
- `EnvironmentFile=/etc/yct/env` est obligatoire et reste en `0600 root:root`.
- Le builder refuse HTTP, borne chaque réponse à 8 Mio et écrit atomiquement.
- Le service tourne sous `yct`, sans privilèges, avec `/var/lib/yct` comme seul chemin inscriptible.
- Le timer couvre les publications CFTC et BCE; les seuils de fraîcheur sont fondés sur la cadence des sources, pas sur l'heure de passage du timer.
- Toute mise à jour est bloquée avant le front si la source, le snapshot ou le validateur échoue.
