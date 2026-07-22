@echo off
:: ============================================
:: OmniVoice Dependencies Installer
:: ============================================
:: Installs PyTorch (CUDA 12.8) and related deps
:: into the embedded Python environment.
:: ============================================

cd /d "%~dp0"

set PYTHON=python\python.exe

echo.
echo  ===================================================
echo   Installing OmniVoice Dependencies
echo  ===================================================
echo.

:: Check if already installed
"%PYTHON%" -c "import importlib.util; exit(0 if importlib.util.find_spec('torch') else 1)" >nul 2>&1
if not errorlevel 1 (
    echo PyTorch is already installed.
    echo.
    pause
    exit /b 0
)

echo Step 1/2: Installing PyTorch + torchaudio (CUDA 12.8)...
echo This is a large download (~2.5 GB). Please be patient.
echo.
bin\sfw.exe "%PYTHON%" -m pip install torch>=2.4.0 torchaudio>=2.4.0 --index-url https://download.pytorch.org/whl/cu128 --no-warn-script-location --timeout 120
if errorlevel 1 (
    echo.
    echo ERROR: PyTorch installation failed.
    echo Check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo Step 2/2: Installing transformers + accelerate + safetensors...
echo.
bin\sfw.exe "%PYTHON%" -m pip install transformers>=4.45.0 accelerate safetensors torchcodec --no-warn-script-location --timeout 120
if errorlevel 1 (
    echo.
    echo ERROR: Transformers installation failed.
    echo.
    pause
    exit /b 1
)

echo.
echo  ===================================================
echo   OmniVoice dependencies installed successfully!
echo   The server will restart automatically.
echo  ===================================================
echo.

:: Write flag so the UI knows installation completed
echo ok > data\.omnivoice_deps_installed

pause
