#!/bin/sh
set -eu

UID_NUM="${1:?Usage: sh install-web-proxy.sh <NAS_UID> <NAS_IP>}"
NAS_IP="${2:?Usage: sh install-web-proxy.sh <NAS_UID> <NAS_IP>}"
USER_NAME="u${UID_NUM}"
BACKUP=/data/backup/plugins/motrix
PROXY="/home/${USER_NAME}/plugin/motrix/src/files/web_proxy.py"
WS_TICKET_FILE="$BACKUP/ws-ticket"
CONFIG=/etc/nginx/conf.d/luci/motrix.conf
UNIT=/etc/systemd/system/motrix-web-proxy.service

test -f "$PROXY"
test -f "$BACKUP/motrix-nginx.conf.tmpl"
test -s "$WS_TICKET_FILE"
WS_TICKET="$(cat "$WS_TICKET_FILE")"
case "$WS_TICKET" in *[!0-9a-f]* | '') echo 'Invalid Motrix API ticket' >&2; exit 1;; esac
if [ "${#WS_TICKET}" -ne 64 ]; then echo 'Invalid Motrix API ticket length' >&2; exit 1; fi

cat > "$UNIT" <<EOF
[Unit]
Description=Motrix Web UI bridge for Xiaomi NAS plugin
After=network.target motrix-restore.service

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
RuntimeDirectory=motrix-web-proxy
RuntimeDirectoryMode=0700
ExecStart=/usr/bin/python3 $PROXY --unix-socket /run/motrix-web-proxy/proxy.sock --prefix /plugin/$UID_NUM/motrix/api/$WS_TICKET --public-origin http://$NAS_IP:8080
Restart=always
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable motrix-web-proxy.service >/dev/null
if [ "${MOTRIX_SKIP_SERVICE_START:-0}" != 1 ]; then
    systemctl restart motrix-web-proxy.service
fi

if [ -f "$CONFIG" ]; then cp -p "$CONFIG" "$CONFIG.bak-motrix"; fi
python3 - "$BACKUP/motrix-nginx.conf.tmpl" "$CONFIG" "$UID_NUM" "$NAS_IP" <<'PY'
import ipaddress
import pathlib
import re
import sys

template, destination, uid, address = sys.argv[1:]
if not uid.isdecimal():
    raise SystemExit('UID must be numeric')
ipaddress.ip_address(address)
content = pathlib.Path(template).read_text(encoding='utf-8')
token = pathlib.Path('/data/motrix-server/data/operator-token').read_text(encoding='utf-8').strip()
ws_ticket = pathlib.Path('/data/backup/plugins/motrix/ws-ticket').read_text(encoding='ascii').strip()
if not re.fullmatch(r'[A-Za-z0-9._~+/-]+', token):
    raise SystemExit('Invalid Motrix operator token format')
if not re.fullmatch(r'[0-9a-f]{64}', ws_ticket):
    raise SystemExit('Invalid Motrix WebSocket ticket format')
content = (content.replace('__UID__', uid)
                  .replace('__NAS_IP__', address)
                  .replace('__OPERATOR_TOKEN__', token)
                  .replace('__WS_TICKET__', ws_ticket))
path = pathlib.Path(destination)
path.write_text(content, encoding='utf-8')
path.chmod(0o600)
PY
if ! nginx -t; then
    if [ -f "$CONFIG.bak-motrix" ]; then
        cp -p "$CONFIG.bak-motrix" "$CONFIG"
    else
        rm -f "$CONFIG"
    fi
    exit 1
fi
if systemctl is-active --quiet nginx; then systemctl reload nginx; fi
if [ "${MOTRIX_SKIP_SERVICE_START:-0}" != 1 ]; then
    i=0
    until curl -fsS --max-time 2 --unix-socket /run/motrix-web-proxy/proxy.sock http://localhost/healthz >/dev/null 2>&1; do
        i=$((i+1))
        if [ "$i" -ge 30 ]; then
            echo 'Motrix Web proxy did not become ready' >&2
            exit 1
        fi
        sleep 1
    done
fi
