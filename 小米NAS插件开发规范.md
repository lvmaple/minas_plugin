# 小米 NAS 插件开发规范｜手动部署实测总结

> 基于 sysinfo 自研插件 + Portainer 插件化部署真机实测整理  
> 来源：小红书 @Jray · 2025-09-02  
> **实战补充**：sysmon 插件在小米智能存储（<nas-hostname> / aarch64 / Python 3.12）真机部署验证，含桌面端适配  
> ⚠️ 官方安装通道无法绕过 RSA 验签，本规范适用于**手动部署**路径  
> ⚠️ 不同固件版本后续可能存在差异，修改 /etc、Nginx、systemd、plugincenter 配置前务必做好备份  
> ⚠️ **下文标注「真机实测」的条目与小红书原笔记有差异，以真机为准**

---

## 1. 插件是什么

**一句话定义：小米 NAS 插件 = 本地 HTTP 服务 + Web UI + 注册条目**

本质上是本地服务、前端页面和注册信息的组合，在米家 App 的应用市场中展示，点击后通过内嵌 WebView 加载。

### 整体链路

```
米家 App（应用市场/插件页）
  → https://NAS:443/plugin/<uid>/<pluginid>/...
  → nginx（统一转发入口）
  → plugin.cgi 或自定义 nginx 反代
  → 插件本地服务（自选端口）/ 静态 UI 文件
```

- HTTPS：mTLS + 登录态
- 入口与展示由米家 App 负责，真正插件内容在 NAS 本地

### 两种插件形态

|     | A. 本地服务型           | B. 静态 UI 型        |
| --- | ------------------ | ----------------- |
| 服务  | 插件自己启动 HTTP 服务     | 纯前端 HTML/JS 文件    |
| 语言  | Python / Go / Node | —                 |
| 网关  | nginx 反代 API       | 经 plugin.cgi 提供页面 |
| 适用  | 有后端逻辑、采集、控制        | 纯展示、跳转、简单页面       |

### 三个关键词（缺一不可）

1. **服务** — 本地 HTTP 服务，提供真实能力
2. **Web UI** — 前端页面，提供用户体验
3. **注册条目** — 在应用市场中被发现与加载

---

## 2. 目录结构规范

### 真机实测目录（小米智能存储 — 以此为准）

根路径：`/home/u<uid>/plugin/<plugin>/`（**不是** `pluginsrc`）

```
/home/u<uid>/plugin/sysmon/
├── INFO                            # 插件元数据（JSON）
├── src/
│   ├── files/
│   │   ├── httpd.py                # 后端 HTTP 服务
│   │   └── server.sh               # 服务控制脚本（start/stop/status）
│   └── ui/
│       ├── index.html              # 主页面
│       ├── sysmon.cgi              # plugin.cgi 入口脚本（必需）
│       ├── config                  # 前端配置（title/desc/url/sortid）
│       ├── icon.png                # 图标源文件 300×300
│       ├── style.css
│       ├── app.js
│       └── ...
├── scripts/
│   └── control                     # 生命周期钩子脚本（必需）
├── etc/                            # 配置/数据（可选）
├── var/                            # 运行时数据（可选）
└── tmp/                            # 临时文件（可选）
```

www 软链指向 `src/ui`（不是插件根目录）：

```
/data/plugin/www/u<uid>/sysmon → /home/u<uid>/plugin/sysmon/src/ui
```

### 笔记中的目录（sysinfo 范本 — 备查）

```
/nas/pool0/u<uid>/plugin/pluginsrc/<plugin>/
```

### 必须存在的关键文件

| 文件                    | 作用                                    |
| --------------------- | ------------------------------------- |
| `INFO`                | 插件元数据，App 显示与行为依据                     |
| `src/ui/<plugin>.cgi` | plugin.cgi 服务 UI 的入口脚本                |
| `src/ui/index.html`   | 主页面（App 默认入口）                         |
| `src/ui/config`       | 前端配置（title/desc/url/sortid）— **真机必需** |
| `scripts/control`     | 生命周期钩子脚本（必需）                          |

### 注意事项

- **目录名 `<plugin>` 必须与 `INFO.plugin` 保持一致**（如 `sysmon`）
- 所有脚本建议使用 **UTF-8 编码，Linux 换行（LF）**；PowerShell 生成的文件注意去掉 `\r`
- 脚本文件需 `chmod +x`（control、cgi、server.sh）
- 文件属主建议 `chown -R u<uid>:u<uid>

---

## 3. INFO 文件格式（JSON）

插件元数据，必须符合规范。同一份 INFO 需要放在**两个位置**：

```
/nas/pool0/u<uid>/plugin/pluginsrc/<plugin>/INFO
/home/u<uid>/plugin/<plugin>/INFO
```

### INFO 标准格式（以 sysinfo 为例）

```json
{
  "plugin": "sysinfo",
  "name": "设备信息",
  "id": 99,
  "version": "1.0.0",
  "tags": ["tool"],
  "timestamp": <UNIX_TIMESTAMP>,
  "desc": "插件描述",
  "developer": "Jray",
  "publisher": "jrp",
  "changelog": "版本说明",
  "system": false,
  "size": 10000,
  "port": "9300",
  "type": "standard",
  "forceupgrade": false,
  "ext": {}
}
```

### 关键字段说明

| 字段          | 说明         | 约束                   |
| ----------- | ---------- | -------------------- |
| `plugin`    | 插件 ID（目录名） | 与目录名一致               |
| `name`      | 显示名称       | App 中展示              |
| `id`        | 数字 ID      | 自研建议 ≥98（避开官方 14/9x） |
| `port`      | 本地服务端口     | **必须是字符串**，不是数字      |
| `type`      | 类型         | **必须为 `"standard"`** |
| `system`    | 是否系统插件     | 自研**必须为 `false`**    |
| `ext`       | 扩展字段       | 保留为空对象 `{}`          |
| `timestamp` | 时间戳        | 秒级，可能用于排序/更新         |

### 最小可用示例

```json
{
  "plugin": "sysinfo",
  "name": "设备信息",
  "id": 99,
  "version": "1.0.0",
  "port": "9300",
  "type": "standard",
  "system": false,
  "ext": {}
}
```

### 注意事项

- 端口是**字符串**，不是数字（`"9300"` ✅，`9300` ❌）
- 所有字段名必须**小写**
- **JSON 不能有注释（//）**
- 修改后需重启服务生效
- INFO 文件必须是合法 JSON，且**两处路径的内容保持一致**

### 建议与最佳实践

- 每次发布新版本，更新 `version` 与 `changelog`
- `timestamp` 使用当前时间戳（秒级）
- `id` 避开官方占用（官方：14/9x），避免冲突
- 填写完整信息，便于在 App 中展示专业度
- `ext` 预留未来扩展字段，不建议随意定义

---

## 4. UI 文件部署规范（www 链接）

前端资源必须放到 www 目录并正确链接。App 通过固定路径访问 UI 文件：

```
/data/plugin/www/u<uid>/<plugin>/...
```

需要将插件的 `ui/` 目录链接到该位置，并将图标放到 `icon/` 目录。

### 目标目录结构

```
/data/plugin/www/
├── u<uid>/
│   └── <plugin>/ → symlink
│       ├── index.html
│       ├── <plugin>.cgi
│       └── icon.png
└── icon/
    └── <plugin>.icon    # 300×300 PNG
