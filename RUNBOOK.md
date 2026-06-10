# Déploiement de Carry Yen sur un serveur Debian / Apache 2

Architecture snapshot, privacy-first: un timer systemd régénère un `data.json` local, Apache sert du statique, le navigateur ne contacte aucun tiers et aucune clé n'est exposée.

```
  timer systemd (4x/jour)
        |
        v
  build_snapshot.py  --(HTTPS)-->  CFTC + BCE + FRED (optionnel)
        |
        v
  /var/lib/yct/data.json   (ecriture atomique, hors web root)
        ^
        | Alias /data.json (lecture seule)
  Apache 443  -->  /var/www/html/yct/{index.html,app.css,app.js}
```

On part du dépôt cloné (ou de l'archive extraite). Les fichiers web sont dans `web/`, les fichiers de déploiement dans `deploy/`, le builder et `env.example` à la racine.

---

## 0. Prérequis

- Enregistrement DNS `A`/`AAAA`: `yct.l0g.fr` vers l'IP du serveur.
- Modules Apache: `sudo a2enmod ssl headers rewrite`
- Python 3 présent (le builder n'utilise que la stdlib, aucun pip).

```bash
git clone https://github.com/<compte>/yct.git ~/yct && cd ~/yct
```

---

## 1. Utilisateur système et arborescence

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin yct

sudo mkdir -p /var/lib/yct && sudo chown yct:yct /var/lib/yct && sudo chmod 755 /var/lib/yct

sudo mkdir -p /opt/yct
sudo install -o root -g root -m 0755 build_snapshot.py /opt/yct/build_snapshot.py

sudo mkdir -p /var/www/html/yct
sudo install -o root -g root -m 0644 web/index.html /var/www/html/yct/index.html
sudo install -o root -g root -m 0644 web/app.css   /var/www/html/yct/app.css
sudo install -o root -g root -m 0644 web/app.js    /var/www/html/yct/app.js
```

Note: ne pas copier `web/data.json` en production, c'est l'échantillon de démo. En production, l'URL `/data.json` est servie par un Alias vers `/var/lib/yct/data.json` (étape 6).

---

## 2. Configuration (taux + clés optionnelles)

```bash
sudo mkdir -p /etc/yct
sudo install -o root -g root -m 0600 env.example /etc/yct/env
sudo nano /etc/yct/env     # ajuster BOJ_RATE, et FRED_API_KEY si souhaite
```

Le fichier est en `600 root:root`: systemd le lit en root puis bascule sur `yct`, donc la clé n'est jamais lisible sur disque par l'utilisateur de service.

---

## 3. Première génération manuelle (vérification)

```bash
sudo -u yct OUT_PATH=/var/lib/yct/data.json BOJ_RATE=0.75 /usr/bin/python3 /opt/yct/build_snapshot.py
head -c 400 /var/lib/yct/data.json ; echo
```

Doit afficher un JSON avec `generated`, `rates`, `fx`, `cot`, `spot`, et en logs `CFTC ok ... net=-... oi=...`.

---

## 4. Timer systemd

```bash
sudo install -o root -g root -m 0644 deploy/yct-snapshot.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/yct-snapshot.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now yct-snapshot.timer
sudo systemctl start yct-snapshot.service
sudo journalctl -u yct-snapshot.service -n 20 --no-pager
systemctl list-timers yct-snapshot.timer --no-pager
```

Si le service échoue au démarrage sur une erreur d'appel système (rare), commenter `SystemCallFilter` et `MemoryDenyWriteExecute` dans le `.service`, `daemon-reload`, relancer. Le reste du durcissement peut rester.

---

## 5. Certificat TLS (méthode webroot)

Important: activer d'abord un vhost HTTP qui sert `/.well-known/acme-challenge/` avant de lancer certbot, sinon le challenge retombe sur le vhost par défaut.

```bash
sudo mkdir -p /var/www/html/yct/.well-known/acme-challenge
sudo chown -R www-data:www-data /var/www/html/yct/.well-known

# vhost HTTP provisoire
sudo tee /etc/apache2/sites-available/yct-http.conf >/dev/null <<'EOF'
<VirtualHost *:80>
    ServerName yct.l0g.fr
    DocumentRoot /var/www/html/yct
    <Directory "/var/www/html/yct">
        Options -Indexes
        Require all granted
    </Directory>
</VirtualHost>
EOF
sudo a2ensite yct-http && sudo apache2ctl configtest && sudo systemctl reload apache2

# verif que le challenge passe par le bon vhost
echo ok | sudo tee /var/www/html/yct/.well-known/acme-challenge/ping >/dev/null
curl -s http://yct.l0g.fr/.well-known/acme-challenge/ping   # doit afficher: ok
sudo rm -f /var/www/html/yct/.well-known/acme-challenge/ping

sudo certbot certonly --webroot -w /var/www/html/yct -d yct.l0g.fr
```

---

## 6. Vhost complet (443) et bascule

```bash
sudo a2dissite yct-http && sudo rm /etc/apache2/sites-available/yct-http.conf
sudo install -o root -g root -m 0644 deploy/yct.l0g.fr.conf /etc/apache2/sites-available/yct.l0g.fr.conf
sudo a2ensite yct.l0g.fr
sudo apache2ctl configtest && sudo systemctl reload apache2
```

---

## 7. Vérifications

```bash
curl -sI https://yct.l0g.fr/data.json | grep -iE 'HTTP/|content-type|cache-control'
curl -sI https://yct.l0g.fr/ | grep -iE 'strict-transport|content-security|x-frame|x-content|referrer'
curl -sI http://yct.l0g.fr/  | grep -iE 'HTTP/|location'   # doit montrer un 301 vers https
```

Ouvrir `https://yct.l0g.fr/`: la pastille affiche `snapshot du <date>` et, dans l'onglet Réseau, seul `data.json` est chargé en même origine.

---

## 8. Renouvellement TLS automatique

Le cert ayant été obtenu en `certonly`, ajouter un hook de rechargement (global, idempotent):

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-apache.sh >/dev/null <<'EOF'
#!/bin/sh
systemctl reload apache2
EOF
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-apache.sh
sudo certbot renew --dry-run
```

---

## 9. Entretien

- Décision BoJ: éditer `BOJ_RATE` dans `/etc/yct/env`, puis `sudo systemctl start yct-snapshot.service`.
- Le différentiel Fed se met à jour seul si `FRED_API_KEY` est renseignée.
- Mise à jour de l'app: remplacer les fichiers dans `/var/www/html/yct`.
- Logs du builder: `sudo journalctl -u yct-snapshot.service`.

---

## 10. Sécurité, points clés

- Aucune clé côté navigateur, aucun appel tiers depuis le client, pas de fuite d'IP visiteur.
- CSP `default-src 'none'`, en-têtes HSTS, nosniff, frame DENY, Referrer-Policy no-referrer, COOP, CORP, Permissions-Policy.
- Builder non privilégié et fortement sandboxé, un seul chemin inscriptible, refus des schémas non https, taille de réponse bornée.
- Secret FRED jamais sur disque pour l'utilisateur de service.
- `data.json` servi par un Alias depuis un chemin hors racine web.
