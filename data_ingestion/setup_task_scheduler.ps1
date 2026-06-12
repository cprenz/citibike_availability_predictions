# Registers a Windows Task Scheduler job that launches ingest.py once at logon.
# ingest.py runs as a long-lived loop and polls every 2.5 minutes internally.
# Run this script once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_task_scheduler.ps1

$TaskName  = "CitibikeDataIngestion"
$ScriptDir = "C:\Users\clark\Desktop\citibike\data_ingestion"
$PythonExe = "C:\Users\clark\AppData\Local\Programs\Python\Python39\pythonw.exe"
$ScriptFile = "$ScriptDir\ingest.py"

# Remove existing task if present
schtasks /Delete /TN $TaskName /F 2>$null
Write-Host "Removed existing task (if any)."

# Register via schtasks.exe (works reliably on PowerShell 5.1)
# ONLOGON: launches once when you log in; ingest.py loops internally.
schtasks /Create `
    /TN  $TaskName `
    /TR  "`"$PythonExe`" `"$ScriptFile`"" `
    /SC  ONLOGON `
    /RL  LIMITED `
    /F `
    /IT

# Enable "restart on failure": retry up to 999 times, 1 minute apart.
# schtasks.exe can't set this directly, so use the PowerShell cmdlets.
$task = Get-ScheduledTask -TaskName $TaskName
$task.Settings.RestartCount    = 999
$task.Settings.RestartInterval = "PT1M"
$task.Settings.ExecutionTimeLimit = "PT0S"   # no time limit
Set-ScheduledTask -InputObject $task | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered successfully."
Write-Host "Restart-on-failure enabled: 999 retries, 1 minute apart."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  View log:  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Stop:      Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Disable:   Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Remove:    schtasks /Delete /TN '$TaskName' /F"
