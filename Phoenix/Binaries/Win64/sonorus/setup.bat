@echo off
echo ============================================
echo  Sonorus Setup
echo ============================================
echo.

cd /d "%~dp0"

set PYTHON=python\python.exe

:: Check portable Python exists
if not exist "%PYTHON%" (
    echo ERROR: Portable Python not found at %PYTHON%
    echo Please ensure the python folder is included with Sonorus.
    pause
    exit /b 1
)

echo Found portable Python at %PYTHON%

:: Bootstrap pip if not present
if not exist "python\Scripts\pip.exe" (
    echo.
    echo Bootstrapping pip...

    if not exist "get-pip.py" (
        echo Downloading get-pip.py...
        powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    )

    "%PYTHON%" get-pip.py --no-warn-script-location

    if errorlevel 1 (
        echo ERROR: Failed to install pip
        pause
        exit /b 1
    )

    del get-pip.py 2>nul
    echo Pip installed successfully.
)

:: Ensure setuptools and wheel are installed
echo.
echo Ensuring setuptools and wheel are installed...
"%PYTHON%" -m pip install setuptools wheel --no-warn-script-location -q

:: Install dependencies
echo.
echo Installing dependencies from requirements.txt...
echo This can take up to 10 minutes, please wait...
echo.
"%PYTHON%" -m pip install -r requirements.txt --no-warn-script-location

if errorlevel 1 (
    echo.
    echo ERROR: Failed to install some dependencies.
    echo Check the output above for details.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete!
echo ============================================
echo.
echo Next steps:
echo   1. Ensure SonorusMod is added and enabled in UE4SS (no other mod loaders!)
echo   2. Launch Hogwarts Legacy
echo   3. Follow the setup wizard in the web browser that opens
echo.
pause
