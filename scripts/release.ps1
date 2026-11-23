param(
    [string]$Version = "",
    [string]$Repo = "",
    [switch]$Draft
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
if (-not (Test-Path $gh)) { $gh = 'gh' }

if (-not $Version) {
    $Version = (Get-Content (Join-Path $root 'version.json') -Raw | ConvertFrom-Json).version
}
if (-not $Repo) {
    $remote = (& git -C $root remote get-url origin 2>$null)
    if ($remote -match 'github\.com[:/](?<repo>[^/]+/[^/.]+)') {
        $Repo = $matches['repo']
    } else {
        throw 'pass -Repo owner/name or add a GitHub origin remote'
    }
}

$dist = Join-Path $root 'dist'
$assets = @(
    (Join-Path $dist "Blendermp-$Version-Setup.exe"),
    (Join-Path $dist "blendermp-windows-x64-$Version.zip"),
    (Join-Path $dist "blender_multiplayer-$Version.zip"),
    (Join-Path $dist "Blendermp-Updater.exe")
)
foreach ($asset in $assets) {
    if (-not (Test-Path $asset)) { throw "missing asset: $asset" }
}

$tag = "v$Version"
& git -C $root tag -f $tag
& git -C $root push origin $tag --force

$releaseArgs = @(
    'release', 'create', $tag,
    '--repo', $Repo,
    '--title', "Blendermp $Version",
    '--notes', "Blendermp $Version. Run Blendermp-Updater.exe to install or update; it always fetches this release."
)
if ($Draft) { $releaseArgs += '--draft' }
foreach ($asset in $assets) { $releaseArgs += $asset }
& $gh @releaseArgs
Write-Host "Release $tag published to $Repo" -ForegroundColor Green
