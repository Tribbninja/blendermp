<#
Session render check: the host asks the client to join a 3-frame Workbench
render, both render frames, the host saves them, and an MP4 is produced.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$hostData = Join-Path $env:TEMP 'bmp-render-host'
$clientData = Join-Path $env:TEMP 'bmp-render-client'
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$hostData*" -or $_.CommandLine -like "*$clientData*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

Remove-Item $hostData, $clientData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_NO_CONSENT_DIALOG = '1'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'render_host.py') + '"'), '--', '--bmp-data', $hostData)
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'render_client.py') + '"'), '--', '--bmp-data', $clientData)
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(120)
do {
    Start-Sleep -Seconds 2
    $hostDone = Test-Path (Join-Path $hostData 'host-result.json')
    $clientDone = Test-Path (Join-Path $clientData 'client-result.json')
} while ((Get-Date) -lt $deadline -and -not ($hostDone -and $clientDone))

if (-not $clientDone) {
    Write-Host 'client did not finish' -ForegroundColor Red
    Get-Content (Join-Path $clientData 'stderr.log') -Tail 20 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $clientData 'stdout.log') -Tail 20 -ErrorAction SilentlyContinue
    exit 2
}
$client = Get-Content (Join-Path $clientData 'client-result.json') -Raw | ConvertFrom-Json
$hostResult = $null
if ($hostDone) { $hostResult = Get-Content (Join-Path $hostData 'host-result.json') -Raw | ConvertFrom-Json }
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) { if (-not $condition) { $script:failures.Add($message) } }

Check ($client.state -eq 'connected') "client state was '$($client.state)'"
Check ($client.accepted -ne '') 'client never accepted the render'
Check ([int]$client.done -gt 0) "client rendered no frames (done=$($client.done))"
if ($null -ne $hostResult) {
    Check ([bool]$hostResult.started) 'host never started the render'
    Check ([bool]$hostResult.render.finished) 'host render never finished'
    $frames = @($hostResult.frames | Where-Object { $_ -like 'frame_*.png' })
    Check ($frames.Count -eq 3) "host saved $($frames.Count) frame(s), expected 3"
    $video = @($hostResult.frames | Where-Object { $_ -like '*.mp4' })
    Check ($video.Count -ge 1) 'host did not produce an MP4'
}

Write-Host ("client accepted={0} done={1} host_frames={2} host_finished={3}" -f $client.accepted, $client.done, (@($hostResult.frames).Count), $hostResult.render.finished)
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    if ($null -ne $hostResult) { $hostResult | ConvertTo-Json -Depth 8 }
    exit 1
}
Write-Host 'RENDER E2E OK' -ForegroundColor Green
exit 0
