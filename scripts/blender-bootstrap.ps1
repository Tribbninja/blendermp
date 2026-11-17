# Blendermp setup helper: download the official Blender MSI and install it
# silently. Progress is published to small text files so the NSIS page can show
# a live bar. All output is ASCII.
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Sha256,
    [Parameter(Mandatory = $true)][string]$MsiPath,
    [string]$ProgressFile = "",
    [string]$StatusFile = "",
    [string]$ExitFile = "",
    [string]$LogFile = "",
    [int]$MinBytes = 52428800,
    [switch]$DownloadOnly,
    [switch]$KeepDownload
)

$ErrorActionPreference = "Stop"

if (-not $LogFile) { $LogFile = Join-Path $env:TEMP "blendermp-blender.log" }
try { [IO.File]::WriteAllText($LogFile, "") } catch { }

function Write-Log([string]$text) {
    if ($LogFile) {
        try {
            Add-Content -Path $LogFile -Value ("{0} {1}" -f (Get-Date -Format s), $text) -Encoding UTF8
        } catch { }
    }
}

function Write-Status([string]$text) {
    Write-Log $text
    $safe = $text -replace '[^\x20-\x7E]', '?'
    if ($StatusFile) {
        try { [IO.File]::WriteAllText($StatusFile, $safe, (New-Object Text.UTF8Encoding($false))) } catch { }
    }
}

function Set-ProgressValue([int]$percent) {
    if ($percent -lt 0) { $percent = 0 }
    if ($percent -gt 100) { $percent = 100 }
    if ($ProgressFile) {
        try { [IO.File]::WriteAllText($ProgressFile, [string]$percent, (New-Object Text.UTF8Encoding($false))) } catch { }
    }
}

function Finish([int]$code) {
    if ($ExitFile) {
        try { [IO.File]::WriteAllText($ExitFile, [string]$code, (New-Object Text.UTF8Encoding($false))) } catch { }
    }
    if (-not $KeepDownload -and (Test-Path $MsiPath)) {
        try { Remove-Item -Path $MsiPath -Force -ErrorAction SilentlyContinue } catch { }
    }
    Write-Log "exit $code"
    exit $code
}

try {
    $dir = Split-Path -Parent $MsiPath
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Path $MsiPath) { Remove-Item -Path $MsiPath -Force -ErrorAction SilentlyContinue }

    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

    Set-ProgressValue 0
    Write-Status "Connecting to blender.org..."

    $request = $null
    $response = $null
    $stream = $null
    $file = $null
    $total = 0
    $read = 0

    $request = [Net.HttpWebRequest]::Create($Url)
    $request.UserAgent = "Blendermp-Setup/0.1"
    $request.AllowAutoRedirect = $true
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 60000
    $response = $request.GetResponse()
    $total = $response.ContentLength
    $stream = $response.GetResponseStream()
    $file = [IO.File]::Open($MsiPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $buffer = New-Object byte[] 1048576
    $last = -1

    while (($n = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $file.Write($buffer, 0, $n)
        $read += $n
        if ($total -gt 0) {
            $pct = [int](($read * 100) / $total)
            if ($pct -ne $last) {
                $last = $pct
                Set-ProgressValue ([int]($pct * 0.9))
                Write-Status ("Downloading Blender 5.3... {0}% ({1:N0} of {2:N0} MB)" -f $pct, ($read / 1MB), ($total / 1MB))
            }
        }
    }
}
catch {
    Write-Status ("Blender download failed: " + $_.Exception.Message)
    Finish 10
}
finally {
    if ($file) { try { $file.Close() } catch { } }
    if ($stream) { try { $stream.Close() } catch { } }
    if ($response) { try { $response.Close() } catch { } }
}

try {
    $size = (Get-Item $MsiPath).Length
    Write-Log "downloaded $size bytes"
    if ($size -lt $MinBytes) {
        Write-Status "The Blender download was incomplete."
        Finish 11
    }

    Set-ProgressValue 91
    Write-Status "Verifying the Blender download..."
    $actual = (Get-FileHash -Algorithm SHA256 -Path $MsiPath).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Write-Log "checksum mismatch: $actual"
        Write-Status "The Blender download failed verification."
        Finish 12
    }
    Write-Log "checksum ok"

    if ($DownloadOnly) {
        Set-ProgressValue 100
        Write-Status "Blender download verified."
        Finish 0
    }

    Set-ProgressValue 94
    Write-Status "Installing Blender 5.3 (Windows may ask for permission)..."
    $msiArgs = "/i `"$MsiPath`" /qn /norestart ALLUSERS=1"
    $msiexec = Join-Path $env:SystemRoot "System32\msiexec.exe"
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $exitCode = -1
    try {
        if ($isAdmin) {
            $proc = Start-Process -FilePath $msiexec -ArgumentList $msiArgs -Wait -PassThru -WindowStyle Hidden
        }
        else {
            $proc = Start-Process -FilePath $msiexec -ArgumentList $msiArgs -Verb RunAs -Wait -PassThru
        }
        $exitCode = $proc.ExitCode
    }
    catch {
        Write-Status "Blender installation was cancelled or blocked."
        Write-Log ("msiexec error: " + $_.Exception.Message)
        Finish 13
    }
    Write-Log "msiexec exit $exitCode"

    Set-ProgressValue 98
    Write-Status "Checking the Blender installation..."
    $roots = @()
    foreach ($kind in @("ProgramFiles", "ProgramFilesX86")) {
        $base = [Environment]::GetFolderPath($kind)
        if ($base) { $roots += (Join-Path $base "Blender Foundation") }
    }
    $found = $null
    foreach ($root in $roots) {
        if (Test-Path $root) {
            $found = Get-ChildItem -Path $root -Filter blender.exe -Recurse -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1
            if ($found) { break }
        }
    }
    if (-not $found) {
        Write-Status "Blender was installed but blender.exe was not found."
        Finish 14
    }

    Set-ProgressValue 100
    Write-Status ("Blender 5.3 is installed at " + $found.DirectoryName)
    Finish 0
}
catch {
    Write-Status ("Blender setup failed: " + $_.Exception.Message)
    Finish 15
}
