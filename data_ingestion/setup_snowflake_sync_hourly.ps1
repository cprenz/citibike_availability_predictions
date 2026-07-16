# Registers an hourly Task Scheduler job that syncs model_predictions to Snowflake.
# Runs at :10 each hour — after CitibikeWeatherRealtime (:00) and
# CitibikeScoring (:05) so fresh predictions are always written first.
# Run once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_snowflake_sync_hourly.ps1

$TaskName   = "CitibikeSnowflakeSyncHourly"
$ProjectDir = "C:\Users\clark\Desktop\citibike"
$PythonExe  = "C:\Users\clark\AppData\Local\Programs\Python\Python39\pythonw.exe"
$ScriptFile = "$ProjectDir\data_ingestion\sync_to_snowflake_hourly.py"

schtasks /Delete /TN $TaskName /F 2>$null
Write-Host "Removed existing task (if any)."

schtasks /Create `
    /TN  $TaskName `
    /TR  "`"$PythonExe`" `"$ScriptFile`"" `
    /SC  HOURLY `
    /MO  1 `
    /ST  00:10 `
    /RL  LIMITED `
    /F `
    /IT

$task = Get-ScheduledTask -TaskName $TaskName
$task.Settings.RestartCount       = 3
$task.Settings.RestartInterval    = "PT5M"
$task.Settings.ExecutionTimeLimit = "PT10M"
Set-ScheduledTask -InputObject $task | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered. Runs hourly at :10."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Status:   Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Remove:   schtasks /Delete /TN '$TaskName' /F"
