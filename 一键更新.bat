@echo off
setlocal
chcp 65001 >nul
title AkashaBot WeFlow Bridge Update
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Update-Installed.ps1" %*
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
  echo Update completed successfully.
) else (
  echo Update failed with exit code %CODE%.
)
pause
exit /b %CODE%
