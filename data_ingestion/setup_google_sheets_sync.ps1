$taskName  = "CitibikeGoogleSheetsSync"
$scriptPath = "C:\Users\clark\Desktop\citibike\data_ingestion\sync_to_google_sheets.py"
$workDir    = "C:\Users\clark\Desktop\citibike"
$pythonw    = (Get-Command pythonw.exe).Source

$action  = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument $scriptPath `
    -WorkingDirectory $workDir

# 10:30pm daily — 30 min after the Snowflake daily sync finishes
$trigger = New-ScheduledTaskTrigger -Daily -At "22:30"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force

Write-Host "Registered '$taskName' - runs daily at 10:30 PM."
