<#
Object details two-Blender check: parenting, matrix_parent_inverse, modifiers on a
non-active object, collection membership/visibility, object colour and flags.

    powershell -NoProfile -ExecutionPolicy Bypass -File tests/e2e/run_details.ps1
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*details_host.py*' -or $_.CommandLine -like '*details_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-details-host'
$clientData = Join-Path $env:TEMP 'bmp-details-client'
$sync = Join-Path $env:TEMP 'bmp-details-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'details_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'details_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(70)
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
$initial = $client.initial
$settled = $client.settled
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) {
    if (-not $condition) { $script:failures.Add($message) }
}

Check ($client.reason -eq 'changed') "client finished by '$($client.reason)'"
Check ($null -ne $initial) 'client saw ChildCube'
if ($null -ne $initial) {
    Check ($initial.parent -eq 'RigRoot') "initial parent was '$($initial.parent)'"
    Check ([math]::Abs($initial.world[0] - 3.0) -le 0.01) "initial world x was $($initial.world[0])"
    Check ($initial.modifiers -contains 'SUBSURF' -and $initial.modifiers -contains 'BEVEL') "initial modifiers were $($initial.modifiers -join ',')"
    Check ($initial.modifiers -contains 'BOOLEAN') "initial modifiers were $($initial.modifiers -join ',')"
    Check ($initial.boolean.operation -eq 'DIFFERENCE') "initial boolean operation was $($initial.boolean.operation)"
    Check ($initial.boolean.object -eq 'Cutter') "initial boolean operand was $($initial.boolean.object)"
    Check ($initial.subsurf_levels -eq 2) "initial subsurf levels were $($initial.subsurf_levels)"
    Check ([math]::Abs($initial.color[0] - 0.2) -le 0.01 -and [math]::Abs($initial.color[1] - 0.8) -le 0.01) "initial colour was $($initial.color -join ',')"
    Check ($initial.hide_render -eq $true) 'initial hide_render was not true'
    Check ($initial.constraints -contains 'Follow Root') "initial constraints were $($initial.constraints -join ',')"
    Check ($initial.collections -contains 'Props') "initial collections were $($initial.collections -join ',')"
    Check ($initial.collection_hide_render -eq $true) 'initial collection hide_render was not true'
}
if ($null -ne $settled) {
    Check ([math]::Abs($settled.world[0] - 6.0) -le 0.01) "changed world x was $($settled.world[0])"
    Check ($settled.subsurf_levels -eq 1) "changed subsurf levels were $($settled.subsurf_levels)"
    Check ([math]::Abs($settled.color[0] - 0.9) -le 0.01) "changed colour was $($settled.color -join ',')"
    Check ($settled.collection_hide_render -eq $false) 'changed collection hide_render was not false'
    Check ($settled.collection_hide_viewport -eq $true) 'changed collection hide_viewport was not true'
}
if ($null -ne $hostResult -and $null -ne $settled) {
    Check ($hostResult.summary.subsurf_levels -eq $settled.subsurf_levels) 'host/client subsurf levels differ'
    Check (($hostResult.summary.collections -join ',') -eq ($settled.collections -join ',')) 'host/client collections differ'
    Check ($hostResult.summary.boolean.operation -eq $settled.boolean.operation) 'host/client boolean operation differ'
    Check ($hostResult.summary.boolean.object -eq $settled.boolean.object) 'host/client boolean operand differ'
}

Write-Host ("client: parent={0} world={1} modifiers={2} subsurf={3} color={4} hide_render={5} collections={6} coll_render={7} coll_view={8} reason={9}" -f $settled.parent, ($settled.world -join ','), ($settled.modifiers -join ','), $settled.subsurf_levels, ($settled.color -join ','), $settled.hide_render, ($settled.collections -join ','), $settled.collection_hide_render, $settled.collection_hide_viewport, $client.reason)
if ($null -ne $hostResult) {
    Write-Host ("host:   {0}" -f ($hostResult.summary | ConvertTo-Json -Compress))
}
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    Write-Host 'client result:'; $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'OBJECT DETAILS E2E OK' -ForegroundColor Green
exit 0
