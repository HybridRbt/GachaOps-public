[CmdletBinding()]
param(
    [string]$Configuration,
    [string]$PythonExecutable,
    [string]$OutputDirectory,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
if (-not $Configuration) { $Configuration = Join-Path $projectDirectory 'config\local.assistants.json' }
if (-not $CheckOnly) {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Open an administrator PowerShell, then run this script again. No assistant started.'
    }
}
if (-not $PythonExecutable) {
    $PythonExecutable = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'Supply -PythonExecutable with Python 3.11+.' }
$resolvedConfiguration = (Resolve-Path -LiteralPath $Configuration).Path
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectDirectory 'runs' }
$action = if ($CheckOnly) { 'check-assistants' } else { 'run-assistants' }
Push-Location -LiteralPath $projectDirectory
try {
    & $PythonExecutable -m gachaops $action --config $resolvedConfiguration --output $OutputDirectory
    $result = $LASTEXITCODE
} finally { Pop-Location }
exit $result
