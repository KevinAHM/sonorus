@echo off
setlocal enabledelayedexpansion
title Sonorus Mod Uninstaller
cls

echo.
echo  ===================================================
echo           S O N O R U S   U N I N S T A L L E R
echo  ===================================================
echo.

:: ---- Check game is closed ----
:check_closed
echo  Is Hogwarts Legacy closed?
choice /c YN /n /m "  (Y/N): "
if !errorlevel! equ 2 (
    echo.
    echo  Please close Hogwarts Legacy before uninstalling.
    echo.
    goto :check_closed
)
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

:: --- Registry lookup for Steam install path ---
set "STEAM_DIR="
for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Valve\Steam" /v InstallPath 2^>nul') do set "STEAM_DIR=%%B"
if not defined STEAM_DIR (
    for /f "tokens=2*" %%A in ('reg query "HKCU\SOFTWARE\Valve\Steam" /v SteamPath 2^>nul') do (
        set "STEAM_DIR=%%B"
    )
    if defined STEAM_DIR set "STEAM_DIR=!STEAM_DIR:/=\!"
)

:: --- Check default steamapps from registry path ---
if defined STEAM_DIR (
    if exist "!STEAM_DIR!\steamapps\common\Hogwarts Legacy" (
        set "GAME_PATH=!STEAM_DIR!\steamapps\common\Hogwarts Legacy"
        goto :confirm
    )
)

:: --- Parse libraryfolders.vdf for additional Steam libraries ---
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

:: --- Scan all drives for common Steam library locations ---
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
:: If user chose "manual" directly, ask about Xbox
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
:: Clean up user input (quotes, forward slashes, trailing backslash)
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
::  CONFIRM
:: ============================================================
:confirm
echo.
echo  Found Hogwarts Legacy at:
echo    !GAME_PATH!
echo.

set "BIN_DIR=Win64"
if !IS_XBOX! equ 1 set "BIN_DIR=WinGDK"

set "DST=!GAME_PATH!\Phoenix"

:: --- Check what Sonorus files exist ---
set "FOUND_ANYTHING=0"

set "MODS_PATH=!DST!\Binaries\!BIN_DIR!\ue4ss\Mods\SonorusMod"
set "PAKS_PATH=!DST!\Content\Paks\LogicMods"
set "PAK_FOLDER=!PAKS_PATH!\sonorusblueprintmod"
set "PAK_FILE=!PAKS_PATH!\sonorusblueprintmod.pak"
set "UCAS_FILE=!PAKS_PATH!\sonorusblueprintmod.ucas"
set "UTOC_FILE=!PAKS_PATH!\sonorusblueprintmod.utoc"

echo  Sonorus files found:
echo.
if exist "!MODS_PATH!" (
    echo    - SonorusMod  ^(ue4ss\Mods\SonorusMod^)
    set "FOUND_ANYTHING=1"
)
if exist "!PAK_FOLDER!" (
    echo    - sonorusblueprintmod folder  ^(LogicMods\sonorusblueprintmod^)
    set "FOUND_ANYTHING=1"
)
if exist "!PAK_FILE!" (
    echo    - sonorusblueprintmod.pak
    set "FOUND_ANYTHING=1"
)
if exist "!UCAS_FILE!" (
    echo    - sonorusblueprintmod.ucas
    set "FOUND_ANYTHING=1"
)
if exist "!UTOC_FILE!" (
    echo    - sonorusblueprintmod.utoc
    set "FOUND_ANYTHING=1"
)

if !FOUND_ANYTHING! equ 0 (
    echo    (none)
    echo.
    echo  Sonorus does not appear to be installed here.
    echo.
    pause
    exit /b 0
)

echo.
choice /c YN /n /m "  Remove these files? (Y/N): "
if !errorlevel! equ 2 (
    echo.
    echo  Uninstall cancelled.
    echo.
    pause
    exit /b 0
)


:: ============================================================
::  BACKUP DATA
:: ============================================================
set "DATA_PATH=!DST!\Binaries\!BIN_DIR!\sonorus\data"
set "BACKUP_DIR=%~dp0backup"

if exist "!DATA_PATH!" (
    echo.
    echo  Do you want to back up your Sonorus data
    echo  ^(memories, dialog, settings^)?
    echo.
    choice /c YN /n /m "  (Y/N): "
    if !errorlevel! equ 1 (
        echo.
        echo  Backing up data...
        if exist "!BACKUP_DIR!\data" rmdir /S /Q "!BACKUP_DIR!\data"
        robocopy "!DATA_PATH!" "!BACKUP_DIR!\data" /E /R:3 /W:5 /NFL /NDL /NP >nul 2>&1
        if exist "!BACKUP_DIR!\data" (
            >>"!BACKUP_DIR!\how-to-restore.txt" (
                echo Copy the /data/ folder into any new Win64/sonorus/
                echo installation and replace existing.
            )
            echo  Backed up to: !BACKUP_DIR!\data
        ) else (
            echo  WARNING: Backup failed. Continuing anyway.
        )
    )
)


:: ============================================================
::  UNINSTALL
:: ============================================================
echo.
echo  Removing Sonorus...
echo.

set "ERRORS=0"

if exist "!MODS_PATH!" (
    echo  Removing SonorusMod...
    rmdir /S /Q "!MODS_PATH!" 2>nul
    if exist "!MODS_PATH!" (
        echo  WARNING: Could not remove SonorusMod folder.
        set "ERRORS=1"
    )
)

if exist "!PAK_FOLDER!" (
    echo  Removing sonorusblueprintmod folder...
    rmdir /S /Q "!PAK_FOLDER!" 2>nul
    if exist "!PAK_FOLDER!" (
        echo  WARNING: Could not remove sonorusblueprintmod folder.
        set "ERRORS=1"
    )
)

if exist "!PAK_FILE!" (
    echo  Removing sonorusblueprintmod.pak...
    del /F /Q "!PAK_FILE!" 2>nul
    if exist "!PAK_FILE!" (
        echo  WARNING: Could not remove sonorusblueprintmod.pak.
        set "ERRORS=1"
    )
)

if exist "!UCAS_FILE!" (
    echo  Removing sonorusblueprintmod.ucas...
    del /F /Q "!UCAS_FILE!" 2>nul
    if exist "!UCAS_FILE!" (
        echo  WARNING: Could not remove sonorusblueprintmod.ucas.
        set "ERRORS=1"
    )
)

if exist "!UTOC_FILE!" (
    echo  Removing sonorusblueprintmod.utoc...
    del /F /Q "!UTOC_FILE!" 2>nul
    if exist "!UTOC_FILE!" (
        echo  WARNING: Could not remove sonorusblueprintmod.utoc.
        set "ERRORS=1"
    )
)

if !ERRORS! equ 1 (
    echo.
    echo  ===================================================
    echo    Some files could not be removed.
    echo  ===================================================
    echo.
    echo  Try right-clicking uninstall_sonorus.bat and choosing
    echo  "Run as administrator", or delete the files manually.
    echo.
    echo  Need help? https://discord.gg/YXhJy3pA7b
) else (
    echo.
    echo  ===================================================
    echo    Sonorus uninstalled successfully!
    echo  ===================================================
    echo.
    echo  The Sonorus mod files have been removed.
    echo  Your game will run without Sonorus next time you launch it.
)

echo.
pause
exit /b 0
