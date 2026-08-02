# Déploiement sûr de Carry Yen sur yct.l0g.fr

`yct.l0g.fr` est la référence de production. Le dépôt Git est l'unique source;
un bundle plat local ne doit plus être maintenu à la main. Une release est un
artefact à liste blanche, lié à un SHA Git, accompagné de ses sommes SHA-256 et
sans `.git`, configuration locale ni secret.

```
timer systemd
     |
     v
build_snapshot.py --> CFTC Legacy + CFTC TFF + BCE + FRED
     |                     |
     |                     +-- BoJ: valeur revue contre le calendrier officiel
     |                     +-- Massive optionnel vers market.json
     v
candidate.json --> verify_snapshot.py --> data.json, promotion atomique
     |
     +--> status.json, résultat de chaque tentative

Apache 443 --> application statique + data.json + status.json + market.json optionnel
```

## 1. Contrats à préserver

- URLs: `https://yct.l0g.fr/`, `/data.json`, `/status.json` et `/market.json` si Massive est configuré.
- Le navigateur ne contacte que `yct.l0g.fr`: aucun script, pixel, police, image ou appel automatique tiers.
- `data.json` est toujours le dernier snapshot intégralement sain. Une panne ne le modifie jamais.
- `status.json` expose le dernier contrôle, avec codes d'erreur normalisés, sans URL signée ni message brut pouvant contenir un secret.
- CFTC Legacy et TFF utilisent exclusivement le contrat CME JPY `097741`, exactement 170 semaines et deux catégories jamais fusionnées.
- USD/JPY canonique est la référence quotidienne BCE. `market.json` est un spot Massive optionnel, séparé et horodaté.
- La formule de risque `1.0.0` reste explicitement heuristique. Toute nouvelle pondération exige un backtest documenté.
- Les calendriers CFTC et BoJ versionnés dans `config/source-calendars.json` doivent être renouvelés avant `valid_through`.
- Le taux, la date, l'URL et l'empreinte SHA-256 du PDF BoJ n'existent que dans `config/boj-policy.json`. Le builder, le validateur et l'activation lisent ce même contrat.

## 2. Préflight local

Depuis un checkout propre du commit candidat:

```bash
python3 -m py_compile build_snapshot.py yct_quality.py verify_snapshot.py verify_live.py gen_sample.py tools/release.py tests/test_yct.py
python3 -m unittest discover -s tests -v
node --check web/app.js
bash -n deploy/activate-release.sh
```

Générer un candidat réel hors production:

```bash
mkdir -p /private/tmp/yct-candidate
OUT_PATH=/private/tmp/yct-candidate/data.json \
STATUS_PATH=/private/tmp/yct-candidate/status.json \
CANDIDATE_PATH=/private/tmp/yct-candidate/candidate.json \
MARKET_PATH=/private/tmp/yct-candidate/market.json \
SOURCE_CALENDAR_PATH="$PWD/config/source-calendars.json" \
BOJ_POLICY_PATH="$PWD/config/boj-policy.json" \
python3 build_snapshot.py

python3 verify_snapshot.py /private/tmp/yct-candidate/data.json \
  --status-path /private/tmp/yct-candidate/status.json \
  --calendar-path config/source-calendars.json \
  --boj-policy-path config/boj-policy.json
```

Rejouer une seconde fois le builder et vérifier que le SHA-256 de `data.json`
ne change pas quand les observations sont identiques. Seul `status.json` doit
recevoir un nouveau `checked_at` et `reason=unchanged`.

## 3. Commit, CI et artefact exact

Après commit et push:

```bash
git status --short
git rev-parse HEAD
gh run list --branch main --limit 5
```

Attendre la réussite du workflow du SHA exact. Construire ensuite l'artefact:

```bash
SHA=$(git rev-parse HEAD)
python3 tools/release.py build --output dist
python3 tools/release.py verify-archive \
  --archive "dist/yct-release-${SHA}.tar.gz" \
  --expected-sha "$SHA"
```

Le constructeur refuse un checkout sale, copie uniquement `SOURCE_FILES`,
recherche plusieurs formes de secrets et produit `SOURCE_SHA` plus
`SHA256SUMS`. Aucun fichier `.env`, certificat, clé ou jeton n'entre dans
l'archive.

## 4. Transfert non privilégié

Transférer uniquement `yct-release-<SHA>.tar.gz` vers `/home/bluetouff` avec la
configuration SSH habituelle. Ne jamais transférer `/etc/yct/env` depuis ou vers
le poste de travail.

Sur le serveur, en utilisateur `bluetouff`:

```bash
cd /home/bluetouff
tar -xzf "yct-release-<SHA>.tar.gz"
python3 "/home/bluetouff/yct-release-<SHA>/tools/release.py" verify-dir \
  --directory "/home/bluetouff/yct-release-<SHA>" \
  --expected-sha "<SHA>"
python3 -m unittest discover \
  -s "/home/bluetouff/yct-release-<SHA>/tests" -v
```

