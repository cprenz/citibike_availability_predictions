# Checks every run whether the citibike ingest is alive.
# - If a pythonw process running ingest.py exists, log a heartbeat and exit.
# - If not, start the scheduled task and log the restart.
# Appends every check to watchdog.log.

$ScriptDir   = "C:\Users\clark\Desktop\citibike\data_ingestion"
$ScriptFile  = "$ScriptDir\ingest.py"
$WatchdogLog = "$ScriptDir\watchdog.log"
$TaskName    = "CitibikeDataIngestion"

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Find pythonw processes running our ingest.py via the CommandLine property
$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*ingest.py*" }

if ($running) {
    $pids = ($running.ProcessId -join ",")
    Add-Content -Path $WatchdogLog -Value "$timestamp OK    ingest alive (pid=$pids)"
} else {
    Add-Content -Path $WatchdogLog -Value "$timestamp DOWN  ingest not running - restarting via scheduled task"
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Add-Content -Path $WatchdogLog -Value "$timestamp START Start-ScheduledTask issued for '$TaskName'"
    } catch {
        Add-Content -Path $WatchdogLog -Value "$timestamp ERROR Failed to start task: $($_.Exception.Message)"
    }
}
