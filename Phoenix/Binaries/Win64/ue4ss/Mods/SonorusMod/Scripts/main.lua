-- ============================================
-- Sonorus Mod - Main (Hooks & Keybinds only)
-- All logic is in logic.lua (hot-reloadable via F11)
-- ============================================
print("[Sonorus] main.lua starting...")

-- Detect folder structure: ue4ss\Mods\ vs Mods\
local f = io.open("ue4ss\\Mods\\SonorusMod\\Scripts\\logic.lua", "r")
local scriptsPath = f and "ue4ss\\Mods\\SonorusMod\\Scripts\\" or "Mods\\SonorusMod\\Scripts\\"
if f then f:close() end
_G.SonorusScriptsPath = scriptsPath

-- Check for hot reload and warn user
if _G.SonorusModLoaded then
    print("[Sonorus] !! HOT RELOAD DETECTED !!")
    print("[Sonorus] This mod does not support hot reload.")
    print("[Sonorus] Please restart the game to reload the mod.")
    return
end
_G.SonorusModLoaded = true

-- ============================================
-- Developer Mode (synced from settings.json via Python server)
-- ============================================
_G.SonorusDevMode = false  -- Default off, synced when Python connects
_G.LogToFile = false         -- Blocking DevPrint breadcrumbs for crash debugging

local DevLog = require("Utils.DevLog")
local TickScheduler = require("Utils.TickScheduler")

function _G.DevPrint(...)
    if _G.LogToFile and DevLog then
        DevLog.Log("DevPrint", ...)
    end
    if _G.SonorusDevMode then
        print(...)
    end
end

-- BP-driven pause state. Default to paused until Blueprint tells us otherwise.
_G.GamePauseState = _G.GamePauseState or {
    isPaused = true,
    hasBPEvent = false,
    updatedAt = 0,
}
_G.GamePauseState.isPaused = _G.GamePauseState.isPaused ~= false
_G.LastKnownPauseState = (_G.LastKnownPauseState == nil) and true or _G.LastKnownPauseState

-- ============================================
-- Global State (shared with logic.lua)
-- ============================================
_G.SonorusState = {
    -- Phase-based state machine
    -- Values: "idle", "preparing", "playing", "closing"
    phase = "idle",
    currentTurnId = nil,       -- Which turn is active (used by GetCurrentSpeakerActor)
    -- Active fields
    playerName = "",           -- Player's character name (for dialogue history)
    playerHouse = "",          -- Player's house (Gryffindor, Slytherin, etc.)
    playerLoaded = false,      -- True after player is in game (ClientRestart fired)
    sonorusModActor = nil,     -- Cached Blueprint ModActor reference
    sonorusModActorName = nil, -- Cached actor full name for debugging stale refs
    pendingIdle = false,       -- Deferred idle transition (wait for mouth to close)
    pendingIdleAt = 0,         -- Timestamp for a deferred idle during turn handoff
    pendingEndBehavior = nil,  -- Optional idle cleanup override from Python
    hasLoadedOnce = false
}

local _handshakePending = false

-- ============================================
-- Player Info Update (on load/reload)
-- ============================================
local Utils = require("Utils.Utils")
local Cache = require("Utils.Cache")
local BlueprintHelpers = require("Utils.BlueprintHelpers")
local LocationRegistry = require("Utils.LocationRegistry")

-- Updates player info (name/house) - must be called on game thread
local function UpdatePlayerInfo()
    -- Get player name
    local firstName, lastName, fullName = Utils.GetPlayerName()
    if fullName and fullName ~= "" and string.lower(fullName) ~= "firstname lastname" then
        _G.SonorusState.playerName = fullName
        print("[Sonorus] Player name: " .. fullName)
    end
    -- Get player house
    local house = Utils.GetPlayerHouse()
    if house and house ~= "" then
        _G.SonorusState.playerHouse = house
    end
end

function ShowHint(message, duration, layout)
    local UIManager = Cache.Get("UIManager", function()
        return FindFirstOf("UIManager")
    end)
    if UIManager and UIManager:IsValid() then
        layout = layout or {
            Position = { X = 500, Y = 500 },
            Alignment = { X = 500, Y = 500 }
        }
        UIManager:SetAndShowHintMessage(message, layout, true, duration or 2)
    end
