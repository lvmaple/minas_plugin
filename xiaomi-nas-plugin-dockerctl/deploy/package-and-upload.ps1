# dockerctl 打包 & 上传到小米 NAS（Windows PowerShell）
# 用法：
#   .\package-and-upload.ps1 -NasIp <NAS_IP> -Uid <NAS_UID> -User root
#   .\package-and-upload.ps1 -OnlyPack            # 仅打包不上传
param(
    [string] $NasIp = "",
    [string] $Uid = "",
    [string] $User = "root",
    [switch] $OnlyPack
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path        # deploy/
$PluginDir = Split-Path -Parent $Root                          # xiaomi-nas-plugin-dockerctl/
$WorkDir = Split-Path -Parent $PluginDir                       # 工程根目录
$Zip = Join-Path $WorkDir "dockerctl-plugin.zip"

Write-Host "==> 打包 $PluginDir" -ForegroundColor Cyan
if (Test-Path $Zip) { Remove-Item $Zip -Force }
# 用 Python 打包：Compress-Archive 会把反斜杠写进 zip 条目名，NAS 端 unzip 解不开目录结构
python (Join-Path $Root "pack.py")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Zip)) { throw "打包失败" }
Write-Host "    已生成 $Zip ($([math]::Round((Get-Item $Zip).Length/1KB,1)) KB)"

if ($OnlyPack) {
    Write-Host "==> 仅打包完成，跳过上传" -ForegroundColor Green
    exit 0
}
if ([string]::IsNullOrWhiteSpace($NasIp) -or $Uid -notmatch '^\d+$') {
    throw "部署时请指定 -NasIp <NAS_IP> 和 -Uid <NAS_UID>"
}

# 传输走 WSL（真机实测：WSL 内 ed25519 密钥免密直连；Windows 原生 ssh 密钥不可用）
$drive = $Zip.Substring(0,1).ToLower()
$ZipWsl = "/mnt/$drive" + ($Zip.Substring(2) -replace "\\", "/")

Write-Host "==> 上传到 $User@${NasIp}:/tmp/（via WSL）" -ForegroundColor Cyan
wsl -- bash -lc "scp -o BatchMode=yes -o ConnectTimeout=8 '$ZipWsl' ${User}@${NasIp}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "scp 上传失败（确认 WSL 免密可连 $NasIp）" }

Write-Host "==> SSH 解压并部署（UID=$Uid）" -ForegroundColor Cyan
wsl -- bash -lc "ssh -o BatchMode=yes ${User}@${NasIp} 'rm -rf /tmp/_dockerctl_pkg && mkdir -p /tmp/_dockerctl_pkg && unzip -o /tmp/dockerctl-plugin.zip -d /tmp/_dockerctl_pkg/ && sh /tmp/_dockerctl_pkg/deploy/deploy.sh $Uid'"
if ($LASTEXITCODE -ne 0) { throw "SSH 部署失败（退出码 $LASTEXITCODE）" }

Write-Host ""
Write-Host "==> 完成。请完全退出米家 App / 桌面端后重进，在应用市场打开「Docker 控制台」。" -ForegroundColor Green
Write-Host "    若列表未出现：plugincenter -u u$Uid list | grep dockerctl"
Write-Host "    后端日志：wsl 内 ssh ${User}@${NasIp} 'tail -50 /tmp/dockerctl/server.log'"
