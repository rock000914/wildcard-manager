@echo off
cd /d "%~dp0"
start "" /b wscript.exe //nologo "%~dp0run_wildcard_manager.vbs"
exit /b
