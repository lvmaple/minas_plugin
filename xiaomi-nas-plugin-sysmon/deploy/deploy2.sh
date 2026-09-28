#!/bin/sh
# sysmon 重新部署脚本（v2）— 在小米 NAS 上以 root 执行
# 用法: sh deploy2.sh <uid>
#
# 相比旧版 deploy.sh 的变化：
#   - 使用真机验证布局 /home/u<uid>/plugin/sysmon/src/{files,ui}（不再用 pluginsrc）
#   - control 使用真机 action 名（postinstall/enable/...）
#   - nginx 反代 UID 参数化（修复旧脚本硬编码 UID 的问题）
#   - 重启持久化三层保险（ports 表注册 + /data 备份 + 开机自愈服务）
set -e

UID_NUM="${1:-}"
case "$UID_NUM" in
    ''|*[!0-9]*) echo "Usage: sh deploy2.sh <NAS_UID>" >&2; exit 2 ;;
esac
PLUGIN="sysmon"
USER_NAME="u${UID_NUM}"
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="/home/${USER_NAME}/plugin/${PLUGIN}"
WWW_DIR="/data/plugin/www/${USER_NAME}"
ICON_DST="/data/plugin/www/icon/${PLUGIN}.icon"
LIST_FILE="/data/plugin/${USER_NAME}.list"
NGINX_CONF="/etc/nginx/conf.d/luci/plugincenter.conf"
BACKUP_DIR="/data/backup/plugins/${PLUGIN}"
RESTORE_UNIT="/etc/systemd/system/${PLUGIN}-restore.service"
STAMP="$(date +%F-%H%M%S)"
PORT="9301"

echo "==> [1/8] 备份 .list / nginx 配置"
[ -f "$LIST_FILE" ] && cp "$LIST_FILE" "${LIST_FILE}.bak-${STAMP}" && echo "    → ${LIST_FILE}.bak-${STAMP}"
[ -f "$NGINX_CONF" ] && cp "$NGINX_CONF" "${NGINX_CONF}.bak-${STAMP}" && echo "    → ${NGINX_CONF}.bak-${STAMP}"

echo "==> [2/8] 部署插件目录（真机布局 src/{files,ui}）"
mkdir -p "${HOME_DIR}/src" "${HOME_DIR}/scripts"
cp -a "${SRC_ROOT}/INFO" "${HOME_DIR}/INFO"
# 先删旧目录再拷贝：cp -a 到已存在目录会嵌套成 src/ui/ui，导致更新部署悄悄失败
rm -rf "${HOME_DIR}/src/files" "${HOME_DIR}/src/ui"
cp -a "${SRC_ROOT}/files" "${SRC_ROOT}/ui" "${HOME_DIR}/src/"
chmod +x "${HOME_DIR}/src/files/server.sh" "${HOME_DIR}/src/ui/${PLUGIN}.cgi"
# 生成真机 action 名的 control（旧版脚本用的是已被证伪的 action 名）
cat > "${HOME_DIR}/scripts/control" <<'CTL'
#!/bin/sh
PLUGIN_NAME="sysmon"
HTTPD="$PLUG_SRC_DIR/files/httpd.py"
LOGDIR="/tmp/${PLUGIN_NAME}"
PORT="${PLUG_PORT_ALLOC:-9301}"

_write_unit() {
  cat > "/etc/systemd/system/${PLUGIN_NAME}.service" <<UNIT
[Unit]
Description=${PLUGIN_NAME} service (system monitor)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${HTTPD}
Restart=on-failure
RestartSec=5
Environment=PLUG_PORT=${PORT}
StandardOutput=append:${LOGDIR}/server.log
StandardError=append:${LOGDIR}/server.log

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "${PLUGIN_NAME}.service" 2>/dev/null
}

_start() {
  mkdir -p "$LOGDIR"
  if [ -f "/etc/systemd/system/${PLUGIN_NAME}.service" ]; then
    systemctl restart "${PLUGIN_NAME}.service"; return $?
  fi
  PLUG_PORT="$PORT" nohup /usr/bin/python3 "$HTTPD" >>"$LOGDIR/server.log" 2>&1 &
  echo $! > "$LOGDIR/server.pid"
}

