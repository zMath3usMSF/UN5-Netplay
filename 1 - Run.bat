@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title UN5 Netplay Launcher

echo ========================================
echo       UN5 Netplay Launcher
echo ========================================
echo.

:: ==================================================
:: AUTOMATIC GITHUB UPDATER
:: ==================================================

echo Checking for launcher updates...

set "REPO_API=https://api.github.com/repos/zMath3usMSF/UN5-Netplay/commits/main"
set "UPDATE_ZIP=%TEMP%\UN5NetplayUpdate.zip"
set "UPDATE_DIR=%TEMP%\UN5NetplayUpdate"
set "UPDATE_MARKER=%~dp0.update_commit"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "try {" ^
 "  $headers=@{'User-Agent'='UN5-Netplay-Updater'};" ^
 "  $commit=Invoke-RestMethod -Uri '%REPO_API%' -Headers $headers -TimeoutSec 10;" ^
 "  $sha=$commit.sha;" ^
 "  $old='';" ^
 "  if(Test-Path -LiteralPath '%UPDATE_MARKER%') {$old=(Get-Content -LiteralPath '%UPDATE_MARKER%' -Raw).Trim()};" ^
 "  if($old -eq $sha) {Write-Host 'Launcher is already up to date.'; exit 0};" ^
 "  Write-Host 'Update found. Downloading...';" ^
 "  Invoke-WebRequest -Uri 'https://github.com/zMath3usMSF/UN5-Netplay/archive/refs/heads/main.zip' -OutFile '%UPDATE_ZIP%' -TimeoutSec 60;" ^
 "  if(Test-Path '%UPDATE_DIR%') {Remove-Item '%UPDATE_DIR%' -Recurse -Force};" ^
 "  Expand-Archive -LiteralPath '%UPDATE_ZIP%' -DestinationPath '%UPDATE_DIR%' -Force;" ^
 "  $src=Get-ChildItem '%UPDATE_DIR%' -Directory | Select-Object -First 1;" ^
 "  if(!$src -or !(Test-Path (Join-Path $src.FullName 'launcher.py'))) {throw 'Invalid update archive: launcher.py not found'};" ^
 "  $root=(Get-Location).Path;" ^
 "  $exclude=@('.git','.venv','venv','__pycache__','.vscode','.idea');" ^
 "  $files=Get-ChildItem -LiteralPath $src.FullName -File -Recurse;" ^
 "  foreach($f in $files) {" ^
 "    $rel=$f.FullName.Substring($src.FullName.Length+1);" ^
 "    $parts=$rel -split '[\\/]';" ^
 "    $skip=$false;" ^
 "    foreach($p in $parts) {" ^
 "      if($exclude -contains $p -or $p -match '\.bak($|[0-9])' -or $p -match '\.bak_seguranca$' -or $p -match '\.p2s$' -or $p -in @('config.json','session.json','symbols.json') -or $p -match '\.(key|pem)$' -or $p -eq '.env' -or $p -eq '.update_commit') {$skip=$true}" ^
 "    };" ^
 "    if($skip) {continue};" ^
 "    $target=Join-Path $root $rel;" ^
 "    $parent=Split-Path $target -Parent;" ^
 "    if(!(Test-Path -LiteralPath $parent)) {New-Item -ItemType Directory -Path $parent -Force | Out-Null};" ^
 "    Copy-Item -LiteralPath $f.FullName -Destination $target -Force;" ^
 "  };" ^
 "  Set-Content -LiteralPath '%UPDATE_MARKER%' -Value $sha -NoNewline;" ^
 "  Write-Host 'Update completed successfully.';" ^
 "} catch {" ^
 "  Write-Host ('WARNING: Update failed: ' + $_.Exception.Message);" ^
 "  Write-Host 'Continuing with the installed version...';" ^
 "} finally {" ^
 "  Remove-Item -LiteralPath '%UPDATE_ZIP%' -Force -ErrorAction SilentlyContinue;" ^
 "  Remove-Item -LiteralPath '%UPDATE_DIR%' -Recurse -Force -ErrorAction SilentlyContinue;" ^
 "}"

echo.

:: ==================================================
:: CHECK FOR PYTHON
:: ==================================================

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
if not errorlevel 1 goto CHECK_CERTIFI

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

:: ==================================================
:: CERTIFICATES (certifi): fixes "CERTIFICATE_VERIFY_FAILED"
:: Installed/updated on every start. Not fatal if it fails (no internet, etc.).
:: ==================================================
:CHECK_CERTIFI
echo.
echo Updating TLS certificates (certifi)...
%PYTHON_CMD% -m pip install --upgrade --quiet certifi
if errorlevel 1 (
    echo WARNING: Could not update certifi. Continuing anyway...
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
endlocal