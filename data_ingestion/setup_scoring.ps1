# Registers an hourly Task Scheduler job that scores all active stations and
# writes predictions to model_predictions. Runs at :05 each hour, 5 minutes
# after CitibikeWeatherRealtime so fresh weather is always available first.
# Run once from an elevated (Administrator) PowerShell prompt.
#
# Usage:
#   Right-click PowerShell -> "Run as Administrator"
#   .\setup_scoring.ps1

$TaskName   = "CitibikeScoring"
$ProjectDir = "C:\Users\clark\Desktop\citibike"
$PythonExe  = "C:\Users\clark\AppData\Local\Programs\Python\Python39\pythonw.exe"
$ScriptFile = "$ProjectDir\model_training\score_stations.py"

schtasks /Delete /TN $TaskName /F 2>$null
Write-Host "Removed existing task (if any)."

schtasks /Create `
    /TN  $TaskName `
    /TR  "`"$PythonExe`" `"$ScriptFile`"" `
    /SC  HOURLY `
    /MO  1 `
    /ST  00:05 `
    /RL  LIMITED `
    /F `
    /IT

$task = Get-ScheduledTask -TaskName $TaskName
$task.Settings.RestartCount       = 3
$task.Settings.RestartInterval    = "PT5M"
$task.Settings.ExecutionTimeLimit = "PT10M"
Set-ScheduledTask -InputObject $task | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered. Runs hourly at :05."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Status:   Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Remove:   schtasks /Delete /TN '$TaskName' /F"
