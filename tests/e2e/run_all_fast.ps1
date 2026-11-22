param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe',
    [int]$Parallel = 2,
    [string]$Only = "",
    [switch]$Window
)
$ErrorActionPreference = 'Stop'

# Open a persistent, always-on-top console window with the live progress bar.
if ($Window) {
    $launcher = Join-Path $PSScriptRoot 'run_tests.cmd'
    $args = @('/c', ('"' + $launcher + '"'))
    Start-Process -FilePath 'cmd.exe' -ArgumentList $args -WindowStyle Normal
    return
}

. (Join-Path $PSScriptRoot '..\..\scripts\_console.ps1')
Set-ConsoleTitle 'Blendermp E2E'
Set-AlwaysOnTop

$suites = @(
    'run_material.ps1', 'run_details.ps1', 'run_topology.ps1', 'run_resume.ps1',
    'run_checkpoint.ps1', 'run_undo.ps1', 'run_lease.ps1', 'run_lock.ps1',
    'run_animation.ps1', 'run_sculpt.ps1', 'run_collab.ps1', 'run_chunk.ps1',
    'run_reference.ps1', 'run_render.ps1', 'run_foundation.ps1', 'run_mesh_relay.ps1'
)
if ($Only) {
    $suites = @($suites | Where-Object { $_ -like "*$Only*" })
}
$total = $suites.Count
if ($total -eq 0) { Write-Host "No suites matched '$Only'" -ForegroundColor Red; exit 1 }

$logDir = Join-Path $env:TEMP 'bmp-e2e-fast'
Remove-Item $logDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Start-Suite([string]$name, [int]$attempt) {
    $scriptPath = Join-Path $PSScriptRoot $name
    $log = Join-Path $logDir ("{0}.{1}.log" -f $name, $attempt)
    $codeFile = Join-Path $logDir ("{0}.{1}.code" -f $name, $attempt)
    $wrapper = Join-Path $PSScriptRoot '_run_one.ps1'
    $proc = Start-Process -FilePath 'powershell' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $wrapper + '"'),
        '-Suite', ('"' + $scriptPath + '"'),
        '-Blender', ('"' + $Blender + '"'),
        '-Log', ('"' + $log + '"'),
        '-Code', ('"' + $codeFile + '"')
    ) -PassThru -WindowStyle Hidden
    return [pscustomobject]@{ Name = $name; Proc = $proc; Log = $log; Code = $codeFile; Attempt = $attempt }
}

function Show-Progress([int]$completed) {
    $percent = [math]::Max(0, [math]::Min(100, [int](($completed / $total) * 100)))
    $status = "{0}/{1} done Â- {2} passed Â- {3} running" -f $completed, $total, $passed, $running.Count
    Write-Progress -Id 1 -Activity 'Blendermp E2E' -Status $status -PercentComplete $percent
}

$queue = [System.Collections.Generic.Queue[string]]::new()
foreach ($suite in $suites) { $queue.Enqueue($suite) }
$running = @()
$failed = @()
$passed = 0
$completed = 0
$started = Get-Date

Write-Host ''
Write-Host ("Blendermp E2E - {0} suites, {1} at a time" -f $total, $Parallel) -ForegroundColor Cyan
Write-Host ''
Show-Progress 0

while ($queue.Count -gt 0 -or $running.Count -gt 0) {
    while ($running.Count -lt $Parallel -and $queue.Count -gt 0) {
        $name = $queue.Dequeue()
        $running += Start-Suite $name 1
        Write-Host ("  start  {0}" -f $name) -ForegroundColor DarkGray
    }
    Start-Sleep -Milliseconds 600
    $finished = @($running | Where-Object {
        $_.Proc.Refresh()
        $_.Proc.HasExited
    })
    foreach ($entry in $finished) {
        $running = @($running | Where-Object { $_ -ne $entry })
        $code = 1
        if (Test-Path $entry.Code) {
            $raw = Get-Content $entry.Code -Raw -ErrorAction SilentlyContinue
            if ($raw -match '^\s*(-?\d+)\s*$') { $code = [int]$matches[1] }
        }
        if ($code -eq 0) {
            $passed += 1
            $completed += 1
            Write-Host ("  ok     {0}" -f $entry.Name) -ForegroundColor Green
        } elseif ($entry.Attempt -lt 2) {
            Write-Host ("  retry  {0} (exit {1})" -f $entry.Name, $code) -ForegroundColor Yellow
            Get-Content $entry.Log -Tail 6 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkYellow }
            Start-Sleep -Milliseconds 800
            $running += Start-Suite $entry.Name 2
        } else {
            $failed += $entry.Name
            $completed += 1
            Write-Host ("  FAIL   {0}" -f $entry.Name) -ForegroundColor Red
            Get-Content $entry.Log -Tail 12 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkRed }
        }
        Show-Progress $completed
    }
}

Show-Progress $total
Write-Progress -Id 1 -Activity 'Blendermp E2E' -Completed
$elapsed = [int]((Get-Date) - $started).TotalSeconds
Write-Host ''
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($failed -join ', ')) -ForegroundColor Red
    Write-Host ("{0}/{1} suites passed in {2}s" -f $passed, $total, $elapsed) -ForegroundColor Yellow
    exit 1
}
Write-Host ("ALL E2E OK - {0}/{1} in {2}s" -f $passed, $total, $elapsed) -ForegroundColor Green
exit 0


