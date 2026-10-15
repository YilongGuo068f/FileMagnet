@echo off
setlocal
cd /d "%~dp0"
title Multi-camera copy tool (PowerShell engine)

if not exist "engine\server.ps1" (
  echo.
  echo  [ERROR] Please extract the WHOLE zip file first.
  echo.
  pause
  exit /b 1
)

rem Always use the built-in Windows PowerShell engine (no Python needed).
powershell -NoProfile -STA -ExecutionPolicy Bypass -File "engine\server.ps1"

if errorlevel 1 (
  echo.
  echo  The tool stopped with an error. See the messages above.
  pause
)
endlocal