_stop() {
  [ -f "/etc/systemd/system/${PLUGIN_NAME}.service" ] && \
    systemctl stop "${PLUGIN_NAME}.service" 2>/dev/null
  [ -f "$LOGDIR/server.pid" ] && { kill "$(cat "$LOGDIR/server.pid")" 2>/dev/null; rm -f "$LOGDIR/server.pid"; }
}

case "$1" in
    preinstall)  mkdir -p "$LOGDIR"; exit 0 ;;
    postinstall)
        _write_unit
        _start; exit $? ;;
    enable|start)  _start; exit $? ;;
    disable|stop)
        [ "$PLUG_ACTION" = "uninstall" ] && {
            systemctl disable "${PLUGIN_NAME}.service" 2>/dev/null
            rm -f "/etc/systemd/system/${PLUGIN_NAME}.service"
            systemctl daemon-reload 2>/dev/null
        }
        _stop; exit 0 ;;
    status)
        if systemctl is-active --quiet "${PLUGIN_NAME}.service" 2>/dev/null; then
            echo "running"; exit 0
        fi
        if [ -f "$LOGDIR/server.pid" ] && kill -0 "$(cat "$LOGDIR/server.pid")" 2>/dev/null; then
            echo "running"; exit 0
        fi
        echo "stopped"; exit 1 ;;
    preuninstall)  _stop; exit 0 ;;
    postuninstall)
        rm -f "/etc/systemd/system/${PLUGIN_NAME}.service"
        systemctl daemon-reload 2>/dev/null
        rm -rf "$LOGDIR"; exit 0 ;;
    preupgrade)  exit 0 ;;
    postupgrade)
        _write_unit
        _start; exit $? ;;
    *) exit 0 ;;
esac
CTL
chmod +x "${HOME_DIR}/scripts/control"
chown -R "${USER_NAME}:${USER_NAME}" "${HOME_DIR}" 2>/dev/null || true

echo "==> [3/8] www 软链 + 图标"
mkdir -p "$WWW_DIR" "/data/plugin/www/icon"
ln -sfn "${HOME_DIR}/src/ui" "${WWW_DIR}/${PLUGIN}"
cp "${HOME_DIR}/src/ui/icon.png" "$ICON_DST"
chown "${USER_NAME}:${USER_NAME}" "$ICON_DST" 2>/dev/null || true

echo "==> [4/8] 注册 .list 条目"
python3 - "$LIST_FILE" "$PLUGIN" "$UID_NUM" "$PORT" "${BACKUP_DIR}" <<'PY'
import json, time, sys, os
path, plugin, uid, port, backup_dir = sys.argv[1:6]
home = f"/home/u{uid}/plugin/{plugin}"
desc = "查看 NAS CPU、内存、磁盘、温度、风扇与 Docker 容器状态"
data = {}
if os.path.isfile(path):
    with open(path) as f:
        data = json.load(f)
