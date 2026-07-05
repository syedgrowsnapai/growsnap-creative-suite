@echo off
echo ===================================================
echo   GrowSnap One — GitHub Installer
echo ===================================================
echo.

:: ────────── CONFIGURATION ──────────
:: Set your GitHub repository details below:
set GITHUB_USER=syedgrowsnapai
set GITHUB_REPO=growsnap-creative-suite
set BRANCH=main
:: ───────────────────────────────────
cd /d "%~dp0"

:: 1. Check for Python
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

echo [Installer] Python is not installed or not on PATH.
echo Downloading Python 3.11.9 installer for Windows...
powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile 'python_setup.exe'"
echo Launching Python installer.
echo.
echo IMPORTANT: You MUST check "Add python.exe to PATH" at the bottom of the installer window!
echo.
start /wait python_setup.exe
del python_setup.exe

:: Check python again after install
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

echo [ERROR] Python installation not completed or not added to PATH.
echo Please install Python manually and check the PATH box, then run this installer again.
pause
exit /b

:python_found

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
if exist "GrowSnap One" (
    echo [Installer] Updating existing GrowSnap One folder...
    xcopy /E /I /Y %EXTRACTED_FOLDER% "GrowSnap One"
    rd /S /Q %EXTRACTED_FOLDER%
) else (
    move %EXTRACTED_FOLDER% "GrowSnap One"
)

cd "GrowSnap One"

:: 3. Setup Virtual Environment
echo [Installer] Creating virtual environment...
%PYTHON_CMD% -m venv .venv

echo [Installer] Installing package dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install pyqt6 patchright requests yt-dlp gTTS

set "DESKTOP_DIR=%userprofile%\Desktop"
if exist "%userprofile%\OneDrive\Desktop" set "DESKTOP_DIR=%userprofile%\OneDrive\Desktop"
echo Set oWS = CreateObject("WScript.Shell") > "%temp%\CreateShortcut.vbs"
echo sLinkFile = "%DESKTOP_DIR%\GrowSnap One.lnk" >> "%temp%\CreateShortcut.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%temp%\CreateShortcut.vbs"
echo oLink.TargetPath = "%~dp0GrowSnap One\run_grow_snap.bat" >> "%temp%\CreateShortcut.vbs"
echo oLink.WorkingDirectory = "%~dp0GrowSnap One" >> "%temp%\CreateShortcut.vbs"
echo oLink.Description = "Launch GrowSnap One" >> "%temp%\CreateShortcut.vbs"
echo oLink.IconLocation = "%~dp0GrowSnap One\grow_snap_dola\dola_automation\resources\icon.ico" >> "%temp%\CreateShortcut.vbs"
echo oLink.Save >> "%temp%\CreateShortcut.vbs"
cscript /nologo "%temp%\CreateShortcut.vbs" >nul 2>&1
del "%temp%\CreateShortcut.vbs" >nul 2>&1

echo.
echo ===================================================
echo   Installation Completed Successfully!
echo   We have created a "GrowSnap One" shortcut on your Desktop.
echo   You can now launch the app directly from your Desktop!
echo ===================================================
pause
