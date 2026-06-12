# Registers a Windows Task Scheduler job that runs watchdog.ps1 every 5 minutes.
# The watchdog checks whether the ingest process is alive and restarts it if dead.
# Run this script once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_watchdog.ps1

$TaskName     = "CitibikeIngestWatchdog"
$ScriptDir    = "C:\Users\clark\Desktop\citibike\data_ingestion"
$WatchdogFile = "$ScriptDir\watchdog.ps1"

# Remove existing task if present
schtasks /Delete /TN $TaskName /F 2>$null
Write-Host "Removed existing watchdog task (if any)."

# Run every 5 minutes indefinitely. Launch via wscript + a VBS wrapper so that
# PowerShell runs truly invisibly (schtasks + -WindowStyle Hidden still flashes a window).
$VbsFile = "$ScriptDir\watchdog_hidden.vbs"
$WsExe   = "wscript.exe"

schtasks /Create `
    /TN  $TaskName `
    /TR  "$WsExe `"$VbsFile`"" `
    /SC  MINUTE `
    /MO  5 `
    /RL  LIMITED `
    /F `
    /IT

Write-Host ""
Write-Host "Task '$TaskName' registered successfully."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:    Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  View log:   Get-Content '$ScriptDir\watchdog.log' -Tail 10"
Write-Host "  Disable:    Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Remove:     schtasks /Delete /TN '$TaskName' /F"
