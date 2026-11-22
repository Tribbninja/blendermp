#requires -Version 5.1
<#
  Build a release desktop app and a Blender 5.x extension zip.

  Outputs:
    dist/blender_multiplayer-<version>.zip    installable Blender extension
    dist/blendermp-windows-x64-<version>.zip  app + addon + docs
#>
param([string]$Version = "")

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root 'dist'
$tools = Join-Path $root '.tools'

. (Join-Path $PSScriptRoot '_console.ps1')
Set-ConsoleTitle 'Blendermp build'
Set-AlwaysOnTop
Write-Progress -Id 1 -Activity 'Blendermp build' -Status 'checking versions' -PercentComplete 2

if (-not $Version) {
    $Version = (Get-Content (Join-Path $root 'version.json') -Raw | ConvertFrom-Json).version
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "invalid version: $Version" }

# Catch a half-finished version bump before building anything.
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
& $python (Join-Path $root 'scripts\check_version.py')
if ($LASTEXITCODE -ne 0) { throw 'version mismatch; update every version string first' }

$mingw = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter gcc.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\mingw64\\bin\\gcc\.exe$' } | Select-Object -First 1
if ($mingw) { $env:Path = "$($mingw.DirectoryName);$env:Path" }
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA 'Temp\bmp-target'

Write-Host 'Cleaning dist...' -ForegroundColor Cyan
Write-Progress -Id 1 -Activity 'Blendermp build' -Status 'cleaning dist' -PercentComplete 5
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# --- Blender extension ------------------------------------------------------
Write-Host 'Packing Blender extension...' -ForegroundColor Cyan
Write-Progress -Id 1 -Activity 'Blendermp build' -Status 'packing Blender extension' -PercentComplete 10
$stage = Join-Path $env:TEMP "bmp-extension-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $root 'addon\blender_multiplayer\*') $stage -Recurse
Get-ChildItem $stage -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force
$extension = Join-Path $dist "blender_multiplayer-$Version.zip"
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $extension -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

# --- Release app ------------------------------------------------------------
Write-Host 'Building release desktop app (this takes a few minutes)...' -ForegroundColor Cyan
$manifest = Join-Path $root 'desktop\src-tauri\Cargo.toml'

function Invoke-CargoBuild {
    param([bool]$Offline, [string]$Label)
    $started = Get-Date
    $log = Join-Path $env:TEMP 'bmp-cargo-build.log'
    $codeFile = Join-Path $env:TEMP 'bmp-cargo-build.code'
    Remove-Item $codeFile -ErrorAction SilentlyContinue
    $wrapper = Join-Path $PSScriptRoot '_cargo_build.ps1'
    $buildArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $wrapper + '"'),
        '-Manifest', ('"' + $manifest + '"'),
        '-Log', ('"' + $log + '"'),
        '-Code', ('"' + $codeFile + '"')
    )
    if ($Offline) { $buildArgs += '-Offline' }
    $proc = Start-Process -FilePath 'powershell' -ArgumentList $buildArgs -WindowStyle Hidden -PassThru
    while (-not $proc.HasExited) {
        $proc.Refresh()
        $elapsed = [int]((Get-Date) - $started).TotalSeconds
        # Cargo has no machine-readable progress here, so ease the bar toward
        # 78% over roughly two minutes and keep the elapsed time visible.
        $pct = [math]::Min(78, 20 + [int]($elapsed / 3))
        Write-Progress -Id 1 -Activity 'Blendermp build' -Status ("{0} - {1}s (release builds take a few minutes)" -f $Label, $elapsed) -PercentComplete $pct
        Start-Sleep -Seconds 1
    }
    $code = 1
    if (Test-Path $codeFile) {
        $raw = Get-Content $codeFile -Raw -ErrorAction SilentlyContinue
        if ($raw -match '^\s*(-?\d+)\s*$') { $code = [int]$matches[1] }
    }
    if ($code -ne 0) {
        Write-Host "cargo build failed; last lines of ${log}:" -ForegroundColor Red
        Get-Content $log -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
    }
    return $code
}

