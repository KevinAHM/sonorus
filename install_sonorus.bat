@echo off
setlocal enabledelayedexpansion
title Sonorus Mod Installer
cls

echo.
echo  ===================================================
echo             S O N O R U S   I N S T A L L E R
echo  ===================================================
echo.

:: ---- Verify Phoenix folder exists next to this script ----
set "MOD_DIR=%~dp0Phoenix"
if not exist "%MOD_DIR%\" (
    echo  ERROR: Phoenix folder not found next to this installer.
    echo  Make sure install_sonorus.bat and the Phoenix folder
    echo  are in the same directory.
    echo.
    pause
    exit /b 1
)

:: ---- Detect extraction-into-game-directory mistake ----
if exist "%~dp0Engine" (
    echo  NOTE: It looks like you extracted the zip directly into
    echo  your Hogwarts Legacy game directory. The mod files are
    echo  probably already in place, so it might just work.
    echo.
    echo  If you run into problems, re-extract the zip to a
    echo  separate folder ^(like your Desktop^) and run the
    echo  installer from there.
    echo.
    pause
    exit /b 0
)

:: ---- Check game is closed ----
:check_closed
echo  Is Hogwarts Legacy closed?
choice /c YN /n /m "  (Y/N): "
if !errorlevel! equ 2 (
    echo.
    echo  Please close Hogwarts Legacy before installing.
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
        :: Only process lines containing "path"
        set "VTEST=!VLINE:path=!"
        if not "!VLINE!"=="!VTEST!" (
            :: Replace quotes with # for safe tokenization
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

:: Basic validation - check for Phoenix or Engine subfolder
if not exist "!GAME_PATH!\Phoenix" if not exist "!GAME_PATH!\Engine" (
    echo  WARNING: This doesn't look like a Hogwarts Legacy installation.
    echo  Expected to find Phoenix or Engine subfolder.
    echo.
)

choice /c YN /n /m "  Install Sonorus here? (Y/N): "
if !errorlevel! equ 2 (
    echo.
    echo  Installation cancelled.
    echo.
    pause
    exit /b 0
)


:: ============================================================
::  INSTALL
:: ============================================================
echo.
echo  Installing Sonorus...
echo.

set "SRC=%~dp0Phoenix"
set "DST=!GAME_PATH!\Phoenix"

:: --- Detect existing UE4SS files/folders so Sonorus installs cleanly ---
set "BIN_DIR=Binaries\Win64"
if !IS_XBOX! equ 1 set "BIN_DIR=Binaries\WinGDK"

set "LEGACY_FOUND=0"
set "HAS_UE4SS_DIR=0"
set "HAS_UE4SS_DLL=0"
set "HAS_UE4SS_INI=0"
set "HAS_MODS=0"

:: Back up either folder casing. The bundled Sonorus copy uses lowercase ue4ss,
:: but users may already have an existing ue4ss/UE4SS install in place.
if exist "!DST!\!BIN_DIR!\ue4ss" set "LEGACY_FOUND=1" & set "HAS_UE4SS_DIR=1"
dir /b "!DST!\!BIN_DIR!" 2>nul | findstr /x "UE4SS.dll" >nul 2>&1
if !errorlevel! equ 0 set "LEGACY_FOUND=1" & set "HAS_UE4SS_DLL=1"
dir /b "!DST!\!BIN_DIR!" 2>nul | findstr /x "UE4SS-settings.ini" >nul 2>&1
if !errorlevel! equ 0 set "LEGACY_FOUND=1" & set "HAS_UE4SS_INI=1"
if exist "!DST!\!BIN_DIR!\Mods" set "LEGACY_FOUND=1" & set "HAS_MODS=1"

:: --- Prompt user if legacy files detected ---
if !LEGACY_FOUND! equ 1 (
    echo  WARNING: Existing UE4SS installation detected:
    echo.
    if !HAS_UE4SS_DIR! equ 1 echo    - ue4ss/UE4SS folder
    if !HAS_UE4SS_DLL! equ 1 echo    - UE4SS.dll
    if !HAS_UE4SS_INI! equ 1 echo    - UE4SS-settings.ini
    if !HAS_MODS! equ 1     echo    - Mods folder
    echo.
    echo  Sonorus includes its own version of UE4SS. Your existing
    echo  version is outdated and incompatible with Sonorus.
    echo.
    echo  The old files will be backed up ^(not deleted^).
    echo.
    choice /c YN /n /m "  Replace with Sonorus version? (Y/N): "
    if !errorlevel! equ 2 (
        echo.
        echo  Installation cancelled.
        echo.
        pause
        exit /b 0
    )
    echo.
)

:: --- Backup legacy UE4SS files ---
if !HAS_UE4SS_DIR! equ 1 (
    echo  Backing up ue4ss folder...
    if exist "!DST!\!BIN_DIR!\ue4ss-backup" rmdir /S /Q "!DST!\!BIN_DIR!\ue4ss-backup"
    ren "!DST!\!BIN_DIR!\ue4ss" "ue4ss-backup"
)
if !HAS_UE4SS_DLL! equ 1 (
    echo  Backing up UE4SS.dll...
    if exist "!DST!\!BIN_DIR!\UE4SS.dll.backup" del "!DST!\!BIN_DIR!\UE4SS.dll.backup"
    ren "!DST!\!BIN_DIR!\UE4SS.dll" "UE4SS.dll.backup"
)
if !HAS_UE4SS_INI! equ 1 (
    echo  Backing up UE4SS-settings.ini...
    if exist "!DST!\!BIN_DIR!\UE4SS-settings.ini.backup" del "!DST!\!BIN_DIR!\UE4SS-settings.ini.backup"
    ren "!DST!\!BIN_DIR!\UE4SS-settings.ini" "UE4SS-settings.ini.backup"
)
if !HAS_MODS! equ 1 (
    echo  Backing up Mods folder...
    if exist "!DST!\!BIN_DIR!\Mods-Backup" rmdir /S /Q "!DST!\!BIN_DIR!\Mods-Backup"
    ren "!DST!\!BIN_DIR!\Mods" "Mods-Backup"
)

if !IS_XBOX! equ 1 (
    echo  [Xbox] Copying Win64 as WinGDK...
    echo.
)

:: --- Run robocopy (IS=overwrite same, IT=overwrite tweaked) ---
if !IS_XBOX! equ 1 (
    robocopy "%SRC%" "!DST!" /E /IS /IT /XD "%SRC%\Binaries\Win64" /R:3 /W:5 /NFL /NDL /NP
    set "RC1=!errorlevel!"
    robocopy "%SRC%\Binaries\Win64" "!DST!\Binaries\WinGDK" /E /IS /IT /R:3 /W:5 /NFL /NDL /NP
    set "RC2=!errorlevel!"
) else (
    robocopy "%SRC%" "!DST!" /E /IS /IT /R:3 /W:5 /NFL /NDL /NP
    set "RC1=!errorlevel!"
    set "RC2=0"
)

:: robocopy: 0-7 = success, 8+ = error
if !RC1! geq 8 goto :install_failed
if !RC2! geq 8 goto :install_failed

:: --- Verify files actually landed at destination ---
set "CHECK_DIR=Win64"
if !IS_XBOX! equ 1 set "CHECK_DIR=WinGDK"

if not exist "!DST!\Binaries\!CHECK_DIR!\dwmapi.dll" goto :install_failed
if not exist "!DST!\Binaries\!CHECK_DIR!\sonorus\server.py" goto :install_failed
if not exist "!DST!\Binaries\!CHECK_DIR!\ue4ss\Mods\SonorusMod\Scripts\main.lua" goto :install_failed
if not exist "!DST!\Content\Paks\LogicMods\SonorusMod.pak" goto :install_failed
if not exist "!DST!\Content\Paks\LogicMods\SonorusMod.ucas" goto :install_failed
if not exist "!DST!\Content\Paks\LogicMods\SonorusMod.utoc" goto :install_failed
if not exist "!DST!\Content\Paks\LogicMods\SonorusMod\config.lua" goto :install_failed

echo.
echo  ===================================================
echo    Sonorus installed successfully!
echo  ===================================================
echo.
echo  Location: !DST!
if !IS_XBOX! equ 1 echo  Win64 files mapped to WinGDK
echo.
echo  You can now delete the extracted zip files.
echo  Sonorus has been copied into your game directory.
echo.
echo  Launch Hogwarts Legacy to start using Sonorus.
goto :install_done

:install_failed
echo.
echo  ERROR: Installation failed.
echo  Files were not found at the destination after copying.
echo.
echo  Destination: !DST!
echo.
echo  Try right-clicking install_sonorus.bat and choosing
echo  "Run as administrator", or copy the Phoenix folder
echo  into your game directory manually.
echo.
echo  Need help? https://discord.gg/YXhJy3pA7b

:install_done
echo.
pause
exit /b 0