```

### 部署命令（真机实测版，以 sysmon / UID <uid> 为例）

```bash
# 1. 创建 www 链接（指向 src/ui，不是插件根目录）
ln -sfn /home/u<uid>/plugin/sysmon/src/ui \
        /data/plugin/www/u<uid>/sysmon

# 2. 复制图标到 icon 目录（并重命名为 .icon）
cp /home/u<uid>/plugin/sysmon/src/ui/icon.png \
   /data/plugin/www/icon/sysmon.icon

# 3. 设置属主
chown -R u<uid>:u<uid> /data/plugin/www/u<uid> /data/plugin/www/icon/sysmon.icon
```

- `ln -sfn` 会强制更新软链接，确保指向最新的 ui 目录
- App 读取图标时会去 `/icon/` 目录，文件名必须是 `<plugin>.icon`

### 图标规格要求

| 项目   | 要求                       |
| ---- | ------------------------ |
| 尺寸   | 300 × 300 像素             |
| 格式   | PNG（RGBA）                |
| 背景   | 建议透明或纯色                  |
| 文件名  | `<plugin>.icon`          |
| 放置路径 | `/data/plugin/www/icon/` |

> 图标尺寸不符合规范（如 162×162）可能导致 App 中显示异常或被判定为不合格。

### 验证检查清单

- [ ] `/data/plugin/www/u<uid>/<plugin>/` 是软链接
- [ ] 链接指向源码 ui 目录下
- [ ] `/data/plugin/www/icon/<plugin>.icon` 存在
- [ ] 图标尺寸为 300×300
- [ ] `index.html` 可直接通过浏览器打开（需登录态）
- [ ] 图标在 App 中正常显示

> www 链接路径错误 → App 点击插件会空白或 404  
> 图标文件名错误或放错目录 → 插件图标不显示  
> 修改后建议 `nginx -t && nginx -s reload`

---

## 5. .list 注册条目规范（核心配置）

**App 能否显示插件，取决于 `u<uid>.list` 配置是否正确。**

### .list 文件位置

```
/data/plugin/u<uid>.list    # JSON 格式
```

每个插件在 .list 中是一个 key。修改前务必备份，避免导致整个应用市场异常！

### .list 结构说明

| 字段                              | 作用                  |
| ------------------------------- | ------------------- |
| `status` / `install` / `enable` | 状态控制                |
| `resource`                      | 资源路径（mpk 必须指向 INFO） |
| `info`                          | 插件完整信息（**必须完整！**）   |
| `frontend`                      | 前端展示配置（决定 App 如何打开） |

### .list 标准示例（真机实测，以 sysmon 为例）

> **关键差异**：`frontend.url` 是**数组**，每个元素按 `dev_type` 分别指定 URL；URL **必须带 hash 路由**（`#/`）。

```json
{
  "sysmon": {
    "status": "running",
    "install": true,
    "upgrade": false,
    "enable": true,
    "changetime": <UNIX_TIMESTAMP>,
    "icon": "/icon/sysmon.icon",
    "progress": "100",
    "resource": {
      "mpk": "/home/u<uid>/plugin/sysmon/INFO",
      "icon": "/icon/sysmon.icon",
      "preview": []
    },
    "info": {
      "plugin": "sysmon",
      "name": "系统状态",
      "id": 98,
      "version": "1.0.0",
      "tags": ["tool"],
      "timestamp": <UNIX_TIMESTAMP>,
      "desc": "查看 NAS CPU、内存、磁盘、温度、风扇与 Docker 容器状态",
      "developer": "YourName",
      "publisher": "minas",
      "changelog": "1.0.0 首个版本",
      "system": false,
      "size": 20000,
      "port": "9301",
      "type": "standard",
      "forceupgrade": false,
      "ext": {}
    },
    "frontend": {
      "title": "系统状态",
      "desc": "查看 NAS CPU、内存、磁盘、温度、风扇与 Docker 容器状态",
      "icon": "/sysmon.icon",
      "type": "url",
      "dev_type": [1, 2, 3, 4],
      "url": [
        { "dev_type": [1],     "url": "/index.html#/" },
        { "dev_type": [2, 3, 4], "url": "/index.html#/" }
      ],
      "sortid": 30,
      "widget": []
    }
  }
}
```

**官方插件的 url 数组范例**（baidupan，分手机/桌面两套路由）：

```json
"url": [
  { "dev_type": [1],     "url": "/index.html#/baiduNetdisk_app" },
  { "dev_type": [2, 3, 4], "url": "/index.html#/baiduNetdisk_pc" }
]
```

xunlei 的写法（全端一个入口 + hash）：

```json
"url": [
  { "dev_type": [1, 2, 3, 4], "url": "/xunlei.cgi#/home" }
]
```

### 致命坑（必看！）

| 错误                 | 后果                           |
| ------------------ | ---------------------------- |
| `info` 对象缺失        | **整个应用市场变空**（不只是该插件不显示）      |
| `resource.mpk` 为空串 | 同样导致列表异常，必须指向本地 INFO 文件      |
| `url` 字段设为 `"./"`  | App 会拼出 `<id>./` 畸形路径，导致 404 |
| 修改后未验证             | 插件可能不生效或列表异常                 |

### 正确做法

```bash
# 1. 备份 .list 文件
cp u<uid>.list u<uid>.list.bak

# 2. 按规范完整填写所有字段

# 3. 保存后验证插件列表是否正常
plugincenter -u u<uid> list | grep sysinfo
```

