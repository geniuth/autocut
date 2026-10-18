@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "C:\Users\geniu\AppData\Local\Programs\Python\Python311\pythonw.exe" "%~dp0autocut_gui.py"
