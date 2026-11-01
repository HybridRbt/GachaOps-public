param([Parameter(Mandatory)][string]$AppDirectory,[Parameter(Mandatory)][string]$EvidenceDirectory)
$ErrorActionPreference='Stop'
$taskApp=(Resolve-Path -LiteralPath $AppDirectory).Path
$taskEvidence=[IO.Path]::GetFullPath($EvidenceDirectory)
if (Test-Path -LiteralPath $taskEvidence) { throw 'Smoke evidence directory already exists' }
if (-not (Test-Path -LiteralPath (Join-Path $taskApp 'GachaOps.exe'))) { throw 'Frozen application missing' }
New-Item -ItemType Directory -Path $taskEvidence | Out-Null
$taskCopy=Join-Path $taskEvidence 'app with 空格'
Copy-Item -LiteralPath $taskApp -Destination $taskCopy -Recurse
$taskExe=Join-Path $taskCopy 'GachaOps.exe'
$taskData=Join-Path $taskEvidence 'fresh 数据'
$taskScreenshot=Join-Path $taskEvidence 'first-start.png'
function Invoke-SmokeProcess([string]$Arguments) {
    $taskChild=Start-Process -FilePath $taskExe -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $taskChild.WaitForExit(30000)) {
        $taskChild.Kill()
        throw 'Frozen no-game smoke timed out'
    }
    if ($taskChild.ExitCode -ne 0) { throw ('Frozen smoke failed: ' + $taskChild.ExitCode) }
}
Invoke-SmokeProcess ('--data-root "'+$taskData+'" --smoke-test "'+$taskScreenshot+'"')
if (-not (Test-Path -LiteralPath $taskScreenshot)) { throw 'GUI screenshot not produced' }
$taskConfigs=@(Get-ChildItem -LiteralPath (Join-Path $taskData 'config/profiles') -Filter config.json -Recurse)
if ($taskConfigs.Count -ne 1) { throw 'Unexpected first-start profile count' }
$taskConfiguration=Get-Content -LiteralPath $taskConfigs[0].FullName -Raw | ConvertFrom-Json
if ($taskConfiguration.assistants.Count -ne 7 -or @($taskConfiguration.assistants | Where-Object {$_.enabled -or $_.installation_root}).Count -ne 0) {
    throw 'First start was not disabled and unregistered'
}
if (Test-Path -LiteralPath (Join-Path $taskData 'state/execution.json')) { throw 'GUI first start wrote an execution journal' }
$taskLog=Join-Path $taskEvidence 'worker.log'
Invoke-SmokeProcess ('--worker --log "'+$taskLog+'" recovery-status --data-root "'+$taskData+'"')
$taskWorker=Get-Content -LiteralPath $taskLog -Raw | ConvertFrom-Json
if ($taskWorker.phase -ne 'unused') { throw 'Frozen worker used an unexpected journal' }
@{schema_version=1; gui='passed'; worker='passed'; first_start='seven_disabled_unregistered';
  executable_sha256=(Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash;
  game_execution=$false; clean_machine_acceptance=$false} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $taskEvidence 'smoke-result.json')
Write-Output 'Frozen GUI and read-only worker smoke passed; no games executed'