返回结果包含新插件，`code=0` 表示列表配置正确。

> 建议插件 ID 使用高位数字（如 98/99），避免与官方系统插件冲突。

---

## 6. App URL 拼接规则（关键差异）

理解 App 的访问路径，避免 URL 拼接错误。

### 前缀规则：无 u 前缀

**实际请求路径（正确）：**

```
https://NAS:443/plugin/<NAS_UID>/<plugin>/...
```

**不是下面这种（错误）：**

```
https://NAS:443/plugin/u<NAS_UID>/<plugin>/...
```

> App 在拼接时不会带上 `u` 前缀。Nginx 反代规则必须兼容两种前缀：`u?<NAS_UID>`。

### 请求路径构成

```
https://NAS:443/plugin/ + <NAS_UID> + /sysinfo + /index.html
     固定路径              UID       插件ID      url字段
                          (数字)    (目录名)    (相对路径)
```

### url 字段拼接规则

| 写法       | 示例                     | App 实际访问路径                                  | 结果                           |
| -------- | ---------------------- | ------------------------------------------- | ---------------------------- |
| **推荐**   | `"/index.html#/"`      | `/plugin/<uid>/sysmon/index.html#/`      | ✅ 手机+桌面                      |
| 可选       | `"/index.html#/route"` | `/plugin/<uid>/sysmon/index.html#/route` | ✅ SPA 路由                     |
| 可选       | `""`（空串）               | `/plugin/<uid>/sysmon/`                  | ✅ 手机可能 OK                    |
| **错误**   | `"./"`                 | `/plugin/<uid>/sysmon./`                 | ❌ 畸形路径                       |
| **真机踩坑** | `"/index.html"`        | `/plugin/<uid>/sysmon/index.html`        | ⚠️ 手机能开，**桌面端 Electron 打不开** |

**真机结论：URL 必须带 hash 路由（`#/`），否则桌面端 App（Electron）无法加载。**

### Nginx 反代需兼容两种前缀（推荐正则）

```nginx
# 推荐正则：兼容 /plugin/<NAS_UID>/... 和 /plugin/u<NAS_UID>/...
location ~ ^/plugin/u?<NAS_UID>/sysinfo/?.?/?(.*)$ {
  proxy_pass http://127.0.0.1:9300/$1;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";  # WebSocket 支持
  proxy_read_timeout 300s;
}
```

### 匹配效果示例

| 请求路径                               | 结果                   |
| ---------------------------------- | -------------------- |
| `/plugin/<NAS_UID>/sysinfo/`         | ✅ 正配                 |
| `/plugin/u<NAS_UID>/sysinfo/`        | ✅ 正配                 |
| `/plugin/<NAS_UID>/sysinfo/api/info` | ✅ 匹配（捕获 $1=api/info） |
| `/plugin/u<NAS_UID>/sysinfo/./`      | ✅ 匹配（捕获 $1=空）        |
| `/plugin/<NAS_UID>/others/`          | ❌ 不匹配                |

### 常见问题

**Q: 为什么点击插件后是 404 或空白？**  
A: 检查 url 字段是否为 `"/index.html"` 或空串，并确认 nginx 反代或 www 文件部署正确。

**Q: 为什么 Nginx 反代后 API 能用，但 UI 403？**  
A: UI 静态文件经 plugin.cgi 会要求登录态，需使用 alias/反代劫持绕过鉴权（见第 8 节）。

---

## 7. plugin.cgi 入口脚本规范（静态服务型）

plugin.cgi 负责加载 UI 文件，并注入环境变量给入口脚本。

### 作用说明

plugin.cgi（NAS 原生 FastCGI 二进制）会：

1. 验证 NAS 登录态（无效 token → 403）
2. 注入环境变量（FILE_URI / FILE_REQ 等）
3. 执行 `<plugin>.cgi` 脚本并返回响应

### 环境变量（关键）

| 变量名              | 说明                                    |
| ---------------- | ------------------------------------- |
| `FILE_URI`       | 相对路径（如 `api/info`、`css/style.css`）    |
| `FILE_REQ`       | 绝对路径（`/data/plugin/www/.../文件` 的完整路径） |
| `REQUEST_METHOD` | HTTP 方法（GET、POST 等）                   |
| `QUERY_STRING`   | 查询参数（不含 `?`）                          |

> 只要通过 `/plugin/<uid>/<plugin>/...` 访问，都会经过 plugin.cgi。

### 请求处理流程

```
米家 App 访问插件 URL
  → HTTPS (443)
  → Nginx（反代 /plugin 路由）
  → plugin.cgi（FastCGI 二进制）
  → <plugin>.cgi（入口脚本）
  → 静态文件 / 本地服务（返回内容）
```

静态服务型必须提供 `<plugin>.cgi`，才能让 plugin.cgi 正确处理请求并返回 UI 文件或 API。

### `<plugin>.cgi` 脚本模板

```sh
#!/bin/sh
# 入口脚本：处理 API 请求或返回静态文件
URI="$FILE_URI"
REQ="$FILE_REQ"

# 1. API 请求：转发到本地服务
case "$URI" in
  api/*)
    resp=$(curl -s --max-time 5 "http://127.0.0.1:9300/$URI")
    echo -e "Content-type: application/json\r\nAccess-Control-Allow-Origin: *\r\n"
    echo "$resp"
    exit 0
    ;;
esac

# 2. 静态文件：按扩展名输出正确 MIME
if [ -f "$REQ" ]; then
  ext="${REQ##*.}"
  case "$ext" in
    png)  mime="image/png" ;;
    css)  mime="text/css" ;;
    js)   mime="application/javascript" ;;
    html) mime="text/html" ;;
    *)    mime=$(file -b --mime-type "$REQ" 2>/dev/null) ;;
  esac
  echo -e "Content-type: $mime\r\n"
  cat "$REQ"
else
  # 3. 404 处理
  echo -e "Status: 404 Not Found\r\nContent-type: text/html\r\n\r\n"
  echo "<h1>404 Not Found</h1>"
fi
```

### 脚本逻辑说明

1. **API 优先**：当 `FILE_URI` 以 `api/` 开头时，代理到本地服务（如 `http://127.0.0.1:9300`）
2. **静态文件服务**：否则根据 `FILE_REQ` 找到静态文件，按扩展名返回正确 Content-Type
3. **404 处理**：文件不存在时返回 404，避免页面空白

