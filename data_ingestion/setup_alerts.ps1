# Register the CitibikeAlerts Task Scheduler job.
# Runs send_alerts.py every 30 minutes at :05 and :35.
# Run once as Administrator.

$ProjectRoot = "C:\Users\clark\Desktop\citibike"
$Vbs         = "$ProjectRoot\data_ingestion\alerts_hidden.vbs"

$Action  = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$Vbs`"" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -Once -At "00:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName   "CitibikeAlerts" `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -RunLevel   Highest `
    -Force

Write-Host "CitibikeAlerts task registered. Runs every 30 minutes at :00 and :30."
Write-Host "Log: $LogFile"
