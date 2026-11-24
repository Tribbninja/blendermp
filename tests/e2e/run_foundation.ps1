<#
Headless foundation check for 0.9.0: AI tools, mesh export, bridge, broadcast,
recording/replay tap.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*foundation_check.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 400

$data = Join-Path $env:TEMP 'bmp-foundation'
Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $data | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_TEST_DATA = $data
# Keep heartbeats, status and control files inside this suite's temp dir so a
# running desktop app or a previous suite cannot leak a stale crash record.
$env:LOCALAPPDATA = $data

$args = @(
    '--background',
    '--factory-startup',
    '--python', ('"' + (Join-Path $PSScriptRoot 'foundation_check.py') + '"')
)
$process = Start-Process -FilePath $Blender -ArgumentList $args -PassThru `
    -RedirectStandardOutput (Join-Path $data 'stdout.log') `
    -RedirectStandardError (Join-Path $data 'stderr.log')

$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 1
    $done = Test-Path (Join-Path $data 'foundation-result.json')
} while ((Get-Date) -lt $deadline -and -not $done -and -not $process.HasExited)

if (-not $done) {
    Write-Host 'foundation test did not finish' -ForegroundColor Red
    Get-Content (Join-Path $data 'stderr.log') -Tail 30 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $data 'stdout.log') -Tail 30 -ErrorAction SilentlyContinue
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    exit 2
}

$result = Get-Content (Join-Path $data 'foundation-result.json') -Raw | ConvertFrom-Json
if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
$result | ConvertTo-Json -Depth 8
if (-not $result.ok) {
    Write-Host 'FOUNDATION FAIL' -ForegroundColor Red
    exit 1
}
Write-Host 'FOUNDATION E2E OK' -ForegroundColor Green
exit 0
