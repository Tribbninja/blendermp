#requires -Version 5.1
<#
  Build a release desktop app and a Blender 5.x extension zip.

  Outputs:
    dist/blender_multiplayer-<version>.zip    installable Blender extension
    dist/blendermp-windows-x64-<version>.zip  app + addon + docs
#>
param([string]$Version = "0.1.0")

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root 'dist'
$tools = Join-Path $root '.tools'

$mingw = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter gcc.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\mingw64\\bin\\gcc\.exe$' } | Select-Object -First 1
if ($mingw) { $env:Path = "$($mingw.DirectoryName);$env:Path" }
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA 'Temp\bmp-target'

Write-Host 'Cleaning dist...' -ForegroundColor Cyan
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# --- Blender extension ------------------------------------------------------
Write-Host 'Packing Blender extension...' -ForegroundColor Cyan
$stage = Join-Path $env:TEMP "bmp-extension-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $root 'addon\blender_multiplayer\*') $stage -Recurse
Get-ChildItem $stage -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force
$extension = Join-Path $dist "blender_multiplayer-$Version.zip"
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $extension -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

# --- Release app ------------------------------------------------------------
Write-Host 'Building release desktop app (this takes a few minutes)...' -ForegroundColor Cyan
& cargo build --release --manifest-path (Join-Path $root 'desktop\src-tauri\Cargo.toml')
if ($LASTEXITCODE -ne 0) { throw 'release build failed' }

$appDir = Join-Path $dist 'blendermp'
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
Copy-Item (Join-Path $env:CARGO_TARGET_DIR 'release\blendermp.exe') $appDir
Copy-Item (Join-Path $root 'addon') (Join-Path $appDir 'addon') -Recurse
Copy-Item (Join-Path $root 'README.md') $appDir
Copy-Item (Join-Path $root 'SECURITY.md') $appDir
Copy-Item (Join-Path $root 'LICENSE') $appDir

$appZip = Join-Path $dist "blendermp-windows-x64-$Version.zip"
Compress-Archive -Path $appDir -DestinationPath $appZip -CompressionLevel Optimal

Write-Host ''
Write-Host "Extension: $extension" -ForegroundColor Green
Write-Host "Desktop:   $appZip" -ForegroundColor Green
Write-Host ''
Write-Host 'Install the extension with:' -ForegroundColor Cyan
Write-Host "  blender --command extension install-file -r user_default --enable `"$extension`""