entry = {
    "resource": {"mpk": f"{home}/INFO", "icon": f"/icon/{plugin}.icon", "preview": []},
    "status": "running", "install": True, "upgrade": False, "enable": True,
    "changetime": int(time.time()), "icon": f"/icon/{plugin}.icon", "progress": "100",
    "frontend": {
        "title": "系统状态", "desc": desc,
        "icon": f"/{plugin}.icon", "type": "url", "dev_type": [1, 2, 3, 4],
        "url": [
            {"dev_type": [1], "url": "/index.html#/"},
            {"dev_type": [2, 3, 4], "url": "/index.html#/"}
        ],
        "sortid": 30, "widget": []
    },
    "info": {
        "plugin": plugin, "name": "系统状态", "id": 98, "version": "1.0.1",
        "tags": ["tool"], "timestamp": int(time.time()),
        "desc": desc, "developer": "YourName", "publisher": "minas",
        "changelog": "1.0.1 重新部署：补 ui/config、桌面端 apiUrl 适配、重启自愈",
        "system": False, "size": 20000,
        "port": port, "type": "standard", "forceupgrade": False, "ext": {}
    }
}
data[plugin] = entry
with open(path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print(f"    已写入 {path}[{plugin}]，当前插件 keys: {list(data.keys())}")
os.makedirs(backup_dir, exist_ok=True)
with open(os.path.join(backup_dir, "list-entry.json"), "w") as f:
    json.dump({plugin: entry}, f, ensure_ascii=False, indent=1)
PY

echo "==> [5/8] 启动服务（systemd）"
PLUG_SRC_DIR="${HOME_DIR}/src" PLUG_HOME_DIR="${HOME_DIR}" PLUG_USER="${USER_NAME}" \
  PLUG_PORT_ALLOC="${PORT}" sh "${HOME_DIR}/scripts/control" postinstall
sleep 2
curl -s --max-time 3 "http://127.0.0.1:${PORT}/api/health" || { echo "    ⚠️ 后端未响应，查看 /tmp/${PLUGIN}/server.log"; exit 1; }
echo ""

echo "==> [6/8] 注入 Nginx 反代规则（UID 已参数化，修复旧脚本硬编码）"
if [ -f "$NGINX_CONF" ]; then
  if ! grep -q "sysmon/api/" "$NGINX_CONF"; then
    python3 - "$NGINX_CONF" "$UID_NUM" "$PORT" <<'PY'
import sys
path, uid, port = sys.argv[1], sys.argv[2], sys.argv[3]
block = f"""
# === sysmon (auto-added) ===
location ~ ^/plugin/u?{uid}/sysmon/api/ {{
  rewrite ^/plugin/u?{uid}/sysmon/(api/.*)$ /$1 break;
  proxy_pass http://127.0.0.1:{port};
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  add_header Access-Control-Allow-Origin "*" always;
  add_header Cache-Control "no-store";
}}
# === /sysmon ===
"""
with open(path) as f:
    lines = f.readlines()
out, inserted = [], False
for line in lines:
    if not inserted and "location /plugin" in line:
        out.append(block)
        inserted = True
    out.append(line)
if not inserted:
    out.append(block)
with open(path, "w") as f:
    f.writelines(out)
print("    已注入 nginx location")
PY
    nginx -t && nginx -s reload
  else
    echo "    sysmon 规则已存在，跳过"
  fi
fi

echo "==> [7/8] 重启持久化三层保险"
echo "  ① 注册端口到 /etc/config/plugin ports 表"
python3 - "$UID_NUM" "$PORT" <<'PY'
import json, sys
uid, port = sys.argv[1], int(sys.argv[2])
for p in ["/etc/config/plugin", "/data/etc/upper/config/plugin"]:
    try:
        cfg = json.load(open(p))
    except FileNotFoundError:
        print(f"    跳过（不存在）: {p}")
        continue
    cfg.setdefault("ports", {})["sysmon"] = {f"u{uid}": port}
    json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=1)
    print(f"    已注册 sysmon → {p}")
PY

echo "  ② 源码备份到 ${BACKUP_DIR}（/data 分区重启不丢）"
mkdir -p "${BACKUP_DIR}"
rm -rf "${BACKUP_DIR}/${PLUGIN}"
cp -a "${HOME_DIR}" "${BACKUP_DIR}/${PLUGIN}"

echo "  ③ 安装开机自愈服务 ${PLUGIN}-restore.service"
cat > "${BACKUP_DIR}/restore.sh" <<'RST'
#!/bin/sh
# sysmon 开机自愈：系统重启会清掉手动插件目录与 .list，从 /data 备份恢复
UID_N=__NAS_UID__
PLUGIN=sysmon
PORT=9301
USER_NAME=u${UID_N}
HOME_DIR=/home/${USER_NAME}/plugin/${PLUGIN}
WWW_DIR=/data/plugin/www/${USER_NAME}
LIST_FILE=/data/plugin/${USER_NAME}.list
NGINX_CONF=/etc/nginx/conf.d/luci/plugincenter.conf
BACKUP_DIR=/data/backup/plugins/${PLUGIN}
SRC=${BACKUP_DIR}/${PLUGIN}

if [ ! -f "${HOME_DIR}/INFO" ] && [ -f "${SRC}/INFO" ]; then
    mkdir -p /home/${USER_NAME}/plugin
    cp -a "${SRC}" "${HOME_DIR}"
    chmod +x "${HOME_DIR}/scripts/control" "${HOME_DIR}/src/files/server.sh" "${HOME_DIR}/src/ui/${PLUGIN}.cgi"
    chown -R "${USER_NAME}:${USER_NAME}" "${HOME_DIR}" 2>/dev/null || true
    echo "restore: 插件目录已恢复"
fi

