# Registers a daily Task Scheduler job that aggregates station_status_hourly_clean
# into station_daily_status. Runs at 9:30 PM — 30 minutes before CitibikeSnowflakeSyncDaily
# (10:00 PM) so the table is fresh before Snowflake picks it up.
# Run once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_daily_status_build.ps1

$TaskName   = "CitibikeDailyStatusBuild"
$ProjectDir = "C:\Users\clark\Desktop\citibike"
$PythonExe  = "C:\Users\clark\AppData\Local\Programs\Python\Python39\pythonw.exe"
$ScriptFile = "$ProjectDir\data_ingestion\build_station_daily_status.py"

schtasks /Delete /TN $TaskName /F 2>$null
Write-Host "Removed existing task (if any)."

schtasks /Create `
    /TN  $TaskName `
    /TR  "`"$PythonExe`" `"$ScriptFile`"" `
    /SC  DAILY `
    /ST  21:30 `
    /RL  LIMITED `
    /F `
    /IT

$task = Get-ScheduledTask -TaskName $TaskName
$task.Settings.RestartCount       = 3
$task.Settings.RestartInterval    = "PT10M"
$task.Settings.ExecutionTimeLimit = "PT30M"
Set-ScheduledTask -InputObject $task | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered. Runs daily at 9:30 PM."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Status:   Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Remove:   schtasks /Delete /TN '$TaskName' /F"
