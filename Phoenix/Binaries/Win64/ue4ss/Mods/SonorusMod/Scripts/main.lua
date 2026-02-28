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

function _G.DevPrint(...)
    if _G.SonorusDevMode then
        print(...)
    end
end

-- ============================================
-- Console Commands
-- ============================================
RegisterConsoleCommandHandler("sonorus_test", function(FullCommand, Parameters, Ar)
    print("[Sonorus] sonorus_test command fired")
    return true
end)

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
    pendingIdle = false,       -- Deferred idle transition (wait for mouth to close)
}

-- ============================================
-- Player Info Update (on load/reload)
-- ============================================
local Utils = require("Utils.Utils")
local Cache = require("Utils.Cache")

-- Updates player info (name/house) - must be called on game thread
local function UpdatePlayerInfo()
    -- Get player name
    local firstName, lastName, fullName = Utils.GetPlayerName()
    if fullName and fullName ~= "" then
        _G.SonorusState.playerName = fullName
        print("[Sonorus] Player name: " .. fullName)
    end
    -- Get player house
    local house = Utils.GetPlayerHouse()
    if house and house ~= "" then
        _G.SonorusState.playerHouse = house
    end
end

-- Loading screen detection - fires when entering/exiting game
-- (note: "Loadingcreen" is the actual class name, not a typo)
NotifyOnNewObject("/Script/Phoenix.Loadingcreen", function(Context)
    print("[Sonorus] Loading screen detected - player entering or leaving game world")
    _G.SonorusState.playerLoaded = true  -- Fallback: if ClientRestart didn't fire, fast travel proves player is loaded
    -- Clear all caches - objects will be invalid after load
    Cache.ClearObjects()
    Cache.ClearAllEntities()  -- NPCs will be invalid after load - force re-FindAllOf
    Cache.InvalidateStatic()
    -- Release all locked NPCs (including preview locks) - they become invalid after load
    if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
    -- Restore normal input mode (in case chat was open during load)
    if SetInputModeGameOnly then pcall(SetInputModeGameOnly) end
    -- Reset chat input state (prevents stale preview lock on new world)
    if _G.ChatInputState then
        _G.ChatInputState.active = false
        _G.ChatInputState.text = ""
    end
    _G.ChatPreviewLock = nil
    _G.STTPreviewLock = nil
    -- Invalidate time cache and permanent statics so next tick does a full refresh
    if _G._TimeCache then _G._TimeCache.initialized = false end
    if _G._PermanentStatics then _G._PermanentStatics.initialized = false end
    -- Mark active commitments as dirty for re-apply after load
    if _G.CommitmentManager then pcall(_G.CommitmentManager.MarkAllDirty) end
    print("[Sonorus] Caches cleared for loading")
end)

-- Hook on save load / character change
RegisterHook("/Script/Engine.PlayerController:ClientRestart", function(Context, NewPawn)
    print("[Sonorus] ClientRestart hook fired - player loaded into game")
    _G.SonorusState.playerLoaded = true  -- Player now in game world
    -- Clear caches and force refresh - new game world
    Cache.ClearObjects()
    Cache.ClearAllEntities()  -- NPCs changed - force fresh FindAllOf
    Cache.InvalidateStatic()
    -- Release all locked NPCs (including preview locks) - they become invalid after load
    if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
    -- Restore normal input mode (in case chat was open)
    if SetInputModeGameOnly then pcall(SetInputModeGameOnly) end
    -- Reset chat input state (prevents stale preview lock on new world)
    if _G.ChatInputState then
        _G.ChatInputState.active = false
        _G.ChatInputState.text = ""
    end
    _G.ChatPreviewLock = nil
    _G.STTPreviewLock = nil
    -- Fresh time cache after load (delay to ensure scheduler is ready)
    ExecuteInGameThreadWithDelay(500, function()
        if RefreshTimeCache then pcall(RefreshTimeCache, true) end
        if TimeDilation and TimeDilation.IsActive() then
            TimeDilation.UpdateRate(true) -- Force update after load
        end
    end)
    -- Delay slightly to ensure UIManager is ready (runs on game thread)
    ExecuteInGameThreadWithDelay(1000, UpdatePlayerInfo)
    -- Refresh house points after 2s (new save may have different standings)
    ExecuteInGameThreadWithDelay(2000, function()
        if RefreshHousePoints then RefreshHousePoints() end
    end)
    -- Commitment re-apply handled by timeUpdated event (CommitmentManager listens)
    -- Reapply companion follow distance after 5s (CompanionManager config resets on load)
    ExecuteInGameThreadWithDelay(5000, function()
        if CompanionFollow then pcall(CompanionFollow.applySettings) end
    end)
end)