end
-- Immediate cleanup for any world transition (safe to call mid-load)
local function InvalidateWorld(reason)
    print("[Sonorus] InvalidateWorld: " .. reason)
    Cache.ClearObjects()
    Cache.ClearAllEntities()
    Cache.InvalidateStatic()
    BlueprintHelpers.InvalidateSonorusModActor(reason)
    if TimeDilation and TimeDilation.MarkDirty then
        pcall(TimeDilation.MarkDirty, tostring(reason))
    end
    if StopAmbientGaze then pcall(function() StopAmbientGaze("invalidate world: " .. tostring(reason)) end) end
    if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
    if SetInputModeGameOnly then pcall(SetInputModeGameOnly) end
    if _G.ChatInputState then
        _G.ChatInputState.active = false
        _G.ChatInputState.text = ""
    end
    _G.ChatPreviewLock = nil
    _G.STTPreviewLock = nil
    if _G.SonorusState then _G.SonorusState.playerVoiceId = nil end
    if _G._TimeCache then _G._TimeCache.initialized = false end
    if _G._PermanentStatics then _G._PermanentStatics.initialized = false end
    if _G.CommitmentManager then pcall(_G.CommitmentManager.MarkAllDirty) end
    _G._MissionStatusCache = nil
end

-- Delayed refreshes for after player is back in the world (NOT safe mid-load)
local function RefreshWorld(reason)
    print("[Sonorus] RefreshWorld: " .. reason)
    ExecuteInGameThreadWithDelay(2000, function()
        if not _G.SonorusState.playerLoaded then return end
        if RefreshTimeCache then pcall(RefreshTimeCache, true) end
        if TimeDilation and TimeDilation.IsActive() then
            ExecuteInGameThreadWithDelay(3000, function()
                if not _G.SonorusState.playerLoaded then return end
                TimeDilation.UpdateRate(true)
            end)
        end
        UpdatePlayerInfo()
        if _G.PathNav then
            pcall(_G.PathNav.Clear)
            pcall(_G.PathNav.RestartIfPending)
        end
        if RefreshHousePoints then RefreshHousePoints() end
        if CompanionFollow then pcall(CompanionFollow.applySettings) end
    end)
    if _G.ForceRefreshStaticCache then _G.ForceRefreshStaticCache() end
end

local function DoPlayerHandshake()
    local firstName, lastName, fullName = Utils.GetPlayerName()
    if not fullName or fullName == "" then
        print("[Sonorus] No player name available — deferring handshake\n")
        return false
    end

    -- During character creation, the game returns placeholder "firstname lastname"
    if string.lower(fullName) == "firstname lastname" then
        print("[Sonorus] Placeholder player name detected — deferring handshake\n")
        return false
    end

    _handshakePending = true
    print("[Sonorus] Sending player_handshake: " .. fullName .. "\n")
    SocketClient.send({
        type = "player_handshake",
        data = { playerName = fullName }
    })
    return true
end

function OnPlayerReady()
    _handshakePending = false
    _G.SonorusState.playerLoaded = true
    _G.SonorusState.playerLoadedAt = os.clock()
    print("[Sonorus] Player ready — playerLoaded = true\n")

    -- Build location registry lookup tables (localization file exists by now)
    -- Use _G ref so hot-reloaded module (from logic.lua dofile) is picked up
    local LR = _G.LocationRegistryModule or LocationRegistry
    LR.Init()

    -- Insert 24hr commitment activities into game DB (needs registry + spots loaded)
    if _G.CommitmentManager then
        local ok, err = pcall(_G.CommitmentManager.Init)
        if not ok then
            print("[Sonorus] CommitmentManager.Init error: " .. tostring(err))
        end
    end

    -- Now safe to send loading:finished and do post-load work
    RefreshWorld("player handshake complete")
    SocketClient.send({
        type = "game_event",
        event = "loading:finished",
        data = {}
    })

end

function OnLoadingScreenFinished()
    if _G.SonorusState.playerLoaded then return end  -- Dedup (hook + poll can both fire)
    if _handshakePending then return end  -- Already waiting

    -- Try to handshake. If no player name yet (character creation),
    -- playerLoaded stays false and we'll try again next loading screen.
    local sent = DoPlayerHandshake()
    if not sent then return end

    -- Timeout: if no player_ready after 15 seconds, retry once.
    -- If retry also fails after 15 more seconds, proceed anyway.
    ExecuteInGameThreadWithDelay(15000, function()
        if _handshakePending then
            print("[Sonorus] Handshake timeout — retrying\n")
            DoPlayerHandshake()
            ExecuteInGameThreadWithDelay(15000, function()
                if _handshakePending then
                    print("[Sonorus] Handshake retry timeout — proceeding without handshake\n")
                    _handshakePending = false
                    _G.SonorusState.playerLoaded = true
                    _G.SonorusState.playerLoadedAt = os.clock()
                    RefreshWorld("handshake timeout fallback")
                    SocketClient.send({
                        type = "game_event",
                        event = "loading:finished",
                        data = {}
                    })
                end
            end)
        end
    end)
