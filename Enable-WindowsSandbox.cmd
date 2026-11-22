@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\enable_sandbox_launcher.ps1"
echo.
echo Result: Log\sandbox\enable-windows-sandbox-result.json
pause