### 文件放置位置

```
/nas/pool0/u<uid>/plugin/pluginsrc/<plugin>/ui/
├── index.html          # 主页面
├── <plugin>.cgi        # 入口脚本（必需）
├── api/                # 可选：前端调用的 API
├── css/                # 样式
├── js/                 # 脚本
└── ...
```

### 注意事项

- `<plugin>.cgi` **必须可执行**（`chmod +x`）
- 脚本编码建议 UTF-8（无 BOM）
- 必须使用 CRLF（`\r\n`）作为 HTTP 头换行
- 不要输出多余空行或调试信息
- 所有路径以 `$REQ` 为准，避免硬编码
- `Access-Control-Allow-Origin: *` 便于前端跨域
- API 超时建议 3~5 秒，避免 UI 卡死

### 快速验证

```bash
# 浏览器访问（需登录态）
https://NAS:443/plugin/<uid>/<plugin>/index.html

# 直接访问 API（需登录态）
https://NAS:443/plugin/<uid>/<plugin>/api/info
```

查看响应头是否包含正确的 Content-Type。

> **plugin.cgi 会强制校验 NAS 登录态（token），未登录或 token 过期 → 403 Forbidden。**  
> 如需第三方 App（如 Portainer）无鉴权访问，请使用 Nginx 反代劫持。

---

## 8. Nginx 反代配置规范（核心规则）

正确的反代配置让 API / UI 正常访问。

### 配置文件位置

```
/etc/nginx/conf.d/luci/plugincenter.conf
```

该文件在 luci.conf 中被 include，属于 server 级配置。

**修改前务必备份：**

```bash
cp plugincenter.conf plugincenter.conf.bak-$(date +%F)
```

### 规则优先级（致命！）

1. **自定义正则 location 必须在 `location /plugin`（前缀）之前** — 否则会被前缀匹配截胡，导致反代不生效！
2. **正则 location 不能嵌套在 `location /plugin` 内部** — Nginx 不会生效！
3. **每个自定义 location 追加在文件顶部** — 按业务类型分组，便于维护。

**错误示例：** 把自定义 location 放在 `/plugin` 后面 → 全部失败！

```nginx
# ❌ 错误顺序
location /plugin { ... }
location ~ ^/plugin/... { ... }

# ✅ 正确顺序
location ~ ^/plugin/... { ... }
location /plugin { ... }
```

### 常用反代规则（按场景选择）

#### A. API 反代（本地服务型）

只反代 `/api/*` 给本地服务，其余走 plugin.cgi：

```nginx
location ~ ^/plugin/u?<NAS_UID>/sysinfo/api/ {
  rewrite ^/plugin/u?<NAS_UID>/sysinfo/(api/.*)$ /$1 break;
  proxy_pass http://127.0.0.1:9300;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_http_version 1.1;
  add_header Access-Control-Allow-Origin "*" always;
  add_header Cache-Control "no-store";
}
```

**适用场景：** 后端提供 REST API，前端 UI 走 plugin.cgi

#### B. 完整劫持（第三方 Web App）

完全接管插件路径，转发到本地 Web 应用：

```nginx
location ~ ^/plugin/u?<NAS_UID>/portainer/?.?/?(.*)$ {
  proxy_pass http://127.0.0.1:9443/$1;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_read_timeout 300s;
}
```

**适用场景：** 第三方 Web App（如 Portainer），需要访问根路径 `/`、`/static`、`/api` 等

#### C. UI 静态文件绕鉴权（alias）

绕过 plugin.cgi 登录态，直接服务静态文件：

```nginx
location ~ ^/plugin/u?<NAS_UID>/sysinfo/((?!api/).*)$ {
  alias /data/plugin/www/u<NAS_UID>/sysinfo/$1;
  default_type text/html;
}
```

**适用场景：** 只想提供静态页面（无需登录态），第三方 App 嵌入（如 iframe）

### 请求处理流程（以 Portainer 劫持为例）

```
米家 App 浏览器
  → HTTPS (443)
  → Nginx（正则匹配，剥前缀转发）
  → http://127.0.0.1:9443（Portainer 服务）
  → 返回响应 HTML / API
```

URL 示例：`https://NAS:443/plugin/<NAS_UID>/portainer/#!/containers`
→ Nginx 转发到 `http://127.0.0.1:9443/#!/containers`

### 操作流程

```bash
# 1. 备份配置
cp /etc/nginx/conf.d/luci/plugincenter.conf \
   /etc/nginx/conf.d/luci/plugincenter.conf.bak-$(date +%F)

# 2. 编辑配置（在文件顶部添加自定义 location ...）

# 3. 语法检查
nginx -t

# 4. 重新加载
nginx -s reload

# 5. 验证访问
# https://NAS:443/plugin/u<uid>/<plugin>/...
```

### 验证清单

- [ ] API 能正常返回数据（200）
- [ ] UI 页面能正常加载（非 403 / 404）
- [ ] WebSocket（如 Portainer）能正常连接
- [ ] 图标与名称在 App 中正确显示
- [ ] 无 Nginx 错误日志（`/var/log/nginx/error.log`）

### 技术细节

- 正则中的 `u?` 表示兼容有无 u 前缀
- `(.*)` 捕获 remainder 用于路径透传
- 如遇 403，多半是走了 plugin.cgi 被登录态拦了
- Nginx 正则默认区分大小写，如需忽略大小写用 `~*`

> **Nginx 规则顺序错误是插件无法访问的首要原因！**  
> 修改后务必验证，备份先行。如需重置，请恢复备份并 reload Nginx。

---

## 9. 后端服务规范（本地服务型）

systemd + server.sh，让插件服务稳定运行。

### 推荐方式：systemd 服务

```ini
[Unit]
Description=<plugin> service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /nas/pool0/u<uid>/plugin/pluginsrc/<plugin>/files/httpd.py
Restart=on-failure
RestartSec=5
Environment=PLUG_PORT=<port>
Environment=SYSINFO_SCRIPT=../files/<采集>.sh
StandardOutput=append:/tmp/<plugin>/server.log
StandardError=append:/tmp/<plugin>/server.log

[Install]
WantedBy=multi-user.target
```

优势：开机自启、崩溃自动重启、便于 systemctl 管理、日志清晰

### 部署流程

