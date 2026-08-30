#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_SHA=${EXPECTED_SHA:?EXPECTED_SHA est obligatoire}
SOURCE_DIR=${SOURCE_DIR:?SOURCE_DIR est obligatoire}

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "[yct-release] EXPECTED_SHA invalide" >&2
  exit 2
}
[[ "$SOURCE_DIR" == "/home/bluetouff/yct-release-$EXPECTED_SHA" && ! -L "$SOURCE_DIR" ]] || {
  echo "[yct-release] SOURCE_DIR refuse: $SOURCE_DIR" >&2
  exit 2
}

if [[ $(id -u) -ne 0 ]]; then
  echo "[yct-release] cette activation doit etre lancee avec sudo" >&2
  exit 2
fi

for command_name in install cp mv awk cmp mktemp rmdir systemctl python3; do
  command -v "$command_name" >/dev/null || {
    echo "[yct-release] commande absente: $command_name" >&2
    exit 2
  }
done
[[ -x /usr/sbin/apache2ctl ]] || {
  echo "[yct-release] commande absente: /usr/sbin/apache2ctl" >&2
  exit 2
}

for required_path in \
  "$SOURCE_DIR/build_snapshot.py" \
  "$SOURCE_DIR/yct_quality.py" \
  "$SOURCE_DIR/verify_snapshot.py" \
  "$SOURCE_DIR/config/boj-policy.json" \
  "$SOURCE_DIR/config/source-calendars.json" \
  "$SOURCE_DIR/tools/release.py" \
  "$SOURCE_DIR/deploy/yct-snapshot.service" \
  "$SOURCE_DIR/deploy/yct-snapshot.timer" \
  "$SOURCE_DIR/deploy/yct.l0g.fr.conf" \
  "$SOURCE_DIR/web/index.html" \
  "$SOURCE_DIR/web/en/index.html" \
  "$SOURCE_DIR/web/app.css" \
  "$SOURCE_DIR/web/app.js" \
  /opt/yct/build_snapshot.py \
  /etc/yct/env \
  /etc/systemd/system/yct-snapshot.service \
  /etc/systemd/system/yct-snapshot.timer \
  /etc/apache2/sites-available/yct.l0g.fr.conf \
  /var/lib/yct/data.json \
  /var/www/html/yct/index.html \
  /var/www/html/yct/app.css \
  /var/www/html/yct/app.js; do
  [[ -f "$required_path" ]] || {
    echo "[yct-release] fichier requis absent: $required_path" >&2
    exit 2
  }
done

if [[ -d "$SOURCE_DIR/.git" ]]; then
  command -v git >/dev/null || {
    echo "[yct-release] git absent pour verifier le checkout" >&2
    exit 2
  }
  actual_sha=$(git -C "$SOURCE_DIR" rev-parse HEAD)
  [[ "$actual_sha" == "$EXPECTED_SHA" ]] || {
    echo "[yct-release] SHA inattendu: $actual_sha" >&2
    exit 2
  }
  [[ -z $(git -C "$SOURCE_DIR" status --porcelain) ]] || {
    echo "[yct-release] checkout de release modifie" >&2
    exit 2
  }
else
  python3 "$SOURCE_DIR/tools/release.py" verify-dir \
    --directory "$SOURCE_DIR" --expected-sha "$EXPECTED_SHA"
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
short_sha=${EXPECTED_SHA:0:12}
BACKUP_DIR="/var/backups/yct/${timestamp}-${short_sha}"
install -d -o root -g root -m 0700 "$BACKUP_DIR"

backup_optional() {
  source_path=$1
  backup_name=$2
  if [[ -f "$source_path" ]]; then
    cp -a "$source_path" "$BACKUP_DIR/$backup_name"
    touch "$BACKUP_DIR/.had-$backup_name"
  fi
}

restore_optional() {
  backup_name=$1
  destination=$2
  owner=$3
  group=$4
  mode=$5
  if [[ -f "$BACKUP_DIR/.had-$backup_name" ]]; then
    install -o "$owner" -g "$group" -m "$mode" "$BACKUP_DIR/$backup_name" "$destination"
  else
    rm -f "$destination"
  fi
}

