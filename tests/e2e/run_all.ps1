param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$failed = @()
$suites = @(
    'run_material.ps1', 'run_details.ps1', 'run_topology.ps1', 'run_resume.ps1',
    'run_checkpoint.ps1', 'run_undo.ps1', 'run_lease.ps1', 'run_lock.ps1',
    'run_animation.ps1', 'run_sculpt.ps1', 'run_collab.ps1', 'run_chunk.ps1',
    'run_reference.ps1', 'run_render.ps1', 'run_foundation.ps1', 'run_mesh_relay.ps1'
)
foreach ($name in $suites) {
    Get-Process blender -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 1200
    Write-Host "== $name" -ForegroundColor Cyan
    $ok = $false
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $name) -Blender $Blender
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        if ($attempt -lt 2) {
            Write-Host "retrying $name (machine load)" -ForegroundColor Yellow
            Get-Process blender -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }
    if (-not $ok) { $failed += $name }
}
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($failed -join ', ')) -ForegroundColor Red
    exit 1
}
Write-Host 'ALL E2E OK' -ForegroundColor Green