```bash
# 1. 准备后端程序（如 httpd.py）
# 2. 写入 systemd service 文件
# 3. 创建日志目录
mkdir -p /tmp/<plugin>

# 4. 启用并启动服务
systemctl daemon-reload
systemctl enable <plugin>.service
systemctl start <plugin>.service
systemctl status <plugin>.service
```

### files/server.sh 控制脚本

若插件通过 scripts/control 调用服务控制，通常会委托给 files/server.sh：

```sh
#!/bin/sh
PORT="${PLUG_PORT:-<port>}"
PIDFILE="/tmp/<plugin>/server.pid"

start()   { nohup python3 httpd.py >>/tmp/<plugin>/server.log 2>&1 & echo $! > $PIDFILE; }
stop()    { kill $(cat $PIDFILE); rm -f $PIDFILE; }
status()  { [ -f $PIDFILE ] && echo running || echo stopped; }
restart() { stop; start; }

case "$1" in
  start|stop|status|restart) "$1" ;;
esac
```

支持动作：`start` / `stop` / `status` / `restart`

### 踩坑提醒（重点）

1. **systemd 直接跑前台进程**，不要用 nohup 后台化作为 systemd 的主进程
2. **日志目录必须先 mkdir**，否则可能 exit 209
3. **端口避开官方常用端口**（如 9000/9100/9200），9300 仅作示例，注意未来冲突

### 快速验证清单

- [ ] `systemctl status` 显示 active
- [ ] 服务端口可访问
- [ ] `/tmp/<plugin>/server.log` 有输出
- [ ] App 或 nginx 反代后 API 返回正常
- [ ] 开机重启后服务仍自动启动

---

## 10. scripts/control 生命周期脚本

control 是插件的生命周期钩子脚本，**必须可执行**。

### 真机实测：action 名称与触发时机

> **关键差异**：真机 action 名称与小红书原笔记不同，以真机为准。

| action          | 触发时机 | 常见用途                               |
| --------------- | ---- | ---------------------------------- |
| `preinstall`    | 安装前  | 准备目录/校验                            |
| `postinstall`   | 安装后  | 初始化配置、写 systemd、启动服务               |
| `enable`        | 启用时  | 启动服务                               |
| `disable`       | 禁用时  | 停止服务                               |
| `status`        | 查询时  | 输出 `running` / `stopped`（exit 0/1） |
| `preuninstall`  | 卸载前  | 停止服务                               |
| `postuninstall` | 卸载后  | 清理 systemd / 临时文件                  |
| `preupgrade`    | 升级前  | 可选                                 |
| `postupgrade`   | 升级后  | 重启服务                               |

> `install/uninstall/start/stop` 不是 plugincenter 调用的 action 名，勿照搬原笔记。

### 真机实测：plugincenter 注入的环境变量

| 变量                | 含义           | 示例                                  |
| ----------------- | ------------ | ----------------------------------- |
| `PLUG_SRC_DIR`    | 插件 `src/` 目录 | `/home/u<uid>/plugin/sysmon/src` |
| `PLUG_HOME_DIR`   | 插件根目录        | `/home/u<uid>/plugin/sysmon`     |
| `PLUG_USER`       | 用户名          | `u<uid>`                         |
| `PLUG_POOL_DIR`   | 存储池目录        | `/nas/pool0/...`                    |
| `PLUG_PORT_ALLOC` | 分配的端口        | `9301`                              |
| `PLUG_ACTION`     | 当前动作         | `uninstall`（disable 分支判断用）          |

**后端路径用 `$PLUG_SRC_DIR/files/httpd.py`（不是 `$PLUG_HOME_DIR/files/...`）。**

### 标准模板示例（真机版）

```sh
#!/bin/sh
PLUGIN_NAME="sysmon"
HTTPD="$PLUG_SRC_DIR/files/httpd.py"
LOGDIR="/tmp/${PLUGIN_NAME}"
PORT="${PLUG_PORT_ALLOC:-9301}"

_start() {
    mkdir -p "$LOGDIR"
    if [ -f "/etc/systemd/system/${PLUGIN_NAME}.service" ]; then
        systemctl start "${PLUGIN_NAME}.service"; return $?
    fi
    PLUG_PORT="$PORT" nohup python3 "$HTTPD" >>"$LOGDIR/server.log" 2>&1 &
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
        cat > "/etc/systemd/system/${PLUGIN_NAME}.service" <<UNIT
[Unit]
Description=${PLUGIN_NAME} service
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
        systemctl enable "${PLUGIN_NAME}.service"
        _start; exit $? ;;
    enable|start)  _start; exit $? ;;
    disable|stop)
        [ "$PLUG_ACTION" = "uninstall" ] && {
            systemctl disable "${PLUGIN_NAME}.service" 2>/dev/null
            rm -f "/etc/systemd/system/${PLUGIN_NAME}.service"
            systemctl daemon-reload
        }
        _stop; exit 0 ;;
    status)
        if [ -f "$LOGDIR/server.pid" ] && kill -0 "$(cat "$LOGDIR/server.pid")" 2>/dev/null; then
            echo "running"; exit 0
        fi
        systemctl is-active --quiet "${PLUGIN_NAME}.service" 2>/dev/null && { echo "running"; exit 0; }
        echo "stopped"; exit 1 ;;
    preuninstall)  _stop; exit 0 ;;
    postuninstall)
        rm -f "/etc/systemd/system/${PLUGIN_NAME}.service"
        systemctl daemon-reload 2>/dev/null
        rm -rf "$LOGDIR"; exit 0 ;;
    preupgrade|postupgrade) exit 0 ;;
    *) exit 0 ;;
esac
```

### 常见坑

| 错误                                       | 后果                              |
| ---------------------------------------- | ------------------------------- |
| action 名用 `install/uninstall/start/stop` | plugincenter 不会调用这些名字           |
| 路径用 `$PLUG_HOME_DIR/files/...`           | 真机文件在 `$PLUG_SRC_DIR/files/...` |
| systemd ExecStart 路径漏了 `src/`            | 服务启动失败 exit 2                   |
| 路径硬编码 UID                                | 换用户就挂                           |
| 缺少 status action                         | App 查状态失败                       |
| 脚本无执行权限                                  | 调用报 Permission denied           |
| 忘记写日志/清理                                 | 升级/卸载后残留文件                      |

---

## 11. 插件上架与版本管理规范

合规上架小米应用商店，规范版本迭代与维护流程。

### 上架流程总览

