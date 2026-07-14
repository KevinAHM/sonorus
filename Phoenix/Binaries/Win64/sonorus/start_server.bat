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
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\parseltongue.zip" "https://github.com/insomnious/parseltongue/releases/download/v0.2.3/parseltongue-0.2.3.zip"
    powershell -Command "Expand-Archive -Path 'bin\parseltongue.zip' -DestinationPath 'bin\parseltongue_temp' -Force"
    move /Y "bin\parseltongue_temp\parseltongue.exe" "bin\parseltongue.exe" >nul
    rmdir /S /Q "bin\parseltongue_temp" 2>nul
    del "bin\parseltongue.zip" 2>nul
)

if not exist "bin\wwiser.pyz" (
    echo Downloading wwiser...
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\wwiser.pyz" "https://github.com/bnnm/wwiser/releases/download/v20250928/wwiser.pyz"
)

if not exist "bin\wwnames.db3" (
    echo Downloading wwnames.db3...
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\wwnames.db3" "https://github.com/bnnm/wwiser/releases/download/v20250928/wwnames.db3"
)

if not exist "bin\repak.exe" (
    echo Downloading repak...
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\repak.zip" "https://github.com/trumank/repak/releases/download/v0.2.3/repak_cli-x86_64-pc-windows-msvc.zip"
    powershell -Command "Expand-Archive -Path 'bin\repak.zip' -DestinationPath 'bin\repak_temp' -Force"
    move /Y "bin\repak_temp\repak.exe" "bin\repak.exe" >nul
    rmdir /S /Q "bin\repak_temp" 2>nul
    del "bin\repak.zip" 2>nul
)

if not exist "bin\oo2core_9_win64.dll" (
    echo Downloading oo2core...
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\oo2core_9_win64.dll" "https://raw.githubusercontent.com/WorkingRobot/OodleUE/refs/heads/main/Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/win/redist/oo2core_9_win64.dll"
)

if not exist "bin\sfw.exe" (
    echo Downloading Socket Firewall...
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\sfw.exe" "https://github.com/SocketDev/sfw-free/releases/latest/download/sfw-free-windows-x86_64.exe"
)

if not exist "bin\vgmstream\vgmstream-cli.exe" (
    echo Downloading vgmstream...
    if not exist "bin\vgmstream" mkdir "bin\vgmstream"
    curl.exe -fL --retry 3 --retry-delay 2 -o "bin\vgmstream.zip" "https://github.com/vgmstream/vgmstream/releases/download/r2055/vgmstream-win64.zip"
    powershell -Command "Expand-Archive -Path 'bin\vgmstream.zip' -DestinationPath 'bin\vgmstream' -Force"
    del "bin\vgmstream.zip" 2>nul
)

:: Bootstrap pip if not working
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo Bootstrapping pip...
    curl.exe -fL --retry 3 --retry-delay 2 -o "get-pip.py" "https://bootstrap.pypa.io/get-pip.py"
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
"%PYTHON%" -c "import sys; import importlib.util; sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in ['kaldi_native_fbank', 'real_ladybug', 'json_repair', 'qdrant_client']) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Installing Python dependencies...
    bin\sfw.exe "%PYTHON%" -m pip install setuptools wheel --no-warn-script-location -q
    bin\sfw.exe "%PYTHON%" -m pip install -r requirements.txt --no-warn-script-location
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

:: Pre-download ONNX models (turn detection)
if not exist "models\smart-turn-v3.2-cpu.onnx" (
    echo Downloading turn detection model...
    if not exist "models" mkdir models
    "%PYTHON%" -c "from huggingface_hub import hf_hub_download; hf_hub_download('pipecat-ai/smart-turn-v3','smart-turn-v3.2-cpu.onnx', local_dir='models')"
)

if "%DEBUG_MODE%"=="1" set SONORUS_DEBUG=1

echo Starting Sonorus server...
echo The web interface will open in your browser shortly.
echo.
:server_loop
"%PYTHON%" server.py
if exist "data\.server_restart" (
    del "data\.server_restart" 2>nul
    echo Server restarting...
    goto server_loop
)

:: Signal heartbeat to stop, clean up lock
echo. > server.lock.stop
del server.lock 2>nul
exit
