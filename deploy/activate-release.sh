#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_SHA=${EXPECTED_SHA:?EXPECTED_SHA est obligatoire}
SOURCE_DIR=${SOURCE_DIR:?SOURCE_DIR est obligatoire}

case "$SOURCE_DIR" in
  /home/bluetouff/yct-release-*) ;;
  *)
    echo "[yct-release] SOURCE_DIR refuse: $SOURCE_DIR" >&2
    exit 2
    ;;
esac

if [[ $(id -u) -ne 0 ]]; then
  echo "[yct-release] cette activation doit etre lancee avec sudo" >&2
  exit 2
fi

for command_name in git install cp mv awk mktemp systemctl; do
  command -v "$command_name" >/dev/null || {
    echo "[yct-release] commande absente: $command_name" >&2
    exit 2
  }
done

for required_path in \
  "$SOURCE_DIR/build_snapshot.py" \
  "$SOURCE_DIR/verify_snapshot.py" \
  "$SOURCE_DIR/deploy/yct-snapshot.service" \
  "$SOURCE_DIR/web/index.html" \
  "$SOURCE_DIR/web/app.css" \
  "$SOURCE_DIR/web/app.js" \
  /opt/yct/build_snapshot.py \
  /etc/yct/env \
  /etc/systemd/system/yct-snapshot.service \
  /var/lib/yct/data.json \
  /var/www/html/yct/index.html \
  /var/www/html/yct/app.css \
  /var/www/html/yct/app.js; do
  [[ -f "$required_path" ]] || {
    echo "[yct-release] fichier requis absent: $required_path" >&2
    exit 2
  }
done

actual_sha=$(git -C "$SOURCE_DIR" rev-parse HEAD)
[[ "$actual_sha" == "$EXPECTED_SHA" ]] || {
  echo "[yct-release] SHA inattendu: $actual_sha" >&2
  exit 2
}
[[ -z $(git -C "$SOURCE_DIR" status --porcelain) ]] || {
  echo "[yct-release] checkout de release modifie" >&2
  exit 2
}

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
short_sha=${EXPECTED_SHA:0:12}
BACKUP_DIR="/var/backups/yct/${timestamp}-${short_sha}"
install -d -o root -g root -m 0700 "$BACKUP_DIR"

cp -a /opt/yct/build_snapshot.py "$BACKUP_DIR/build_snapshot.py"
cp -a /etc/yct/env "$BACKUP_DIR/env"
cp -a /etc/systemd/system/yct-snapshot.service "$BACKUP_DIR/yct-snapshot.service"
cp -a /var/lib/yct/data.json "$BACKUP_DIR/data.json"
cp -a /var/www/html/yct/index.html "$BACKUP_DIR/index.html"
cp -a /var/www/html/yct/app.css "$BACKUP_DIR/app.css"
cp -a /var/www/html/yct/app.js "$BACKUP_DIR/app.js"

if [[ -f /opt/yct/verify_snapshot.py ]]; then
  cp -a /opt/yct/verify_snapshot.py "$BACKUP_DIR/verify_snapshot.py"
  touch "$BACKUP_DIR/.had-verify-snapshot"
fi
if [[ -f /opt/yct/SOURCE_SHA ]]; then
  cp -a /opt/yct/SOURCE_SHA "$BACKUP_DIR/SOURCE_SHA"
  touch "$BACKUP_DIR/.had-source-sha"
