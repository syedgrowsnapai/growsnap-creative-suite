@echo off
echo ===================================================
echo   GrowSnap Creative Suite — GitHub Installer
echo ===================================================
echo.

:: ────────── CONFIGURATION ──────────
:: Set your GitHub repository details below:
set GITHUB_USER=syedgrowsnapai
set GITHUB_REPO=growsnap-creative-suite
set BRANCH=main
:: ───────────────────────────────────

:: 1. Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [Installer] Python is not installed.
    echo Downloading Python 3.11.9 installer for Windows...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile 'python_setup.exe'"
    echo Launching Python installer.
    echo.
    echo IMPORTANT: You MUST check "Add python.exe to PATH" at the bottom of the installer window!
    echo.
    start /wait python_setup.exe
    del python_setup.exe
    
    :: Check python again to verify
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] Python installation not completed or not added to PATH.
        echo Please install Python manually and check the PATH box, then run this installer again.
        pause
        exit /b
    )
)

:: 2. Download code from GitHub
echo [Installer] Downloading code package from GitHub (%GITHUB_USER%/%GITHUB_REPO%)...
set REPO_ZIP_URL=https://github.com/%GITHUB_USER%/%GITHUB_REPO%/archive/refs/heads/%BRANCH%.zip
powershell -Command "Invoke-WebRequest -Uri '%REPO_ZIP_URL%' -OutFile 'growsnap.zip'"

if not exist growsnap.zip (
    echo [ERROR] Failed to download from GitHub. Please check your repository URL settings.
    pause
    exit /b
)

echo [Installer] Extracting application files...
powershell -Command "Expand-Archive -Path 'growsnap.zip' -DestinationPath '.'"
del growsnap.zip

:: The folder extracted will be named REPO-BRANCH
set EXTRACTED_FOLDER=%GITHUB_REPO%-%BRANCH%
if not exist %EXTRACTED_FOLDER% (
    echo [ERROR] Extraction folder %EXTRACTED_FOLDER% not found.
    pause
    exit /b
)

:: Rename or copy contents
if exist "GrowSnap Creative Suite" (
    echo [Installer] Updating existing GrowSnap Creative Suite folder...
    xcopy /E /I /Y %EXTRACTED_FOLDER% "GrowSnap Creative Suite"
    rd /S /Q %EXTRACTED_FOLDER%
) else (
    move %EXTRACTED_FOLDER% "GrowSnap Creative Suite"
)

cd "GrowSnap Creative Suite"

:: 3. Setup Virtual Environment
echo [Installer] Creating virtual environment...
python -m venv .venv

echo [Installer] Installing package dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install pyqt6 patchright requests yt-dlp

echo.
echo ===================================================
echo   Installation Completed Successfully!
echo   Double-click 'run_grow_snap.bat' inside the
echo   "GrowSnap Creative Suite" folder to launch!
echo ===================================================
pause
