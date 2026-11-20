<#
Reconnect/resume check: the client socket is closed mid-session; it must
reconnect with the same scene and leases instead of a full reset.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*resume_host.py*' -or $_.CommandLine -like '*resume_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-resume-host'
$clientData = Join-Path $env:TEMP 'bmp-resume-client'
$sync = Join-Path $env:TEMP 'bmp-resume-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'resume_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'resume_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(75)
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
if ($hostDone) {
    $hostResult = Get-Content (Join-Path $hostData 'host-result.json') -Raw | ConvertFrom-Json
}
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) {
    if (-not $condition) { $script:failures.Add($message) }
}

Check ($client.reason -eq 'resumed') "client finished by '$($client.reason)'"
Check ($null -ne $client.loaded) 'client never loaded the host scene'
Check ($null -ne $client.dropped) 'client socket was not dropped'
Check ($null -ne $client.reconnected) 'client did not reconnect'
Check ($client.replacing -eq 1) "client replaced its scene $($client.replacing) times"
Check ($client.kept -eq $true) 'scene was not kept across reconnect'
Check ((($client.messages -join '|') -match 'Restored 1 offline edit') -or (($client.messages -join '|') -match 'Offline journal resolved')) 'client did not report journal replay'
Check ($null -ne $client.moved -and [math]::Abs($client.moved[0] - 3.0) -le 0.01) "moved cube was $($client.moved -join ',')"
if ($null -ne $hostResult) {
    Check (($hostResult.messages -join '|') -match 'reconnected') 'host did not report the reconnect'
    $jc = $hostResult.phase.journal_cube
    Check ($null -ne $jc -and [math]::Abs($jc[0]-2) -le 0.01 -and [math]::Abs($jc[1]-3) -le 0.01 -and [math]::Abs($jc[2]-4) -le 0.01) "offline journal cube was $($jc -join ',')"
}

Write-Host ("client loaded={0} dropped={1} reconnected={2} replacing={3} cube={4} reason={5}" -f $client.loaded, $client.dropped, $client.reconnected, $client.replacing, ($client.moved -join ','), $client.reason)
if ($null -ne $hostResult) {
    Write-Host ("host messages: " + ($hostResult.messages -join ' | '))
}
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'RESUME E2E OK' -ForegroundColor Green
exit 0
