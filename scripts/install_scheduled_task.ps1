[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$musicRoot = Split-Path -Parent $PSScriptRoot
$musicRoot = (Resolve-Path -LiteralPath $musicRoot).Path
$musicPython = Join-Path $musicRoot '.venv\Scripts\pythonw.exe'
$musicRunner = Join-Path $musicRoot 'scripts\run_scheduled.py'
$musicTaskName = 'CollectorMusic-AutomaticArchive'
$musicDescription = 'collector-music: platform collection, publication, backup and audit. NO USB/device access.'
$musicOldTask = Get-ScheduledTask -TaskName $musicTaskName -ErrorAction SilentlyContinue
if ($musicOldTask) {
    if ($musicOldTask.Description -ne $musicDescription -or
        $musicOldTask.Actions.Count -ne 1 -or
        $musicOldTask.Actions[0].Execute -ne $musicPython -or
        $musicOldTask.Actions[0].Arguments -ne ('"' + $musicRunner + '"')) {
        throw 'Existing task does not match this project. Refusing to replace/remove it.'
    }
}
if ($Remove) {
    if ($musicOldTask) {
        Disable-ScheduledTask -TaskName $musicTaskName | Out-Null
        Unregister-ScheduledTask -TaskName $musicTaskName -Confirm:$false
    }
    Write-Output 'Task removed. Existing data and configuration are preserved.'
    return
}
if (-not (Test-Path -LiteralPath $musicPython) -or -not (Test-Path -LiteralPath $musicRunner)) {
    throw 'Project Python or runner is missing.'
}
$musicConfig = Join-Path $musicRoot '.local\automation.json'
if (-not (Test-Path -LiteralPath $musicConfig)) {
    $musicOptions = Get-Content -LiteralPath (Join-Path $musicRoot 'automation.example.json') -Raw | ConvertFrom-Json
    $musicNode = (Get-Command node.exe -ErrorAction Stop).Source
    $musicOptions | Add-Member -NotePropertyName node_executable -NotePropertyValue $musicNode
    $musicOptions | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $musicConfig -Encoding UTF8
}
$musicAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$musicAction = New-ScheduledTaskAction -Execute $musicPython -Argument ('"' + $musicRunner + '"') -WorkingDirectory $musicRoot
$musicLogon = New-ScheduledTaskTrigger -AtLogOn -User $musicAccount
# No repetition duration means an indefinite trigger; the runner checks per-job deadlines.
$musicPulse = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$musicSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$musicPrincipal = New-ScheduledTaskPrincipal -UserId $musicAccount -LogonType Interactive -RunLevel Limited
$musicTask = New-ScheduledTask -Action $musicAction -Trigger @($musicLogon, $musicPulse) `
    -Settings $musicSettings -Principal $musicPrincipal -Description $musicDescription
Register-ScheduledTask -TaskName $musicTaskName -InputObject $musicTask -Force | Select-Object TaskName,State
