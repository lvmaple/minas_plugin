# sysmon 打包 & 上传到小米 NAS（Windows PowerShell）
# 用法：
#   .\package-and-upload.ps1 -NasIp <NAS_IP> -Uid <NAS_UID>
#   .\package-and-upload.ps1 -NasIp <NAS_IP> -Uid <NAS_UID> -User root -OnlyPack
param(
    [Parameter(Mandatory = $true)] [string] $NasIp,
    [Parameter(Mandatory = $true)] [string] $Uid,
    [string] $User = "root",
    [switch] $OnlyPack
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path   # deploy/
$PluginDir = Split-Path -Parent $Root                     # xiaomi-nas-plugin-sysmon
$WorkDir = Split-Path -Parent $PluginDir                  # 工程根目录
$Zip = Join-Path $WorkDir "sysmon-plugin.zip"

Write-Host "==> 打包 $PluginDir" -ForegroundColor Cyan
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path $PluginDir -DestinationPath $Zip -Force
Write-Host "    已生成 $Zip ($([math]::Round((Get-Item $Zip).Length/1KB,1)) KB)"

if ($OnlyPack) {
    Write-Host "==> 仅打包完成，跳过上传" -ForegroundColor Green
    exit 0
}

Write-Host "==> 上传到 $User@${NasIp}:/tmp/" -ForegroundColor Cyan
scp $Zip "${User}@${NasIp}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "scp 上传失败" }

Write-Host "==> SSH 解压并部署（UID=$Uid）" -ForegroundColor Cyan
ssh "${User}@${NasIp}" "rm -rf /tmp/xiaomi-nas-plugin-sysmon && unzip -o /tmp/sysmon-plugin.zip -d /tmp/ && sh /tmp/xiaomi-nas-plugin-sysmon/deploy/deploy.sh $Uid"

Write-Host ""
Write-Host "==> 完成。请杀掉米家 App 重进，在应用市场打开「系统状态」。" -ForegroundColor Green
Write-Host "    若列表未出现，先在 NAS 上跑：plugincenter -u u$Uid list | grep sysmon"
