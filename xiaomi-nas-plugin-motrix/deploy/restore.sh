#!/bin/sh
set -eu
UID_NUM="${1:?Usage: sh restore.sh <NAS_UID>}"
USER_NAME="u${UID_NUM}"
BACKUP=/data/backup/plugins/motrix
HOME_DIR="/home/${USER_NAME}/plugin/motrix"
LIST_FILE="/data/plugin/${USER_NAME}.list"
if [ ! -f "$HOME_DIR/INFO" ]; then
  mkdir -p "$(dirname "$HOME_DIR")"
  cp -a "$BACKUP/motrix" "$HOME_DIR"
  chown -R "${USER_NAME}:${USER_NAME}" "$HOME_DIR"
fi
mkdir -p "/data/plugin/www/${USER_NAME}" /data/plugin/www/icon
ln -sfn "$HOME_DIR/src/ui" "/data/plugin/www/${USER_NAME}/motrix"
cp "$HOME_DIR/src/ui/icon.png" /data/plugin/www/icon/motrix.icon
chown "${USER_NAME}:${USER_NAME}" /data/plugin/www/icon/motrix.icon
python3 - "$LIST_FILE" "$BACKUP/list-entry.json" <<'PY'
import json, os, sys
path, entry_path = sys.argv[1:3]
data = json.load(open(path, encoding='utf-8')) if os.path.isfile(path) else {}
data.update(json.load(open(entry_path, encoding='utf-8')))
with open(path, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=1)
PY
/data/docker/docker start motrix-server >/dev/null 2>&1 || true
if [ -f "$BACKUP/ensure-egress.sh" ]; then
  /bin/sh "$BACKUP/ensure-egress.sh"
fi
if [ -f "$BACKUP/install-web-proxy.sh" ]; then
  MOTRIX_SKIP_SERVICE_START=1 /bin/sh "$BACKUP/install-web-proxy.sh" "$UID_NUM"
fi
