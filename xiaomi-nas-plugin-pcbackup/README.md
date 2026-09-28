# pcbackup — 小米智能存储「PC 备份」插件

把 **电脑上的目录** 增量备份到 **小米智能存储（NAS）**。插件按照《小米NAS插件开发规范》开发，
在米家 App / 桌面端的应用市场里打开使用。

## 功能

| 场景 | 行为 |
|---|---|
| PC 上新增的文件 | 上传到 NAS |
| PC 上修改过的文件（大小或修改时间变化） | 覆盖上传 |
| 未变化的文件 | 跳过（零流量） |
| **PC 上已删除的文件** | **NAS 端把对应备份文件改名为 `<原名>.del`**（数据保留，不真正删除） |

附加能力：

- 分块上传（默认 8 MiB/块），**断点续传**：中断后再次备份会从 `.pcbp.part` 续传
- 桌面端原生路径备份会保存本轮计划；中断后任务卡片提示「继续备份」，只核对待传文件并从已有分块继续，不重新遍历整个 PC 目录
- 上传后保留文件原始修改时间（mtime），SMB 里看到的时间与 PC 一致
- 删除保护：删除量超过清单 20% 且 ≥10 个文件时需要勾选"强制执行"，防止选错目录导致大面积误标记
- 目录切换保护：换了目录名会先弹确认，确认后重建清单，旧文件**不会**被误标删除
- 文件复活：被标记 `.del` 的文件若在 PC 上重新出现，会重新上传为新文件，旧 `.del` 保留
- 排除规则（支持 `*` 通配，`node_modules/` 表示任意层级的同名目录）
- 新任务默认排除依赖和虚拟环境（如 `node_modules/`、`.venv/`）、开发缓存（如 `__pycache__/`、`.gradle/`）、测试报告与覆盖率产物（如 `test-results/`、`coverage/`）及 `*.pyc` 等生成文件。测试源码目录 `test/`、`tests/` 不在默认规则内
- 排除规则可在任务编辑页逐行修改；更新插件不会覆盖已有任务保存的规则。新增排除规则后，已备份的匹配文件会移出清单，NAS 上的文件默认保留，只有选择清理排除文件时才会删除
- 备份历史、备份文件在线浏览与下载
- 可在任务卡片删除任务；删除任务会移除配置和清单，NAS 上已备份的文件保留

## 工作原理

```
米家 App / 桌面端（Electron WebView）
  └─ 插件页面 src/ui（选目录 → 增量比对 → 分块上传）
       │  HTTPS /plugin/<uid>/pcbackup/api/...（nginx 反代）
       ▼
后端服务 src/files/httpd.py（NAS 上 systemd 常驻，127.0.0.1:9303）
  ├─ 清单库 etc/jobs/<id>/manifest.json（每个文件的大小+修改时间）
  ├─ 增量计划：比对「本次选择的文件」与「清单库」
  ├─ 上传落盘：xxx.pcbp.part → 原子改名为正式文件
  └─ 删除标记：正式文件 → <原名>.del
```

变更判定与 rsync 默认一致：**大小 + 修改时间（毫秒）**。
（浏览器安全策略限制，页面无法记住本地路径，所以每次备份都需要重新选择一次目录。）

## 目录结构

```
xiaomi-nas-plugin-pcbackup/
├── INFO                     # 插件元数据（id=96, port=9303）
├── src/
│   ├── files/
│   │   ├── httpd.py         # 后端服务（纯 Python 标准库）
│   │   └── server.sh        # start/stop/status/restart
│   └── ui/
│       ├── index.html       # 主页面（hash 路由 #/）
│       ├── app.js           # 前端逻辑
│       ├── style.css
│       ├── pcbackup.cgi     # plugin.cgi 入口（api 兜底代理 + 静态文件）
│       ├── config           # 前端配置（真机必需）
│       └── icon.png         # 300×300 图标
├── scripts/control          # 生命周期钩子（postinstall 写 systemd 等）
├── deploy/
│   ├── deploy.sh            # NAS 上一键部署（root 执行）
│   ├── nginx-location.conf  # 反代规则（上传必需！）
│   ├── list-entry.json      # .list 手动注册用
│   └── package-and-upload.ps1  # Windows 打包+上传+部署
├── test/test_backend.py     # 本地自动化测试（131 项断言）
└── tools/make_icon.py       # 图标生成器
```

## 部署

### 前置条件

- NAS 已开启 SSH，Windows 可 `ssh root@<NAS_IP>` 免密登录（工程里
  `xiaomi-storage-minas-tool` 有开 SSH 的脚本）
- 真机 UID：<NAS_UID> / IP：<NAS_IP>（可在命令行参数覆盖）

### 一键部署（Windows PowerShell）

```powershell
cd <PROJECT_DIR>\xiaomi-nas-plugin-pcbackup\deploy
.\package-and-upload.ps1                    # 默认 <NAS_IP> / uid <NAS_UID>
```

### 手动部署