Ces commandes ne nécessitent ni `sudo` ni lecture des secrets de production.

## 5. Activation transactionnelle

L'utilisateur exécute lui-même l'unique commande privilégiée:

```bash
sudo EXPECTED_SHA=<SHA> \
SOURCE_DIR=/home/bluetouff/yct-release-<SHA> \
/home/bluetouff/yct-release-<SHA>/deploy/activate-release.sh
```

Le script:

1. vérifie le SHA et toutes les sommes de l'artefact, ou le checkout Git propre;
2. sauvegarde backend, calendrier, politique BoJ, unités, configuration, snapshots, vhost et assets;
3. conserve les secrets de `/etc/yct/env` en `0600 root:root`, retire les trois anciennes variables BoJ désormais versionnées et n'affiche jamais le fichier;
4. installe le backend et les deux contrats de source avant le front;
5. génère, valide et promeut le snapshot;
6. installe les assets avec HTML en dernier;
7. teste Apache avant reload;
8. réactive le timer;
9. restaure automatiquement la sauvegarde au premier échec.

La ligne finale `OK` fournit le chemin de sauvegarde. Son SHA doit être celui du
workflow GitHub et de l'artefact transféré.

## 6. Preuve post-déploiement

Depuis le checkout exact:

```bash
python3 verify_live.py --base-url https://yct.l0g.fr/ --bundle-dir .
```

Le vérificateur compare les trois assets au bundle, contrôle CSP, HSTS et cache,
valide `data.json` contre les calendriers, puis recoupe `status.json`. Si Massive
est déclaré frais, il valide aussi `market.json`.

Sur le serveur, sans privilège:

```bash
cat /opt/yct/SOURCE_SHA
systemctl status yct-snapshot.service --no-pager
systemctl list-timers yct-snapshot.timer --no-pager
journalctl -u yct-snapshot.service -n 50 --no-pager
```

Contrôles navigateur:

- référence BCE, différentiel Fed-BoJ, Legacy et TFF correspondent au JSON public;
- le sélecteur Legacy/TFF change uniquement le graphique de positionnement;
- l'appréciation du yen porte le signe économique correct;
- cinq badges de source sont visibles;
- le spot Massive n'apparaît que s'il est configuré et frais;
- aucune erreur console et aucune requête vers un domaine tiers.

## 7. Rollback

L'activation restaure automatiquement au premier échec. Pour un incident après
une activation réussie, utiliser le chemin de sauvegarde annoncé par le script,
restaurer toutes les surfaces ensemble, puis relancer service, timer et contrôle
Apache. Ne pas restaurer seulement le front ou seulement `data.json`: schéma,
builder, calendrier et vhost forment un contrat unique.

## 8. Installation initiale

Pour une nouvelle machine uniquement:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin yct
sudo install -d -o yct -g yct -m 0755 /var/lib/yct
sudo install -d -o root -g root -m 0755 /opt/yct /etc/yct /var/www/html/yct
sudo install -o root -g root -m 0755 build_snapshot.py yct_quality.py verify_snapshot.py /opt/yct/
sudo install -o root -g root -m 0644 config/boj-policy.json /opt/yct/boj-policy.json
sudo install -o root -g root -m 0644 config/source-calendars.json /opt/yct/source-calendars.json
sudo install -o root -g root -m 0600 env.example /etc/yct/env
sudo install -o root -g root -m 0644 deploy/yct-snapshot.service deploy/yct-snapshot.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 web/index.html web/app.css web/app.js /var/www/html/yct/
sudo systemctl daemon-reload
sudo systemctl enable --now yct-snapshot.timer
sudo systemctl start yct-snapshot.service
```

Installer ensuite le vhost, exécuter `apache2ctl configtest`, puis seulement
recharger Apache.

## 9. Maintenance des sources

- CFTC: renouveler la liste annuelle depuis l'URL officielle inscrite dans le JSON. La CI doit tester une publication ordinaire et chaque report de jour férié.
- BoJ: renouveler les fins de réunions puis, après chaque réunion même en cas de statu quo, mettre à jour uniquement `config/boj-policy.json`, y compris l'empreinte SHA-256 du PDF. L'index officiel bloque toute nouvelle décision non revue et le builder vérifie les octets du document.
- BCE: TARGET2 est calculé, y compris Vendredi saint, lundi de Pâques, 1er mai, 25 et 26 décembre.
- FRED: API JSON si une clé est présente, sinon CSV officiel; aucune valeur de repli n'est publiée en cas de panne.
- Massive: `MASSIVE_API_KEY` est optionnelle et exclusivement serveur. Une panne Massive ne bloque pas le snapshot BCE.
- Alertes: `status.json` et l'état systemd sont la base d'un futur moniteur. Aucun service d'alerte externe n'est activé sans choix explicite de destination et de politique de confidentialité.
