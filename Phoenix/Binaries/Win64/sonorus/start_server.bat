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
            pause
            exit /b 1
        )
    ) else (
        echo ERROR: Python folder not found and python.zip is missing.
        pause
        exit /b 1
    )
)

echo Starting Sonorus server...
echo The web interface will open in your browser shortly.
echo.
python\python.exe server.py
exit
