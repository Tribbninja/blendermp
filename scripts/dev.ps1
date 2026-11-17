<#
  Build and run the desktop app with the right toolchain on PATH.

  The repository path contains a space, and windres cannot handle that, so the
  target directory is redirected to a space-free location.
#>
param(
    [switch]$Autohost,
    [switch]$Release
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

$mingw = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter gcc.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\mingw64\\bin\\gcc\.exe$' } |
    Select-Object -First 1
if ($mingw) { $env:Path = "$($mingw.DirectoryName);$env:Path" }
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

$env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA 'Temp\bmp-target'
$env:BMP_ADDON_ROOT = Join-Path $root 'addon'

$profileName = if ($Release) { 'release' } else { 'debug' }
$cargoArgs = @('build', '--manifest-path', (Join-Path $root 'desktop\src-tauri\Cargo.toml'))
if ($Release) { $cargoArgs += '--release' }

Write-Host "Building Blendermp ($profileName)..." -ForegroundColor Cyan
& cargo @cargoArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$exe = Join-Path $env:CARGO_TARGET_DIR "$profileName\blendermp.exe"
if (-not (Test-Path $exe)) { throw "build did not produce $exe" }

$appArgs = @()
if ($Autohost) { $appArgs += '--autohost' }

Write-Host "Starting $exe" -ForegroundColor Green
Start-Process -FilePath $exe -ArgumentList $appArgs -WorkingDirectory $root