fi

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
  install -o yct -g yct -m 0644 "$BACKUP_DIR/data.json" /var/lib/yct/data.json
  install -o root -g root -m 0644 "$BACKUP_DIR/index.html" /var/www/html/yct/index.html
  install -o root -g root -m 0644 "$BACKUP_DIR/app.css" /var/www/html/yct/app.css
  install -o root -g root -m 0644 "$BACKUP_DIR/app.js" /var/www/html/yct/app.js
  if [[ -f "$BACKUP_DIR/.had-verify-snapshot" ]]; then
    install -o root -g root -m 0755 "$BACKUP_DIR/verify_snapshot.py" /opt/yct/verify_snapshot.py
  else
    rm -f /opt/yct/verify_snapshot.py
  fi
  if [[ -f "$BACKUP_DIR/.had-source-sha" ]]; then
    install -o root -g root -m 0644 "$BACKUP_DIR/SOURCE_SHA" /opt/yct/SOURCE_SHA
  else
    rm -f /opt/yct/SOURCE_SHA
  fi
  rm -f /var/www/html/yct/index.html.next /var/www/html/yct/app.css.next /var/www/html/yct/app.js.next
  [[ -z "$ENV_TMP" ]] || rm -f "$ENV_TMP"
  [[ -z "$SHA_TMP" ]] || rm -f "$SHA_TMP"
  systemctl daemon-reload
  systemctl start yct-snapshot.service
  systemctl start yct-snapshot.timer
  echo "[yct-release] rollback termine; sauvegarde conservee: $BACKUP_DIR" >&2
  exit "$exit_code"
}
trap rollback ERR

systemctl stop yct-snapshot.timer

ENV_TMP=$(mktemp /etc/yct/env.yct-release.XXXXXX)
awk -F= \
  -v boj_rate="1.00" \
  -v boj_date="2026-07-31" \
  -v boj_url="https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/k260731a.pdf" \
  -v fed_rate="3.625" \
  -v fed_date="2026-07-29" \
  -v fed_url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm" '
  BEGIN {
    replacement["BOJ_RATE"] = boj_rate
    replacement["BOJ_RATE_AS_OF"] = boj_date
    replacement["BOJ_SOURCE_URL"] = boj_url
    replacement["FED_RATE"] = fed_rate
    replacement["FED_RATE_AS_OF"] = fed_date
    replacement["FED_RATE_SOURCE_URL"] = fed_url
  }
  $1 in replacement {
    print $1 "=" replacement[$1]
    seen[$1] = 1
    next
  }
  { print }
  END {
    for (key in replacement) {
      if (!(key in seen)) print key "=" replacement[key]
    }
  }
' /etc/yct/env >"$ENV_TMP"
install -o root -g root -m 0600 "$ENV_TMP" /etc/yct/env
rm -f "$ENV_TMP"
ENV_TMP=""

install -o root -g root -m 0755 "$SOURCE_DIR/build_snapshot.py" /opt/yct/build_snapshot.py
install -o root -g root -m 0755 "$SOURCE_DIR/verify_snapshot.py" /opt/yct/verify_snapshot.py
install -o root -g root -m 0644 "$SOURCE_DIR/deploy/yct-snapshot.service" /etc/systemd/system/yct-snapshot.service

SHA_TMP=$(mktemp /opt/yct/SOURCE_SHA.XXXXXX)
printf '%s\n' "$EXPECTED_SHA" >"$SHA_TMP"
install -o root -g root -m 0644 "$SHA_TMP" /opt/yct/SOURCE_SHA
rm -f "$SHA_TMP"
SHA_TMP=""

systemctl daemon-reload
systemctl start yct-snapshot.service
/usr/bin/python3 /opt/yct/verify_snapshot.py /var/lib/yct/data.json

install -o root -g root -m 0644 "$SOURCE_DIR/web/app.css" /var/www/html/yct/app.css.next
install -o root -g root -m 0644 "$SOURCE_DIR/web/app.js" /var/www/html/yct/app.js.next
install -o root -g root -m 0644 "$SOURCE_DIR/web/index.html" /var/www/html/yct/index.html.next
mv /var/www/html/yct/app.css.next /var/www/html/yct/app.css
mv /var/www/html/yct/app.js.next /var/www/html/yct/app.js
mv /var/www/html/yct/index.html.next /var/www/html/yct/index.html

systemctl start yct-snapshot.timer
systemctl is-active --quiet yct-snapshot.timer

trap - ERR
echo "[yct-release] OK sha=$EXPECTED_SHA backup=$BACKUP_DIR"
