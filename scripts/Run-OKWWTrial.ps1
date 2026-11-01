[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PreparedPlan,
    [Parameter(Mandatory = $true)][string]$ApprovedPlanSha256,
    [string]$PythonExecutable,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open an administrator PowerShell for the authorized trial. This script does not elevate itself.'
}
$projectDirectory = Split-Path -Parent $PSScriptRoot
$resolvedPlan = (Resolve-Path -LiteralPath $PreparedPlan).Path
$actualHash = (Get-FileHash -LiteralPath $resolvedPlan -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ApprovedPlanSha256.ToLowerInvariant() -ne $actualHash) {
    throw 'The prepared plan changed or its exact approval hash is missing.'
}
if (-not $PythonExecutable) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        $PythonExecutable = $bundledPython
    } else {
        throw 'Supply -PythonExecutable with a Python 3.11+ interpreter.'
    }
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw 'Python executable does not exist.'
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectDirectory 'runs'
}
Push-Location -LiteralPath $projectDirectory
try {
    & $PythonExecutable -m gachaops trial-ok --config $resolvedPlan --approved-plan-sha256 $actualHash --output $OutputDirectory
    $trialExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $trialExitCode
