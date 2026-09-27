# Portable Windows build of Save Sync.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Ludusavi C:\path\ludusavi.exe]
param([string]$Ludusavi = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -e ".[dev]"
& .venv\Scripts\python.exe -m pytest tests -q
& .venv\Scripts\pyinstaller.exe packaging\savesync.spec --noconfirm --clean
# Layout for the USB drive: <USB>:\SaveSync\ludusavi\ludusavi.exe
$usb = "dist\usb-layout\SaveSync"
New-Item -ItemType Directory -Force "$usb\ludusavi", "$usb\backups" | Out-Null
if ($Ludusavi) { Copy-Item $Ludusavi "$usb\ludusavi\ludusavi.exe" }
Write-Host "Portable app: dist\SaveSync\SaveSync.exe"
Write-Host "USB layout:   dist\usb-layout (copy SaveSync\ to the root of the USB drive)"
