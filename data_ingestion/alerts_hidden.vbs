' Launches send_alerts.py with no visible window.
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c """"C:\Users\clark\AppData\Local\Programs\Python\Python39\pythonw.exe"" ""C:\Users\clark\Desktop\citibike\data_ingestion\send_alerts.py"" >> ""C:\Users\clark\Desktop\citibike\data_ingestion\alerts.log"" 2>&1""", 0, False
