@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Calibrate-Uia.ps1" -InstallRoot "%~dp0"
set "code=%ERRORLEVEL%"
echo.
pause
exit /b %code%