cp -a /opt/yct/build_snapshot.py "$BACKUP_DIR/build_snapshot.py"
cp -a /etc/yct/env "$BACKUP_DIR/env"
cp -a /etc/systemd/system/yct-snapshot.service "$BACKUP_DIR/yct-snapshot.service"
cp -a /etc/systemd/system/yct-snapshot.timer "$BACKUP_DIR/yct-snapshot.timer"
cp -a /etc/apache2/sites-available/yct.l0g.fr.conf "$BACKUP_DIR/yct.l0g.fr.conf"
cp -a /var/lib/yct/data.json "$BACKUP_DIR/data.json"
cp -a /var/www/html/yct/index.html "$BACKUP_DIR/index.html"
cp -a /var/www/html/yct/app.css "$BACKUP_DIR/app.css"
cp -a /var/www/html/yct/app.js "$BACKUP_DIR/app.js"
backup_optional /opt/yct/yct_quality.py yct_quality.py
backup_optional /opt/yct/verify_snapshot.py verify_snapshot.py
backup_optional /opt/yct/boj-policy.json boj-policy.json
backup_optional /opt/yct/source-calendars.json source-calendars.json
backup_optional /opt/yct/SOURCE_SHA SOURCE_SHA
backup_optional /var/lib/yct/status.json status.json
backup_optional /var/lib/yct/market.json market.json
backup_optional /var/www/html/yct/en/index.html en-index.html

ENV_TMP=""
SHA_TMP=""

rollback() {
  exit_code=$?
  trap - ERR
  set +e
  echo "[yct-release] echec, restauration depuis $BACKUP_DIR" >&2
  systemctl stop yct-snapshot.timer
  install -o root -g root -m 0755 "$BACKUP_DIR/build_snapshot.py" /opt/yct/build_snapshot.py
  install -o root -g root -m 0600 "$BACKUP_DIR/env" /etc/yct/env
  install -o root -g root -m 0644 "$BACKUP_DIR/yct-snapshot.service" /etc/systemd/system/yct-snapshot.service
  install -o root -g root -m 0644 "$BACKUP_DIR/yct-snapshot.timer" /etc/systemd/system/yct-snapshot.timer
  install -o root -g root -m 0644 "$BACKUP_DIR/yct.l0g.fr.conf" /etc/apache2/sites-available/yct.l0g.fr.conf
  install -o yct -g yct -m 0644 "$BACKUP_DIR/data.json" /var/lib/yct/data.json
  install -o root -g root -m 0644 "$BACKUP_DIR/index.html" /var/www/html/yct/index.html
  install -o root -g root -m 0644 "$BACKUP_DIR/app.css" /var/www/html/yct/app.css
  install -o root -g root -m 0644 "$BACKUP_DIR/app.js" /var/www/html/yct/app.js
  restore_optional yct_quality.py /opt/yct/yct_quality.py root root 0755
  restore_optional verify_snapshot.py /opt/yct/verify_snapshot.py root root 0755
  restore_optional boj-policy.json /opt/yct/boj-policy.json root root 0644
  restore_optional source-calendars.json /opt/yct/source-calendars.json root root 0644
  restore_optional SOURCE_SHA /opt/yct/SOURCE_SHA root root 0644
  restore_optional status.json /var/lib/yct/status.json yct yct 0644
  restore_optional market.json /var/lib/yct/market.json yct yct 0644
  install -d -o root -g root -m 0755 /var/www/html/yct/en
  restore_optional en-index.html /var/www/html/yct/en/index.html root root 0644
  if [[ ! -f "$BACKUP_DIR/.had-en-index.html" ]]; then
    rmdir /var/www/html/yct/en 2>/dev/null || true
  fi
  rm -f /var/lib/yct/candidate.json
  rm -f /var/www/html/yct/index.html.next /var/www/html/yct/app.css.next /var/www/html/yct/app.js.next \
    /var/www/html/yct/en/index.html.next
  [[ -z "$ENV_TMP" ]] || rm -f "$ENV_TMP"
  [[ -z "$SHA_TMP" ]] || rm -f "$SHA_TMP"
  systemctl daemon-reload
  systemctl start yct-snapshot.service
  systemctl start yct-snapshot.timer
  /usr/sbin/apache2ctl configtest && systemctl reload apache2
  echo "[yct-release] rollback termine; sauvegarde conservee: $BACKUP_DIR" >&2
  exit "$exit_code"
}
trap rollback ERR

