#!/bin/sh
# Run as root on the Xiaomi NAS after extracting this package.
set -eu

UID_NUM="${1:?Usage: sh deploy.sh <NAS_UID> <NAS_IP>}"
NAS_IP="${2:?Usage: sh deploy.sh <NAS_UID> <NAS_IP>}"
PLUGIN=motrix
USER_NAME="u${UID_NUM}"
DOCKER=/data/docker/docker
IMAGE=ghcr.io/agalwood/motrix-server:2.0.0-beta.40
CONTAINER=motrix-server
PORT=8080
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
HOME_DIR="/home/${USER_NAME}/plugin/${PLUGIN}"
BACKUP_DIR="/data/backup/plugins/${PLUGIN}"
WWW_DIR="/data/plugin/www/${USER_NAME}"
LIST_FILE="/data/plugin/${USER_NAME}.list"
STATE_DIR=/data/motrix-server/data
DATA_ROOT="/nas/pool0/${USER_NAME}/data"
DOWNLOAD_DIR="${DATA_ROOT}/motrix-downloads"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [ "$(id -u)" != 0 ]; then echo 'Must run as root' >&2; exit 1; fi
if [ ! -x "$DOCKER" ]; then echo "Docker missing: $DOCKER" >&2; exit 1; fi
if [ ! -f "$ROOT/INFO" ]; then echo 'Package missing INFO' >&2; exit 1; fi
if ! "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Image missing: $IMAGE (pull it first)" >&2; exit 1
fi
if [ -f "$LIST_FILE" ]; then cp -a "$LIST_FILE" "${LIST_FILE}.bak-${STAMP}"; fi
for f in /etc/config/plugin /data/etc/upper/config/plugin; do
  if [ -f "$f" ]; then cp -a "$f" "${f}.bak-${STAMP}"; fi
done

echo '==> Prepare persistent directories'
if [ ! -d "$DATA_ROOT" ] || [ -L "$DATA_ROOT" ]; then
  echo "NAS data root is missing or is a symlink: $DATA_ROOT" >&2; exit 1
fi
mkdir -p "$STATE_DIR" "$DOWNLOAD_DIR"
OWNER="$(id -u "$USER_NAME"):$(id -g "$USER_NAME")"
chown "$OWNER" "$STATE_DIR" "$DOWNLOAD_DIR"
chmod 700 "$STATE_DIR"
chmod 750 "$DOWNLOAD_DIR"

echo '==> Start Motrix Server container'
create_container() {
  "$DOCKER" run -d \
    --name "$CONTAINER" --init --restart unless-stopped --stop-timeout 120 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
    --security-opt no-new-privileges:true --user "$OWNER" \
    -e "MOTRIX_PUBLIC_URL=http://${NAS_IP}:${PORT}" \
    -e "MOTRIX_DEFAULT_SAVE_DIR=${DOWNLOAD_DIR}" \
    -e "MOTRIX_ALLOWED_SAVE_DIRS=${DATA_ROOT}" \
    -p "127.0.0.1:${PORT}:8080" \
    -v "${STATE_DIR}:/data" \
    -v "${DATA_ROOT}:${DATA_ROOT}" \
    "$IMAGE" >/dev/null
}
OLD_CONTAINER=
if "$DOCKER" container inspect "$CONTAINER" >/dev/null 2>&1; then
  CURRENT_IMAGE="$("$DOCKER" inspect -f '{{.Config.Image}}' "$CONTAINER")"
  if [ "$CURRENT_IMAGE" != "$IMAGE" ]; then
    echo "Existing $CONTAINER uses $CURRENT_IMAGE; refusing to replace it" >&2; exit 1
  fi
  if "$DOCKER" inspect "$CONTAINER" | python3 -c '
import json, sys
container = json.load(sys.stdin)[0]
root, default, port = sys.argv[1:]
mounts = container["Mounts"]
env = set(container["Config"]["Env"])
bindings = container["HostConfig"].get("PortBindings") or {}
web = bindings.get("8080/tcp") or []
local_port = (len(web) == 1 and web[0].get("HostIp") == "127.0.0.1"
              and web[0].get("HostPort") == port)
matches = (any(m["Source"] == root and m["Destination"] == root for m in mounts)
           and f"MOTRIX_DEFAULT_SAVE_DIR={default}" in env
           and f"MOTRIX_ALLOWED_SAVE_DIRS={root}" in env
           and local_port)
sys.exit(0 if matches else 1)
' "$DATA_ROOT" "$DOWNLOAD_DIR" "$PORT"; then
    "$DOCKER" start "$CONTAINER" >/dev/null
  else
    echo '==> Update Motrix container data mount and local-only port'
    "$DOCKER" stop "$CONTAINER" >/dev/null
    OLD_CONTAINER="${CONTAINER}-before-local-port-${STAMP}"
    "$DOCKER" rename "$CONTAINER" "$OLD_CONTAINER"
    if ! create_container; then
      "$DOCKER" rm -f "$CONTAINER" >/dev/null 2>&1 || true
      "$DOCKER" rename "$OLD_CONTAINER" "$CONTAINER"
      "$DOCKER" start "$CONTAINER" >/dev/null
      echo 'New Motrix container failed; restored previous container' >&2
      exit 1
    fi
  fi
else
  create_container
fi

echo '==> Enable Motrix container outbound network'
mkdir -p "$BACKUP_DIR"
cp "$ROOT/deploy/ensure-egress.sh" "$BACKUP_DIR/ensure-egress.sh"
chmod 755 "$BACKUP_DIR/ensure-egress.sh"
/bin/sh "$BACKUP_DIR/ensure-egress.sh"

