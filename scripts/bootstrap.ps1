#requires -Version 5.1
<#
  One-command developer bootstrap.

  Installs only what this project needs, user-scoped where possible:
    * Node.js LTS            (tooling, no build step required by the app)
    * Rust + GNU toolchain    (Tauri; avoids the multi-GB MSVC build tools)
    * MinGW-w64 (WinLibs)     (linker for the GNU toolchain)
    * Blender 5.x             (runtime for the addon)

  Everything is placed under the repo's .tools folder except Node and rustup,
  which use their normal per-user locations. Re-running is safe.
#>
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root '.tools'
$log = Join-Path $tools 'bootstrap.log'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

function Log([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -Path $log -Value $line
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$user;$machine"
}

function Install-Winget([string]$Id, [string]$Scope = 'user') {
    Log "winget install $Id ($Scope)"
    $args = @('install', '--id', $Id, '--exact', '--silent', '--accept-package-agreements',
        '--accept-source-agreements', '--disable-interactivity')
    if ($Scope) { $args += @('--scope', $Scope) }
    & winget @args
    if ($LASTEXITCODE -ne 0) { Log "  (exit $LASTEXITCODE, continuing)" }
}

Log "=== Blendermp bootstrap ==="

# --- Node -------------------------------------------------------------------
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Install-Winget 'OpenJS.NodeJS.LTS' 'user'
    Refresh-Path
} else {
    Log "node already present: $((node --version))"
}

# --- Rust -------------------------------------------------------------------
if (-not (Get-Command rustup -ErrorAction SilentlyContinue)) {
    Install-Winget 'Rustlang.Rustup' 'user'
    Refresh-Path
}
$cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
if (Test-Path $cargoBin) { $env:Path = "$cargoBin;$env:Path" }

if (Get-Command rustup -ErrorAction SilentlyContinue) {
    Log "installing GNU Rust toolchain (no MSVC needed)"
    rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal
    rustup default stable-x86_64-pc-windows-gnu
} else {
    Log "rustup unavailable; Tauri build will be skipped"
}

# --- MinGW ------------------------------------------------------------------
$gcc = Get-ChildItem -Path $tools -Filter gcc.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\bin\\gcc\.exe$' } | Select-Object -First 1
if (-not $gcc) {
    Log "installing WinLibs MinGW via winget"
    Install-Winget 'BrechtSanders.WinLibs.POSIX.UCRT' 'user'
    $gcc = Get-ChildItem -Path $tools -Filter gcc.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\bin\\gcc\.exe$' } | Select-Object -First 1
    if (-not $gcc) {
        $gcc = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter gcc.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\mingw64\\bin\\gcc\.exe$' } | Select-Object -First 1
    }
}
if (-not $gcc) {
    Log "downloading WinLibs MinGW"
    try {
        $headers = @{ 'User-Agent' = 'blendermp-bootstrap' }
        $release = Invoke-RestMethod 'https://api.github.com/repos/brechtsanders/winlibs_mingw/releases/latest' -Headers $headers
        $asset = $release.assets |
            Where-Object { $_.name -match '^winlibs-x86_64-posix-seh-ucrt-.*\.zip$' } |
            Select-Object -First 1
        if (-not $asset) { throw 'no matching WinLibs asset' }
        $zip = Join-Path $tools 'winlibs.zip'
        Log "  $($asset.name)"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers $headers
        Expand-Archive -Path $zip -DestinationPath (Join-Path $tools 'mingw') -Force
        Remove-Item $zip -Force
        $gcc = Get-ChildItem -Path $tools -Filter gcc.exe -Recurse |
            Where-Object { $_.FullName -match '\\bin\\gcc\.exe$' } | Select-Object -First 1
    } catch {
        Log "  MinGW download failed: $_"
    }
}
if ($gcc) {
    $mingwBin = Split-Path -Parent $gcc.FullName
    Log "mingw: $mingwBin"
    $env:Path = "$mingwBin;$env:Path"
}

# --- Blender ----------------------------------------------------------------
$blender = Get-ChildItem -Path 'C:\Program Files\Blender Foundation' -Filter blender.exe -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $blender) {
    Install-Winget 'BlenderFoundation.Blender' 'machine'
    $blender = Get-ChildItem -Path 'C:\Program Files\Blender Foundation' -Filter blender.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
}
if ($blender) {
    Log "blender: $($blender.FullName)"
    $blender.FullName | Set-Content (Join-Path $tools 'blender-path.txt')

    # Blender disables Python's user site, so TLS certificate generation needs a
    # per-user vendor directory that the addon adds to sys.path itself.
    $blenderRoot = Split-Path -Parent $blender.FullName
    $blenderPy = Get-ChildItem -Path $blenderRoot -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\python\\bin\\python\.exe$' } | Select-Object -First 1
    if ($blenderPy) {
        $vendor = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
        New-Item -ItemType Directory -Force -Path $vendor | Out-Null
        Log "installing cryptography for Blender into $vendor"
        & $blenderPy.FullName -m pip install -q --target $vendor cryptography
        if ($LASTEXITCODE -ne 0) { Log "  cryptography install failed (hosting will report it)" }
    } else {
        Log "Blender python.exe not found; skipping the TLS dependency"
    }
} else {
    Log "Blender not installed; the desktop app can still be built"
}

# --- Project dependencies ---------------------------------------------------
Push-Location $root
try {
    if (Test-Path 'requirements-dev.txt') {
        Log "python deps"
        if (Test-Path '.venv\Scripts\python.exe') {
            & .\.venv\Scripts\python.exe -m pip install -q -r requirements-dev.txt
        } else {
            python -m pip install -q -r requirements-dev.txt
        }
    }
} finally {
    Pop-Location
}

Log "=== done ==="
Log "node:  $((node --version) 2>$null)"
Log "cargo: $((cargo --version) 2>$null)"
Log "gcc:   $((gcc --version) 2>$null | Select-Object -First 1)"
