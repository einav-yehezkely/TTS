@echo off
rem Double-click to start Kokoro PDF Reader. The first start installs everything into this folder.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    rem a library added in an update (for example PyAV for video export) also triggers setup, which installs it
    ".venv\Scripts\python.exe" -c "import av" >nul 2>&1 || goto setup
    goto run
)

:setup
title Kokoro PDF Reader - setup
echo.
echo  First-time setup. This needs the internet and takes a few minutes.
echo  Everything is installed inside this folder only.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\setup.ps1"
if errorlevel 1 (
    echo.
    echo  Setup did not finish. Read the message above, check the internet connection,
    echo  and double-click the launcher again to retry.
    echo.
    pause
    exit /b 1
)

:run
start "" ".venv\Scripts\pythonw.exe" "%~dp0launcher\start.pyw" %*
exit /b 0
