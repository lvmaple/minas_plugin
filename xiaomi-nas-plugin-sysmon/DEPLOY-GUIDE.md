# sysmon 部署到小米 NAS — 从零实操指南

面向场景：插件代码在 **Windows 电脑** `<PROJECT_DIR>\xiaomi-nas-plugin-sysmon\`，要装到**小米 NAS** 上，让米家 App 能打开「系统状态」。

---

## 你将需要

| 项目 | 说明 |
|---|---|
| 小米 NAS | 已联网、已绑定米家 App |
| SSH root 权限 | 这套流程的前提。没有 root 改不了 `/nas`、`/data/plugin`、nginx、systemd |
| Windows 本机 | 存放插件源码 |
| 你的数字 UID | 形如 `<NAS_UID>`（**无 u 前缀**） |

> ⚠️ 官方 plugincenter 安装通道有 RSA 验签，**不能**用自研包从 App「安装」按钮塞进去。  
> 这套教程走的是**手动部署**（直接落文件 + 注册），也就是笔记里验证过的路径。

---

## 第 0 步：搞清楚 3 个地址

### 0.1 NAS 的 IP

在米家 App → 小米 NAS → 设置 / 设备信息里看，或在路由器后台看。  
示例下文用 `192.168.1.50`，**改成你自己的**。

### 0.2 你的数字 UID

三种拿法（任选其一）：

1. **SSH 上去查**（最准）：
   ```bash
   ls /nas/pool0/
   # 目录名形如 u<NAS_UID>，取数字部分
   ls /data/plugin/
   # 会看到 u<NAS_UID>.list
   ```
2. **从 App 的插件 URL 看**：浏览器开发者工具 Network 里，插件请求形如  
   `https://NAS:443/plugin/<NAS_UID>/sysinfo/...` → UID 就是 `<NAS_UID>`
3. **查 plugincenter**：
   ```bash
   plugincenter list
   ```

### 0.3 SSH 登录

```bash
ssh root@192.168.1.50
# 密码 / 密钥按你平时的方式
```

小米 NAS 的 SSH 开启方式与型号相关（有的在「更多设置 → 开发者/SSH」，有的需要先开开发模式）。  
**如果 `ssh root@` 连不上，先解决 SSH/root 权限，否则后面全部做不了。**

---

## 第 1 步：把插件打包成 zip（Windows 上）

在 PowerShell 里执行：

```powershell
cd <PROJECT_DIR>
Compress-Archive -Path xiaomi-nas-plugin-sysmon -DestinationPath sysmon-plugin.zip -Force
# 得到 <PROJECT_DIR>\sysmon-plugin.zip
```

或者直接把整个 `xiaomi-nas-plugin-sysmon` 文件夹拷进 NAS 的 SMB 共享（见第 2 步）。

---

## 第 2 步：传到 NAS

### 方式 A：scp（推荐）

```powershell
cd <PROJECT_DIR>
scp sysmon-plugin.zip root@192.168.1.50:/tmp/
```

### 方式 B：SMB 网络共享

1. 文件资源管理器地址栏输入 `\\192.168.1.50`
2. 把 `sysmon-plugin.zip`（或整个文件夹）拖到 NAS 的共享目录（如 `/volume1/share` 或 `/nas/pool0/share`，按你的共享配置）
3. SSH 上去后从共享目录再挪到 `/tmp`

### 方式 C：插件目录直接上传

如果你有 root 且知道路径，也可直接：

```powershell
scp -r xiaomi-nas-plugin-sysmon root@192.168.1.50:/tmp/
```

---

## 第 3 步：SSH 上去部署

```bash
ssh root@192.168.1.50
```

### 3.1 解压

```bash
# 方式 A/B 用了 zip：
unzip -o /tmp/sysmon-plugin.zip -d /tmp/
cd /tmp/xiaomi-nas-plugin-sysmon

# 方式 C 直接拷文件夹：
cd /tmp/xiaomi-nas-plugin-sysmon
```

### 3.2 一键部署（替换 UID）

```bash
# 先确认 UID，再执行（示例 UID=<NAS_UID>）
sh deploy/deploy.sh <NAS_UID>
```

脚本会自动做完这 7 件事，并打印每步结果：

| 步骤 | 做什么 |
|---|---|
| 1 | 复制源码到 `/nas/pool0/u<UID>/plugin/pluginsrc/sysmon/` |
| 2 | 同步 `INFO` 到 `/home/u<UID>/plugin/sysmon/` |
| 3 | `ln -sfn` 建 www 软链 + 复制 300×300 图标 |
| 4 | **备份** `u<UID>.list` 后合并 sysmon 注册条目 |
| 5 | 写 systemd 单元并 `enable + start` |
| 6 | **备份** `plugincenter.conf` 后注入 Nginx location，`nginx -t && reload` |
| 7 | 打印验收结果 |

> 脚本里所有 `.list` / nginx 修改都会先 `cp` 出带时间戳的 `.bak-*`，出事可回滚。

### 3.3 如果只想手动做（不跑脚本）

照下面的顺序，命令一条条粘贴（`UID_NUM` 先 `export`）：

