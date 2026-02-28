@echo off
:: ============================================
:: Sonorus Mod - Server Launcher
:: ============================================
:: This script is launched automatically by the game.
:: Do not run it manually.
:: ============================================

:: Debug mode - skip game check and heartbeat for standalone testing
set DEBUG_MODE=0
if "%~1"=="--debug" set DEBUG_MODE=1

:: Verify launched by the game (passes --from-game flag)
if not "%~1"=="--from-game" if "%DEBUG_MODE%"=="0" (
    echo.
    echo  ===================================================
    echo   Do not run this file directly.
    echo   The game will start the server automatically
    echo   on launch if Sonorus is installed correctly.
    echo.
    echo   Use --debug flag to run standalone for testing.
    echo   If you need help, join our Discord:
    echo   https://discord.gg/YXhJy3pA7b
    echo  ===================================================
    echo.
    pause
    exit /b 0
)

cd /d "%~dp0"

set PYTHON=python\python.exe

:: Detect Wine/Proton - native C extensions (numpy etc.) crash under Wine,
:: so we must run the server with native Linux Python instead.
reg query "HKLM\Software\Wine" >nul 2>&1
if not errorlevel 1 (
    echo Detected Wine/Proton - using native Linux Python...
    echo.
    :: Launch native bash - Z: drive maps to Linux root
    Z:\bin\bash start_server.sh
    echo. > server.lock.stop
    del server.lock 2>nul
    exit
)

:: Write initial lock and clear stale files
echo %time% > server.lock
del server.lock.stop 2>nul
del server.heartbeat 2>nul

:: Start background heartbeat (skip in debug mode)
if "%DEBUG_MODE%"=="0" (
    start /b "" python\python.exe heartbeat.py
)

echo      *    .  *  .    *    .  *  .    *
echo     .                                 .
echo                S O N O R U S
echo             ~~~~~~~~~~~~~~~~~~~
echo               Hogwarts Legacy
echo                   AI Mod
echo             ~~~~~~~~~~~~~~~~~~~
echo     .                                 .
echo      *    .  *  .    *    .  *  .    *
if "%DEBUG_MODE%"=="1" (
    echo.
    echo             --- DEBUG MODE ---
    echo      Server will stay open without the game.
    echo      Press Ctrl+C to stop.
)
echo.


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
"%PYTHON%" -c "import sys; import importlib.util; sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in ['onnx_asr', 'websocket', 'diskcache', 'posthog', 'dotenv']) else 1)" >nul 2>&1
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

:: Copy tkinter module to site-packages (tkinter can't be installed via pip on Windows)
if not exist "python\Lib\site-packages\tkinter" (
    if exist "voice_manager\tkinter" (
        echo Installing tkinter module...
        robocopy "voice_manager\tkinter" "python\Lib\site-packages\tkinter" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul 2>&1 & if errorlevel 8 echo WARNING: Failed to copy tkinter module
    )
)

:: heartbeat.py exits when server.py creates server.heartbeat, or when we write server.lock.stop

:: Pre-download ONNX models (VAD + turn detection)
if not exist "models\silero_vad.onnx" (
    echo Downloading VAD model...
    if not exist "models" mkdir models
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx' -OutFile 'models\silero_vad.onnx'"
)
if not exist "models\smart-turn-v3.2-cpu.onnx" (
    echo Downloading turn detection model...
    if not exist "models" mkdir models
    powershell -Command "Invoke-WebRequest -Uri 'https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx' -OutFile 'models\smart-turn-v3.2-cpu.onnx'"
)

if "%DEBUG_MODE%"=="1" set SONORUS_DEBUG=1

echo Starting Sonorus server...
echo The web interface will open in your browser shortly.
echo.
"%PYTHON%" server.py

:: Signal heartbeat to stop, clean up lock
echo. > server.lock.stop
del server.lock 2>nul
exit
