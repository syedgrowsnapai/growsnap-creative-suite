@echo off
cd /d "%~dp0"
echo ===================================================
echo   GrowSnap One — Launcher
echo ===================================================
echo.

:: 1. If virtual env already exists, run the application directly
if exist .venv\Scripts\python.exe (
    if not exist "%userprofile%\Desktop\GrowSnap One.lnk" call :create_shortcut
    echo Starting GrowSnap One...
    ".venv\Scripts\python.exe" "grow_snap_dola\main.py" %*
    if %errorlevel% neq 0 (
        echo.
        echo [ERROR] GrowSnap One exited with error code %errorlevel%
        echo Please take a screenshot of this error and report it.
        pause
    )
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

:: Create Desktop Shortcut on first run
if not exist "%userprofile%\Desktop\GrowSnap One.lnk" call :create_shortcut

echo Starting GrowSnap One...
python grow_snap_dola/main.py %*
exit /b

:create_shortcut
echo Creating Desktop Shortcut...
echo Set oWS = CreateObject("WScript.Shell") > "%temp%\CreateShortcut.vbs"
echo sLinkFile = "%userprofile%\Desktop\GrowSnap One.lnk" >> "%temp%\CreateShortcut.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%temp%\CreateShortcut.vbs"
echo oLink.TargetPath = "%~dp0run_grow_snap.bat" >> "%temp%\CreateShortcut.vbs"
echo oLink.WorkingDirectory = "%~dp0" >> "%temp%\CreateShortcut.vbs"
echo oLink.Description = "Launch GrowSnap One" >> "%temp%\CreateShortcut.vbs"
echo oLink.IconLocation = "%~dp0grow_snap_dola\dola_automation\resources\icon.ico" >> "%temp%\CreateShortcut.vbs"
echo oLink.Save >> "%temp%\CreateShortcut.vbs"
cscript /nologo "%temp%\CreateShortcut.vbs" >nul 2>&1
del "%temp%\CreateShortcut.vbs" >nul 2>&1
exit /b
