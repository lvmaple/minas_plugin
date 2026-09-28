#!/bin/sh
# dockerctl 一键部署脚本 — 在小米 NAS 上以 root 执行
# 用法: sh deploy.sh <uid>
# 示例: sh deploy.sh <NAS_UID>
#
# 重启持久化（三层保险，经验来自 sysmon 真机实测）：
#   ① 端口注册进 /etc/config/plugin 的 ports 表（系统开机只认表内插件）
#   ② 源码备份到 /data/backup/plugins/（/data 分区重启不丢）
#   ③ dockerctl-restore.service 开机自愈（恢复目录/软链/.list/unit 并启动服务）
set -e

UID_NUM="${1:-}"
case "$UID_NUM" in
    ''|*[!0-9]*) echo "UID 必须是数字" >&2; exit 2 ;;
esac
PLUGIN="dockerctl"
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
PORT="9302"
DESC="Docker 控制台：容器管理、镜像仓库、一键部署、存储卷与网络管理"
PKGSIZE="$(python3 -c "import json;print(json.load(open('${SRC_ROOT}/INFO'))['size'])" 2>/dev/null || echo 40000)"

echo "==> [1/8] 备份 .list / nginx 配置"
[ -f "$LIST_FILE" ] && cp "$LIST_FILE" "${LIST_FILE}.bak-${STAMP}" && echo "    → ${LIST_FILE}.bak-${STAMP}"
[ -f "$NGINX_CONF" ] && cp "$NGINX_CONF" "${NGINX_CONF}.bak-${STAMP}" && echo "    → ${NGINX_CONF}.bak-${STAMP}"

echo "==> [2/8] 部署插件目录（真机布局 src/{files,ui}）"
mkdir -p "${HOME_DIR}/src" "${HOME_DIR}/scripts"
cp -a "${SRC_ROOT}/INFO" "${HOME_DIR}/INFO"
# 先删旧目录再拷贝：防止 cp 合并语义让已删除的旧文件残留
rm -rf "${HOME_DIR}/src/files" "${HOME_DIR}/src/ui"
cp -a "${SRC_ROOT}/src/files" "${SRC_ROOT}/src/ui" "${HOME_DIR}/src/"
cp -a "${SRC_ROOT}/scripts/control" "${HOME_DIR}/scripts/control"
cp -a "${SRC_ROOT}/scripts/skopeo-container" "${HOME_DIR}/scripts/skopeo-container"
chmod +x "${HOME_DIR}/scripts/control" "${HOME_DIR}/scripts/skopeo-container" "${HOME_DIR}/src/files/server.sh" "${HOME_DIR}/src/ui/${PLUGIN}.cgi"
mkdir -p "${HOME_DIR}/etc" "${HOME_DIR}/tmp"
chown -R "${USER_NAME}:${USER_NAME}" "${HOME_DIR}" 2>/dev/null || true

echo "==> [3/8] www 软链 + 图标"
mkdir -p "$WWW_DIR" "/data/plugin/www/icon"
ln -sfn "${HOME_DIR}/src/ui" "${WWW_DIR}/${PLUGIN}"
cp "${HOME_DIR}/src/ui/icon.png" "$ICON_DST"
chown "${USER_NAME}:${USER_NAME}" "$ICON_DST" 2>/dev/null || true

echo "==> [4/8] 注册 .list 条目"
python3 - "$LIST_FILE" "$PLUGIN" "$UID_NUM" "$PORT" "${BACKUP_DIR}" "$DESC" "$PKGSIZE" <<'PY'
import json, time, sys, os
path, plugin, uid, port, backup_dir, desc, size = sys.argv[1:8]
home = f"/home/u{uid}/plugin/{plugin}"
data = {}
if os.path.isfile(path):
    with open(path) as f:
        data = json.load(f)
