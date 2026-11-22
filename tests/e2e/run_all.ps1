param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$failed = @()
foreach ($name in @('run_material.ps1', 'run_details.ps1', 'run_topology.ps1', 'run_resume.ps1', 'run_checkpoint.ps1', 'run_undo.ps1', 'run_lease.ps1', 'run_lock.ps1', 'run_animation.ps1', 'run_sculpt.ps1', 'run_collab.ps1', 'run_chunk.ps1', 'run_foundation.ps1')) {
    Write-Host "== $name" -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $name) -Blender $Blender
    if ($LASTEXITCODE -ne 0) {
        $failed += $name
    }
}
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($failed -join ', ')) -ForegroundColor Red
    exit 1
}
Write-Host 'ALL E2E OK' -ForegroundColor Green
