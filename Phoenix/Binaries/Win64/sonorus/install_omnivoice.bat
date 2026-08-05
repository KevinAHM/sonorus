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

echo Step 1/2: Installing PyTorch + torchaudio (CUDA 12.8)...
echo This is a large download (~2.5 GB). Please be patient.
echo.
"%PYTHON%" -c "import importlib.metadata as md,importlib.util,os,sys; s=importlib.util.find_spec('torch'); d=os.path.dirname(s.origin) if s and s.origin else ''; ok=d and os.path.exists(os.path.join(d,'lib','torch_cpu.dll')) and all(importlib.util.find_spec(m) and md.version(m) for m in ('torch','torchaudio')); sys.exit(0 if ok else 1)" >nul 2>&1
if errorlevel 1 (
    bin\sfw.exe "%PYTHON%" -m pip install "torch>=2.4.0" "torchaudio>=2.4.0" --index-url https://download.pytorch.org/whl/cu128 --no-warn-script-location --timeout 120
    if errorlevel 1 (
        echo.
        echo ERROR: PyTorch installation failed.
        echo Check your internet connection and try again.
        echo.
        pause
        exit /b 1
    )
) else (
    echo PyTorch and torchaudio are already installed.
)

echo.
echo Step 2/2: Installing transformers + accelerate + safetensors + soundfile...
echo.
"%PYTHON%" -c "import importlib.metadata as md,importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) and md.version(m) for m in ('transformers','accelerate','safetensors','soundfile')) else 1)" >nul 2>&1
if errorlevel 1 (
    bin\sfw.exe "%PYTHON%" -m pip install "transformers>=4.45.0" accelerate safetensors soundfile --no-warn-script-location --timeout 120
    if errorlevel 1 (
        echo.
        echo ERROR: Transformers installation failed.
        echo.
        pause
        exit /b 1
    )
) else (
    echo Transformers, accelerate, safetensors, and SoundFile are already installed.
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
