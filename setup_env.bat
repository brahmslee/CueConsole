@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  mtc-obs-bridge environment setup (delivery-ready)
rem  - uses system Python 3.12 (py launcher or PATH)
rem  - creates venv INSIDE project folder: .\venv
rem  - installs deps from requirements.txt
rem  Target machine only needs: Python 3.12 installed once.
rem ============================================================

echo ============================================
echo  mtc-obs-bridge environment setup
echo ============================================

rem ---- [0/4] locate Python 3.12 ----
set "PYCMD=py -3.12"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
    python --version 2>&1 | findstr /C:"3.12" >nul
    if errorlevel 1 (
        echo [ERROR] Python 3.12 not found.
        echo Install it from: https://www.python.org/downloads/
        echo IMPORTANT: check "Add python.exe to PATH" during install.
        pause
        exit /b 1
    )
    set "PYCMD=python"
)
echo Using Python: %PYCMD%
%PYCMD% --version

rem ---- [1/4] create venv in project folder ----
if exist "venv\Scripts\python.exe" (
    echo [1/4] venv already exists, skip creation.
) else (
    echo [1/4] Creating venv ...
    %PYCMD% -m venv venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
)

set "VPY=%~dp0venv\Scripts\python.exe"

rem ---- [2/4] pip upgrade ----
echo [2/4] Upgrading pip (Tsinghua mirror) ...
"%VPY%" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

rem ---- [3/4] install deps ----
echo [3/4] Installing deps from requirements.txt ...
"%VPY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [ERROR] pip install failed. Check network / mirror.
    pause
    exit /b 1
)

rem ---- [4/4] verify ----
echo [4/4] Verifying imports ...
"%VPY%" -c "import mido, rtmidi, obsws_python; from importlib.metadata import version; print('OK: mido', version('mido'))"
if errorlevel 1 (
    echo [ERROR] verification failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup done.
echo  Start bridge:  start_bridge.bat
echo  Self test:     start_bridge.bat --selftest
echo ============================================
pause