-- Hook on fast travel completion - NPCs change after fast travel
RegisterHook("/Script/Phoenix.FastTravelManager:FinishWait", function(Context)
    print("[Sonorus] Fast travel finished - clearing caches")
    _G.SonorusState.playerLoaded = true  -- Fallback: if ClientRestart didn't fire, fast travel proves player is loaded
    Cache.ClearObjects()
    Cache.ClearAllEntities()  -- NPCs changed - force fresh FindAllOf
    Cache.InvalidateStatic()
    -- Release all locked NPCs (including preview locks) - NPCs change after fast travel
    if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
    -- Restore normal input mode (in case chat was open)
    if SetInputModeGameOnly then pcall(SetInputModeGameOnly) end
    -- Reset chat input state
    if _G.ChatInputState then
        _G.ChatInputState.active = false
        _G.ChatInputState.text = ""
    end
    _G.ChatPreviewLock = nil
    _G.STTPreviewLock = nil
    -- Fresh time cache + time dilation after fast travel (delay to ensure scheduler is ready)
    ExecuteInGameThreadWithDelay(500, function()
        if RefreshTimeCache then pcall(RefreshTimeCache, true) end
        if TimeDilation and TimeDilation.IsActive() then
            TimeDilation.UpdateRate(true) -- Force update after fast travel
        end
    end)
    -- Refresh house points after 2s (in case time passed during travel)
    ExecuteInGameThreadWithDelay(2000, function()
        if RefreshHousePoints then RefreshHousePoints() end
    end)
    -- Commitment re-apply handled by timeUpdated event (CommitmentManager listens)
    -- Reapply companion follow distance after 5s (CompanionManager config resets on fast travel)
    ExecuteInGameThreadWithDelay(5000, function()
        if CompanionFollow then pcall(CompanionFollow.applySettings) end
    end)
end)

-- ============================================
-- Blueprint Mod Actor Detection
-- ============================================
NotifyOnNewObject("/Game/Mods/sonorusblueprintmod/ModActor.ModActor_C", function(Context)
    _G.SonorusState.sonorusModActor = Context
    print("[Sonorus] Sonorus ModActor found: " .. Context:GetName())
end)

-- Delayed search for already-created actors (timing fallback)
-- Uses class path to distinguish between Sonorus and ConvAI actors
-- Retries every 2 seconds until Sonorus actor found (max 60 seconds)
local modActorSearchStart = os.time()
local modActorSearchAttempt = 0
local modActorSearchHandle  -- Declare first for closure capture
modActorSearchHandle = LoopInGameThreadWithDelay(2000, function()
    modActorSearchAttempt = modActorSearchAttempt + 1

    -- Give up after 60 seconds
    if os.time() - modActorSearchStart > 60 then
        print("[Sonorus] ModActor search timeout - giving up")
        CancelDelayedAction(modActorSearchHandle)
        return
    end

    -- Already found, stop searching
    if _G.SonorusState.sonorusModActor then
        CancelDelayedAction(modActorSearchHandle)
        return
    end

    -- Already on game thread with LoopInGameThreadWithDelay, no wrapper needed
    local modactors = FindAllOf("ModActor_C")
    if modactors then
        for _, actor in ipairs(modactors) do
            -- Use SafeIsValid - corrupted references can crash
            if Utils.SafeIsValid(actor) then
                -- Use class path to identify which mod the actor belongs to
                pcall(function()
                    local class = actor:GetClass()
                    if class then
                        local className = class:GetFullName()

                        if not _G.SonorusState.sonorusModActor and className:find("sonorusblueprintmod") then
                            _G.SonorusState.sonorusModActor = actor
                            print("[Sonorus] Sonorus ModActor detected (by class): " .. actor:GetName())
                        end
                    end
                end)
            end
        end
    end

    -- Check if found this iteration
    if _G.SonorusState.sonorusModActor then
        CancelDelayedAction(modActorSearchHandle)
    end
end)

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
        print("[Sonorus] Logic reloaded!")
    else
        print("[Sonorus] Reload failed: " .. tostring(err))
    end
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
-- Broom State (polling in logic.lua unified loop)
-- ============================================
-- NOTE: Broom detection moved to polling in logic.lua unified loop (2s interval)
-- This avoids ReceiveTick hooks which fire every frame
_G.BroomState = _G.BroomState or { mounted = false }

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

-- ============================================
-- Delayed Hook Registration (after ModActors load)
-- ============================================
local hookRegistrationAttempted = false

function TryRegisterHooksForModActors()
    if hookRegistrationAttempted then return true end

    local sonorusActor = nil
    pcall(function()
        if _G.SonorusState and _G.SonorusState.sonorusModActor then
            sonorusActor = _G.SonorusState.sonorusModActor
        end
    end)

    if not sonorusActor then
        return false  -- Not ready yet
    end

    hookRegistrationAttempted = true
    print("[Sonorus] ModActors detected, hook registration complete")
    return true
end

-- Check for ModActors periodically until found (quieter than before)
local hookRegistrationHandle  -- Declare first for closure capture
hookRegistrationHandle = LoopInGameThreadWithDelay(2000, function()
    if TryRegisterHooksForModActors() then
        CancelDelayedAction(hookRegistrationHandle)
    end
end)
