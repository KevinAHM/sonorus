@echo off
setlocal
:: ============================================
:: OmniVoice (Vulkan) Runtime + Models Installer
:: ============================================
:: Downloads the verified portable runtime and
:: the three GGUF models used by Sonorus.
:: ============================================

cd /d "%~dp0"

set "PYTHON=python\python.exe"
set "STATUS_FILE=data\.omnivoice_cpp_install_status"

echo.
echo  ===================================================
echo   Installing OmniVoice (Vulkan)
echo  ===================================================
echo.

if not exist "%PYTHON%" (
    echo ERROR: Embedded Python was not found.
    echo Launch Sonorus once, then try again.
    >"%STATUS_FILE%" echo error
    goto :failed
)

>"%STATUS_FILE%" echo installing

echo Downloading and verifying the portable OmniVoice runtime.
echo.

"%PYTHON%" -c "import sys; sys.path.insert(0, '.'); from services.omnivoice_cpp_engine import download_runtime; download_runtime(lambda current, total, message: print(f'[{current}/{total}] {message}', flush=True))"
if errorlevel 1 (
    echo.
    echo ERROR: OmniVoice runtime installation failed.
    echo Check your internet connection and try again.
    >"%STATUS_FILE%" echo error
    goto :failed
)

"%PYTHON%" -c "import huggingface_hub" >nul 2>&1
if errorlevel 1 (
    echo ERROR: huggingface_hub is not installed.
    echo Launch Sonorus once so its required packages are installed, then try again.
    >"%STATUS_FILE%" echo error
    goto :failed
)

echo Downloading approximately 1.5 GB from the model hosts.
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
echo   OmniVoice (Vulkan) installed successfully!
echo  ===================================================
echo.
goto :finish

:failed
echo.
:: Always pause on failure - even when launched from the config UI with
:: --no-pause - so the error stays readable instead of the window closing.
pause
exit /b 1

:finish
if /I not "%~1"=="--no-pause" pause
exit /b 0
