Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "d:\workspace\autocut"
WshShell.Run """C:\Users\geniu\AppData\Local\Programs\Python\Python311\pythonw.exe"" ""d:\workspace\autocut\autocut_gui.py""", 1, False
