```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
title UN5 Netplay Launcher

echo ========================================
echo       UN5 Netplay Launcher
echo ========================================
echo.

:: Check for Python
where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto CHECK_PYTHON
)

where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    goto CHECK_PYTHON
)

echo Python is not installed.
echo Downloading the official Python installer...
echo.

set "INSTALLER=%TEMP%\python-installer.exe"

curl -fL "https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe" -o "%INSTALLER%"
if errorlevel 1 (
    echo ERROR: Failed to download Python.
    echo Install it manually from:
    echo https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

echo.
echo Installing Python...
start /wait "" "%INSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1

set "PATH=%LocalAppData%\Programs\Python\Python313;%LocalAppData%\Programs\Python\Python313\Scripts;%PATH%"

where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto CHECK_PYTHON
)

where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    goto CHECK_PYTHON
)

echo ERROR: Python could not be detected after installation.
pause
exit /b 1

:CHECK_PYTHON
echo Checking Python installation...
%PYTHON_CMD% --version
if errorlevel 1 (
    echo ERROR: Python is not working correctly.
    pause
    exit /b 1
)

echo.
echo Checking Pillow...

%PYTHON_CMD% -c "from PIL import Image, ImageDraw, ImageTk" >nul 2>&1
if not errorlevel 1 goto RUN_LAUNCHER

echo Pillow is missing. Installing...
%PYTHON_CMD% -m pip install --upgrade Pillow

if errorlevel 1 (
    echo ERROR: Failed to install Pillow.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "from PIL import Image, ImageDraw, ImageTk" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Pillow installation could not be verified.
    pause
    exit /b 1
)

:RUN_LAUNCHER
echo.
echo All dependencies are ready.
echo Starting launcher...
echo.

%PYTHON_CMD% "%~dp0launcher.py"

if errorlevel 1 (
    echo.
    echo The program exited with an error.
    echo Copy the error message above for support.
)

echo.
pause
```