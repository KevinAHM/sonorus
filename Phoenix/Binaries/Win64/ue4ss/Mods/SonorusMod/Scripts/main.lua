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
-- Developer Mode (set to true to enable debug prints and F7/F11)
-- ============================================
_G.SonorusDevMode = false

function _G.DevPrint(...)
    if _G.SonorusDevMode then
        print(...)
    end
end

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
    print("[Sonorus] Loading screen detected - player leaving game world")
    -- _G.SonorusState.playerLoaded = false  -- Player leaving game world - may not be reliably firing before ClientRestart?
    -- Clear all caches - objects will be invalid after load
    Cache.ClearObjects()
    Cache.ClearAllEntities()  -- NPCs will be invalid after load - force re-FindAllOf
    Cache.InvalidateStatic()
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
    -- Delay slightly to ensure UIManager is ready (runs on game thread)
    ExecuteInGameThreadWithDelay(1000, UpdatePlayerInfo)
end)

-- Hook on fast travel completion - NPCs change after fast travel
RegisterHook("/Script/Phoenix.FastTravelManager:FinishWait", function(Context)
    print("[Sonorus] Fast travel finished - clearing caches")
    Cache.ClearObjects()
    Cache.ClearAllEntities()  -- NPCs changed - force fresh FindAllOf
    Cache.InvalidateStatic()
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
            -- Wrap IsValid in pcall - corrupted references can crash
            local isValid = false
            pcall(function() isValid = actor:IsValid() end)
            if isValid then
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
-- Dialogue Tracker Hooks
-- ============================================
local dialogueTrackerSetup = false

local function setupDialogueTracker()
    if dialogueTrackerSetup then return end
    dialogueTrackerSetup = true
    print("[Sonorus] Setting up dialogue tracker...")

    -- Hook InitAudioDialogueLineData
    pcall(function()
        RegisterHook("/Script/Phoenix.SubtitleElement:InitAudioDialogueLineData",
            function(Context, AudioDialogueLineData)
                if ProcessInitDialogueData then
                    ProcessInitDialogueData(Context, AudioDialogueLineData)
                end
            end
        )
        print("[Sonorus] Hooked: InitAudioDialogueLineData")
    end)

    -- NOTE: BPAddSubtitleEvent is DEPRECATED - does not fire in practice
    -- All dialogue (including player spells) is captured via InitAudioDialogueLineData
    -- See CLAUDE.md for details

    print("[Sonorus] Dialogue tracker ready")
end

-- ============================================
-- Auto-setup dialogue tracker after delay
-- (Dialogue blocker is set up on first conversation)
-- ============================================
ExecuteInGameThreadWithDelay(3000, function()
    pcall(setupDialogueTracker)
end)

-- ============================================
-- Spell Tracking Hook
-- ============================================
local spellTrackerSetup = false

local function setupSpellTracker()
    if spellTrackerSetup then return end
    spellTrackerSetup = true
    print("[Sonorus] Setting up spell tracker...")

    pcall(function()
        RegisterHook("/Script/Phoenix.SpellTool:Start",
            function(Context, loc, muzzleloc)
                -- Get the spell class name
                local spellClass = nil
                pcall(function()
                    spellClass = Context:get():GetClass():GetFullName()
                end)

                if spellClass and RecordSpellCast then
                    RecordSpellCast(spellClass)
                end
            end
        )
        print("[Sonorus] Hooked: SpellTool:Start")
    end)

    print("[Sonorus] Spell tracker ready")
end

-- Setup spell tracker after a delay (alongside dialogue tracker)
ExecuteInGameThreadWithDelay(3000, function()
    pcall(setupSpellTracker)
end)

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
