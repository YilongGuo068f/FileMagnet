@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title File Extract Workbench - keep this window open while using the tool

if not exist "engine\server.py" goto notextracted

set "PY="
rem 1) Bundled Python (official, no install needed)
if exist "runtime\windows-x64\python.exe" (
  "runtime\windows-x64\python.exe" -c "import sys" >nul 2>&1 && set "PY=runtime\windows-x64\python.exe"
)
rem 2) Python 3.8+ installed on this computer
if not defined PY (
  where py >nul 2>&1 && py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1 && python -c "import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto legacy

%PY% -X utf8 engine\server.py %*
if errorlevel 1 (
  echo.
  echo  The tool stopped with an error. See the messages above.
  pause
)
goto :eof

:legacy
echo.
echo  The bundled Python could not start and no Python 3.8+ is installed.
echo  Starting the simplified version that uses Windows PowerShell instead...
echo.
call "legacy\v1\start-windows-powershell.bat"
goto :eof

:notextracted
echo.
echo  [ERROR] Please extract the WHOLE zip file first,
echo          then double-click start-windows.bat inside the extracted folder.
echo.
pause
exit /b 1
