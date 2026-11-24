#requires -Version 5.1
<#
  Build a self-contained Blender extension zip that does not need the desktop
  app. It includes the addon plus the Python packages that Blender's bundled
  Python does not ship (cryptography/cffi/pycparser).

  cryptography ships an abi3 extension, so one copy covers every supported
  Blender. cffi's _cffi_backend is built per Python version, so the script
  bundles the cp311/cp312/cp313 builds (Blender 4.2 through 5.x).

  imageio-ffmpeg is intentionally left out: its Windows ffmpeg binary is ~100 MB.
  Without it the addon still renders PNG frames; only MP4/WebM compilation
  reports that imageio-ffmpeg is missing.
#>
param([string]$Version = "")
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root 'dist'
if (-not $Version) {
    $Version = (Get-Content (Join-Path $root 'version.json') -Raw | ConvertFrom-Json).version
}
$stage = Join-Path $env:TEMP "bmp-standalone-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $root 'addon\blender_multiplayer\*') $stage -Recurse
Get-ChildItem $stage -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force

$vendorSource = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$vendorTarget = Join-Path $stage 'vendor'
New-Item -ItemType Directory -Force -Path $vendorTarget | Out-Null
foreach ($pattern in @(
    'cryptography',
    'cffi',
    'pycparser',
    'cryptography-*.dist-info',
    'cffi-*.dist-info',
    'pycparser-*.dist-info'
)) {
    Get-ChildItem $vendorSource -Filter $pattern -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $vendorTarget $_.Name) -Recurse
    }
}
Get-ChildItem $vendorSource -Filter '_cffi_backend*.pyd' -File -ErrorAction SilentlyContinue |
    Copy-Item -Destination $vendorTarget

# Fetch the per-Python cffi extension modules once, then reuse the cache.
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$wheelCache = Join-Path $root 'build\wheels'
foreach ($py in @('311', '312', '313')) {
    $target = Join-Path $wheelCache "cp$py"
    $wheel = Get-ChildItem $target -Filter 'cffi-*.whl' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $wheel) {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        & $python -m pip download 'cffi==2.1.1' --no-deps --only-binary=:all: `
            --platform win_amd64 --python-version $py --implementation cp --abi "cp$py" `
            -d $target --quiet
        $wheel = Get-ChildItem $target -Filter 'cffi-*.whl' | Select-Object -First 1
    }
    if ($wheel) {
        $unpacked = Join-Path $env:TEMP "bmp-cffi-$py-$([guid]::NewGuid().ToString('N'))"
        New-Item -ItemType Directory -Force -Path $unpacked | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($wheel.FullName, $unpacked)
        Get-ChildItem $unpacked -Filter '_cffi_backend*.pyd' -File |
            Copy-Item -Destination $vendorTarget -Force
        Remove-Item $unpacked -Recurse -Force
    }
}
Get-ChildItem $vendorTarget -Directory -Filter '__pycache__' -Recurse | Remove-Item -Recurse -Force

New-Item -ItemType Directory -Force -Path $dist | Out-Null
$zip = Join-Path $dist "blender_multiplayer-standalone-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force
Write-Host "Standalone addon: $zip"