```bash
export UID_NUM=<NAS_UID>          # ← 改
export PLUGIN=sysmon
export SRC=/tmp/xiaomi-nas-plugin-sysmon
export DST=/nas/pool0/u${UID_NUM}/plugin/pluginsrc/${PLUGIN}

# 1 源码
mkdir -p "$DST"
cp -a "$SRC"/{INFO,files,scripts,ui} "$DST/"
chmod +x "$DST/scripts/control" "$DST/files/server.sh" "$DST/ui/${PLUGIN}.cgi"

# 2 INFO 第二份
mkdir -p /home/u${UID_NUM}/plugin/${PLUGIN}
cp "$DST/INFO" /home/u${UID_NUM}/plugin/${PLUGIN}/INFO

# 3 www 软链 + 图标
mkdir -p /data/plugin/www/u${UID_NUM} /data/plugin/www/icon
ln -sfn "$DST/ui" /data/plugin/www/u${UID_NUM}/${PLUGIN}
cp "$DST/ui/icon.png" /data/plugin/www/icon/${PLUGIN}.icon

# 4 .list 注册（先备份！）
cp /data/plugin/u${UID_NUM}.list /data/plugin/u${UID_NUM}.list.bak-$(date +%F)
# 然后把 deploy/list-entry.json 里的 "sysmon" 对象合并进 u<UID>.list
# （合并 JSON 建议用脚本里的 python 片段，避免手改弄坏原文件）

# 5 systemd
mkdir -p /tmp/${PLUGIN}
# 编辑 deploy/sysmon.service，把 <uid> 改成 $UID_NUM 后：
cp "$SRC/deploy/sysmon.service" /etc/systemd/system/${PLUGIN}.service
systemctl daemon-reload
systemctl enable ${PLUGIN}.service
systemctl start ${PLUGIN}.service

# 6 nginx（备份！规则要放在 location /plugin 之前）
cp /etc/nginx/conf.d/luci/plugincenter.conf \
   /etc/nginx/conf.d/luci/plugincenter.conf.bak-$(date +%F)
# 把 deploy/nginx-location.conf 的 location 粘贴进去（顶部）
nginx -t && nginx -s reload
```

---

## 第 4 步：逐项验收（SSH 里执行）

```bash
UID_NUM=<NAS_UID>   # ← 改

# ① 插件列表里能看到 sysmon
plugincenter -u u${UID_NUM} list | grep sysmon
# 期望：code=0，并出现 "plugin": "sysmon"

# ② 服务在跑
systemctl status sysmon.service --no-pager
# 期望：Active: active (running)

# ③ 后端通
curl -s http://127.0.0.1:9301/api/health
# 期望：{"code":0,...,"status":"running"}

# ④ 经 Nginx 通（注意路径无 u 前缀）
curl -sk https://127.0.0.1:443/plugin/${UID_NUM}/sysmon/api/info | head -c 200
# 期望：返回 JSON，含 hostname/cpu/memory

# ⑤ 文件都到位
ls -l /data/plugin/www/u${UID_NUM}/sysmon/     # 应是软链，指向 pluginsrc/ui
ls -l /data/plugin/www/icon/sysmon.icon        # 300x300 图标
```

---

## 第 5 步：在米家 App 里打开

1. **彻底杀掉米家 App**（从后台划掉，必要时再打开）
2. 进入 小米 NAS → **应用中心 / 应用市场**
3. 找到 **「系统状态」** 卡片（图标是橙色仪表盘）
4. 点开，应看到：CPU 环 + 内存/磁盘/负载 + 温度 + 风扇 + Docker

如果列表里没有 / 点开空白：

| 现象 | 先查什么 |
|---|---|
| 应用市场整个变空 | `.list` JSON 写坏了 → 恢复 `u<UID>.list.bak-*` |
| 有卡片但点开空白/404 | `www` 软链是否正确、`url` 字段是否为 `"/index.html"` |
| 有卡片但 403 | 走了 plugin.cgi 登录态 → 确认 Nginx `api/` 反代存在 |
| 图标不显示 | `/data/plugin/www/icon/sysmon.icon` 是否 300×300 |
| 服务没起 | `journalctl -u sysmon.service -n 50`、`cat /tmp/sysmon/server.log` |

---

## 回滚 / 卸载

```bash
# 服务
systemctl stop sysmon.service
systemctl disable sysmon.service
rm -f /etc/systemd/system/sysmon.service
systemctl daemon-reload

# 文件
rm -rf /nas/pool0/u${UID_NUM}/plugin/pluginsrc/sysmon
rm -rf /home/u${UID_NUM}/plugin/sysmon
rm -f  /data/plugin/www/u${UID_NUM}/sysmon      # 软链
rm -f  /data/plugin/www/icon/sysmon.icon

# .list / nginx 恢复备份
cp /data/plugin/u${UID_NUM}.list.bak-YYYY-MM-DD /data/plugin/u${UID_NUM}.list
cp /etc/nginx/conf.d/luci/plugincenter.conf.bak-YYYY-MM-DD \
   /etc/nginx/conf.d/luci/plugincenter.conf
nginx -t && nginx -s reload

# 然后杀掉米家 App 重进
```

---

## 常见疑问

**Q: 必须 root 吗？**  
A: 是。要写 `/nas/pool0/...`、`/data/plugin/...`、`/etc/nginx/...`、`/etc/systemd/system/...`，普通用户不够权限。

**Q: 固件升级后要重装吗？**  
A: 很可能。升级可能重置 nginx/systemd/.list。请保留本插件目录和所有 `.bak-*`，升级后重新跑 `deploy.sh`。

**Q: UID 打错了会怎样？**  
A: 插件会注册到别人的用户名下或找不到文件。先 `ls /nas/pool0/` 核对 `u<数字>` 再跑。

**Q: 能不能不改 nginx？**  
A: 可以只部署 UI + plugin.cgi（登录态访问 UI），但 `api/*` 要让 UI 读到数据，通常仍需一条 nginx 反代（场景 A）。用 `deploy.sh` 会帮你插好。
