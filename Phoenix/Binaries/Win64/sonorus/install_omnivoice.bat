@echo off
:: ============================================
:: OmniVoice Dependencies Installer  (patched)
:: ============================================
cd /d "%~dp0"
set PYTHON=python\python.exe
echo.
echo   Installing OmniVoice Dependencies
echo.
:: --- Step 1/3: PyTorch + torchaudio (CUDA 12.8) ---
"%PYTHON%" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('torch') else 1)" >nul 2>&1
if errorlevel 1 (
    echo Step 1/3: Installing PyTorch + torchaudio [CUDA 12.8] - large download ~2.5 GB...
    bin\sfw.exe "%PYTHON%" -m pip install "torch>=2.4.0" "torchaudio>=2.4.0" --index-url https://download.pytorch.org/whl/cu128 --no-warn-script-location --timeout 120
    if errorlevel 1 goto err_torch
) else (
    echo Step 1/3: PyTorch already installed - skipping.
)
:: --- Step 2/3: transformers + accelerate + safetensors ---
"%PYTHON%" -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ['safetensors','transformers','accelerate']) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Step 2/3: Installing transformers + accelerate + safetensors...
    bin\sfw.exe "%PYTHON%" -m pip install "transformers>=4.45.0" accelerate safetensors --no-warn-script-location --timeout 120
    if errorlevel 1 goto err_tf
) else (
    echo Step 2/3: transformers/accelerate/safetensors already installed - skipping.
)
:: --- Step 3/3: torchcodec (own step so a failure cannot block Step 2) ---
"%PYTHON%" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('torchcodec') else 1)" >nul 2>&1
if errorlevel 1 (
    echo Step 3/3: Installing torchcodec...
    bin\sfw.exe "%PYTHON%" -m pip install torchcodec --no-warn-script-location --timeout 120
    if errorlevel 1 echo WARNING: torchcodec failed to install - audio will not load until it is installed.
) else (
    echo Step 3/3: torchcodec already installed - skipping.
)
:: --- FFmpeg 7 DLLs for torchcodec on Windows (4-7 only; NOT 8) ---
if exist "ffmpeg\avutil-59.dll" (
    echo Installing bundled FFmpeg 7 DLLs into python\ ...
    copy /Y "ffmpeg\*.dll" "python\" >nul
) else (
    echo NOTE: torchcodec needs FFmpeg 7 shared DLLs in the python\ folder. FFmpeg 8 will NOT work on Windows. See ffmpeg\READ_ME_FFMPEG.txt.
)
echo ok > data\.omnivoice_deps_installed
echo.
echo   OmniVoice dependency step complete.
echo.
pause
exit /b 0

:err_torch
echo ERROR: PyTorch installation failed. Check your internet connection and try again.
pause
exit /b 1

:err_tf
echo ERROR: transformers/safetensors installation failed.
pause
exit /b 1
