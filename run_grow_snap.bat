@echo off
cd /d "%~dp0"
echo ===================================================
echo   GrowSnap Creative Suite — Launcher
echo ===================================================
echo.

:: 1. If virtual env already exists, run the application directly
if exist .venv\Scripts\python.exe (
    echo Starting GrowSnap Creative Suite...
    start "" ".venv\Scripts\python.exe" "grow_snap_dola/main.py" %*
    exit /b
)

:: 2. If virtual env doesn't exist, search for python
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
    goto python_found
)

py --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py
    goto python_found
)

for %%V in (312 311 310 313) do (
    if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" (
        set PYTHON_CMD="%LocalAppData%\Programs\Python\Python%%V\python.exe"
        goto python_found
    )
)

for %%V in (312 311 310 313) do (
    if exist "C:\Program Files\Python%%V\python.exe" (
        set PYTHON_CMD="C:\Program Files\Python%%V\python.exe"
        goto python_found
    )
)

echo [ERROR] Python is not installed or not added to PATH!
echo Please install Python (3.10 or newer) and check the "Add to PATH" box.
pause
exit /b

:python_found
echo [First Run] Initializing Python virtual environment...
%PYTHON_CMD% -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment!
    pause
    exit /b
)

echo Installing required packages...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install pyqt6 patchright requests yt-dlp gTTS

echo Starting GrowSnap Creative Suite...
python grow_snap_dola/main.py %*
