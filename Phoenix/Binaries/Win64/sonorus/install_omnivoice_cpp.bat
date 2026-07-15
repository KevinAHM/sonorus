@echo off
setlocal
:: ============================================
:: OmniVoice (Vulkan) Models Installer
:: ============================================
:: Downloads the two GGUF models used by the
:: bundled omnivoice.cpp/ggml runtime.
:: ============================================

cd /d "%~dp0"

set "PYTHON=python\python.exe"
set "STATUS_FILE=data\.omnivoice_cpp_install_status"

echo.
echo  ===================================================
echo   Installing OmniVoice (Vulkan) Models
echo  ===================================================
echo.

if not exist "%PYTHON%" (
    echo ERROR: Embedded Python was not found.
    echo Launch Sonorus once, then try again.
    >"%STATUS_FILE%" echo error
    goto :failed
)

for %%F in (omnivoice.dll ggml.dll ggml-base.dll ggml-cpu.dll ggml-vulkan.dll) do (
    if not exist "omnivoice_cpp\bin\%%F" (
        echo ERROR: Missing bundled runtime file: %%F
        echo Reinstall or update Sonorus, then try again.
        >"%STATUS_FILE%" echo error
        goto :failed
    )
)

"%PYTHON%" -c "import huggingface_hub" >nul 2>&1
if errorlevel 1 (
    echo ERROR: huggingface_hub is not installed.
    echo Launch Sonorus once so its required packages are installed, then try again.
    >"%STATUS_FILE%" echo error
    goto :failed
)

>"%STATUS_FILE%" echo installing

echo Downloading approximately 1.3 GB from Hugging Face.
echo Existing and partially downloaded files will be reused.
echo.

"%PYTHON%" -c "import sys; sys.path.insert(0, '.'); from services.omnivoice_cpp_engine import download_models; download_models(lambda current, total, message: print(f'[{current}/{total}] {message}', flush=True))"
if errorlevel 1 (
    echo.
    echo ERROR: OmniVoice model download failed.
    echo Check your internet connection and try again.
    >"%STATUS_FILE%" echo error
    goto :failed
)

>"%STATUS_FILE%" echo complete

echo.
echo  ===================================================
echo   OmniVoice (Vulkan) models installed successfully!
echo  ===================================================
echo.
goto :finish

:failed
echo.
if /I not "%~1"=="--no-pause" pause
exit /b 1

:finish
if /I not "%~1"=="--no-pause" pause
exit /b 0
