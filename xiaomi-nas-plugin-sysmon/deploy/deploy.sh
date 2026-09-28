#!/bin/sh
# sysmon 一键部署脚本（在小米 NAS 上以 root 执行）
# 用法: sh deploy.sh <uid>
# 示例: sh deploy.sh <NAS_UID>
set -e

UID_NUM="${1:-}"
if [ -z "$UID_NUM" ]; then
  echo "Usage: sh deploy.sh <uid>"
  echo "  <uid> 为米家 App 用户的数字 UID（无 u 前缀）"
  exit 1
fi
case "$UID_NUM" in
  *[!0-9]*) echo "UID 必须是数字" >&2; exit 2 ;;
esac

PLUGIN="sysmon"
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGINSRC="/nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}"
WWW_LINK="/data/plugin/www/u${UID_NUM}/${PLUGIN}"
ICON_DST="/data/plugin/www/icon/${PLUGIN}.icon"
LIST_FILE="/data/plugin/u${UID_NUM}.list"
LIST_BAK="${LIST_FILE}.bak-$(date +%F-%H%M%S)"
NGINX_CONF="/etc/nginx/conf.d/luci/plugincenter.conf"
NGINX_BAK="${NGINX_CONF}.bak-$(date +%F-%H%M%S)"
SYSTEMD_UNIT="/etc/systemd/system/${PLUGIN}.service"
PORT="9301"

echo "==> 1/7 复制插件源码到 pluginsrc"
mkdir -p "$PLUGINSRC"
cp -a "${SRC_ROOT}/INFO" "${SRC_ROOT}/files" "${SRC_ROOT}/scripts" "${SRC_ROOT}/ui" "$PLUGINSRC/"
chmod +x "${PLUGINSRC}/scripts/control" "${PLUGINSRC}/files/server.sh" "${PLUGINSRC}/ui/${PLUGIN}.cgi"

echo "==> 2/7 同步 INFO 到 /home/u${UID_NUM}/plugin/"
mkdir -p "/home/u${UID_NUM}/plugin/${PLUGIN}"
cp "${PLUGINSRC}/INFO" "/home/u${UID_NUM}/plugin/${PLUGIN}/INFO"

echo "==> 3/7 创建 www 软链接 + 图标"
mkdir -p "/data/plugin/www/u${UID_NUM}" "/data/plugin/www/icon"
ln -sfn "${PLUGINSRC}/ui" "$WWW_LINK"
cp "${PLUGINSRC}/ui/icon.png" "$ICON_DST"

echo "==> 4/7 注册 .list 条目（先备份）"
if [ -f "$LIST_FILE" ]; then
  cp "$LIST_FILE" "$LIST_BAK"
  echo "    已备份 → $LIST_BAK"
fi
# 用 Python 合并 JSON，避免手写破坏原文件
python3 - "$LIST_FILE" "$PLUGIN" "$UID_NUM" <<'PY'
import json, sys
path, plugin, uid = sys.argv[1], sys.argv[2], sys.argv[3]
entry = {
  "status": "running", "install": True, "upgrade": False, "enable": True,
  "changetime": 1758900000, "icon": f"/icon/{plugin}.icon", "progress": "100",
  "resource": {
    "mpk": f"/nas/pool0/u{uid}/plugin/pluginsrc/{plugin}/INFO",
    "icon": f"/icon/{plugin}.icon", "preview": []
  },
  "info": {
    "plugin": plugin, "name": "系统状态", "id": 98, "version": "1.0.0",
    "tags": ["tool"], "timestamp": 1758900000,
    "desc": "查看 NAS CPU、内存、磁盘与运行状态",
    "developer": "YourName", "publisher": "YourTeam",
    "changelog": "1.0.0 首个版本：系统状态仪表盘",
    "system": False, "size": 20000, "port": "9301",
    "type": "standard", "forceupgrade": False, "ext": {}
  },
  "frontend": {
    "title": "系统状态", "desc": "查看 NAS CPU、内存、磁盘与运行状态",
    "icon": f"/{plugin}.icon", "type": "url", "dev_type": [1, 2, 3, 4],
    "url": {"dev_type": [1, 2, 3, 4], "url": "/index.html"},
    "sortid": 20, "widget": []
  }
}
data = {}
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    pass
data[plugin] = entry
with open(path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"    已写入 {path}[{plugin}]")
PY

echo "==> 5/7 写入 systemd 单元并启动"
cat > "$SYSTEMD_UNIT" <<UNIT
[Unit]
Description=${PLUGIN} service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${PLUGINSRC}/files/httpd.py
Restart=on-failure
RestartSec=5
Environment=PLUG_PORT=${PORT}
StandardOutput=append:/tmp/${PLUGIN}/server.log
StandardError=append:/tmp/${PLUGIN}/server.log

[Install]
WantedBy=multi-user.target
UNIT
mkdir -p "/tmp/${PLUGIN}"
systemctl daemon-reload
systemctl enable "${PLUGIN}.service"
systemctl restart "${PLUGIN}.service"

echo "==> 6/7 注入 Nginx 反代规则（备份后追加到文件顶部区域）"
if [ -f "$NGINX_CONF" ]; then
  cp "$NGINX_CONF" "$NGINX_BAK"
  echo "    已备份 → $NGINX_BAK"
  # 若尚未包含 sysmon 规则则插入
  if ! grep -q "sysmon/api/" "$NGINX_CONF"; then
    python3 - "$NGINX_CONF" "$UID_NUM" <<'PY'
import sys
path, uid = sys.argv[1:3]
snippet = open("/tmp/.sysmon_nginx_snippet", "w")
block = """
# === sysmon (auto-added) ===
location ~ ^/plugin/u?__NAS_UID__/sysmon/api/ {
  rewrite ^/plugin/u?__NAS_UID__/sysmon/(api/.*)$ /$1 break;
  proxy_pass http://127.0.0.1:9301;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  add_header Access-Control-Allow-Origin "*" always;
  add_header Cache-Control "no-store";
}
# === /sysmon ===
"""
block = block.replace("__NAS_UID__", uid)
# 在第一个 server { 之后、location /plugin 之前插入
with open(path) as f:
    lines = f.readlines()
out = []
inserted = False
for i, line in enumerate(lines):
    out.append(line)
    if not inserted and "location /plugin" in line:
        out.insert(len(out) - 1, block)
        inserted = True
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
else
  echo "    ⚠️ 未找到 $NGINX_CONF，请手动合并 deploy/nginx-location.conf"
fi

echo "==> 7/7 验收"
echo "--- plugincenter list ---"
plugincenter -u "u${UID_NUM}" list 2>/dev/null | grep -A2 "${PLUGIN}" || echo "（plugincenter 不可用时跳过）"
echo "--- systemctl ---"
systemctl status "${PLUGIN}.service" --no-pager | head -8 || true
echo "--- curl ---"
curl -sk "http://127.0.0.1:${PORT}/api/health" || true
echo ""
echo "✅ 部署完成。请杀掉米家 App 重进，在应用市场查看「系统状态」。"
echo "   备份文件：$LIST_BAK / $NGINX_BAK"