entry = {
    "resource": {"mpk": f"{home}/INFO", "icon": f"/icon/{plugin}.icon", "preview": []},
    "status": "running", "install": True, "upgrade": False, "enable": True,
    "changetime": int(time.time()), "icon": f"/icon/{plugin}.icon", "progress": "100",
    "frontend": {
        "title": "Docker 控制台", "desc": desc,
        "icon": f"/{plugin}.icon", "type": "url", "dev_type": [1, 2, 3, 4],
        "url": [
            {"dev_type": [1], "url": "/index.html#/"},
            {"dev_type": [2, 3, 4], "url": "/index.html#/"}
        ],
        "sortid": 40, "widget": []
    },
    "info": {
        "plugin": plugin, "name": "Docker 控制台", "id": 97, "version": "1.0.0",
        "tags": ["tool"], "timestamp": int(time.time()),
        "desc": desc, "developer": "minas", "publisher": "minas",
        "changelog": "1.0.0 完整 Docker 控制台：容器/镜像/部署/存储/网络",
        "system": False, "size": int(size), "port": port,
        "type": "standard", "forceupgrade": False, "ext": {}
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

echo "==> [5/8] 启动服务（systemd；先停旧进程，确保加载新代码）"
sh "${HOME_DIR}/scripts/control" stop 2>/dev/null || true
if [ -d "/tmp/${PLUGIN}/compose" ]; then
    mkdir -p "${BACKUP_DIR}/compose"
    for old_project in "/tmp/${PLUGIN}/compose/"*; do
        [ -d "$old_project" ] || continue
        new_project="${BACKUP_DIR}/compose/${old_project##*/}"
        if [ ! -e "$new_project" ]; then
            cp -a "$old_project" "$new_project"
            echo "    已迁移 Compose 项目 ${old_project##*/}"
        fi
    done
fi
PLUG_SRC_DIR="${HOME_DIR}/src" PLUG_HOME_DIR="${HOME_DIR}" PLUG_USER="${USER_NAME}" \
  PLUG_PORT_ALLOC="${PORT}" sh "${HOME_DIR}/scripts/control" postinstall
sleep 2
curl -s --max-time 3 "http://127.0.0.1:${PORT}/api/health" || { echo "    ⚠️ 后端未响应，查看 /tmp/${PLUGIN}/server.log"; exit 1; }
echo ""

echo "==> [6/8] 注入 Nginx 反代规则"
# 不带 CORS 头（后端仅监听 127.0.0.1 + 同源访问，见 README 安全说明）
if [ -f "$NGINX_CONF" ]; then
  if ! grep -q "${PLUGIN}/api/" "$NGINX_CONF"; then
    python3 - "$NGINX_CONF" "$UID_NUM" "$PORT" "$PLUGIN" <<'PY'
import sys
path, uid, port, plugin = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
block = f"""
# === {plugin} (auto-added) ===
location ~ ^/plugin/u?{uid}/{plugin}/api/ {{
  rewrite ^/plugin/u?{uid}/{plugin}/(api/.*)$ /$1 break;
  proxy_pass http://127.0.0.1:{port};
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  client_max_body_size 16m;
  proxy_read_timeout 330s;
  proxy_send_timeout 330s;
  add_header Cache-Control "no-store";
}}
# === /{plugin} ===
"""
with open(path) as f:
    lines = f.readlines()
out, inserted = [], False
for line in lines:
    if not inserted and "location /plugin" in line:
        out.append(block)   # 必须在 location /plugin 前缀之前
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
    echo "    ${PLUGIN} 规则已存在，跳过（如修改过请 nginx -t && nginx -s reload）"
  fi
else
  echo "    ⚠️ 未找到 $NGINX_CONF，请手动添加 ${PLUGIN}/api/ 反代（proxy_pass http://127.0.0.1:${PORT}）"
fi

echo "==> [7/8] 重启持久化三层保险"
echo "  ① 注册端口到 /etc/config/plugin ports 表"
python3 - "$UID_NUM" "$PORT" "$PLUGIN" <<'PY'
import json, sys
uid, port, plugin = sys.argv[1], int(sys.argv[2]), sys.argv[3]
for p in ["/etc/config/plugin", "/data/etc/upper/config/plugin"]:
    try:
        cfg = json.load(open(p))
    except FileNotFoundError:
        print(f"    跳过（不存在）: {p}")
        continue
    cfg.setdefault("ports", {})[plugin] = {f"u{uid}": port}
    json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=1)
    print(f"    已注册 {plugin} → {p}")
PY

echo "  ② 源码备份到 ${BACKUP_DIR}（/data 分区重启不丢）"
mkdir -p "${BACKUP_DIR}"
rm -rf "${BACKUP_DIR}/${PLUGIN}"
cp -a "${HOME_DIR}" "${BACKUP_DIR}/${PLUGIN}"

echo "  ③ 安装开机自愈服务 ${PLUGIN}-restore.service"
cat > "${BACKUP_DIR}/restore.sh" <<'RST'
#!/bin/sh
# dockerctl 开机自愈：系统重启会清掉手动插件目录与 .list，从 /data 备份恢复
UID_N=__DEPLOY_UID__
PLUGIN=dockerctl
PORT=9302
USER_NAME=u${UID_N}
HOME_DIR=/home/${USER_NAME}/plugin/${PLUGIN}
WWW_DIR=/data/plugin/www/${USER_NAME}
LIST_FILE=/data/plugin/${USER_NAME}.list
NGINX_CONF=/etc/nginx/conf.d/luci/plugincenter.conf
BACKUP_DIR=/data/backup/plugins/${PLUGIN}
SRC=${BACKUP_DIR}/${PLUGIN}

# 1) 插件目录（存在则跳过）
if [ ! -f "${HOME_DIR}/INFO" ] && [ -f "${SRC}/INFO" ]; then
    mkdir -p /home/${USER_NAME}/plugin
    cp -a "${SRC}" "${HOME_DIR}"
    chmod +x "${HOME_DIR}/scripts/control" "${HOME_DIR}/scripts/skopeo-container" "${HOME_DIR}/src/files/server.sh" "${HOME_DIR}/src/ui/${PLUGIN}.cgi"
    chown -R "${USER_NAME}:${USER_NAME}" "${HOME_DIR}" 2>/dev/null || true
    echo "restore: 插件目录已恢复"
fi

# 2) www 软链 + 图标
mkdir -p "${WWW_DIR}" /data/plugin/www/icon
ln -sfn "${HOME_DIR}/src/ui" "${WWW_DIR}/${PLUGIN}"
cp "${HOME_DIR}/src/ui/icon.png" "/data/plugin/www/icon/${PLUGIN}.icon" 2>/dev/null || true
chown "${USER_NAME}:${USER_NAME}" "/data/plugin/www/icon/${PLUGIN}.icon" 2>/dev/null || true

# 3) 合并 .list（用备份的注册条目）
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

# 4) systemd unit + 启动服务
if [ -f "${HOME_DIR}/scripts/control" ]; then
    PLUG_SRC_DIR="${HOME_DIR}/src" PLUG_HOME_DIR="${HOME_DIR}" PLUG_USER="${USER_NAME}" \
      PLUG_PORT_ALLOC="${PORT}" sh "${HOME_DIR}/scripts/control" postinstall
fi

# 5) nginx 反代自愈（幂等；nginx 未就绪则稍候重试）
if [ -f "$NGINX_CONF" ] && ! grep -q "${PLUGIN}/api/" "$NGINX_CONF"; then
    sleep 3
    python3 - "$NGINX_CONF" "$UID_N" "$PORT" "$PLUGIN" <<'PY3'
import sys
path, uid, port, plugin = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
block = f"""
# === {plugin} (auto-added) ===
location ~ ^/plugin/u?{uid}/{plugin}/api/ {{
  rewrite ^/plugin/u?{uid}/{plugin}/(api/.*)$ /$1 break;
  proxy_pass http://127.0.0.1:{port};
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  client_max_body_size 16m;
  proxy_read_timeout 330s;
  proxy_send_timeout 330s;
  add_header Cache-Control "no-store";
}}
# === /{plugin} ===
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
sed -i "s/^UID_N=__DEPLOY_UID__$/UID_N=${UID_NUM}/" "${BACKUP_DIR}/restore.sh"
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
python3 -c "import json;print('    ports 表已含 ${PLUGIN}:', '${PLUGIN}' in json.load(open('/etc/config/plugin')).get('ports', {}))"
plugincenter -u "${USER_NAME}" list 2>/dev/null | grep -i "${PLUGIN}" || echo "    （plugincenter 不可用时跳过）"

echo ""
echo "✅ 部署完成。请完全退出米家 App / 桌面端后重进 → 应用市场 →「Docker 控制台」"
echo "   重启自愈：${RESTORE_UNIT}（备份在 ${BACKUP_DIR}）"
echo "   本次备份：${LIST_FILE}.bak-${STAMP} / ${NGINX_CONF}.bak-${STAMP}"
