@echo off
setlocal enabledelayedexpansion
title Sonorus Toggle
cls

echo.
echo  ===================================================
echo        S O N O R U S   E N A B L E / D I S A B L E
echo  ===================================================
echo.

:: ---- Enable or Disable ----
echo  What would you like to do?
echo.
echo    [1] Enable Sonorus
echo    [2] Disable Sonorus
echo.
choice /c 12 /n /m "  Choice: "
set "ACTION=%errorlevel%"
if %ACTION% equ 1 (set "FLAG=--enable") else (set "FLAG=--disable")
echo.

:: ---- Platform Selection ----
echo  Select your platform:
echo.
echo    [1] Steam
echo    [2] Epic Games
echo    [3] Xbox App (Game Pass)
echo    [4] Enter path manually
echo.
choice /c 1234 /n /m "  Choice: "
set "PLAT=%errorlevel%"

set "GAME_PATH="
set "IS_XBOX=0"

if %PLAT% equ 1 goto :find_steam
if %PLAT% equ 2 goto :find_epic
if %PLAT% equ 3 goto :find_xbox
if %PLAT% equ 4 goto :manual


:: ============================================================
::  STEAM
:: ============================================================
:find_steam
echo.
echo  Searching for Hogwarts Legacy (Steam)...

set "STEAM_DIR="
for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Valve\Steam" /v InstallPath 2^>nul') do set "STEAM_DIR=%%B"
if not defined STEAM_DIR (
    for /f "tokens=2*" %%A in ('reg query "HKCU\SOFTWARE\Valve\Steam" /v SteamPath 2^>nul') do (
        set "STEAM_DIR=%%B"
    )
    if defined STEAM_DIR set "STEAM_DIR=!STEAM_DIR:/=\!"
)

if defined STEAM_DIR (
    if exist "!STEAM_DIR!\steamapps\common\Hogwarts Legacy" (
        set "GAME_PATH=!STEAM_DIR!\steamapps\common\Hogwarts Legacy"
        goto :confirm
    )
)

if defined STEAM_DIR if exist "!STEAM_DIR!\steamapps\libraryfolders.vdf" (
    for /f "usebackq delims=" %%L in ("!STEAM_DIR!\steamapps\libraryfolders.vdf") do (
        set "VLINE=%%L"
        set "VTEST=!VLINE:path=!"
        if not "!VLINE!"=="!VTEST!" (
            set VCLEAN=!VLINE:"=#!
            for /f "tokens=4 delims=#" %%V in ("!VCLEAN!") do (
                set "LIB=%%V"
                set "LIB=!LIB:\\=\!"
                if not "!LIB!"=="" if exist "!LIB!\steamapps\common\Hogwarts Legacy" (
                    set "GAME_PATH=!LIB!\steamapps\common\Hogwarts Legacy"
                )
            )
        )
    )
)
if defined GAME_PATH goto :confirm

echo  Scanning drives...
for %%D in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%D:\" (
        for %%P in (
            "%%D:\Program Files (x86)\Steam\steamapps\common\Hogwarts Legacy"
            "%%D:\Program Files\Steam\steamapps\common\Hogwarts Legacy"
            "%%D:\Steam\steamapps\common\Hogwarts Legacy"
            "%%D:\SteamLibrary\steamapps\common\Hogwarts Legacy"
            "%%D:\steamapps\common\Hogwarts Legacy"
            "%%D:\Games\Steam\steamapps\common\Hogwarts Legacy"
            "%%D:\Games\SteamLibrary\steamapps\common\Hogwarts Legacy"
            "%%D:\Games\steamapps\common\Hogwarts Legacy"
        ) do (
            if not defined GAME_PATH if exist "%%~P" (
                set "GAME_PATH=%%~P"
            )
        )
    )
)
if defined GAME_PATH goto :confirm
echo  Could not find Hogwarts Legacy (Steam).
goto :manual


