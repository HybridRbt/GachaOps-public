param(
    [string]$Python = 'python',
    [string]$OutputDirectory,
    [string]$DependenciesDirectory,
    [switch]$RequireClean
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = (Get-Command $Python -ErrorAction Stop).Source
$taskPreviousPythonPath = $env:PYTHONPATH
$taskPreviousPath = $env:PATH
$taskStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $taskRoot ('build/candidates/' + $taskStamp) }
$taskOutput = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $taskOutput) { throw 'Candidate output already exists; choose a new directory' }
Push-Location -LiteralPath $taskRoot
try {
    $taskCommit = & git -c "safe.directory=$taskRoot" rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Cannot determine build source commit' }
    $taskDirty = [bool](& git -c "safe.directory=$taskRoot" status --porcelain)
    if ($RequireClean -and $taskDirty) { throw 'Release candidates require a clean reviewed commit' }
    if (-not $DependenciesDirectory) {
        $DependenciesDirectory = Join-Path $taskRoot 'build/gui-deps'
        & $taskPython -m pip install --target $DependenciesDirectory -r requirements-desktop.txt
        if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed' }
    }
    $env:PYTHONPATH = (Resolve-Path -LiteralPath $DependenciesDirectory).Path
    & $taskPython -c "from importlib.metadata import version; assert version('PySide6-Essentials') == '6.10.2'; assert version('PyInstaller') == '6.22.0'; assert version('shiboken6') == '6.10.2'"
    if ($LASTEXITCODE -ne 0) { throw 'Pinned build dependencies do not match' }
    New-Item -ItemType Directory -Path $taskOutput | Out-Null
    # Native tools in a developer's PATH may carry incompatible ICU/Qt DLLs.
    $env:PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    & $taskPython -m PyInstaller --noconfirm --distpath $taskOutput --workpath (Join-Path $taskRoot ('build/package-' + $taskStamp)) GachaOps.spec *> (Join-Path $taskOutput 'build.log')
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed' }
    $taskExecutable = Join-Path $taskOutput 'GachaOps/GachaOps.exe'
    $taskPythonVersion = & $taskPython -c "import platform; print(platform.python_version())"
    $taskVersion = & $taskPython -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
    $taskManifest = @{
        schema_version=1; commit=$taskCommit; dirty=$taskDirty; version=$taskVersion;
        python=$taskPythonVersion; platform='windows-x64'; signature='unsigned';
        dependencies=@{PySide6_Essentials='6.10.2'; Shiboken6='6.10.2'; PyInstaller='6.22.0'};
        executable_sha256=(Get-FileHash -LiteralPath $taskExecutable -Algorithm SHA256).Hash
    }
    $taskManifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $taskOutput 'GachaOps/build-manifest.json')
    Write-Output $taskExecutable
} finally {
    if ($null -eq $taskPreviousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $taskPreviousPythonPath }
    $env:PATH = $taskPreviousPath
    Pop-Location
}
