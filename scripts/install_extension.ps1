<#
  Install the packed Blender extension and enable it.
  Usage: scripts/install_extension.ps1 [-Zip dist/blender_multiplayer-0.1.0.zip]
#>
param([string]$Zip = "")

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Zip) {
    $Zip = Get-ChildItem (Join-Path $root 'dist') -Filter 'blender_multiplayer-*.zip' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Zip -or -not (Test-Path $Zip)) { throw 'No extension zip found. Run scripts/package.ps1 first.' }

$blender = Get-ChildItem 'C:\Program Files\Blender Foundation' -Filter blender.exe -Recurse -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $blender) { throw 'Blender 5.x was not found.' }

Write-Host "Installing $Zip" -ForegroundColor Cyan
& $blender.FullName --command extension install-file -r user_default -e $Zip
if ($LASTEXITCODE -ne 0) { throw 'extension install failed' }
Write-Host 'Installed and enabled. Open Blender and check View3D > Sidebar > Blendermp.' -ForegroundColor Green