:: ============================================================
::  EPIC GAMES
:: ============================================================
:find_epic
echo.
echo  Searching for Hogwarts Legacy (Epic Games)...
echo  Scanning drives...
for %%D in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%D:\" (
        for %%P in (
            "%%D:\Program Files\Epic Games\HogwartsLegacy"
            "%%D:\Program Files\Epic Games\Hogwarts Legacy"
            "%%D:\Program Files (x86)\Epic Games\HogwartsLegacy"
            "%%D:\Program Files (x86)\Epic Games\Hogwarts Legacy"
            "%%D:\Epic Games\HogwartsLegacy"
            "%%D:\Epic Games\Hogwarts Legacy"
            "%%D:\Games\Epic Games\HogwartsLegacy"
            "%%D:\Games\Epic Games\Hogwarts Legacy"
            "%%D:\Games\HogwartsLegacy"
            "%%D:\Games\Hogwarts Legacy"
        ) do (
            if not defined GAME_PATH if exist "%%~P" (
                set "GAME_PATH=%%~P"
            )
        )
    )
)
if defined GAME_PATH goto :confirm
echo  Could not find Hogwarts Legacy (Epic Games).
goto :manual


:: ============================================================
::  XBOX APP
:: ============================================================
:find_xbox
set "IS_XBOX=1"
echo.
echo  Searching for Hogwarts Legacy (Xbox App)...
echo  Scanning drives...
for %%D in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%D:\" (
        for %%P in (
            "%%D:\XboxGames\Hogwarts Legacy\Content"
            "%%D:\Program Files\XboxGames\Hogwarts Legacy\Content"
            "%%D:\Games\XboxGames\Hogwarts Legacy\Content"
            "%%D:\Xbox Games\Hogwarts Legacy\Content"
        ) do (
            if not defined GAME_PATH if exist "%%~P" (
                set "GAME_PATH=%%~P"
            )
        )
    )
)
if defined GAME_PATH goto :confirm
echo  Could not find Hogwarts Legacy (Xbox App).
goto :manual


:: ============================================================
::  MANUAL PATH ENTRY
:: ============================================================
:manual
echo.
if %PLAT% neq 4 goto :skip_xbox_ask
echo  Is this an Xbox App / Game Pass install?
choice /c YN /n /m "  (Y/N): "
if !errorlevel! equ 1 set "IS_XBOX=1"
echo.
:skip_xbox_ask

if !IS_XBOX! equ 1 (
    echo  Enter game path, e.g. D:\XboxGames\Hogwarts Legacy\Content
) else (
    echo  Enter game path, e.g. D:\SteamLibrary\steamapps\common\Hogwarts Legacy
)
echo.
set /p "GAME_PATH=  Path: "
set "GAME_PATH=!GAME_PATH:"=!"
set "GAME_PATH=!GAME_PATH:/=\!"
if "!GAME_PATH:~-1!"=="\" set "GAME_PATH=!GAME_PATH:~0,-1!"

if not exist "!GAME_PATH!" (
    echo.
    echo  ERROR: That path does not exist.
    echo.
    pause
    exit /b 1
)


:: ============================================================
::  CONFIRM & TOGGLE
:: ============================================================
:confirm
echo.
echo  Found Hogwarts Legacy at:
echo    !GAME_PATH!
echo.

set "BIN_DIR=Binaries\Win64"
if !IS_XBOX! equ 1 set "BIN_DIR=Binaries\WinGDK"

set "MODS_DIR=!GAME_PATH!\Phoenix\!BIN_DIR!\ue4ss\Mods"
set "PYTHON_EXE=!GAME_PATH!\Phoenix\!BIN_DIR!\sonorus\python\python.exe"
set "TOGGLE_PY=!GAME_PATH!\Phoenix\!BIN_DIR!\sonorus\toggle_sonorus.py"

:: Verify Sonorus is installed
if not exist "!PYTHON_EXE!" (
    echo  ERROR: Sonorus does not appear to be installed.
    echo  Could not find: !PYTHON_EXE!
    echo.
    echo  Run install_sonorus.bat first.
    echo.
    pause
    exit /b 1
)

if not exist "!MODS_DIR!" (
    echo  ERROR: UE4SS Mods directory not found.
    echo  Could not find: !MODS_DIR!
    echo.
    pause
    exit /b 1
)

if %ACTION% equ 1 (
    echo  Enabling Sonorus...
) else (
    echo  Disabling Sonorus...
)

"!PYTHON_EXE!" "!TOGGLE_PY!" !FLAG! --mods-dir "!MODS_DIR!"

if !errorlevel! neq 0 (
    echo.
    echo  ERROR: Toggle failed. See error above.
    echo.
    pause
    exit /b 1
)

echo.
echo  ===================================================
if %ACTION% equ 1 (
    echo    Sonorus is now ENABLED.
) else (
    echo    Sonorus is now DISABLED.
)
echo  ===================================================
echo.
echo  Restart Hogwarts Legacy for changes to take effect.
echo.
pause
exit /b 0