```
开发完成（自测通过）
  → 提交审核（应用商店后台）
  → 小米审核（1~3 个工作日）
  → 审核通过（正式上架）
  → 用户安装（可搜索下载）
```

### 上架准备清单

- [ ] 插件功能已完整实现并自测通过
- [ ] 符合小米 NAS 插件开发规范（安全、性能等）
- [ ] 准备插件包（.zip）与完整文档
- [ ] 准备应用商店所需的图标、截图、描述等素材
- [ ] 完成隐私声明与权限说明

### 应用商店提交要求

| 项目   | 要求说明                          |
| ---- | ----------------------------- |
| 应用名称 | 简洁明了，不超过 30 个字符               |
| 应用图标 | 512×512 像素，PNG 格式，清晰美观        |
| 截图   | 至少 3 张，展示主要功能与界面（推荐 1280×720） |
| 应用描述 | 详细介绍功能、使用场景与优势（500 字以内）       |
| 分类   | 选择合适的分类（如：工具、备份、安全等）          |
| 版本号  | 遵循语义化版本规范：v1.0.0              |
| 更新日志 | 说明本版本新增、优化与修复内容               |
| 隐私声明 | 说明数据收集、使用方式与用户隐私保护措施          |
| 权限说明 | 列出所需权限及用途，必要性说明               |

### 版本管理规范（语义化版本）

```
MAJOR.MINOR.PATCH
  1   .  0   .  0

重大更新    功能更新    问题修复
不兼容更改  新增功能    修复 Bug
```

- 版本号示例：v1.2.3
- 命名规则：`v<MAJOR>.<MINOR>.<PATCH>`
- 修复 bug → PATCH+1（v1.2.3 → v1.2.4）
- 新增功能 → MINOR+1（v1.2.4 → v1.3.0）
- 重大变更 → MAJOR+1（v1.3.0 → v2.0.0）

### 更新与维护流程

```
问题发现（用户反馈/日志监控）
  → 问题修复（定位问题/开发修复）
  → 测试验证（功能测试/回归测试）
  → 版本发布（更新日志/提交上架）
  → 持续迭代（优化用户体验）
```

| 更新类型 | 建议频率           |
| ---- | -------------- |
| 紧急修复 | 1~2 天内发布热修复    |
| 常规更新 | 每 2~4 周发布一个小版本 |
| 重大更新 | 根据产品规划与用户需求    |

### 兼容性与升级策略

- 尽量保证向下兼容，避免影响用户现有配置
- 涉及数据库或配置变更，需提供升级脚本
- 提供自动升级功能（可选）
- 升级前自动备份用户配置
- 出现错误时提供回滚方案

### 上架审核常见驳回原因

| 驳回原因           | 解决建议          |
| -------------- | ------------- |
| 功能不完整或存在明显 Bug | 完善功能，充分测试     |
| 界面不美观，用户体验差    | 优化 UI/UX，提升体验 |
| 权限申请不合理或说明不清   | 精简权限，明确必要性说明  |
| 应用描述不清晰或截图不足   | 详细描述，提供高质量截图  |
| 隐私政策不完善或缺失     | 完善隐私政策，合规透明   |
| 违反小米应用商店相关政策   | 仔细阅读并遵守平台政策   |

> 上架仅是开始，持续维护与优化才是关键！

---

## 12. 安全与合规 + 验收清单

部署完成后逐项核对，确保插件可用、合规、稳定。

### 安全与合规核心要点

| #   | 要点                      | 说明                                 |
| --- | ----------------------- | ---------------------------------- |
| 1   | **插件以 root 运行**         | 代码需谨慎，勿留后门                         |
| 2   | **443 是 mTLS**          | 无客户端证书通常返回 400，UI/API 也可能要求登录态     |
| 3   | **官方安装通道 RSA 验签**       | 自研插件需手动部署，不能绕过                     |
| 4   | **plugincenter update** | 实测不会覆盖手动注册条目，但仍需关注云端策略变化           |
| 5   | **固件升级风险**              | 可能重置 /etc、systemd、nginx，配置需准备重部署脚本 |

> 安全不是附加项，而是插件可持续可维护的前提。

### 部署完成后的验收清单（必查）

- [ ] `plugincenter -u u<uid> list` 返回新插件（code=0）
- [ ] App 应用市场显示插件（必要时杀 App 重进）
- [ ] App 点击插件能正常加载 UI（非空白）
- [ ] API 经 nginx 反代 / cgi 代理返回正确数据
- [ ] 图标正常显示（300×300）
- [ ] systemd 服务 active + enabled（开机自启）
- [ ] 备份齐全（.list / nginx conf / shadow 等）

### 快速复核命令

```bash
# 查列表
plugincenter -u u<uid> list

# 查服务
systemctl status <plugin>.service

# 查配置
nginx -t && nginx -s reload

# 查访问
curl -k https://NAS:443/plugin/<uid>/<plugin>/
```

### 最终通过标准

| 显示正常 | 访问正常 | 服务正常 | 配置有备份 | 升级可恢复 |
| ---- | ---- | ---- | ----- | ----- |

**以上项目全部通过，才建议正式长期使用。**

### 重要提醒

- ⚠️ 未通过验收，不要交付使用
- ⚠️ 先备份，再修改；先测试，再上线
- ⚠️ 固件升级后请重新检查 systemd / nginx / .list

---

## 13. 真机部署补充：桌面端适配与 config 文件

### src/ui/config 文件（真机必需）

与 `.list` 的 `frontend` 字段对应，App/桌面端会读取此文件：

```json
{
    "title": "系统状态",
    "desc": "查看 NAS CPU、内存、磁盘、温度、风扇与 Docker 容器状态",
    "icon": "/sysmon.icon",
    "type": "url",
    "dev_type": [1, 2, 3, 4],
    "url": [
        { "dev_type": [1],     "url": "/index.html#/" },
        { "dev_type": [2, 3, 4], "url": "/index.html#/" }
    ],
    "sortid": 30,
    "widget": []
}
```

### 桌面端 App（Electron）适配要点

