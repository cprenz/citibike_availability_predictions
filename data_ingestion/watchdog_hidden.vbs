' Launches watchdog.ps1 with no visible window.
' wscript.exe runs this with no console, and the "0" arg to Run hides PowerShell too.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\clark\Desktop\citibike\data_ingestion\watchdog.ps1""", 0, False
