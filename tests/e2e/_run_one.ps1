param(
    [Parameter(Mandatory = $true)][string]$Suite,
    [Parameter(Mandatory = $true)][string]$Blender,
    [Parameter(Mandatory = $true)][string]$Log,
    [Parameter(Mandatory = $true)][string]$Code
)
& powershell -NoProfile -ExecutionPolicy Bypass -File $Suite -Blender $Blender *> $Log
Set-Content -Path $Code -Value $LASTEXITCODE
