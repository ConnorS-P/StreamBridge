<#
.SYNOPSIS
Installs StreamBridge add-ons and StreamBridge.Player.exe into the local Kodi 21 profile for development/testing.
#>
param(
    [string]$KodiAppData = "$env:APPDATA\Kodi"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$AddonsSrc = Join-Path $RepoRoot "addons"
$KodiAddonsDest = Join-Path $KodiAppData "addons"

if (-not (Test-Path $KodiAddonsDest)) {
    New-Item -ItemType Directory -Force -Path $KodiAddonsDest | Out-Null
}

$AddonNames = @(
    "repository.streambridge",
    "script.module.streambridge",
    "plugin.video.streambridge",
    "service.streambridge"
)

foreach ($Name in $AddonNames) {
    $Src = Join-Path $AddonsSrc $Name
    $Dst = Join-Path $KodiAddonsDest $Name
    if (Test-Path $Dst) {
        Remove-Item -Recurse -Force $Dst
    }
    Copy-Item -Recurse -Force $Src $Dst
    Write-Host "Installed $Name -> $Dst"
}

$PlayerExe = Join-Path $RepoRoot "player\build\Release\StreamBridge.Player.exe"
if (Test-Path $PlayerExe) {
    $BinDest = Join-Path $KodiAddonsDest "plugin.video.streambridge\bin"
    New-Item -ItemType Directory -Force -Path $BinDest | Out-Null
    Copy-Item -Force $PlayerExe (Join-Path $BinDest "StreamBridge.Player.exe")
    Write-Host "Installed StreamBridge.Player.exe -> $BinDest"
}
