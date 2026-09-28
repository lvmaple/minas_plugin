# sysmon — 小米 NAS「系统状态」插件

按《小米 NAS 插件开发规范｜手动部署实测总结》实现的完整示例插件。  
形态：**本地服务型**（Python HTTP API + Web UI + 注册条目）。

```
小米 NAS 插件 = 本地 HTTP 服务 + Web UI + 注册条目
```

## 目录结构

```
xiaomi-nas-plugin-sysmon/
├── INFO                        # 插件元数据（JSON）
├── files/
│   ├── httpd.py                # 后端 HTTP 服务（127.0.0.1:9301）
│   └── server.sh               # start/stop/status/restart
├── scripts/
│   └── control                 # 生命周期钩子
├── ui/
│   ├── index.html              # 仪表盘主页面
│   ├── sysmon.cgi              # plugin.cgi 入口脚本
│   ├── style.css
│   ├── app.js
│   └── icon.png                # 300×300 PNG
├── deploy/
│   ├── deploy.sh               # 一键部署（NAS 上 root 执行）
│   ├── sysmon.service          # systemd 单元
│   ├── nginx-location.conf     # Nginx 反代规则
│   └── list-entry.json         # .list 注册片段
└── README.md
```

## 功能

- CPU 使用率（环形仪表）+ 负载
- 内存 / 磁盘用量进度条
- 主机名、内核、运行时长
- **温度**：读取 `/sys/class/hwmon` 与 `thermal_zone`（CPU/NVMe/主板等），≥70°C 高亮
- **风扇**：读取各 hwmon 风扇 RPM
- **Docker 容器**：列表 + 运行/停止计数（`docker ps -a`，只读）
- 每 5 秒自动刷新
- 小米橙主题 UI

API 端点（经 Nginx / plugin.cgi 转发）：

| 路径 | 说明 |
|---|---|
| `api/info` | 完整状态 JSON（含 temps / fans / docker） |
| `api/temps` | 仅温度 + 风扇 |
| `api/docker` | 仅 Docker 容器 |
| `api/health` | 健康检查 |

## 手动部署步骤

> 在小米 NAS 上以 **root** 执行。所有路径中的 `<uid>` 替换为实际数字 UID（**无 u 前缀**）。

### 0. 前置检查

```bash
# 确认 UID（示例 <NAS_UID>）
whoami   # 应为 root
```

### 1. 复制源码

```bash
PLUGIN=sysmon
UID_NUM=<NAS_UID>   # ← 改成你的 UID

mkdir -p /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}
cp -a INFO files scripts ui /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/

chmod +x \
  /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/scripts/control \
  /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/files/server.sh \
  /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/ui/${PLUGIN}.cgi
```

### 2. 同步 INFO（两处路径必须一致）

```bash
mkdir -p /home/u${UID_NUM}/plugin/${PLUGIN}
cp /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/INFO \
   /home/u${UID_NUM}/plugin/${PLUGIN}/INFO
```

### 3. www 软链接 + 图标

```bash
mkdir -p /data/plugin/www/u${UID_NUM} /data/plugin/www/icon

ln -sfn /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/ui \
        /data/plugin/www/u${UID_NUM}/${PLUGIN}

cp /nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}/ui/icon.png \
   /data/plugin/www/icon/${PLUGIN}.icon
```

### 4. 注册 .list（先备份！）

```bash
cp /data/plugin/u${UID_NUM}.list /data/plugin/u${UID_NUM}.list.bak-$(date +%F)

# 将 deploy/list-entry.json 中的 "sysmon" 对象合并进 u<uid>.list
# 也可执行 deploy.sh 自动合并
```

**验证：**

```bash
plugincenter -u u${UID_NUM} list | grep sysmon
# 期望：code=0，且列表中出现 sysmon
```

### 5. 启动后端服务（systemd）

```bash
mkdir -p /tmp/sysmon

# 将 deploy/sysmon.service 中 <uid> 改成实际值后：
cp deploy/sysmon.service /etc/systemd/system/sysmon.service
systemctl daemon-reload
systemctl enable sysmon.service
systemctl start sysmon.service
systemctl status sysmon.service
```

### 6. Nginx 反代（**关键**）

编辑 `/etc/nginx/conf.d/luci/plugincenter.conf`：

```bash
cp /etc/nginx/conf.d/luci/plugincenter.conf \
   /etc/nginx/conf.d/luci/plugincenter.conf.bak-$(date +%F)
```

把 `deploy/nginx-location.conf` 中的 location **追加到文件顶部**（必须在 `location /plugin` 之前！），然后：

```bash
nginx -t && nginx -s reload
```

### 7. 验收清单

```bash
# ① 插件列表
plugincenter -u u${UID_NUM} list | grep sysmon

# ② 服务状态
systemctl status sysmon.service --no-pager

# ③ 后端健康
curl -s http://127.0.0.1:9301/api/health

# ④ 经 Nginx 访问（需登录态）
curl -k https://NAS:443/plugin/${UID_NUM}/sysmon/api/info

# ⑤ UI 加载
# 浏览器打开 https://NAS:443/plugin/${UID_NUM}/sysmon/index.html
# 或杀掉米家 App 重进，在应用市场点击「系统状态」
```

## 一键部署

```bash
sh deploy/deploy.sh <NAS_UID>
```

脚本会自动完成：复制源码 → INFO 同步 → www 软链/图标 → .list 合并（自动备份）→ systemd 启动 → Nginx 注入（自动备份）→ 验收输出。

## 常见坑对照

| 规范要求 | 本插件做法 |
|---|---|
| INFO `port` 必须是字符串 | `"port": "9301"` |
| INFO `type` 固定 standard | `"type": "standard"` |
| INFO `system` 自研 false | `"system": false` |
| id 避开官方 14/9x | `"id": 98` |
| `.list` 的 `resource.mpk` 指向 INFO | ✅ |
| `.list` 的 `url` 用 `"/index.html"` | ✅ |
| App URL **无 u 前缀** | Nginx 正则 `u?` 兼容两种 |
| Nginx location 在 `/plugin` 之前 | deploy.sh 自动插入到正确位置 |
| `sysmon.cgi` 可执行 | deploy.sh 自动 `chmod +x` |
| 图标 300×300 PNG | `ui/icon.png` |
| systemd 日志目录先 mkdir | deploy.sh `mkdir -p /tmp/sysmon` |
| 端口避开 9000/9100/9200 | 使用 **9301** |

## 升级 / 卸载

```bash
# 升级：覆盖 pluginsrc 后
sh scripts/control upgrade

# 停止
sh scripts/control stop

# 卸载清理
sh scripts/control uninstall
rm -rf /nas/pool0/u${UID_NUM}/plugin/pluginsrc/sysmon
rm -f /data/plugin/www/u${UID_NUM}/sysmon
rm -f /data/plugin/www/icon/sysmon.icon
# 从 u<uid>.list 中删除 "sysmon" 键后重启 App
```

## 安全说明

- 后端仅监听 `127.0.0.1:9301`，不对外暴露端口
- API 只读，无写操作、无命令执行
- 插件以 root 运行，请勿在代码中留后门或未鉴权写接口
- 固件升级可能重置 nginx/systemd/.list，请保留本仓库与备份文件

---

*示例插件，仅供学习与自研部署使用。官方 plugincenter 安装通道有 RSA 验签，不能用自研包绕过。*