systemctl stop yct-snapshot.timer

# Migration unique vers le contrat BoJ versionne. Les anciennes variables
# publiques sont retirees sans afficher ni modifier les secrets et options.
ENV_TMP=$(mktemp /etc/yct/env.yct-release.XXXXXX)
awk -F= '$1 != "BOJ_RATE" && $1 != "BOJ_RATE_AS_OF" && $1 != "BOJ_SOURCE_URL" { print }' \
  /etc/yct/env >"$ENV_TMP"
if ! cmp -s /etc/yct/env "$ENV_TMP"; then
  install -o root -g root -m 0600 "$ENV_TMP" /etc/yct/env
fi
rm -f "$ENV_TMP"
ENV_TMP=""

install -o root -g root -m 0755 "$SOURCE_DIR/build_snapshot.py" /opt/yct/build_snapshot.py
install -o root -g root -m 0755 "$SOURCE_DIR/yct_quality.py" /opt/yct/yct_quality.py
install -o root -g root -m 0755 "$SOURCE_DIR/verify_snapshot.py" /opt/yct/verify_snapshot.py
install -o root -g root -m 0644 "$SOURCE_DIR/config/boj-policy.json" /opt/yct/boj-policy.json
install -o root -g root -m 0644 "$SOURCE_DIR/config/source-calendars.json" /opt/yct/source-calendars.json
install -o root -g root -m 0644 "$SOURCE_DIR/deploy/yct-snapshot.service" /etc/systemd/system/yct-snapshot.service
install -o root -g root -m 0644 "$SOURCE_DIR/deploy/yct-snapshot.timer" /etc/systemd/system/yct-snapshot.timer

SHA_TMP=$(mktemp /opt/yct/SOURCE_SHA.XXXXXX)
printf '%s\n' "$EXPECTED_SHA" >"$SHA_TMP"
install -o root -g root -m 0644 "$SHA_TMP" /opt/yct/SOURCE_SHA
rm -f "$SHA_TMP"
SHA_TMP=""

systemctl daemon-reload
systemctl start yct-snapshot.service
/usr/bin/python3 /opt/yct/verify_snapshot.py /var/lib/yct/data.json \
  --status-path /var/lib/yct/status.json \
  --calendar-path /opt/yct/source-calendars.json \
  --boj-policy-path /opt/yct/boj-policy.json

install -o root -g root -m 0644 "$SOURCE_DIR/web/app.css" /var/www/html/yct/app.css.next
install -o root -g root -m 0644 "$SOURCE_DIR/web/app.js" /var/www/html/yct/app.js.next
install -d -o root -g root -m 0755 /var/www/html/yct/en
install -o root -g root -m 0644 "$SOURCE_DIR/web/en/index.html" /var/www/html/yct/en/index.html.next
install -o root -g root -m 0644 "$SOURCE_DIR/web/index.html" /var/www/html/yct/index.html.next
mv /var/www/html/yct/app.css.next /var/www/html/yct/app.css
mv /var/www/html/yct/app.js.next /var/www/html/yct/app.js
mv /var/www/html/yct/en/index.html.next /var/www/html/yct/en/index.html
mv /var/www/html/yct/index.html.next /var/www/html/yct/index.html

install -o root -g root -m 0644 "$SOURCE_DIR/deploy/yct.l0g.fr.conf" /etc/apache2/sites-available/yct.l0g.fr.conf
/usr/sbin/apache2ctl configtest
systemctl reload apache2

systemctl start yct-snapshot.timer
systemctl is-active --quiet yct-snapshot.timer

trap - ERR
echo "[yct-release] OK sha=$EXPECTED_SHA backup=$BACKUP_DIR"