$exit = Invoke-CargoBuild -Offline $true -Label 'compiling desktop app'
if ($exit -ne 0) {
    Write-Host 'Offline cargo build failed; retrying online...' -ForegroundColor Yellow
    $exit = Invoke-CargoBuild -Offline $false -Label 'compiling desktop app (online)'
}
if ($exit -ne 0) { throw 'release build failed' }
Write-Progress -Id 1 -Activity 'Blendermp build' -Status 'bundling app files' -PercentComplete 82

$appDir = Join-Path $dist 'blendermp'
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
Copy-Item (Join-Path $env:CARGO_TARGET_DIR 'release\blendermp.exe') $appDir
# The GNU build links WebView2Loader.dll dynamically. Ship it next to the exe
# so the app does not depend on another program having it on PATH.
$loader = Join-Path $env:CARGO_TARGET_DIR 'release\WebView2Loader.dll'
if (-not (Test-Path $loader)) { throw 'WebView2Loader.dll was not produced by the release build' }
Copy-Item $loader $appDir
Copy-Item (Join-Path $root 'addon') (Join-Path $appDir 'addon') -Recurse
Get-ChildItem (Join-Path $appDir 'addon') -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force
# Bundle Blender's TLS dependency (cp313) so a fresh PC can host without pip.
$vendorSource = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$vendorTarget = Join-Path $appDir 'python'
if (Test-Path (Join-Path $vendorSource 'cryptography')) {
    Copy-Item $vendorSource $vendorTarget -Recurse
    Get-ChildItem $vendorTarget -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force
    Write-Host "Bundled Python vendor: $vendorTarget" -ForegroundColor Green
} else {
    New-Item -ItemType Directory -Force -Path $vendorTarget | Out-Null
    Write-Host 'WARNING: vendor python (cryptography) not found; the installer will need it for TLS hosting.' -ForegroundColor Yellow
}
Copy-Item (Join-Path $root 'README.md') $appDir
Copy-Item (Join-Path $root 'SECURITY.md') $appDir
Copy-Item (Join-Path $root 'LICENSE') $appDir

$appZip = Join-Path $dist "blendermp-windows-x64-$Version.zip"
Compress-Archive -Path $appDir -DestinationPath $appZip -CompressionLevel Optimal

# --- Installer --------------------------------------------------------------
$makensis = Get-Command makensis.exe -ErrorAction SilentlyContinue
$makensisPath = if ($makensis) { $makensis.Source } else { $null }
if (-not $makensisPath -and (Test-Path 'C:\Program Files (x86)\NSIS\makensis.exe')) {
    $makensisPath = 'C:\Program Files (x86)\NSIS\makensis.exe'
}
$setup = Join-Path $dist "Blendermp-$Version-Setup.exe"
if ($makensisPath) {
    Write-Host 'Building installer...' -ForegroundColor Cyan
    Write-Progress -Id 1 -Activity 'Blendermp build' -Status 'building installer' -PercentComplete 92
    Push-Location $root
    try {
        & $makensisPath "/DVERSION=$Version" 'scripts\installer.nsi' | Out-Host
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) { throw 'installer build failed' }
} else {
    Write-Host 'NSIS not found; skipping installer (winget install NSIS.NSIS)' -ForegroundColor Yellow
}
Write-Progress -Id 1 -Activity 'Blendermp build' -Completed

Write-Host ''
Write-Host "Extension: $extension" -ForegroundColor Green
Write-Host "Desktop:   $appZip" -ForegroundColor Green
if (Test-Path $setup) { Write-Host "Installer: $setup" -ForegroundColor Green }
Write-Host ''
Write-Host 'Install the extension with:' -ForegroundColor Cyan
Write-Host "  blender --command extension install-file -r user_default --enable `"$extension`""