mkdir -p "${WWW_DIR}" /data/plugin/www/icon
ln -sfn "${HOME_DIR}/src/ui" "${WWW_DIR}/${PLUGIN}"
cp "${HOME_DIR}/src/ui/icon.png" "/data/plugin/www/icon/${PLUGIN}.icon" 2>/dev/null || true
chown "${USER_NAME}:${USER_NAME}" "/data/plugin/www/icon/${PLUGIN}.icon" 2>/dev/null || true

if [ -f "${BACKUP_DIR}/list-entry.json" ]; then
    python3 - "${LIST_FILE}" "${BACKUP_DIR}/list-entry.json" <<'PY2'
import json, sys, os
list_path, entry_path = sys.argv[1], sys.argv[2]
entry = json.load(open(entry_path))
data = {}
if os.path.isfile(list_path):
    with open(list_path) as f:
        data = json.load(f)
data.update(entry)
with open(list_path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print("restore: .list 已合并")
PY2
fi

if [ -f "${HOME_DIR}/scripts/control" ]; then
    PLUG_SRC_DIR="${HOME_DIR}/src" PLUG_HOME_DIR="${HOME_DIR}" PLUG_USER="${USER_NAME}" \
      PLUG_PORT_ALLOC="${PORT}" sh "${HOME_DIR}/scripts/control" postinstall
fi

if [ -f "$NGINX_CONF" ] && ! grep -q "sysmon/api/" "$NGINX_CONF"; then
    sleep 3
    python3 - "$NGINX_CONF" "$UID_N" "$PORT" <<'PY3'
import sys
path, uid, port = sys.argv[1], sys.argv[2], sys.argv[3]
block = f"""
# === sysmon (auto-added) ===
location ~ ^/plugin/u?{uid}/sysmon/api/ {{
  rewrite ^/plugin/u?{uid}/sysmon/(api/.*)$ /$1 break;
  proxy_pass http://127.0.0.1:{port};
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  add_header Access-Control-Allow-Origin "*" always;
  add_header Cache-Control "no-store";
}}
# === /sysmon ===
"""
with open(path) as f:
    lines = f.readlines()
out, inserted = [], False
for line in lines:
    if not inserted and "location /plugin" in line:
        out.append(block)
        inserted = True
    out.append(line)
if not inserted:
    out.append(block)
with open(path, "w") as f:
    f.writelines(out)
print("restore: nginx location 已注入")
PY3
    nginx -t && nginx -s reload || echo "restore: nginx reload 失败（稍后可手动 reload）"
fi
exit 0
RST
sed -i "s/^UID_N=__NAS_UID__$/UID_N=${UID_NUM}/" "${BACKUP_DIR}/restore.sh"
chmod +x "${BACKUP_DIR}/restore.sh"
cat > "$RESTORE_UNIT" <<UNIT
[Unit]
Description=${PLUGIN} plugin restore on boot
After=network.target
Before=${PLUGIN}.service
ConditionPathExists=${BACKUP_DIR}/restore.sh

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh ${BACKUP_DIR}/restore.sh

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "${PLUGIN}-restore.service"
echo "    已启用（开机若插件目录被清会自动恢复）"

echo "==> [8/8] 验收"
systemctl is-active "${PLUGIN}.service" || true
systemctl is-enabled "${PLUGIN}-restore.service" || true
test -x "${HOME_DIR}/src/ui/${PLUGIN}.cgi" && echo "    cgi OK"
readlink "${WWW_DIR}/${PLUGIN}" || true
test -f "${BACKUP_DIR}/${PLUGIN}/INFO" && echo "    备份就位：${BACKUP_DIR}"
python3 -c "import json;print('    ports 表已含 sysmon:', 'sysmon' in json.load(open('/etc/config/plugin')).get('ports', {}))"
plugincenter -u "${USER_NAME}" list 2>/dev/null | grep -i "${PLUGIN}" || echo "    （plugincenter 不可用时跳过）"

echo ""
echo "✅ sysmon 部署完成。请完全退出米家 App / 桌面端后重进 → 应用市场 →「系统状态」"
echo "   重启自愈：${RESTORE_UNIT}（备份在 ${BACKUP_DIR}）"
echo "   本次备份：${LIST_FILE}.bak-${STAMP} / ${NGINX_CONF}.bak-${STAMP}"
