#!/bin/sh
set -eu
# All plugins share .list and nginx configuration. Serialize only boot recovery.
if [ "${MINAS_PLUGIN_RESTORE_LOCKED:-0}" != 1 ]; then
    exec python3 - "$0" "$@" <<'PYLOCK'
import fcntl, os, subprocess, sys
os.makedirs("/run/lock", exist_ok=True)
with open("/run/lock/minas-plugin-restore.lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    result = subprocess.run(["/bin/sh", *sys.argv[1:]],
                            env=dict(os.environ, MINAS_PLUGIN_RESTORE_LOCKED="1"))
    sys.exit(result.returncode)
PYLOCK
fi
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
  NAS_IP="${2:-}"
  if [ -z "$NAS_IP" ]; then
    # systemd 只传 UID；本机默认路由出口地址即 WebDAV/客户端可达地址
    NAS_IP="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  fi
  if [ -z "$NAS_IP" ]; then
    NAS_IP="$(hostname -I 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $i !~ /^127\./) {print $i; exit}}')"
  fi
  if [ -z "$NAS_IP" ]; then
    NAS_IP="$(ip -4 addr show scope global 2>/dev/null | awk '/inet / {sub(/\/.*/, "", $2); print $2; exit}')"
  fi
  if [ -z "$NAS_IP" ]; then
    echo 'Motrix restore: cannot determine NAS IPv4 address; proxy restoration failed and can be retried' >&2
    exit 1
  fi
  MOTRIX_SKIP_SERVICE_START=1 /bin/sh "$BACKUP/install-web-proxy.sh" "$UID_NUM" "$NAS_IP"
fi