| 要点             | 说明                                                                                   |
| -------------- | ------------------------------------------------------------------------------------ |
| URL 必须带 hash   | `"/index.html#/"`，纯 `/index.html` 桌面端打不开                                             |
| UI 需响应式        | 桌面是宽屏 Electron WebView，`@media` 分档：`<600px` / `600-900px` / `≥900px`                 |
| API 路径用正则提取    | 桌面端 pathname 可能带盘符前缀（`/D:/plugin/...`），必须提取 `/plugin/<uid>/<plugin>` 段               |
| 滚动用内层容器        | `body` 锁 `height:100%; overflow:hidden`，内部 `#scroll-wrap` 滚动（body 直接滚在 Electron 里无效） |
| dev_type 分路由可选 | 复杂 SPA 可分手机/桌面两套路由（参考 baidupan）；简单页面共用 `#/` 即可                                       |

#### API 路径推导（真机验证版）

桌面端 Electron 经本地代理加载页面，`location.pathname` 可能是：

- 手机端：`/plugin/<uid>/sysmon/index.html`
- 桌面端：`/D:/plugin/<uid>/sysmon/index.html`（**多了 Windows 盘符**）

用正则提取 `/plugin/<uid>/<plugin>` 段，自动剥掉任何前缀：

```js
function apiUrl(ep) {
  var path = window.location.pathname;
  // 提取 /plugin/<uid>/<plugin>，剥掉盘符等前缀
  var m = path.match(/(\/plugin\/[^\/]+\/[^\/]+)/);
  var base = m ? m[1] : path.replace(/\/index\.html$/, "").replace(/\/$/, "");
  return base + "/api/" + ep;
}
```

验证覆盖：`/plugin/...`、`/D:/plugin/...`、`/<PC_PATH>`、带 hash — 全部正确。

#### 滚动布局（真机踩坑：body 滚动在 Electron 里无效）

官方插件（central / ipc）均采用 **内层容器滚动** 模式：

```html
<html id="root_html">
<body id="root_body" style="height: 100%;">
  <div id="scroll-wrap">  <!-- 唯一滚动容器 -->
    ...内容...
  </div>
</body>
```

```css
/* 锁死 html/body，滚动交给内层容器 */
html, body {
  height: 100%;
  overflow: hidden;
}
#scroll-wrap {
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
}
```

> **踩坑**：让 `body` 自己 `overflow-y: auto` 在桌面端 Electron WebView 中**滚动不生效**，内容被裁切且无法滚动。必须用内层容器。

#### 缓存与热更新

| 改了什么                          | 需要做什么                                             |
| ----------------------------- | ------------------------------------------------- |
| `config` / `.list`（URL、标题、图标） | **杀掉 App 重进**（App 启动时才拉 `plugin_list` 并缓存）        |
| `style.css` / `app.js`        | 给引用加 cache-bust：`href="style.css?v=时间戳"`，然后刷新页面即可 |
| `index.html` 结构               | 刷新页面即可                                            |
| 后端 `httpd.py`                 | `systemctl restart sysmon.service`                |

**为什么改 config/.list 必须全关 App？**  
桌面端 App 在启动时调用 `plugincenter/plugin_list` 一次，缓存 URL / 标题 / 排序等元数据。之后 WebView 里刷新只重载页面，不会重新拉列表。改了 URL 或 hash 路由必须让 App 重启重新拉取。

### 真机验证命令速查

```bash
# 服务
systemctl is-active sysmon.service
systemctl is-enabled sysmon.service

# 后端
curl -s http://127.0.0.1:9301/api/health
curl -s http://127.0.0.1:9301/api/info
curl -s http://127.0.0.1:9301/api/temps
curl -s http://127.0.0.1:9301/api/docker

# 注册
plugincenter -u u<uid> list

# 文件
readlink /data/plugin/www/u<uid>/sysmon   # → .../src/ui
ls -la /data/plugin/www/icon/sysmon.icon
test -x /home/u<uid>/plugin/sysmon/src/ui/sysmon.cgi && echo OK

# cgi 自测（模拟 plugin.cgi 注入环境变量）
FILE_URI="api/health" FILE_REQ="" sh /home/u<uid>/plugin/sysmon/src/ui/sysmon.cgi
```

### 真机环境备忘（小米智能存储 <nas-hostname>）

| 项目           | 值                                                              |
| ------------ | -------------------------------------------------------------- |
| 架构           | aarch64 / Linux 6.6.35-yocto                                   |
| Python       | 3.12 (`/usr/bin/python3`)                                      |
| UID          | `<uid>`                                                     |
| 端口占用         | 9000/9100/9200 已占用，sysmon 用 **9301**                           |
| 主存储          | `/nas/pool0`（cfs 15T）                                          |
| Docker CLI   | `/data/docker/docker`（不在 PATH）                                 |
| plugin.cgi   | `/usr/bin/plugin.cgi`（FastCGI，经 `unix:/var/run/fcgi.sock`）     |
| 参考 shell cgi | `/home/u<uid>/plugin/ipc/src/ui/ipc.cgi`                    |
| nginx 插件入口   | `/etc/nginx/conf.d/luci/plugincenter.conf`（`location /plugin`） |

---

## 9. 重启持久化（自研插件被清理的根因与解法）

### 问题：NAS 重启后手动部署的插件消失

重启后 `/home/u<uid>/plugin/` 和 `.list` 被系统还原，手动部署的 sysmon 目录和注册条目全部丢失。但官方插件（baidupan / xunlei / central 等）完好无损。

### 根因

系统开机时从 **`/etc/config/plugin`** 的 `ports` 表 + `server.list` 重建插件目录和 `.list`。官方插件在 `ports` 表里有注册：

```json
// /etc/config/plugin
"ports": {
    "reserve": true,
    "ipc":      { "u<uid>": 9100 },
    "baidupan": { "u<uid>": 9000 },
    "xunlei":   { "u<uid>": 9200 }
}
```

**不在表里的手动插件，系统视为垃圾丢弃。**

### 解法：三层保险

**① 注册端口到系统配置**（让系统"认识"这个插件）

```bash
python3 -c "
import json
for p in ['/etc/config/plugin', '/data/etc/upper/config/plugin']:
    cfg = json.load(open(p))
    cfg['ports']['sysmon'] = {'u<uid>': 9301}
    json.dump(cfg, open(p, 'w'), ensure_ascii=False, indent=1)
"
```

> `/data/etc/upper/config/plugin` 是 overlay 可写层，修改后持久化。

**② 源码备份到持久目录**（`/data` 分区不受重启重置影响）

```bash
mkdir -p /data/backup/plugins/sysmon
cp -a /home/u<uid>/plugin/sysmon /data/backup/plugins/sysmon/
```

**③ 开机自愈服务**（重启后自动恢复）