```bash
# 1. 上传解压到 NAS /tmp/_pcbackup_pkg
scp pcbackup-plugin.zip root@<NAS_IP>:/tmp/
ssh root@<NAS_IP> "unzip -o /tmp/pcbackup-plugin.zip -d /tmp/_pcbackup_pkg"

# 2. 执行部署脚本（自动：备份 .list/nginx → 部署目录 → www 软链 → 注册 → systemd → nginx 注入）
ssh root@<NAS_IP> "sh /tmp/_pcbackup_pkg/deploy/deploy.sh <NAS_UID>"

# 3. 完全退出米家 App / 桌面端后重进（App 启动时才拉插件列表）
```

部署脚本会自动完成规范里所有关键步骤：www 软链指向 `src/ui`、图标放
`/data/plugin/www/icon/pcbackup.icon`、`.list` 的 `frontend.url` 用数组 + hash 路由、
nginx 规则插在 `location /plugin` **之前**，并带 `client_max_body_size 256m`
（分块上传必需）。

## 使用

1. 杀掉米家 App / 桌面端重进 → 应用市场 → **PC 备份**
2. 新建任务：起个名字、选 NAS 存放目录（如 `/nas/pool0/pcbackup/我的文档`）
3. 点「开始备份」→ 选择电脑上的文件夹 → 自动增量比对
4. 确认计划（上传 N / 跳过 M / 标记删除 K）→ 开始备份
5. 完成后可在「文件」里浏览、下载，在「历史」里查看每次备份结果

如果桌面端上传中断，重新打开插件，点击任务卡片的「继续备份」，再选择「继续上次备份」。这会继续上次确认的文件列表；中断期间新增或删除的文件，需要之后再选「重新扫描目录」执行新一轮备份。重新扫描会替换旧的续传记录。续传前会核对待传文件的大小和修改时间，文件已变化时应重新扫描。

## 安全说明

- 插件以 root 运行，后端做了严格的路径校验：拒绝绝对路径、`..` 穿越、反斜杠、
  控制字符，且任何已存在路径组件是符号链接时拒绝写入
- 备份目标目录限定在 `/nas/pool0/`、`/home/`、`/tmp/`、`/mnt/`、`/media/`、`/srv/` 下
- API 只监听 `127.0.0.1`，外部只能经 NAS 443（mTLS）的 nginx 反代访问
- 上传产物 chown 回插件用户（`u<uid>`），SMB 可正常访问
- 清理任务备份数据只允许删除该任务新建的目录；目录含其他备份任务时会拒绝。旧任务的目录默认保留，可手动清理。

## 重启持久化（重要！）

真机实测：**NAS 重启会清掉手动部署的插件**（开机时系统从 `/etc/config/plugin` 的 `ports` 表重建插件目录和 `.list`，不在表里的插件被丢弃）。deploy.sh 已内置三层保险：

| 层 | 机制 |
|---|---|
| ① | 端口注册进 `/etc/config/plugin` 与 `/data/etc/upper/config/plugin` 的 `ports` 表（让系统"认识"插件） |
| ② | 源码整体备份到 `/data/backup/plugins/pcbackup/`（/data 分区重启不丢） |
| ③ | `pcbackup-restore.service` 开机自愈：自动恢复插件目录、www 软链、图标、`.list` 注册、systemd 单元并启动服务、nginx 反代 |

即使极端情况下全部丢失，备份数据与清单也不受影响：
- 清单双写在备份数据目录（`<数据根>/.pcbackup/manifest.json`），插件目录被清时自动恢复
- 两份清单都没了也能靠"认领"（同名 + 大小 + 修改时间一致）从 NAS 现存文件重建

验证方式（不用真重启）：

```bash
# 在 NAS 上模拟清理后手动跑自愈脚本
rm -rf /home/u<NAS_UID>/plugin/pcbackup
sh /data/backup/plugins/pcbackup/restore.sh
curl -s http://127.0.0.1:9303/api/health
```

## 本地测试

```bash
cd xiaomi-nas-plugin-pcbackup
python test/test_backend.py     # 自动化断言：增量/删除标记/续传/穿越/切换/比例保护/浏览下载
```

## 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 上传报 413 | nginx 未注入 `client_max_body_size 256m`，重跑 deploy.sh 第 6 步 |
| 上传中途断开 | 正常，`.part` 会续传；再点一次备份即可 |
| 桌面端插件打不开 | 确认 `.list` 的 url 是数组且带 `#/`；改过 config/.list 必须杀 App 重进 |
| 页面没有显示新功能 | 检查 `index.html` 中脚本的 `?v=` 版本号；升级时同步改版本号强制刷新 |
| 想恢复被标记删除的文件 | 在「文件」里找到 `.del` 文件下载，去掉 `.del` 后缀即可 |
| 服务日志 | NAS 上 `/tmp/pcbackup/server.log` |
| 端口冲突 | 本插件用 9303（9301 sysmon / 9302 dockerctl），改端口需同步改 INFO、.list、control、nginx |

## 已知限制

- 空目录不会被备份（浏览器目录选择不暴露空目录）
- PC 端的符号链接不跟随（以浏览器读取到的文件为准）
- 内容变化但大小和修改时间都没变的文件会被跳过（与 rsync 默认行为一致）
- 浏览器文件选择模式关闭页面后需重新选择目录；免遍历续传依赖桌面 App 原生路径读取
