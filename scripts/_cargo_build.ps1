param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$Log,
    [Parameter(Mandatory = $true)][string]$Code,
    [switch]$Offline
)
$args = @('build', '--release')
if ($Offline) { $args += '--offline' }
$args += @('--manifest-path', $Manifest)
& cargo @args *> $Log
Set-Content -Path $Code -Value $LASTEXITCODE
