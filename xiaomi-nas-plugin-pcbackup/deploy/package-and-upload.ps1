# pcbackup 打包 & 上传到小米 NAS（Windows PowerShell）
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
$PluginDir = Split-Path -Parent $Root                          # xiaomi-nas-plugin-pcbackup/
$WorkDir = Split-Path -Parent $PluginDir                       # 工程根目录
$Zip = Join-Path $WorkDir "pcbackup-plugin.zip"

Write-Host "==> 打包 $PluginDir" -ForegroundColor Cyan
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path "$PluginDir\INFO", "$PluginDir\src", "$PluginDir\scripts", "$PluginDir\deploy" -DestinationPath $Zip -Force
if (-not (Test-Path $Zip)) { throw "打包失败" }
Write-Host "    已生成 $Zip ($([math]::Round((Get-Item $Zip).Length/1KB,1)) KB)"

if ($OnlyPack) {
    Write-Host "==> 仅打包完成，跳过上传" -ForegroundColor Green
    exit 0
}
if ([string]::IsNullOrWhiteSpace($NasIp) -or $Uid -notmatch '^\d+$') {
    throw "部署时请指定 -NasIp <NAS_IP> 和 -Uid <NAS_UID>"
}

Write-Host "==> 上传到 $User@${NasIp}:/tmp/" -ForegroundColor Cyan
scp $Zip "${User}@${NasIp}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "scp 上传失败（确认 NAS 已开启 SSH 且可免密登录）" }

Write-Host "==> SSH 解压并部署（UID=$Uid）" -ForegroundColor Cyan
ssh "${User}@${NasIp}" "rm -rf /tmp/_pcbackup_pkg && mkdir -p /tmp/_pcbackup_pkg && unzip -o /tmp/pcbackup-plugin.zip -d /tmp/_pcbackup_pkg/ && sh /tmp/_pcbackup_pkg/deploy/deploy.sh $Uid"

Write-Host ""
Write-Host "==> 完成。请完全退出米家 App / 桌面端后重进，在应用市场打开「PC 备份」。" -ForegroundColor Green
Write-Host "    若列表未出现：plugincenter -u u$Uid list | grep pcbackup"
Write-Host "    若上传报 413/超时：确认 nginx 注入步骤已执行（deploy/deploy.sh 第 6 步）"