```ini
# /etc/systemd/system/sysmon-restore.service
[Unit]
Description=sysmon plugin restore on boot
After=network.target
Before=sysmon.service
ConditionPathExists=/data/backup/plugins/sysmon/sysmon

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /data/backup/plugins/sysmon/restore.sh

[Install]
WantedBy=multi-user.target
```

恢复脚本 `restore.sh` 做五件事：

1. 恢复插件目录到 `/home/u<uid>/plugin/sysmon/`
2. 重建 www 软链 + 图标
3. 合并注册 `.list`
4. 确保 `sysmon.service` 存在并 enabled
5. 启动服务

```bash
systemctl enable sysmon-restore.service
```

### 重启后的恢复流程

```
NAS 开机
  → systemd 启动 sysmon-restore.service
  → 检查 /home/u<uid>/plugin/sysmon/ 是否存在
  → 不存在则从 /data/backup/plugins/sysmon/ 恢复
  → 注册 .list + 重建软链
  → 启动 sysmon.service
  → 应用市场出现「系统状态」
```

### 关键路径备忘

| 路径                              | 作用               | 重启是否保留        |
| ------------------------------- | ---------------- | ------------- |
| `/etc/config/plugin`            | 系统插件注册表（ports 表） | overlay 可写层保留 |
| `/data/etc/upper/config/plugin` | 同上的持久层           | ✅             |
| `/data/backup/plugins/`         | 自研插件备份           | ✅             |
| `/home/u<uid>/plugin/`          | 插件运行目录           | ❌ 重启被重置       |
| `/data/plugin/u<uid>.list`      | 用户插件列表           | ❌ 重启被重建       |
| `/data/plugin/www/icon/`        | 图标文件             | ✅             |

---


---

## 附录：高频踩坑速查

### 原笔记要点

| 坑                  | 后果            | 预防                                   |
| ------------------ | ------------- | ------------------------------------ |
| `.list` 写错         | **整个应用市场异常**  | 修改前备份，完整填写，修改后验证                     |
| `resource.mpk` 空串  | 列表异常          | 必须指向本地 INFO 文件                       |
| App URL 带 `u` 前缀   | 404           | 实际路径**无 u 前缀**：`/plugin/<NAS_UID>/...` |
| url 字段写 `"./"`     | 畸形路径 404      | 用 `"/index.html#/"`                  |
| Nginx location 顺序错 | 反代完全不生效       | 自定义 location 写在 `/plugin` **之前**     |
| plugin.cgi 登录态     | 403 Forbidden | 需登录态，或用 Nginx 反代劫持绕过                 |
| 图标非 300×300        | App 显示异常      | 严格 300×300 PNG，命名 `<plugin>.icon`    |
| INFO `port` 写数字    | 解析失败          | **必须是字符串** `"9301"`                  |
| 端口撞官方              | 服务冲突          | 避开 9000/9100/9200                    |
| systemd 日志目录缺失     | exit 209      | 先 `mkdir -p /tmp/<plugin>`           |
| 固件升级               | 配置被重置         | 准备重部署脚本，定期备份                         |

### 真机部署新增踩坑（sysmon 实测）

| 坑                            | 现象                           | 解决                                                                            |
| ---------------------------- | ---------------------------- | ----------------------------------------------------------------------------- |
| **目录结构用 pluginsrc**          | 找不到文件 / systemd 启动失败         | 真机是 `/home/u<uid>/plugin/<name>/src/{files,ui}`                               |
| **www 软链指向错误**               | App 空白 / 404                 | 必须指向 `src/ui`，不是插件根目录                                                         |
| **url 不带 hash 路由**           | 手机能开，**桌面端打不开**              | 必须 `"/index.html#/"`                                                          |
| **frontend.url 是数组**         | 插件列表解析异常                     | `[{dev_type:[1],url:"..."},{dev_type:[2,3,4],url:"..."}]`                     |
| **control action 名错误**       | 生命周期不触发                      | 真机是 `preinstall/postinstall/enable/disable/status/preuninstall/postuninstall` |
| **PLUG_SRC_DIR 理解错误**        | 路径拼错                         | `PLUG_SRC_DIR` 指向 `src/`，后端在 `$PLUG_SRC_DIR/files/`                           |
| **systemd ExecStart 漏 src/** | 服务 exit 2                    | `/home/u<uid>/plugin/<name>/src/files/httpd.py`                               |
| **ui/ 缺 config 文件**          | 插件信息不完整                      | 需要 `src/ui/config`（title/desc/url/sortid）                                     |
| **桌面端 UI 不适配**               | 窄条布局                         | `@media (min-width: 768px)` 响应式，max-width ≥1100px                             |
| **fetch 相对路径失效**             | 桌面端 API 404                  | `apiUrl()` 从 `location.pathname` 推导绝对路径                                       |
| **PowerShell 生成文件带 `\r`**    | shell 报 `$'\r'`              | 上传前 `dos2unix` 或用 LF 写入                                                       |
| **磁盘读 `/` 分区**               | 显示 100%                      | 主存储在 `/nas/pool0`（`cfs` 挂载）                                                   |
| **docker 不在 PATH**           | Docker 面板空白                  | CLI 在 `/data/docker/docker`                                                   |
| **传感器标签泛化**                  | 两个都叫 "temp"                  | 自行按 `chip==drivetemp` 编号为硬盘1/硬盘2                                              |
| **端口被残留进程占用**                | systemd 启动失败                 | 先 `netstat -tlnp \| grep <port>` 杀掉残留                                         |
| **桌面端 pathname 带盘符**         | API 请求 400（`/D:/plugin/...`） | `apiUrl()` 用正则提取 `/plugin/<uid>/<plugin>` 段                                   |
| **body 滚动在 Electron 无效**     | 内容裁切、无法滚动                    | `body` 锁 100% + `#scroll-wrap` 内层滚动                                           |
| **改 config/.list 后刷新无效**     | 桌面端还是旧 URL                   | App 启动时才拉 `plugin_list`，必须杀 App 重进                                            |
| **CSS/JS 热更新被缓存**            | 刷新仍是旧样式                      | 引用加 `?v=时间戳` cache-bust                                                       |

---

*文档整理自小红书笔记「小米 NAS 插件开发规范｜手动部署实测总结」（@Jray）13 张配图，并经 sysmon 插件在小米智能存储真机部署验证。仅供学习参考。*
