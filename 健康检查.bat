@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Test-Health.ps1" -InstallRoot "%~dp0."
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%
