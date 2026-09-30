@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  mtc-obs-bridge launcher
rem  usage:
rem    start_bridge.bat              -> run bridge server
rem    start_bridge.bat --selftest   -> self test (OBS resources)
rem ============================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found.
    echo Run setup_env.bat first.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" bridge_server.py %*
pause
