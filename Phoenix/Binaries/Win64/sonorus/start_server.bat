@echo off
:: ============================================
:: Sonorus Mod - Server Launcher
:: ============================================
:: This is part of the Sonorus mod installation for Hogwarts Legacy.
:: It starts the Python server that powers AI conversations with NPCs.
::
:: When the server is ready, your web browser will automatically open
:: to the configuration interface where you can set up your API keys
:: and customize the mod settings.
:: ============================================

cd /d "%~dp0"

:: Write initial lock and clear stop signal
echo %time% > server.lock
del server.lock.stop 2>nul

:: Start background heartbeat - writes current time every 5s, checks for stop signal frequently
start /b cmd /v:on /c "for /l %%x in () do (if exist server.lock.stop (del server.lock.stop 2>nul & exit /b) else (echo !time! > server.lock & ping -n 6 127.0.0.1 >nul))"

echo ============================================
echo   Sonorus Mod - Starting Server
echo ============================================
echo.

:: Check if python folder exists, if not extract from python.zip
if not exist "python\" (
    if exist "python.zip" (
        echo First-time setup: Extracting Python environment...
        echo This may take a moment...
        echo.
        powershell -Command "Expand-Archive -Path 'python.zip' -DestinationPath '.' -Force"
        if exist "python\" (
            echo Extraction complete. Cleaning up...
            del "python.zip"
            echo.
        ) else (
            echo ERROR: Extraction failed.
            echo. > server.lock.stop
            del server.lock 2>nul
            pause
            exit /b 1
        )
    ) else (
        echo ERROR: Python folder not found and python.zip is missing.
        echo. > server.lock.stop
        del server.lock 2>nul
        pause
        exit /b 1
    )
)

set PYTHON=python\python.exe

:: Create bin directory if it doesn't exist
if not exist "bin" mkdir bin

:: Download dependencies if missing
if not exist "bin\parseltongue.exe" (
    echo Downloading parseltongue...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/insomnious/parseltongue/releases/download/v0.2.3/parseltongue-0.2.3.zip' -OutFile 'bin\parseltongue.zip'"
    powershell -Command "Expand-Archive -Path 'bin\parseltongue.zip' -DestinationPath 'bin\parseltongue_temp' -Force"
    move /Y "bin\parseltongue_temp\parseltongue.exe" "bin\parseltongue.exe" >nul
    rmdir /S /Q "bin\parseltongue_temp" 2>nul
    del "bin\parseltongue.zip" 2>nul
)

if not exist "bin\wwiser.pyz" (
    echo Downloading wwiser...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/bnnm/wwiser/releases/download/v20250928/wwiser.pyz' -OutFile 'bin\wwiser.pyz'"
)

if not exist "bin\wwnames.db3" (
    echo Downloading wwnames.db3...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/bnnm/wwiser/releases/download/v20250928/wwnames.db3' -OutFile 'bin\wwnames.db3'"
)

if not exist "bin\repak.exe" (
    echo Downloading repak...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/trumank/repak/releases/download/v0.2.3/repak_cli-x86_64-pc-windows-msvc.zip' -OutFile 'bin\repak.zip'"
    powershell -Command "Expand-Archive -Path 'bin\repak.zip' -DestinationPath 'bin\repak_temp' -Force"
    move /Y "bin\repak_temp\repak.exe" "bin\repak.exe" >nul
    rmdir /S /Q "bin\repak_temp" 2>nul
    del "bin\repak.zip" 2>nul
)

if not exist "bin\oo2core_9_win64.dll" (
    echo Downloading oo2core...
    powershell -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/WorkingRobot/OodleUE/refs/heads/main/Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/win/redist/oo2core_9_win64.dll' -OutFile 'bin\oo2core_9_win64.dll'"
)

if not exist "bin\vgmstream\vgmstream-cli.exe" (
    echo Downloading vgmstream...
    if not exist "bin\vgmstream" mkdir "bin\vgmstream"
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/vgmstream/vgmstream/releases/download/r2055/vgmstream-win64.zip' -OutFile 'bin\vgmstream.zip'"
    powershell -Command "Expand-Archive -Path 'bin\vgmstream.zip' -DestinationPath 'bin\vgmstream' -Force"
    del "bin\vgmstream.zip" 2>nul
)

:: Bootstrap pip if not working
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo Bootstrapping pip...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    "%PYTHON%" get-pip.py --no-warn-script-location
    if errorlevel 1 (
        echo ERROR: Failed to install pip
        echo. > server.lock.stop
        del server.lock 2>nul
        pause
        exit /b 1
    )
    del get-pip.py 2>nul
)

:: Check if dependencies are installed
"%PYTHON%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Installing Python dependencies...
    "%PYTHON%" -m pip install setuptools wheel --no-warn-script-location -q
    "%PYTHON%" -m pip install -r requirements.txt --no-warn-script-location
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        echo. > server.lock.stop
        del server.lock 2>nul
        pause
        exit /b 1
    )
)

:: Let heartbeat keep running - Python's server.heartbeat will take over
:: The background heartbeat will exit when the batch file exits

echo Starting Sonorus server...
echo The web interface will open in your browser shortly.
echo.
"%PYTHON%" server.py

:: Signal heartbeat to stop, clean up lock
echo. > server.lock.stop
del server.lock 2>nul
exit