end

-- Loading screen detection - fires when entering/exiting game
-- (note: "Loadingcreen" is the actual class name, not a typo)
_G._LoadingScreenPollHandle = nil  -- track active poll so we don't stack them
NotifyOnNewObject("/Script/Phoenix.Loadingcreen", function(Context)
    print("[Sonorus] Loading screen started")
    _G.SonorusState.playerLoaded = false
    _G.SonorusState.hasLoadedOnce = true
    InvalidateWorld("loading screen started")

    -- Cancel any existing poll before starting a new one
    if _G._LoadingScreenPollHandle and _G._LoadingScreenPollHandle ~= "loading_screen_poll" then
        pcall(CancelDelayedAction, _G._LoadingScreenPollHandle)
    end
    TickScheduler.Unregister("loading_screen_poll")
    _G._LoadingScreenPollHandle = nil

    -- Poll every 1s — require curtain up for 2 consecutive ticks before firing
    local pollTicks = 0
    local curtainUpTicks = 0
    _G._LoadingScreenPollHandle = "loading_screen_poll"
    TickScheduler.Register("loading_screen_poll", 1013, function()
        pollTicks = pollTicks + 1
        local curtainDown = true
        pcall(function()
            local curtainSys = FindFirstOf("CurtainSubsystem")
            if curtainSys and Utils.SafeIsValid(curtainSys) then
                curtainDown = curtainSys:IsCurtainDown(curtainSys)
            end
        end)
        if curtainDown then
            curtainUpTicks = 0
            print("[Sonorus] Loading screen tick " .. pollTicks .. "s - curtain still down")
        else
            curtainUpTicks = curtainUpTicks + 1
            if false and curtainUpTicks < 2 then
                print("[Sonorus] Loading screen tick " .. pollTicks .. "s - curtain up (" .. curtainUpTicks .. "/2 confirm)")
                return
            end
            TickScheduler.Unregister("loading_screen_poll")
            _G._LoadingScreenPollHandle = nil
            print("[Sonorus] Curtain confirmed after " .. pollTicks .. "s - load complete")
            if OnLoadingScreenFinished then pcall(OnLoadingScreenFinished) end
        end
    end)
end)

-- Hook on save load / character change (logging only — OnLoadingScreenFinished handles setup)
RegisterHook("/Script/Engine.PlayerController:ClientRestart", function(Context, NewPawn)
    print("[Sonorus] ClientRestart hook fired")
    if not _G.SonorusState.hasLoadedOnce then
        print("[Sonorus] Has not loaded yet, skipping hook")
        return
    end
end)

-- Hook on fast travel / wait completion — may or may not trigger a loading screen
-- (Wait/time pass does NOT create a Loadingcreen, but fast travel usually does)
RegisterHook("/Script/Phoenix.FastTravelManager:FinishWait", function(Context)
    print("[Sonorus] FinishWait hook fired")
    InvalidateWorld("fast travel / wait")
    RefreshWorld("fast travel / wait")
end)

-- ============================================
-- Blueprint Mod Actor Detection
-- ============================================
BlueprintHelpers.SetupSonorusModActorLoader()

-- ============================================
-- Keybinds (delegate to logic.lua functions)
-- ============================================

RegisterKeyBind(Key.F7, {}, function()
    if not _G.SonorusDevMode then return end  -- Dev mode only
    if DebugF7 then
        DebugF7()
    else
        print("[Sonorus] DebugF7 not loaded - press F11")
    end
end)

RegisterKeyBind(Key.F11, {}, function()
    if not _G.SonorusDevMode then return end  -- Dev mode only
    print("[Sonorus] Reloading logic.lua...")
    local success, err = pcall(function()
        dofile(_G.SonorusScriptsPath .. "logic.lua")
    end)
    if success then
        Cache.InvalidateStatic()
        if _G.ForceRefreshStaticCache then _G.ForceRefreshStaticCache() end
        print("[Sonorus] Logic reloaded!")
    else
        print("[Sonorus] Reload failed: " .. tostring(err))
    end
end)

