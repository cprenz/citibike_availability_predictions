# Registers a daily Task Scheduler job that syncs station_information and
# station_daily_ridership to Snowflake. Runs at 10:00 PM each day — after
# the trip ingestion job (ingest_trip_monthly.py) and build script have run.
# Run once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_snowflake_sync_daily.ps1

$TaskName   = "CitibikeSnowflakeSyncDaily"
$ProjectDir = "C:\Users\clark\Desktop\citibike"
$PythonExe  = "C:\Users\clark\AppData\Local\Programs\Python\Python39\pythonw.exe"
$ScriptFile = "$ProjectDir\data_ingestion\sync_to_snowflake_daily.py"

schtasks /Delete /TN $TaskName /F 2>$null
Write-Host "Removed existing task (if any)."

schtasks /Create `
    /TN  $TaskName `
    /TR  "`"$PythonExe`" `"$ScriptFile`"" `
    /SC  DAILY `
    /ST  22:00 `
    /RL  LIMITED `
    /F `
    /IT

$task = Get-ScheduledTask -TaskName $TaskName
$task.Settings.RestartCount       = 3
$task.Settings.RestartInterval    = "PT10M"
$task.Settings.ExecutionTimeLimit = "PT30M"
Set-ScheduledTask -InputObject $task | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered. Runs daily at 10:00 PM."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Status:   Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Remove:   schtasks /Delete /TN '$TaskName' /F"
