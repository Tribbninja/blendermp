param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$Out = ""
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Out) { $Out = Join-Path $root 'dist\Blendermp-Updater.exe' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Out) | Out-Null

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path $csc)) { throw 'csc.exe (.NET Framework) was not found' }

$source = Get-Content (Join-Path $PSScriptRoot 'Blendermp-Updater.cs') -Raw
$source = $source.Replace('__OWNER__', $Owner).Replace('__REPO__', $Repo)
$temp = Join-Path $env:TEMP ("bmp-updater-" + [guid]::NewGuid().ToString('N') + ".cs")
Set-Content -Path $temp -Value $source -Encoding UTF8
try {
    & $csc /nologo /target:winexe /optimize+ /out:$Out `
        /reference:System.dll /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
        $temp
    if ($LASTEXITCODE -ne 0) { throw 'bootstrapper compile failed' }
} finally {
    Remove-Item $temp -ErrorAction SilentlyContinue
}
Write-Host "Updater: $Out" -ForegroundColor Green