-- F9: Register commitment spot at player's current position (dev mode only)
RegisterKeyBind(Key.F9, {}, function()
    if not _G.SonorusDevMode then return end
    ExecuteInGameThread(function()
        local location = _G.LastTrackedLocation
        if not location or location == "" then
            if ShowHint then ShowHint("No location tracked", 3) end
            return
        end

        local staticData = Cache.GetStaticData()
        local player = staticData and staticData.player
        if not player or not player:IsValid() then
            if ShowHint then ShowHint("No player actor", 3) end
            return
        end

        local pos, camRot
        pcall(function()
            pos = player:K2_GetActorLocation()
            -- Use camera yaw (where you're looking), not player yaw
            local cam = staticData.cameraManager
            if cam then camRot = cam:GetCameraRotation() end
        end)
        if not pos or not camRot then
            if ShowHint then ShowHint("Could not read position/camera", 3) end
            return
        end

        local yaw = camRot.Yaw or 0
        local msg = string.format("Spot: %s\nX=%.1f Y=%.1f Z=%.1f\nYaw=%.1f",
            location, pos.X, pos.Y, pos.Z, yaw)
        if ShowHint then ShowHint(msg, 5) end

        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({
                type = "register_commitment_spot",
                location = location,
                x = pos.X,
                y = pos.Y,
                z = pos.Z,
                yaw = yaw,
            })
        end
    end)
end)

-- ============================================
-- Dialogue Blocker Hooks (Experimental, may not work)
-- ============================================
local dialogueHookPaths = {
    "/Script/Phoenix.AvaAudioGameplayStatics:PostDialogueEventByReference",
    "/Script/Phoenix.AvaAudioGameplayStatics:PostDialogueEvent",
    "/Script/Phoenix.AvaAudioGameplayStatics:PlayDialogueSequenceByReference",
    "/Script/Phoenix.AvaAudioGameplayStatics:QueueDialogueEventByReference",
}

local dialogueBlockerSetup = false

-- Global so logic.lua can call it on first conversation
function SetupDialogueBlocker()
    if dialogueBlockerSetup then return end
    dialogueBlockerSetup = true
    print("[Sonorus] Setting up dialogue blocker...")

    for _, hookPath in ipairs(dialogueHookPaths) do
        pcall(function()
            RegisterHook(hookPath,
                function(Context)
                    if OnDialoguePreHook then OnDialoguePreHook(Context) end
                end,
                function(Context, ReturnValue)
                    if OnDialoguePostHook then OnDialoguePostHook(Context, ReturnValue) end
                end
            )
            print("[Sonorus] Hooked: " .. hookPath)
        end)
    end

    print("[Sonorus] Dialogue blocker ready")
end

-- ============================================
-- Dialogue Tracker Hook
-- ============================================
RegisterHook("/Script/Phoenix.SubtitleElement:InitAudioDialogueLineData",
    function(Context, AudioDialogueLineData)
        if ProcessInitDialogueData then
            ProcessInitDialogueData(Context, AudioDialogueLineData)
        end
    end
)

-- ============================================
-- Spell Tracking Hook
-- ============================================
RegisterHook("/Script/Phoenix.SpellTool:Start",
    function(Context, loc, muzzleloc)
        if OnSpellToolStart then
            OnSpellToolStart(Context)
        end
    end
)

-- ============================================
-- Death & Damage Tracking Hooks
-- ============================================

-- NPC death event
RegisterHook("/Script/Phoenix.NPC_Character:CharacterDiedEvent",
    function(Context)
        if OnNPCDied then
            OnNPCDied(Context)
        end
    end
)

-- Companion damaged (has damage amount and instigator)
RegisterHook("/Script/Phoenix.CompanionManager:OnCompanionDamaged",
    function(Context, InActor, InInstigator, InDamage, InHit)
        if OnCompanionDamaged then
            OnCompanionDamaged(Context, InActor, InInstigator, InDamage, InHit)
        end
    end
)

-- Register EnemyAIComponent:OnActorDamaged hook
RegisterHook("/Script/Phoenix.EnemyAIComponent:OnActorDamaged",
    function(Context, InActor, InInstigator, InDamage, InHit)
        if OnEnemyDamaged then
            OnEnemyDamaged(Context, InActor, InInstigator, InDamage, InHit)
        end
    end
)

-- ============================================
-- Mount State (polling in logic.lua unified loop)
-- ============================================
-- NOTE: Mount detection moved to polling in logic.lua unified loop (2s interval)
-- Uses IsPlayerOnMount() to detect all mounts (broom, hippogriff, graphorn, niffler)
-- Mount type identified only on state change to avoid per-poll overhead
_G.MountState = _G.MountState or { mounted = false, mountType = nil }

-- ============================================
-- Load logic.lua
-- ============================================
dofile(scriptsPath .. "logic.lua")


-- ============================================
-- Auto-start server on game boot
-- ============================================
if logicLoaded and StartServer then
    print("[Sonorus] Auto-starting server...")
    pcall(StartServer)
end

print("[Sonorus] ========================================")
print("[Sonorus] Mod loaded!")
print("[Sonorus] ========================================")