echo '==> Install Xiaomi plugin entry'
mkdir -p "$HOME_DIR/src/ui" "$HOME_DIR/src/files" "$HOME_DIR/scripts" "$HOME_DIR/licenses" "$BACKUP_DIR" "$WWW_DIR" /data/plugin/www/icon
WS_TICKET_FILE="$BACKUP_DIR/ws-ticket"
if [ ! -s "$WS_TICKET_FILE" ]; then
  python3 - "$WS_TICKET_FILE" <<'PY'
import pathlib, secrets, sys
path = pathlib.Path(sys.argv[1])
path.write_text(secrets.token_hex(32), encoding='ascii')
path.chmod(0o600)
PY
fi
WS_TICKET="$(cat "$WS_TICKET_FILE")"
cp "$ROOT/INFO" "$HOME_DIR/INFO"
cp -a "$ROOT/licenses/." "$HOME_DIR/licenses/"
cp -a "$ROOT/src/ui/." "$HOME_DIR/src/ui/"
python3 "$ROOT/deploy/patch-web-assets.py" "$HOME_DIR/src/ui" "$UID_NUM" "$WS_TICKET"
cp "$ROOT/src/files/web_proxy.py" "$HOME_DIR/src/files/web_proxy.py"
cp "$ROOT/scripts/control" "$HOME_DIR/scripts/control"
chmod 755 "$HOME_DIR/scripts/control" "$HOME_DIR/src/ui/motrix.cgi"
chown -R "${USER_NAME}:${USER_NAME}" "$HOME_DIR"
ln -sfn "$HOME_DIR/src/ui" "$WWW_DIR/$PLUGIN"
cp "$HOME_DIR/src/ui/icon.png" "/data/plugin/www/icon/${PLUGIN}.icon"
chown "${USER_NAME}:${USER_NAME}" "/data/plugin/www/icon/${PLUGIN}.icon"

python3 - "$LIST_FILE" "$HOME_DIR" "$BACKUP_DIR" "$UID_NUM" <<'PY'
import json, os, sys, time
list_path, home, backup, uid = sys.argv[1:5]
info = json.load(open(os.path.join(home, 'INFO'), encoding='utf-8'))
frontend = json.load(open(os.path.join(home, 'src/ui/config'), encoding='utf-8'))
info['timestamp'] = int(time.time())
entry = {
  'resource': {'mpk': f'{home}/INFO', 'icon': '/icon/motrix.icon', 'preview': []},
  'status': 'running', 'install': True, 'upgrade': False, 'enable': True,
  'changetime': int(time.time()), 'icon': '/icon/motrix.icon', 'progress': '100',
  'frontend': frontend, 'info': info,
}
data = json.load(open(list_path, encoding='utf-8')) if os.path.isfile(list_path) else {}
data['motrix'] = entry
with open(list_path, 'w', encoding='utf-8') as f:
  json.dump(data, f, ensure_ascii=False, indent=1)
with open(os.path.join(backup, 'list-entry.json'), 'w', encoding='utf-8') as f:
  json.dump({'motrix': entry}, f, ensure_ascii=False, indent=1)
print('Plugin registered in', list_path)
PY

for f in /etc/config/plugin /data/etc/upper/config/plugin; do
  if [ -f "$f" ]; then
    python3 - "$f" "$USER_NAME" "$PORT" <<'PY'
import json, sys
path, user, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(path, encoding='utf-8') as f: data = json.load(f)
data.setdefault('ports', {})['motrix'] = {user: port}
with open(path, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=1)
PY
  fi
done

mkdir -p "$BACKUP_DIR/motrix"
cp -a "$HOME_DIR/." "$BACKUP_DIR/motrix/"
cp "$ROOT/deploy/restore.sh" "$BACKUP_DIR/restore.sh"
cp "$ROOT/deploy/install-web-proxy.sh" "$BACKUP_DIR/install-web-proxy.sh"
cp "$ROOT/deploy/motrix-nginx.conf.tmpl" "$BACKUP_DIR/motrix-nginx.conf.tmpl"
chmod 755 "$BACKUP_DIR/restore.sh"
cat > /etc/systemd/system/motrix-restore.service <<UNIT
[Unit]
Description=Restore Xiaomi NAS Motrix plugin entry
Requires=docker.service
After=network.target docker.service
ConditionPathExists=$BACKUP_DIR/restore.sh

[Service]
Type=oneshot
TimeoutStartSec=180
ExecStart=/bin/sh $BACKUP_DIR/restore.sh $UID_NUM

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable motrix-restore.service >/dev/null

echo '==> Install same-origin Web UI bridge'
/bin/sh "$BACKUP_DIR/install-web-proxy.sh" "$UID_NUM" "$NAS_IP"

echo '==> Wait for health'
i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS --max-time 2 http://127.0.0.1:8080/healthz; then
    if [ -n "$OLD_CONTAINER" ]; then "$DOCKER" rm "$OLD_CONTAINER" >/dev/null; fi
    echo; echo "Motrix ready through NAS plugin (local service: http://127.0.0.1:${PORT}/)"
    exit 0
  fi
  sleep 2
  i=$((i+1))
done
"$DOCKER" logs --tail 50 "$CONTAINER" >&2
if [ -n "$OLD_CONTAINER" ]; then
  "$DOCKER" rm -f "$CONTAINER" >/dev/null 2>&1 || true
  "$DOCKER" rename "$OLD_CONTAINER" "$CONTAINER"
  "$DOCKER" start "$CONTAINER" >/dev/null
  /bin/sh "$BACKUP_DIR/ensure-egress.sh"
  echo 'Previous Motrix container restored' >&2
fi
echo 'Motrix did not become healthy' >&2
exit 1
