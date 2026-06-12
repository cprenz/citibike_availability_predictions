# Registers a Windows Task Scheduler job that runs ingest_trip_monthly.py
# on the 6th, 7th, and 8th of every month at 10:00 AM.
# The script checks if the month is already loaded and exits immediately if so —
# only the first successful run does any work.
# Run this script once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_trip_monthly_task.ps1

$TaskName   = "CitibikeTripMonthly"
$ScriptDir  = "C:\Users\clark\Desktop\citibike\data_ingestion"
$PythonExe  = "C:\Users\clark\AppData\Local\Programs\Python\Python39\python.exe"
$ScriptFile = "$ScriptDir\ingest_trip_monthly.py"
$LogFile    = "$ScriptDir\trip_monthly.log"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed existing task (if any)."

# Three triggers: 6th, 7th, 8th of every month at 10:00 AM
$triggers = @(
    New-ScheduledTaskTrigger -Monthly -DaysOfMonth 6 -At "10:00AM"
    New-ScheduledTaskTrigger -Monthly -DaysOfMonth 7 -At "10:00AM"
    New-ScheduledTaskTrigger -Monthly -DaysOfMonth 8 -At "10:00AM"
)

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-u `"$ScriptFile`"" `
    -WorkingDirectory "C:\Users\clark\Desktop\citibike"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $triggers `
    -Action $action `
    -Settings $settings `
    -RunLevel Limited `
    -Force | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered successfully."
Write-Host "Schedule: monthly on 6th, 7th, and 8th at 10:00 AM."
Write-Host "Script exits immediately if month already loaded."
Write-Host "Log file: $LogFile"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  View log:  Get-Content '$LogFile' -Tail 50"
Write-Host "  Disable:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Remove:    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
