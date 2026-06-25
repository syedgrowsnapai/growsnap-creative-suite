@echo off
echo ===================================================
echo   GrowSnap Creative Suite — Launcher
echo ===================================================
echo.

:: Check for python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to PATH!
    echo Please install Python (3.10 or newer) to continue.
    pause
    exit /b
)

if not exist .venv (
    echo [First Run] Initializing Python virtual environment...
    python -m venv .venv
    echo Installing required packages...
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install pyqt6 patchright requests
) else (
    call .venv\Scripts\activate.bat
)

echo Starting GrowSnap Creative Suite...
python grow_snap_dola/main.py %*
