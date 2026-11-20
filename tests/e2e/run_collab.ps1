<#
Collaboration check: the host creates a late object and sends it as a bundle;
the client previews and accepts it, including its material and image.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*collab_host.py*' -or $_.CommandLine -like '*collab_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-collab-host'
$clientData = Join-Path $env:TEMP 'bmp-collab-client'
$sync = Join-Path $env:TEMP 'bmp-collab-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'collab_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'collab_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(75)
do {
    Start-Sleep -Seconds 2
    $hostDone = Test-Path (Join-Path $hostData 'host-result.json')
    $clientDone = Test-Path (Join-Path $clientData 'client-result.json')
} while ((Get-Date) -lt $deadline -and -not ($hostDone -and $clientDone))

if (-not $clientDone) {
    Write-Host 'client did not finish' -ForegroundColor Red
    Get-Content (Join-Path $clientData 'stderr.log') -Tail 30 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $clientData 'stdout.log') -Tail 30 -ErrorAction SilentlyContinue
    exit 2
}
$client = Get-Content (Join-Path $clientData 'client-result.json') -Raw | ConvertFrom-Json
$hostResult = $null
if ($hostDone) {
    $hostResult = Get-Content (Join-Path $hostData 'host-result.json') -Raw | ConvertFrom-Json
}
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) { if (-not $condition) { $script:failures.Add($message) } }

Check ($client.reason -eq 'imported') "client finished by '$($client.reason)'"
Check ($client.bundle_sender -eq 'testhost') "bundle sender was '$($client.bundle_sender)'"
Check ($client.asset -eq 'BundleAsset') "imported asset was '$($client.asset)'"
Check (($client.materials -join ',') -match 'BundleMat') "materials: $($client.materials -join ',')"
Check (($client.images -join ',') -match 'BundleTex') "images: $($client.images -join ',')"
Check (($client.messages -join '|') -match 'sent .* object') 'client did not see the bundle notice'
Check ([math]::Abs([double]$client.bookmark_distance - 42.0) -le 0.1) "bookmark distance was '$($client.bookmark_distance)'"
Check ($client.instance_type -eq 'COLLECTION') "instance type was '$($client.instance_type)'"
Check ($client.instance_collection -eq 'InstancedSet') "instance collection was '$($client.instance_collection)'"
Check (($client.instance_objects -join ',') -match 'InstCube') "instance objects: $($client.instance_objects -join ',')"
Check ($null -ne $client.laser -and [math]::Abs($client.laser[0] - 1.0) -le 0.01) "laser point was $($client.laser -join ',')"
if ($null -ne $hostResult) {
    Check (($hostResult.messages -join '|') -match 'Sent .* object') 'host did not confirm the send'
}

Write-Host ("client asset={0} materials={1} images={2} sender={3} reason={4}" -f $client.asset, ($client.materials -join ','), ($client.images -join ','), $client.bundle_sender, $client.reason)
if ($null -ne $hostResult) {
    Write-Host ("host objects={0} messages={1}" -f ($hostResult.objects -join ','), ($hostResult.messages -join ' | '))
}
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'COLLABORATION E2E OK' -ForegroundColor Green
exit 0
