param(
    [string] $NasIp = "",
    [string] $Uid = "",
    [switch] $OnlyPack
)
$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $PSScriptRoot
$Zip = Join-Path $PluginDir "motrix-plugin.zip"
if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
python (Join-Path $PSScriptRoot "pack.py")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Zip)) { throw "打包失败" }
Write-Host "已打包：$Zip"
if ($OnlyPack) { exit 0 }
if ([string]::IsNullOrWhiteSpace($NasIp) -or $Uid -notmatch '^\d+$') {
    throw "部署时请指定 -NasIp <NAS_IP> 和 -Uid <NAS_UID>"
}
$drive = $Zip.Substring(0,1).ToLowerInvariant()
$zipWsl = "/mnt/$drive" + ($Zip.Substring(2) -replace "\\", "/")
wsl -- bash -lc "scp -o BatchMode=yes -o ConnectTimeout=8 '$zipWsl' root@${NasIp}:/tmp/motrix-plugin.zip"
if ($LASTEXITCODE -ne 0) { throw "上传失败" }
wsl -- bash -lc "ssh -o BatchMode=yes root@${NasIp} 'mkdir -p /tmp/motrix-plugin-package && unzip -o /tmp/motrix-plugin.zip -d /tmp/motrix-plugin-package/ && sh /tmp/motrix-plugin-package/deploy/deploy.sh $Uid $NasIp'"
if ($LASTEXITCODE -ne 0) { throw "部署失败" }
