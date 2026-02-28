-- logic.lua - Reloadable logic (press F11 to reload)
print("[Sonorus] logic.lua starting...")

-- DevPrint is defined in main.lua as _G.DevPrint (survives hot reload)
-- _G.SonorusDevMode is also in main.lua - set to true to enable debug output

-- Clear module caches so they reload with logic.lua (F11)
-- Note: Cache.lua uses _G.CacheStore for data persistence, so clearing
-- the module only reloads code, not cached data
-- NOTE: package.loaded clearing alone is NOT sufficient - UE4SS caches file
-- contents at a lower level. Modules that need reliable hot reload use dofile().
package.loaded["Utils.Utils"] = nil
package.loaded["Utils.Cache"] = nil
package.loaded["Utils.Events"] = nil
package.loaded["Utils.FileIO"] = nil
package.loaded["Utils.BlueprintHelpers"] = nil
package.loaded["Utils.AudioMute"] = nil
package.loaded["Utils.NPCFacial"] = nil
package.loaded["Utils.AudioZone"] = nil
package.loaded["Utils.LipSync"] = nil
package.loaded["Utils.NPCLock"] = nil
package.loaded["Utils.PlayerGear"] = nil
package.loaded["Utils.Combat"] = nil
package.loaded["Utils.TimeDilation"] = nil

-- Lipsync enabled (was disabled for lag diagnosis)
_G.DisableLipsync = false
_G.Disable3DAudio = false  -- 3D audio positions enabled

-- Test player init
print("[Sonorus] Init..")

-- Force socket reconnect on reload (handles server restart)
if _G.SocketClient then
    _G.SocketClient.close()
    print("[Sonorus] Socket closed for reconnect")
end

-- Force fresh file read for socket_client (UE4SS caches file contents)
-- Clear all possible caches
package.loaded["socket_client"] = nil
package.preload["socket_client"] = nil

-- JSON library (rxi/json)
local json = require "json"

-- Unified caching utility (persists across F11 reloads)
local Cache = require "Utils.Cache"

-- Utils module
local Utils = require "Utils.Utils"

-- Event system
local Events = require "Utils.Events"
_G.Events = Events  -- Expose globally for other modules

-- File I/O helpers
local FileIO = require "Utils.FileIO"

-- Blueprint helpers
local BlueprintHelpers = require "Utils.BlueprintHelpers"

-- Audio muting helpers
local AudioMute = require "Utils.AudioMute"

-- NPC facial component helpers
local NPCFacial = require "Utils.NPCFacial"

-- Audio zone/reverb detection
local AudioZone = require "Utils.AudioZone"

-- Lip sync system
local LipSync = require "Utils.LipSync"

-- NPC attention lock system (dofile forces fresh file read on F11 - require uses stale cache)
local NPCLock = dofile(_G.SonorusScriptsPath .. "Utils/NPCLock.lua")
local CompanionFollow = dofile(_G.SonorusScriptsPath .. "Utils/CompanionFollow.lua")

-- Player gear system
local PlayerGear = require "Utils.PlayerGear"

-- Combat tracking system
local Combat = require "Utils.Combat"

-- UE Helpers
local UEHelpers = require("UEHelpers")

-- Time dilation system
local TimeDilation = require "Utils.TimeDilation"

-- Helper to expose all module functions as globals
local function expose(...)
    for _, module in ipairs({...}) do
        for name, func in pairs(module) do
            -- Only expose functions, skip private/internal names starting with _
            if type(func) == "function" and not name:match("^_") then
                _G[name] = func
            end
        end
    end
end

-- Auto-expose module functions as globals (import *)
expose(BlueprintHelpers, AudioZone, PlayerGear, NPCLock, NPCFacial, FileIO, AudioMute, LipSync, TimeDilation)

-- Safe IsValid check (from BlueprintHelpers)
local SafeIsValid = BlueprintHelpers.SafeIsValid

-- Clear event listeners on reload (states persist, handlers re-register below)
Events.clear()

-- Socket client for Python server communication (lipsync, visemes)
-- NOTE: socket_client.lua sets _G.SocketClient, use that directly (no local shadow)
-- Use dofile instead of require to force fresh file read on hot reload
dofile(_G.SonorusScriptsPath .. "socket_client.lua")

-- On reload: immediately try to reconnect (don't wait for unified loop tick)
-- This ensures chat works right away after F11
ExecuteInGameThread(function()
    _G.SocketClient.connect()
end)
print("[Sonorus] Socket reconnect triggered")

-- Commitment manager for NPC schedule overrides (dofile for hot reload)
_G.CommitmentManager = dofile(_G.SonorusScriptsPath .. "CommitmentManager.lua")

-- ============================================
-- Access global state from main.lua
-- ============================================
-- NOTE: Use _G.SonorusState directly everywhere (no local State shadows)
-- to avoid closure capture issues with UE4SS Lua registry

-- Server state (persisted in global)
_G.SonorusServerState = _G.SonorusServerState or {
    started = false,
    pid = nil,
    startupInProgress = false,  -- Guard to prevent duplicate spawns
    startupTime = 0,            -- When startup began (for timeout)
}

-- House Points cache (persisted across F11 reloads)
-- Only refreshed at specific trigger points, not every context update
_G.CachedHousePoints = _G.CachedHousePoints or {
    data = nil,        -- { Gryffindor = {season, month, week, day}, ... }
    lastRefresh = 0,   -- os.clock() timestamp
}

-- Combat tracking state is now initialized in Utils/Combat.lua

-- ============================================
-- File paths
-- ============================================
local FILES = {
    dialogueHistory = "sonorus\\data\\dialogue_history.json",
    subtitles = "sonorus\\data\\subtitles.json",
    locations = "sonorus\\data\\locations.json",
    localization = "sonorus\\data\\main_localization.json",
    voiceManifest = "sonorus\\data\\voice_manifest.json",
    spellMappings = "sonorus\\data\\spell_mappings.json",
}

-- Get language-specific file path for localization files
-- EN_US uses base filename, others use suffix (e.g., subtitles_de_de.json)
local function GetLocalizedPath(baseName, extension)
    local lang = _G.SonorusLanguage or "EN_US"
    if lang == "EN_US" then
        return "sonorus\\data\\" .. baseName .. extension
    else
        -- Convert EN_US -> en_us for filename suffix
        local suffix = "_" .. lang:lower()
        return "sonorus\\data\\" .. baseName .. suffix .. extension
    end
end

-- ============================================
-- Static Cache Functions (used throughout file)
-- ============================================
-- These must be defined early as they're used by many functions below

-- Permanent statics: class/default objects that never change. Fetched once.
_G._PermanentStatics = _G._PermanentStatics or {}

local function EnsurePermanentStatics(data)
    local ps = _G._PermanentStatics
    if not ps.initialized then
        ps.bpLibrary = StaticFindObject("/Script/Phoenix.Default__PhoenixBPLibrary")
        ps.gearScreen = StaticFindObject("/Script/Phoenix.Default__GearScreen")
        ps.npcComponentClass = StaticFindObject("/Script/Phoenix.NPC_Component")
        ps.audioStatics = StaticFindObject("/Script/Phoenix.Default__AvaAudioGameplayStatics")
        ps.akComponentClass = StaticFindObject("/Script/AkAudio.AkComponent")
        ps.facialComponentClass = StaticFindObject("/Script/AvaAnimation.FacialComponent")
        ps.kismetSystem = UEHelpers.GetKismetSystemLibrary()
        ps.kismetMath = UEHelpers.GetKismetMathLibrary()
        ps.initialized = true
    end
    data.bpLibrary = ps.bpLibrary
    data.gearScreen = ps.gearScreen
    data.npcComponentClass = ps.npcComponentClass
    data.audioStatics = ps.audioStatics
    data.akComponentClass = ps.akComponentClass
    data.facialComponentClass = ps.facialComponentClass
    data.kismetSystem = ps.kismetSystem
    data.kismetMath = ps.kismetMath
end

-- Dynamic refresh: only objects that change on load/fast travel
local function RefreshStaticData(data)
    -- Permanent statics (fetched once, then just copied)
    EnsurePermanentStatics(data)

    -- Dynamic singletons (change on load/fast travel)
    data.playerController = FindFirstOf("PlayerController")
    if data.playerController then
        local valid = false
        pcall(function() valid = data.playerController:IsValid() end)
        if valid then
            pcall(function() data.cameraManager = data.playerController.PlayerCameraManager end)
        end
    end

    data.player = FindFirstOf("Biped_Player")
    if data.player then
        local valid = false
        pcall(function() valid = data.player:IsValid() end)
        if valid then
            pcall(function() data.playerFullName = data.player:GetFullName() end)
        end
    end

    data.companionManager = FindFirstOf("CompanionManager")
    data.gearManager = FindFirstOf("GearManager")
    data.populationManager = FindFirstOf("PopulationManager")

    -- Mark primary object for validity checks
    data._primary = data.playerController
end

-- Get cached NPCs (initializes on first call, cleans periodically)
local function GetCachedNPCs()
    -- Initialize if needed (one-time FindAllOf)
    if not Cache.IsEntityCacheReady("NPC") then
        Cache.InitEntities("NPC", "NPC_Character")
        -- Safety: clear stale mute state when NPC cache is rebuilt (AkComponents may be invalid)
        if _G.PlaybackState and _G.PlaybackState.serverState == "idle" then
            UnmuteAllSpeakers()
        end
    end

    -- Cleanup invalid entries periodically (every 5s)
    Cache.CleanEntities("NPC", 5)

    local npcs = Cache.GetEntities("NPC")

    -- Fallback: If cache is empty but was initialized, try re-initializing once
    -- This handles cases where all NPCs became invalid (area transition, etc.)
    if #npcs == 0 and Cache.IsEntityCacheReady("NPC") then
        print("[GetCachedNPCs] Cache empty after cleanup - reinitializing")
        Cache.ResetEntityCache("NPC")  -- Clear initialized flag
        Cache.InitEntities("NPC", "NPC_Character")  -- Re-scan
        -- Safety: clear stale mute state when NPC cache is rebuilt
        if _G.PlaybackState and _G.PlaybackState.serverState == "idle" then
            UnmuteAllSpeakers()
        end
        npcs = Cache.GetEntities("NPC")
    end

    return npcs
end

-- Get static cache (refreshes every 30s or when invalid)
local function GetStaticCache()
    return Cache.GetStatic(RefreshStaticData, 30)
end

-- Initialize NPCLock module with static cache getter
NPCLock.init(GetStaticCache)

-- Initialize CompanionFollow module (exposed as global for main.lua hooks)
CompanionFollow.init(GetStaticCache)
CompanionFollow.start()
_G.CompanionFollow = CompanionFollow

-- Initialize LipSync module
LipSync.init()

-- Export GetStaticCache globally (needed by LipSync and other modules)
_G.GetStaticCache = GetStaticCache

--- Check if companion is on broom via CharacterMovementComponent
--- @param companionPawn UObject The companion pawn actor
--- @param staticData table Cached static data (unused, kept for API compatibility)
--- @return boolean True if companion is on broom
local function IsCompanionOnBroom(companionPawn, staticData)
    if not companionPawn then return false end

    local isFlying = false
    pcall(function()
        local movementComp = companionPawn.CharacterMovement
        if movementComp and movementComp:IsValid() then
            -- MovementMode: 0=Flying/Broom, 1=Walking
            local mode = movementComp.MovementMode
            if mode == 0 then
                isFlying = true
            end
        end
    end)

    return isFlying
end

-- ============================================
-- NPC Facial Component Access
-- ============================================
-- Direct property access returns nullptr - must use GetComponentByClass
--
-- AudioDialogueLineData struct properties (from InitAudioDialogueLineData hook):
--   lineID (StrProperty) - dialogue line ID like "DuncanHobhouse_10383"
--   LocDirectKey (StrProperty) - localization key
--   DurationSeconds (FloatProperty) - audio duration
--   DialogueHandle (IntProperty) - handle/ID
--   SpeakingActor (WeakObjectProperty) - the NPC actor speaking
--   VoiceName (StrProperty) - voice name like "DuncanHobhouse"
--   bIsFromConversation (BoolProperty) - true if from native conversation
--   bIsEmote (BoolProperty) - true if emote
--   bForceSubtitle (BoolProperty) - force subtitle display
--   bSuppressSubtitle (BoolProperty) - suppress subtitle (can set but too late in hook)
--   bSuppressSubtitleCharacterName (BoolProperty) - hide character name
--   EmotionHint (EnumProperty) - emotion enum
--   SocialSemanticEmotionHint (ByteProperty)
--   AudioPriority (ByteProperty)
--   bNonSpatialized (BoolProperty) - non-3D audio
--
-- Station class properties (/Script/Phoenix.Station):
--   StationComponent (ObjectProperty) - controls NPC behavior at station
--   MissionID (StructProperty) - which mission this station belongs to
--   MissionUID (IntProperty) - unique mission ID

-- ============================================
-- NPC Animation System (Blueprint-based)
-- ============================================
-- Animation via Lua crashes the game - use Blueprint instead
-- Call PlayNPCEmote(actor, emoteName) which delegates to Blueprint

-- Play emote on NPC via Blueprint ModActor
-- emoteName: "laugh", "shrug", "think", "greet", "wave", "nod"
function PlayNPCEmote(actor, emoteName)
    local mod = GetSonorusModActor()
    if not mod then
        print("[Anim] ModActor not found - can't play emote")
        return false
    end
    if not actor then
        print("[Anim] No actor provided")
        return false
    end

    -- Call Blueprint function: playemote(actor, emoteName)
    local ok, err = pcall(function()
        mod:playemote(actor, emoteName)
    end)

    if not ok then
        print("[Anim] Blueprint playemote error: " .. tostring(err))
        return false
    end

    print("[Anim] Triggered emote '" .. emoteName .. "' via Blueprint")
    return true
end

-- ============================================
-- Player Voice ID Detection
-- ============================================
-- Returns "PlayerMale" or "PlayerFemale" based on character gender
-- Must be called from game thread (or inside ExecuteInGameThread)
function GetPlayerVoiceId()
    local voiceId = "PlayerMale"  -- Default fallback

    pcall(function()
        local staticData = Cache.GetStaticData()
        local audioStatics = staticData and staticData.audioStatics
        if audioStatics then
            local genderVoice = audioStatics:GetPlayerGenderVoice()
            -- 0 = Male, 1 = Female
            if genderVoice == 1 then
                voiceId = "PlayerFemale"
            end
        end
    end)

    return voiceId
end

-- PropTypeIDs enum -> NPC-friendly description
-- From /Script/Phoenix.PropTypeIDs in UE4SS object dump
local PROP_TYPE_LABELS = {
    [0]  = "bed",
    [1]  = "bench",
    [2]  = "bench",
    [3]  = "bespoke",
    [4]  = "bookshelf",
    [5]  = "browsing shelf",
    [6]  = "candy display",
    [7]  = "chair",
    [8]  = "chair",
    [9]  = "chest",
    [10] = "cleaning shelves",
    [11] = "couch",
    [12] = "couch",
    [13] = "couch",
    [14] = "couch",
    [15] = "desk",
    [16] = "desk",
    [17] = "dresser",
    [18] = "drinking tea",
    [19] = "fireside",
    [20] = "fireside bench",
    [21] = "fluid",
    [22] = "globe",
    [23] = "great hall table",
    [24] = "great hall table",
    [25] = "great hall table",
    [26] = "sitting on ground",
    [27] = "guard post",
    [28] = "herbology station",
    [29] = "investigating",
    [30] = "job station",
    [31] = "lounge chair",
    [32] = "mail interaction",
    [33] = "mission interaction",
    [34] = nil, -- NONE (area/zone marker, not a real station)
    [35] = "occupation",
    [36] = "office desk",
    [37] = "patrol",
    [38] = "potion station",
    [39] = "railing lean",
    [40] = "railing lean",
    [41] = "shop register",
    [42] = "service counter",
    [43] = "stairs",
    [44] = "sitting on stairs",
    [45] = "sitting on stairs",
    [46] = "standing",
    [47] = "standing",
    [48] = "standing",
    [49] = "standing",
    [50] = "standing",
    [51] = "standing",
    [52] = "standing in queue",
    [53] = "tall stool",
    [54] = "study desk",
    [55] = "table",
    [56] = "table",
    [57] = "table",
    [58] = "table",
    [59] = "table",
    [60] = "taking notes",
    [61] = "teacher's chair",
    [62] = "drinking tea",
    [63] = "telescope",
    [64] = "vendor stall",
    [65] = "wall lean",
    [66] = "wall sit",
    [67] = "wardrobe",
    [68] = "window shopping",
}

-- Clean up stale gaze state from previous session (survives F11 reload)
if _G.DebugGazeLoop then
    pcall(function() CancelDelayedAction(_G.DebugGazeLoop) end)
    _G.DebugGazeLoop = nil
end
_G.DebugGazeTarget = nil
_G.DebugFaceTarget = nil
-- Test counter: cycles through tests one at a time (persists across F11)
_G.DebugGazeTestIdx = _G.DebugGazeTestIdx or 0

-- Clean up stale blendshape debug state from previous session
if _G.DebugBlendshapeLoop then
    pcall(function() CancelDelayedAction(_G.DebugBlendshapeLoop) end)
    _G.DebugBlendshapeLoop = nil
end
if _G._OrigGetCurrentSpeakerActor then
    _G.GetCurrentSpeakerActor = _G._OrigGetCurrentSpeakerActor
    _G._OrigGetCurrentSpeakerActor = nil
end
_G.DebugBlendshapeTestIdx = _G.DebugBlendshapeTestIdx or 0
_G.DebugBlendshapeActor = nil

-- ============================================
-- F7 Debug Function - Blendshape/Morph Target Tuning
-- Press F7 to cycle through each morph target at max and mapped amplitude
-- Look at an NPC before pressing F7 to select them as the target
-- ============================================

-- Morph target names for the 6 new direct targets
local MORPH_NAMES = {"mouth_press", "upr_lip_up_l", "upr_lip_up_r", "ee", "o", "shh"}

-- Helper: reset all morph targets on an actor (both Blueprint and direct)
local function ResetAllMorphTargets(actor)
    BlueprintHelpers.CallSetBlendshapes(actor, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    pcall(function()
        local mesh = actor.Mesh
        if mesh then
            for _, name in ipairs(MORPH_NAMES) do
                mesh:SetMorphTarget(FName(name), 0, false)
            end
        end
    end)
end

-- Helper: apply a blendshape test configuration
-- config = { jaw=0, smile=0, funnel=0, press=0, lip_up=0, ee=0, o=0, shh=0 }
local function ApplyBlendshapeConfig(actor, config)
    local jaw = config.jaw or 0
    local smile = config.smile or 0
    local funnel = config.funnel or 0

    -- Apply Blueprint-driven shapes
    BlueprintHelpers.CallSetBlendshapes(
        actor,
        jaw,                -- jaw_drop
        smile,              -- smile_l
        smile,              -- smile_r
        funnel,             -- lwr_lip_funl_l
        funnel,             -- lwr_lip_funl_r
        funnel * 0.7,       -- upr_lip_funl_l
        funnel * 0.7,       -- upr_lip_funl_r
        jaw * 0.3,          -- lwr_lip_dn_l
        jaw * 0.3           -- lwr_lip_dn_r
    )

    -- Apply direct morph targets
    pcall(function()
        local mesh = actor.Mesh
        if mesh then
            mesh:SetMorphTarget(FName("mouth_press"), config.press or 0, false)
            mesh:SetMorphTarget(FName("upr_lip_up_l"), config.lip_up or 0, false)
            mesh:SetMorphTarget(FName("upr_lip_up_r"), config.lip_up or 0, false)
            mesh:SetMorphTarget(FName("ee"), config.ee or 0, false)
            mesh:SetMorphTarget(FName("o"), config.o or 0, false)
            mesh:SetMorphTarget(FName("shh"), config.shh or 0, false)
        end
    end)
end

-- ============================================================
-- Schedule Override Probe (F7 toggle) - Hardcoded to GladwinMoon
-- All references fetched fresh each time (safe across fast travel)
-- First press: override schedule to Three Broomsticks
-- Second press: undo override, restore normal schedule
-- ============================================================
_G._ScheduleOverrideState = _G._ScheduleOverrideState or nil

function DebugF7_ScheduleOverride()
    ExecuteInGameThread(function()
        local TAG = "[SchedProbe]"
        local ENTITY_NAME = "GladwinMoon"

        -- Helper: get fresh references every time (safe after fast travel)
        local function GetFreshRefs()
            local refs = {}
            local staticData = GetStaticCache()
            if not staticData then print(TAG .. " No static cache") return nil end
            refs.staticData = staticData

            refs.popManager = staticData.populationManager
            if not refs.popManager or not SafeIsValid(refs.popManager) then
                print(TAG .. " No PopulationManager")
                return nil
            end

            -- Get ScheduledEntity by name (works even when not in flesh)
            pcall(function() refs.se = refs.popManager:GetScheduledEntityFromName(ENTITY_NAME) end)
            if not refs.se then
                print(TAG .. " Could not get ScheduledEntity for " .. ENTITY_NAME)
                return nil
            end
            local seValid = false
            pcall(function() seValid = refs.se:IsValid() end)
            if not seValid then
                print(TAG .. " ScheduledEntity not valid")
                return nil
            end

            -- Provider: mod actor or player controller
            pcall(function() refs.provider = _G.SonorusState and _G.SonorusState.sonorusModActor end)
            if not refs.provider or not SafeIsValid(refs.provider) then
                refs.provider = staticData.playerController
            end

            -- WorldEventActor (fresh find)
            pcall(function() refs.weActor = FindFirstOf("WorldEventActor") end)

            return refs
        end

        -- Helper: print current schedule
        local function PrintSchedule(refs, label)
            local info = Utils.GetNPCScheduleInfo(ENTITY_NAME, refs.staticData)
            if info then
                print(string.format("%s %s: location=%s, activity=%s, type=%s, inFlesh=%s, inTransit=%s",
                    TAG, label, tostring(info.locationName), tostring(info.activity),
                    tostring(info.activityType), tostring(info.inFlesh), tostring(info.isInTransit)))
            else
                print(string.format("%s %s: GetNPCScheduleInfo returned nil", TAG, label))
            end

            -- Also read raw activity data
            pcall(function()
                local out, out2 = {}, {}
                refs.se:GetCurrentActivity(out, out2)
                local activity = out.Activity
                pcall(function() activity = out.Activity:ToString() end)
                local locKey = out.LocationKey
                pcall(function() locKey = out.LocationKey:ToString() end)
                print(string.format("%s %s (raw): activity=%s, locKey=%s, start=%s, end=%s",
                    TAG, label, tostring(activity), tostring(locKey),
                    tostring(out.StartTime), tostring(out.EndTime)))
            end)

            -- Check flesh/transit state
            pcall(function()
                print(string.format("%s %s: inFlesh=%s, isInTransit=%s, isEnabled=%s",
                    TAG, label, tostring(refs.se:CurrentlyInFlesh()),
                    tostring(refs.se:IsInTransit()), tostring(refs.se:IsEnabled())))
            end)
        end

        -- ============================================================
        -- RELEASE PATH
        -- ============================================================
        if _G._ScheduleOverrideState then
            print(string.format("%s === RELEASING %s ===", TAG, ENTITY_NAME))
            local refs = GetFreshRefs()
            if not refs then
                print(TAG .. " Can't get fresh refs - clearing state anyway")
                _G._ScheduleOverrideState = nil
                return
            end

            PrintSchedule(refs, "PRE-RELEASE")

            -- RemoveDynamicActivityFromSE (fresh WorldEventActor)
            if refs.weActor and SafeIsValid(refs.weActor) and _G._ScheduleOverrideState.injectedActivity then
                local ok, err = pcall(function()
                    local result = refs.weActor:RemoveDynamicActivityFromSE(refs.se, _G._ScheduleOverrideState.injectedActivity)
                    print(string.format("%s RemoveDynamicActivityFromSE: %s", TAG, tostring(result)))
                end)
                if not ok then print(string.format("%s RemoveDynamic FAILED: %s", TAG, tostring(err))) end
            else
                print(TAG .. " No WorldEventActor for RemoveDynamic (or no activity to remove)")
            end

            -- FinishSchedulingOverride (fresh provider)
            local ok2, err2 = pcall(function()
                local result = refs.se:FinishSchedulingOverride(4, refs.provider, true, false, true)
                print(string.format("%s FinishSchedulingOverride(priority=4): %s", TAG, tostring(result)))
            end)
            if not ok2 then print(string.format("%s FinishOverride FAILED: %s", TAG, tostring(err2))) end

            -- Re-enable scheduling
            pcall(function() refs.se:EnableScheduling(true, false, true) end)
            print(TAG .. " EnableScheduling(true) called")

            -- Print after release (with delay for scheduler to process)
            ExecuteInGameThreadWithDelay(1000, function()
                local refs2 = GetFreshRefs()
                if refs2 then PrintSchedule(refs2, "POST-RELEASE (1s)") end
            end)

            _G._ScheduleOverrideState = nil
            print(string.format("%s === RELEASE COMPLETE ===", TAG))
            return
        end

        -- ============================================================
        -- OVERRIDE PATH
        -- ============================================================
        print(string.format("%s === SCHEDULE OVERRIDE: %s -> Three Broomsticks ===", TAG, ENTITY_NAME))
        local refs = GetFreshRefs()
        if not refs then return end

        PrintSchedule(refs, "BEFORE")

        local state = { injectedActivity = nil }

        -- NOTE: FinishSchedulingOverride always returns true regardless of existing overrides.
        -- It's an action, not a query. Cannot be used to detect existing overrides.

        -- Step 1: StartSchedulingOverride
        local overrideResult = false
        local ok1, err1 = pcall(function()
            overrideResult = refs.se:StartSchedulingOverride(true, 4, refs.provider, true, true, true)
            print(string.format("%s StartSchedulingOverride(priority=4): %s", TAG, tostring(overrideResult)))
        end)
        if not ok1 then print(string.format("%s StartSchedulingOverride FAILED: %s", TAG, tostring(err1))) end

        -- Print current game time for time window testing
        pcall(function()
            local timeData = GetTimeOfDay and GetTimeOfDay()
            if timeData then
                print(string.format("%s Current game time: %02d:%02d (minute of day: %d)",
                    TAG, timeData.hour or 0, timeData.minute or 0, timeData.minuteOfDay or 0))
            end
        end)

        -- Step 2: InsertDynamicActivityOnSE
        -- Testing TWO activities: one all-day (0-2400) and one restricted (600-2130)
        if refs.weActor and SafeIsValid(refs.weActor) then
            -- Test A: Restricted window activity (600-2130)
            local okA, errA = pcall(function()
                local result = refs.weActor:InsertDynamicActivityOnSE(refs.se, "ThreeBroomsticksHours", "HM_ThreeBroomsticks")
                print(string.format("%s InsertDynamic('ThreeBroomsticksHours' [600-2130]): %s", TAG, tostring(result)))
                if result then state.injectedActivity = "ThreeBroomsticksHours" end
            end)
            if not okA then print(string.format("%s InsertDynamic(restricted) FAILED: %s", TAG, tostring(errA))) end

            -- Test B: All-day activity (0-2400) - only if restricted one failed
            if not state.injectedActivity then
                local okB, errB = pcall(function()
                    local result = refs.weActor:InsertDynamicActivityOnSE(refs.se, "HM_ThreeBroomsticksHours", "HM_ThreeBroomsticks")
                    print(string.format("%s InsertDynamic('HM_ThreeBroomsticksHours' [0-2400]) FALLBACK: %s", TAG, tostring(result)))
                    if result then state.injectedActivity = "HM_ThreeBroomsticksHours" end
                end)
                if not okB then print(string.format("%s InsertDynamic(allday) FAILED: %s", TAG, tostring(errB))) end
            end
        else
            print(TAG .. " No WorldEventActor available")
        end

        -- Print after override (with delay)
        ExecuteInGameThreadWithDelay(1000, function()
            local refs2 = GetFreshRefs()
            if refs2 then PrintSchedule(refs2, "AFTER (1s)") end
        end)

        _G._ScheduleOverrideState = state
        print(string.format("%s === OVERRIDE APPLIED (press F7 again to release) ===", TAG))
    end)
end

-- ============================================================
-- F7 Debug: Floo Companion Dismiss Test
-- Finds BP_Student_Mod_C, sets DismissedCompanion, fires CompanionChanged
-- ============================================================
-- Persistent state for TurnInPlace test toggle
_G._TurnInPlaceState = _G._TurnInPlaceState or nil

function DebugF7()
    ExecuteInGameThread(function()
        local TAG = "[NpcDebug]"
        print(TAG .. " === NPC STATUS CHECK ===")

        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData or not staticData.populationManager then
            print(TAG .. " No PopulationManager")
            return
        end
        local popManager = staticData.populationManager

        -- Check active commitments
        local commitments = _G.ActiveCommitments or {}
        local npcList = {}
        for npcId, entry in pairs(commitments) do
            table.insert(npcList, {id = npcId, commitment = entry})
        end

        -- If no active commitments, check a hardcoded list for debugging
        if #npcList == 0 then
            print(TAG .. " No active commitments. Checking common NPCs...")
            for _, name in ipairs({"DuncanEverette", "SebastianSallow", "NatsaiOnai", "PoppySweeting", "GladwinMoon"}) do
                table.insert(npcList, {id = name, commitment = nil})
            end
        end

        for _, entry in ipairs(npcList) do
            local npcId = entry.id
            print(string.format("%s --- %s ---", TAG, npcId))

            -- Show commitment state if any
            if entry.commitment then
                local c = entry.commitment
                print(string.format("%s   Commitment: %s -> %s (applied=%s dirty=%s)",
                    TAG, c.activity_id or "?", c.location_id or "?",
                    tostring(c.applied), tostring(c.dirty)))
            end

            -- Get ScheduledEntity
            local se = nil
            pcall(function() se = popManager:GetScheduledEntityFromName(npcId) end)
            if not se then
                print(string.format("%s   ScheduledEntity: NOT FOUND", TAG))
            else
                local seValid = false
                pcall(function() seValid = se:IsValid() end)
                if not seValid then
                    print(string.format("%s   ScheduledEntity: INVALID", TAG))
                else
                    -- In flesh?
                    local inFlesh = false
                    pcall(function() inFlesh = se:CurrentlyInFlesh() end)
                    print(string.format("%s   InFlesh: %s", TAG, tostring(inFlesh)))

                    -- Location
                    pcall(function()
                        local loc = se:GetLocation()
                        if loc then
                            print(string.format("%s   Location: %.0f, %.0f, %.0f", TAG, loc.X or 0, loc.Y or 0, loc.Z or 0))
                        end
                    end)

                    -- Is enabled / in transit
                    pcall(function()
                        local enabled = se:IsEnabled()
                        local transit = se:IsInTransit()
                        print(string.format("%s   Enabled: %s  InTransit: %s", TAG, tostring(enabled), tostring(transit)))
                    end)

                    -- Current activity
                    local out1, out2 = {}, {}
                    pcall(function() se:GetCurrentActivity(out1, out2) end)
                    if out1.ActivityIsValid then
                        local actId = "?"
                        pcall(function() actId = out1.Activity:ToString() end)
                        local actType = "?"
                        pcall(function() actType = out1.ActivityType:ToString() end)
                        local locKey = "?"
                        pcall(function() locKey = out1.LocationKey:ToString() end)
                        print(string.format("%s   Activity: %s (%s) at %s  [%s-%s]",
                            TAG, actId, actType, locKey,
                            tostring(out1.StartTime), tostring(out1.EndTime)))
                    else
                        print(string.format("%s   Activity: none/invalid", TAG))
                    end

                    -- Upcoming activity
                    local up1, up2 = {}, {}
                    pcall(function() se:GetUpcomingActivity(up1, up2) end)
                    if up1.ActivityIsValid then
                        local actId = "?"
                        pcall(function() actId = up1.Activity:ToString() end)
                        local locKey = "?"
                        pcall(function() locKey = up1.LocationKey:ToString() end)
                        print(string.format("%s   Upcoming: %s at %s [%s-%s]",
                            TAG, actId, locKey,
                            tostring(up1.StartTime), tostring(up1.EndTime)))
                    end

                    -- Active station
                    pcall(function()
                        local stationComp = se:GetActiveStation()
                        if stationComp then
                            local owner = nil
                            pcall(function() owner = stationComp:GetOwner() end)
                            if owner then
                                local ownerName = "?"
                                pcall(function() ownerName = owner:GetFullName() end)
                                print(string.format("%s   Station: %s", TAG, ownerName))
                            end
                        else
                            print(string.format("%s   Station: none", TAG))
                        end
                    end)

                    -- Player distance
                    pcall(function()
                        local player = staticData.player
                        if player and inFlesh then
                            local flesh = se:GetFlesh()
                            if flesh then
                                local npcLoc = flesh:K2_GetActorLocation()
                                local playerLoc = player:K2_GetActorLocation()
                                local dx = npcLoc.X - playerLoc.X
                                local dy = npcLoc.Y - playerLoc.Y
                                local dz = npcLoc.Z - playerLoc.Z
                                local dist = math.sqrt(dx*dx + dy*dy + dz*dz) / 100
                                print(string.format("%s   Distance: %.0fm", TAG, dist))
                            end
                        end
                    end)
                end
            end
        end

        -- Force-flesh + hobo clear for committed NPCs not in flesh
        for _, entry in ipairs(npcList) do
            if entry.commitment and entry.commitment.applied then
                local se = nil
                pcall(function() se = popManager:GetScheduledEntityFromName(entry.id) end)
                if se then
                    local inFlesh = false
                    pcall(function() inFlesh = se:CurrentlyInFlesh() end)
                    if not inFlesh then
                        print(string.format("%s   Force-fleshing %s...", TAG, entry.id))
                        pcall(function()
                            se:StartPrecachingFlesh(5, nil, 50000.0, true, 0, 0)
                        end)
                        pcall(function()
                            local player = staticData.player
                            local playerLoc = player:K2_GetActorLocation()
                            local transform = {
                                Translation = { X = playerLoc.X + 200, Y = playerLoc.Y + 200, Z = playerLoc.Z },
                                Rotation = { X = 0, Y = 0, Z = 0, W = 1 },
                                Scale3D = { X = 1, Y = 1, Z = 1 }
                            }
                            popManager:PlaceScheduledEntityBP(entry.id, transform)
                            print(string.format("%s   PlaceScheduledEntityBP called for %s", TAG, entry.id))
                        end)
                    end
                end
                -- Clear hobos
                if _G.CommitmentManager then
                    pcall(_G.CommitmentManager.ClearNearbyHobos, entry.id)
                end
            end
        end

        print(TAG .. " === CHECK COMPLETE ===")
    end)
end

function DebugF7_TurnInPlace()
    ExecuteInGameThread(function()
        local TAG = "[TurnInPlace]"

        -- ============================================================
        -- RELEASE PATH (second press)
        -- ============================================================
        if _G._TurnInPlaceState then
            print(TAG .. " === RELEASING ===")
            local st = _G._TurnInPlaceState
            local npcId = st.npcName

            local staticData = GetStaticCache()
            if not staticData then
                print(TAG .. " No static cache - clearing state")
                _G._TurnInPlaceState = nil
                return
            end

            local popManager = staticData.populationManager
            if popManager and SafeIsValid(popManager) then
                local se = nil
                pcall(function() se = popManager:GetScheduledEntityFromName(npcId) end)
                if se then
                    -- Clear AI focus
                    local flesh = nil
                    pcall(function() flesh = se:GetFlesh() end)
                    if flesh and SafeIsValid(flesh) then
                        local ctrl = nil
                        pcall(function() ctrl = flesh.Controller end)
                        if ctrl and SafeIsValid(ctrl) then
                            pcall(function() ctrl:K2_ClearFocus() end)
                            print(TAG .. " K2_ClearFocus OK")
                        end
                    end

                    -- Re-enable scheduling (was disabled to freeze NPC)
                    pcall(function() se:EnableScheduling(true, false, true) end)
                    print(TAG .. " Scheduling re-enabled")
                end
            end

            _G._TurnInPlaceState = nil
            print(TAG .. " === RELEASED (press F7 again to test) ===")
            return
        end

        -- ============================================================
        -- APPLY PATH (first press)
        -- ============================================================
        print(TAG .. " === NPC SetTargetLocationTurnInPlace TEST ===")

        local staticData = GetStaticCache()
        if not staticData then print(TAG .. " No static cache") return end

        local player = staticData.player
        if not player or not SafeIsValid(player) then print(TAG .. " No player") return end

        local playerLoc = player:K2_GetActorLocation()
        print(string.format("%s Player at (%.0f, %.0f, %.0f)", TAG, playerLoc.X, playerLoc.Y, playerLoc.Z))

        -- Find nearest NPC
        local nearestNpc = nil
        local nearestDist = math.huge
        local nearestName = nil
        local playerFullName = staticData.playerFullName or ""

        local npcResult = nil
        pcall(function() npcResult = GetNearbyNPCs(2000, 0.9) end)

        if not npcResult or not npcResult.nearbyList or #npcResult.nearbyList == 0 then
            print(TAG .. " No nearby NPCs found")
            return
        end

        for _, entry in ipairs(npcResult.nearbyList) do
            if entry.actor and SafeIsValid(entry.actor) then
                local entryFullName = entry.actor:GetFullName()
                if entryFullName ~= playerFullName then
                    local npcLoc = entry.actor:K2_GetActorLocation()
                    local dx = npcLoc.X - playerLoc.X
                    local dy = npcLoc.Y - playerLoc.Y
                    local dist = math.sqrt(dx * dx + dy * dy)
                    if dist < nearestDist then
                        nearestDist = dist
                        nearestNpc = entry.actor
                        nearestName = entry.name or "?"
                    end
                end
            end
        end

        if not nearestNpc then
            print(TAG .. " No valid NPC found")
            return
        end

        local npcLoc = nearestNpc:K2_GetActorLocation()
        local npcRot = nearestNpc:K2_GetActorRotation()
        print(string.format("%s Nearest NPC: %s (dist=%.0f, yaw=%.1f)", TAG, nearestName, nearestDist, npcRot.Yaw or 0))

        -- Get ScheduledEntity + PopulationManager
        local popManager = staticData.populationManager
        if not popManager or not SafeIsValid(popManager) then
            print(TAG .. " No PopulationManager")
            return
        end

        local se = nil
        pcall(function() se = popManager:GetScheduledEntityFromActor(nearestNpc, false) end)
        if not se then
            print(TAG .. " No ScheduledEntity for this NPC")
            return
        end

        -- NPCLock-style: brief enable window for animated turn, then freeze

        -- Step 1: Break from station
        pcall(function() se:AbandonStations(0) end)
        print(TAG .. " AbandonStations(0)")

        -- Step 2: Enable scheduling (allows turn animation to play)
        pcall(function() se:EnableScheduling(true, false, true) end)
        print(TAG .. " EnableScheduling(true)")

        -- Step 3: Get NPC_Component and call SetTargetLocationTurnInPlace
        local npcComp = nil
        pcall(function()
            local npcCompClass = staticData.npcComponentClass
            if npcCompClass then
                npcComp = nearestNpc:GetComponentByClass(npcCompClass)
            end
        end)

        if not npcComp then
            print(TAG .. " Could not get NPC_Component")
            return
        end

        local ok2, err2 = pcall(function()
            npcComp:SetTargetLocationTurnInPlace(playerLoc)
        end)
        print(TAG .. " SetTargetLocationTurnInPlace: " .. (ok2 and "OK" or tostring(err2)))

        -- Step 4: After delay, freeze NPC before scheduler reassigns a station
        ExecuteInGameThreadWithDelay(700, function()
            pcall(function()
                if SafeIsValid(nearestNpc) then
                    se:EnableScheduling(false, true, true)
                    print(TAG .. " EnableScheduling(false) - frozen after 700ms")
                end
            end)
        end)

        -- Save state for release toggle
        _G._TurnInPlaceState = {
            npcName = nearestName,
            activityId = nil,
            startYaw = npcRot.Yaw or 0,
        }

        -- Check rotation after delays
        ExecuteInGameThreadWithDelay(1500, function()
            pcall(function()
                if SafeIsValid(nearestNpc) then
                    local newRot = nearestNpc:K2_GetActorRotation()
                    print(string.format("%s After 1.5s: yaw=%.1f (was %.1f, delta=%.1f)",
                        TAG, newRot.Yaw or 0, npcRot.Yaw or 0, (newRot.Yaw or 0) - (npcRot.Yaw or 0)))
                end
            end)
        end)

        ExecuteInGameThreadWithDelay(3000, function()
            pcall(function()
                if SafeIsValid(nearestNpc) then
                    local newRot = nearestNpc:K2_GetActorRotation()
                    print(string.format("%s After 3s: yaw=%.1f (was %.1f, delta=%.1f)",
                        TAG, newRot.Yaw or 0, npcRot.Yaw or 0, (newRot.Yaw or 0) - (npcRot.Yaw or 0)))
                end
            end)
        end)

        print(TAG .. " === TEST FIRED (press F7 again to release) ===")
    end)
end

function DebugF7_ScheduleExplorer()
    ExecuteInGameThread(function()
        print("[DebugF7] === SCHEDULE EXPLORER ===")
        local staticData = GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end

        local popManager = staticData.populationManager
        if not popManager or not SafeIsValid(popManager) then
            print("[DebugF7] No PopulationManager")
            return
        end

        -- Test subjects - mix of nearby and distant NPCs
        local testNames = {
            "PhineasBlack",
            "SebastianSallow",
            "NatasaiOnai",
            "Natsai",
            "NatsaiOnai",
            "PoppySweeting",
            "AbrahamRonen",
            "MatildaWeasley",
        }

        for _, entityName in ipairs(testNames) do
            print(string.format("\n[DebugF7] --- %s ---", entityName))
            local se = nil
            local ok, err = pcall(function()
                se = popManager:GetScheduledEntityFromName(entityName)
            end)
            if not ok then
                print(string.format("[DebugF7]   GetSEFromName FAILED: %s", tostring(err)))
                goto nextEntity
            end
            if not se then
                print("[DebugF7]   ScheduledEntity = nil (not found)")
                goto nextEntity
            end

            -- Check if valid
            local seValid = false
            pcall(function() seValid = se:IsValid() end)
            print(string.format("[DebugF7]   SE valid: %s", tostring(seValid)))
            if not seValid then goto nextEntity end

            -- Identity
            pcall(function()
                local myName = se:GetMyName()
                local nameStr = nil
                pcall(function() nameStr = myName:ToString() end)
                print(string.format("[DebugF7]   GetMyName: %s", tostring(nameStr or myName)))
            end)

            pcall(function()
                local myId = se:GetMyID()
                print(string.format("[DebugF7]   GetMyID: %s", tostring(myId)))
            end)

            -- Is in flesh (streamed in)?
            pcall(function()
                local inFlesh = se:CurrentlyInFlesh()
                print(string.format("[DebugF7]   CurrentlyInFlesh: %s", tostring(inFlesh)))
            end)

            -- Get flesh actor if loaded
            pcall(function()
                local flesh = se:GetFlesh()
                if flesh and SafeIsValid(flesh) then
                    print(string.format("[DebugF7]   Flesh: %s", flesh:GetFullName()))
                else
                    print("[DebugF7]   Flesh: nil/invalid (not streamed in)")
                end
            end)

            -- Location (works even without flesh)
            pcall(function()
                local loc = se:GetLocation()
                if loc then
                    print(string.format("[DebugF7]   Location: (%.0f, %.0f, %.0f)", loc.X or 0, loc.Y or 0, loc.Z or 0))
                else
                    print("[DebugF7]   Location: nil")
                end
            end)

            -- Enabled / state
            pcall(function() print(string.format("[DebugF7]   IsEnabled: %s", tostring(se:IsEnabled()))) end)
            pcall(function() print(string.format("[DebugF7]   IsStudent: %s", tostring(se:IsStudent()))) end)
            pcall(function() print(string.format("[DebugF7]   IsGhost: %s", tostring(se:IsGhost()))) end)
            pcall(function() print(string.format("[DebugF7]   IsHobo: %s", tostring(se:IsHobo()))) end)
            pcall(function() print(string.format("[DebugF7]   IsInTransit: %s", tostring(se:IsInTransit()))) end)

            -- Current activity
            pcall(function()
                local out = {}
                local out2 = {}
                se:GetCurrentActivity(out, out2)
                if out.ActivityIsValid then
                    local activity = nil
                    pcall(function() activity = out.Activity:ToString() end)
                    local actType = nil
                    pcall(function() actType = out.ActivityType:ToString() end)
                    local location = nil
                    pcall(function() location = out.Location:ToString() end)
                    local locKey = nil
                    pcall(function() locKey = out.LocationKey:ToString() end)
                    local stationKey = nil
                    pcall(function() stationKey = out.StationKey:ToString() end)
                    print(string.format("[DebugF7]   CurrentActivity: %s", tostring(activity)))
                    print(string.format("[DebugF7]     Type: %s", tostring(actType)))
                    print(string.format("[DebugF7]     Location: %s", tostring(location)))
                    print(string.format("[DebugF7]     LocationKey: %s", tostring(locKey)))
                    print(string.format("[DebugF7]     StationKey: %s", tostring(stationKey)))
                    print(string.format("[DebugF7]     Time: %s-%s (dur %s min)",
                        tostring(out.StartTime), tostring(out.EndTime), tostring(out.DurationMinutes)))
                    print(string.format("[DebugF7]     DaysMask: %s  Priority: %s", tostring(out.DaysMask), tostring(out.Priority)))
                else
                    print("[DebugF7]   CurrentActivity: NONE (ActivityIsValid=false)")
                end
            end)

            -- Upcoming activity
            pcall(function()
                local out = {}
                local out2 = {}
                se:GetUpcomingActivity(out, out2)
                if out.ActivityIsValid then
                    local activity = nil
                    pcall(function() activity = out.Activity:ToString() end)
                    local location = nil
                    pcall(function() location = out.Location:ToString() end)
                    print(string.format("[DebugF7]   UpcomingActivity: %s @ %s", tostring(activity), tostring(location)))
                    print(string.format("[DebugF7]     Time: %s-%s", tostring(out.StartTime), tostring(out.EndTime)))
                else
                    print("[DebugF7]   UpcomingActivity: NONE")
                end
            end)

            -- Minutes to upcoming
            pcall(function()
                local out = {}
                local out2 = {}
                se:GetMinutesToUpcomingActivity(out, out2)
                if out.ActivityIsValid ~= nil then
                    print(string.format("[DebugF7]   MinutesToUpcoming: %s (valid=%s)",
                        tostring(out.MinutesToUpcomingActivity), tostring(out.ActivityIsValid)))
                end
            end)

            ::nextEntity::
        end

        print("\n[DebugF7] === SCHEDULE EXPLORER DONE ===")
    end)
end

function DebugF7_CompanionOrient()
    ExecuteInGameThread(function()
        print("[DebugF7] === COMPANION ORIENT TO NEAREST NPC ===")
        local staticData = GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end

        local companionMgr = staticData.companionManager
        if not companionMgr then print("[DebugF7] No CompanionManager") return end

        local companionPawn = nil
        pcall(function() companionPawn = companionMgr:GetPrimaryCompanionPawn() end)
        if not companionPawn or not SafeIsValid(companionPawn) then
            print("[DebugF7] No companion pawn")
            return
        end

        local compLoc = companionPawn:K2_GetActorLocation()
        local compRot = companionPawn:K2_GetActorRotation()
        local compName = companionPawn:GetFullName()
        print(string.format("[DebugF7] Companion at (%.0f, %.0f, %.0f) yaw=%.1f",
            compLoc.X, compLoc.Y, compLoc.Z, compRot.Yaw or 0))

        -- Find nearest NPC that isn't the companion
        local nearestNpc = nil
        local nearestDist = math.huge
        local nearestName = nil

        if GetNearbyNPCs then
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                for _, entry in ipairs(npcResult.nearbyList) do
                    if entry.actor and SafeIsValid(entry.actor) then
                        local entryName = entry.actor:GetFullName()
                        if entryName ~= compName then
                            local npcLoc = entry.actor:K2_GetActorLocation()
                            local dx = npcLoc.X - compLoc.X
                            local dy = npcLoc.Y - compLoc.Y
                            local dist = math.sqrt(dx * dx + dy * dy)
                            if dist < nearestDist then
                                nearestDist = dist
                                nearestNpc = entry.actor
                                nearestName = entry.name
                            end
                        end
                    end
                end
            end
        end

        if not nearestNpc then
            print("[DebugF7] No nearby NPC found (excluding companion)")
            return
        end

        local tgtLoc = nearestNpc:K2_GetActorLocation()
        local dx = tgtLoc.X - compLoc.X
        local dy = tgtLoc.Y - compLoc.Y
        local dist = math.sqrt(dx * dx + dy * dy)
        local dirX = dx / dist
        local dirY = dy / dist

        -- Calculate angle
        local angleToTarget = math.atan(dirY, dirX) * 180 / math.pi
        local compYaw = compRot.Yaw or 0
        local diff = angleToTarget - compYaw
        while diff > 180 do diff = diff - 360 end
        while diff < -180 do diff = diff + 360 end
        local turnAngle = math.abs(diff)

        print(string.format("[DebugF7] Nearest NPC: %s (dist=%.0f, angle=%.1f, turnNeeded=%.1f)",
            nearestName, dist, angleToTarget, turnAngle))
        print(string.format("[DebugF7] Direction: (%.3f, %.3f)", dirX, dirY))

        if turnAngle < 35 then
            print("[DebugF7] Already facing target (< 35 deg), skipping")
            return
        end

        -- Check companion speed (already moving = shorter pulse)
        local speed = 0
        pcall(function()
            local vel = companionPawn:GetVelocity()
            if vel then
                speed = math.sqrt(vel.X*vel.X + vel.Y*vel.Y + vel.Z*vel.Z)
            end
        end)

        -- Kill any queued MoveToLocation on the pawn itself
        pcall(function() companionPawn.CharacterMovement:StopMovementImmediately() end)
        -- Also clear CompanionManager stop state
        pcall(function() companionMgr:StopMovement(true) end)
        pcall(function() companionMgr:StopMovement(false) end)

        -- Pulse: SetCompanionForcedWaitLocation toward nearest NPC
        local waitPos = {
            X = compLoc.X + dirX * 200,
            Y = compLoc.Y + dirY * 200,
            Z = compLoc.Z
        }
        local waitDir = { X = dirX, Y = dirY, Z = 0 }
        companionMgr:SetCompanionForcedWaitLocation(waitPos, waitDir)

        local delay = turnAngle > 120 and 700 or 500
        if speed > 1 then
            delay = math.floor(delay / 2)
        end
        print(string.format("[DebugF7] Pulsing companion toward %s (delay=%dms, speed=%.0f)", nearestName, delay, speed))

        ExecuteInGameThreadWithDelay(delay, function()
            pcall(function() companionMgr:StopMovement(true) end)
            pcall(function() companionMgr:StopMovement(false) end)
            pcall(function() companionMgr:StopCompanionForcedWaiting() end)
            print("[DebugF7] Pulse complete, follow restored")
        end)

        if ShowHint then
            ShowHint("Orient companion -> " .. nearestName .. " (angle=" .. math.floor(turnAngle) .. ")", 3)
        end
    end)
end

-- Preserved: Original F7 Gaze/LookAt Tests (renamed)
function DebugF7_GazeTests()
    ExecuteInGameThread(function()
        if _G.DebugGazeLoop then
            pcall(function() CancelDelayedAction(_G.DebugGazeLoop) end)
            _G.DebugGazeLoop = nil
        end
        local staticData = GetStaticCache()
        if not staticData then print("[DebugF7-Gaze] No static cache") return end
        local player = staticData.player
        if not player or not SafeIsValid(player) then print("[DebugF7-Gaze] No player") return end
        local companionMgr = staticData.companionManager
        if not companionMgr then print("[DebugF7-Gaze] No CompanionManager") return end
        local companionPawn = nil
        pcall(function() companionPawn = companionMgr:GetPrimaryCompanionPawn() end)
        if not companionPawn or not SafeIsValid(companionPawn) then
            print("[DebugF7-Gaze] No companion pawn")
            return
        end
        local companionId = Utils.GetCompanionId(companionPawn) or "?"
        _G.DebugGazeTestIdx = _G.DebugGazeTestIdx + 1
        local idx = _G.DebugGazeTestIdx
        print(string.format("[DebugF7-Gaze] === TEST %d === Companion: %s", idx, companionId))
        if idx > 11 then
            print("[DebugF7-Gaze] All tests complete. Resetting.")
            _G.DebugGazeTestIdx = 0
            return
        end
        print("[DebugF7-Gaze] (Old gaze test " .. idx .. " - see git history for full implementation)")
    end)
end

-- F7 Debug Function (Original) - Nearby Station Scanner
function DebugF7_StationScanner()
    ExecuteInGameThread(function()
        if true then
            print("[DebugF7] === VR CONTROLLER DEBUG ===")
            local staticData = GetStaticCache()
            if not staticData then print("[DebugF7] No static cache") return end
            local cam = staticData.cameraManager
            if not cam then print("[DebugF7] No camera") return end
            local camRot
            pcall(function() camRot = cam:GetCameraRotation() end)
            if camRot then
                print(string.format("[DebugF7] CamRot (=wand): yaw=%.1f pitch=%.1f", camRot.Yaw, camRot.Pitch))
            end
            local dbg = _G.VRDebug
            if dbg then
                if dbg.hmd then
                    print(string.format("[DebugF7] HMD: yaw=%.1f pitch=%.1f", dbg.hmd.yaw, dbg.hmd.pitch))
                end
                if dbg.dev1 then
                    print(string.format("[DebugF7] Dev1 (%s): yaw=%.1f pitch=%.1f", dbg.dev1.role or "?", dbg.dev1.yaw, dbg.dev1.pitch))
                end
                if dbg.dev2 then
                    print(string.format("[DebugF7] Dev2 (%s): yaw=%.1f pitch=%.1f", dbg.dev2.role or "?", dbg.dev2.yaw, dbg.dev2.pitch))
                end
                print(string.format("[DebugF7] Using device idx: %s", tostring(dbg.using_idx)))
            else
                print("[DebugF7] No VRDebug data yet")
            end
            local vrOff = _G.VROffset
            if vrOff then
                print(string.format("[DebugF7] Computed offset: yaw=%.1f pitch=%.1f", vrOff.yaw, vrOff.pitch))
            end
            return
        end

        print("[DebugF7] === STATION SCANNER ===")

        local staticData = GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end

        local player = staticData.player
        if not player then print("[DebugF7] No player") return end

        local playerLoc = nil
        pcall(function() playerLoc = player:K2_GetActorLocation() end)
        if not playerLoc then print("[DebugF7] No player location") return end

        local KismetSystem = staticData.kismetSystem
        local KismetMath = staticData.kismetMath

        -- Find all stations
        local allStations = FindAllOf("Station")
        if not allStations then
            ShowHint("No stations found", 3)
            return
        end

        local scanRadius = 1500 -- ~15 meters in UE units
        local nearbyStations = {}

        for _, station in pairs(allStations) do
            pcall(function()
                local stationLoc = station:K2_GetActorLocation()
                local dx = stationLoc.X - playerLoc.X
                local dy = stationLoc.Y - playerLoc.Y
                local dz = stationLoc.Z - playerLoc.Z
                local dist = math.sqrt(dx * dx + dy * dy + dz * dz)

                if dist <= scanRadius then
                    local stationComp = nil
                    pcall(function() stationComp = station:GetStationComponent() end)
                    if not stationComp then return end

                    local active = false
                    pcall(function() active = stationComp:IsStationActive() end)
                    if not active then return end

                    local numConns = 0
                    pcall(function() numConns = stationComp:GetNumConnections() end)

                    local isChair = false
                    pcall(function() isChair = stationComp:IsAChair() end)

                    local isBed = false
                    pcall(function() isBed = stationComp:IsABed() end)

                    local propType = -1
                    pcall(function() propType = stationComp:GetPropType() end)

                    local meshName = "?"
                    pcall(function()
                        local mn = stationComp:GetMeshName()
                        if mn then pcall(function() meshName = mn:ToString() end) end
                    end)

                    local stationName = "?"
                    pcall(function() stationName = station:GetFullName():match("([^%.]+)$") end)

                    local stationClass = "?"
                    pcall(function() stationClass = station:GetClass():GetFullName():match("([^%.]+)$") end)

                    -- Check occupancy
                    local numUsers = 0
                    pcall(function()
                        local users = {}
                        stationComp:GetStationUsers(users)
                        for _ in pairs(users) do numUsers = numUsers + 1 end
                    end)

                    table.insert(nearbyStations, {
                        station = station,
                        stationComp = stationComp,
                        name = stationName,
                        class = stationClass,
                        loc = stationLoc,
                        dist = dist,
                        conns = numConns,
                        users = numUsers,
                        isChair = isChair,
                        isBed = isBed,
                        propType = propType,
                        mesh = meshName,
                    })
                end
            end)
        end

        -- Sort by distance
        table.sort(nearbyStations, function(a, b) return a.dist < b.dist end)

        -- Line trace visibility check for each station
        if KismetSystem and KismetMath then
            local playerHalfHeight = 88
            pcall(function()
                local capsule = player.CapsuleComponent
                if capsule and capsule.CapsuleHalfHeight then
                    playerHalfHeight = capsule.CapsuleHalfHeight
                end
            end)

            local traceStart = nil
            pcall(function()
                traceStart = KismetMath:MakeVector(playerLoc.X, playerLoc.Y, playerLoc.Z + playerHalfHeight * 2 + 20)
            end)

            if traceStart then
                local ETraceTypeQuery_Visibility = 0
                local EDrawDebugTrace_None = 0
                local TraceColor = { R = 0, G = 0, B = 0, A = 0 }
                local ActorsToIgnore = { player }

                for _, s in ipairs(nearbyStations) do
                    s.visible = false
                    pcall(function()
                        local EndVector = KismetMath:MakeVector(s.loc.X, s.loc.Y, s.loc.Z + 50)
                        local HitResult = {}
                        local WasHit = KismetSystem:LineTraceSingle(
                            player, traceStart, EndVector,
                            ETraceTypeQuery_Visibility, false, ActorsToIgnore,
                            EDrawDebugTrace_None, HitResult, true,
                            TraceColor, TraceColor, 0.0
                        )
                        s.visible = not WasHit
                    end)
                end
            end
        end

        -- Filter out PROP_TYPE_NONE (area/zone markers) and assign labels
        local filtered = {}
        for _, s in ipairs(nearbyStations) do
            local label = PROP_TYPE_LABELS[s.propType]
            if label then
                s.typeLabel = label
                table.insert(filtered, s)
            end
        end
        nearbyStations = filtered

        -- Print results
        print(string.format("[DebugF7] Found %d stations within %.0fm", #nearbyStations, scanRadius / 100))

        local hintLines = {}
        for i, s in ipairs(nearbyStations) do
            local vis = s.visible and "VIS" or "HID"
            local spots = s.conns - s.users
            local spotsStr = spots .. "/" .. s.conns
            if s.users > 0 then spotsStr = spotsStr .. " (" .. s.users .. " used)" end

            local line = string.format("%s %.0fm %s spots=%s %s",
                vis, s.dist / 100, s.typeLabel, spotsStr, s.mesh)

            print(string.format("[DebugF7] [%d] %s | %s | class=%s", i, line, s.name, s.class))

            if i <= 15 then
                table.insert(hintLines, string.format("%s %.0fm %s %s", vis, s.dist / 100, s.typeLabel, spotsStr))
            end
        end

        local hintText = string.format("STATIONS (%d within %.0fm):\n", #nearbyStations, scanRadius / 100)
            .. table.concat(hintLines, "\n")
        ShowHint(hintText, 15)

        print("[DebugF7] === SCAN COMPLETE ===")
    end)
end

-- ============================================
-- Speaker Actor Cache (Multi-NPC support)
-- Maps speaker display names to actor references
-- ============================================
_G.SpeakerActorCache = _G.SpeakerActorCache or {}

-- Get actor for a speaker by name (cached)
function GetSpeakerActor(speakerName)
    if not speakerName or speakerName == "" then
        return nil
    end

    -- Special case: "player" means the player character
    if speakerName == "player" then
        local staticData = Cache.GetStaticData()
        local player = staticData and staticData.player
        if player and SafeIsValid(player) then
            return player
        end
        print("[GetSpeakerActor] Player actor requested but not found")
        return nil
    end

    -- Check cache (populated from nearbyNpcs scan during StartConversation)
    local cached = _G.SpeakerActorCache[speakerName]
    if cached then
        if SafeIsValid(cached) then
            return cached
        else
            -- Clear invalid entry to avoid repeated checks
            _G.SpeakerActorCache[speakerName] = nil
            print("[GetSpeakerActor] Cache hit but invalid (cleared): " .. speakerName)
        end
    end

    -- Not in cache - search NPC cache directly (bypasses visibility filtering)
    -- This handles cases where visibility raycast filtered out the speaker
    local npcs = GetCachedNPCs()
    if npcs and #npcs > 0 then
        local staticData = GetStaticCache()
        local targetLower = speakerName:lower()
        for _, npc in pairs(npcs) do
            if SafeIsValid(npc) then
                local npcId = Utils.GetActorVoiceId(npc, staticData)
                if npcId and npcId:lower() == targetLower then
                    -- Found! Cache it for future lookups
                    _G.SpeakerActorCache[speakerName] = npc
                    print("[GetSpeakerActor] Found via NPC cache fallback: " .. speakerName)
                    return npc
                end
            end
        end
    end

    return nil
end

-- Get actor for whoever is currently speaking (turn-based with fallbacks)
function GetCurrentSpeakerActor()
    local pState = _G.PlaybackState
    local currentTurnId = _G.SonorusState and _G.SonorusState.currentTurnId

    -- PRIMARY: Use currentTurnId from state (set by play_turn handler)
    -- This is the new atomic approach - avoids race conditions
    if currentTurnId then
        local actor = _G.TurnActorCache and _G.TurnActorCache[currentTurnId]
        if actor and SafeIsValid(actor) then
            return actor
        end
    end

    -- FALLBACK 1: Queue-based lookup (for legacy code paths)
    if pState and pState.playing and pState.queue and pState.currentIndex <= #pState.queue then
        local currentItem = pState.queue[pState.currentIndex]
        if currentItem then
            -- Try turn ID first if present
            if currentItem.turnId then
                local actor = _G.TurnActorCache and _G.TurnActorCache[currentItem.turnId]
                if actor and SafeIsValid(actor) then
                    return actor
                end
            end
            -- Fall back to speaker name lookup (handles location change recovery)
            local lookupName = currentItem.speakerId or currentItem.speaker
            if lookupName then
                local actor = GetSpeakerActor(lookupName)
                if actor then
                    -- Update TurnActorCache with recovered actor for future lookups
                    local turnId = currentItem.turnId or currentTurnId
                    if turnId and _G.TurnActorCache then
                        _G.TurnActorCache[turnId] = actor
                    end
                    return actor
                end
            end
        end
    end

    -- FALLBACK 2: Legacy CurrentSpeakerId (for old prepare_speaker code path)
    if _G.CurrentSpeakerId then
        local cached = _G.SpeakerActorCache and _G.SpeakerActorCache[_G.CurrentSpeakerId]
        if cached and SafeIsValid(cached) then
            return cached
        end
    end

    -- No actor found
    return nil
end

-- Export globally for LipSync module
_G.GetCurrentSpeakerActor = GetCurrentSpeakerActor

-- Clear speaker cache (on reset or conversation end)
function ClearSpeakerCache()
    _G.SpeakerActorCache = {}
    print("[SpeakerCache] Cache cleared")
end

-- Mute all speakers in the queue (call when queue is populated)
-- Stores speaker names (not AkComponent refs) so unmute works even if NPCs are recreated
function MuteQueueSpeakers(queue)
    if not queue or #queue == 0 then return end
    if not _G.SonorusState then return end

    _G.SonorusState.mutedSpeakers = _G.SonorusState.mutedSpeakers or {}

    local mutedCount = 0
    for _, item in ipairs(queue) do
        local speakerName = item.speakerId or item.speaker
        if speakerName and not _G.SonorusState.mutedSpeakers[speakerName] then
            local actor = GetSpeakerActor(speakerName)
            if actor then
                local comp = MuteNPCAudio(actor)
                if comp then
                    _G.SonorusState.mutedSpeakers[speakerName] = true
                    mutedCount = mutedCount + 1
                end
            end
        end
    end

    if mutedCount > 0 then
        local total = 0
        for _ in pairs(_G.SonorusState.mutedSpeakers) do total = total + 1 end
        print("[Sonorus] Muted " .. mutedCount .. " queue speakers (total: " .. total .. ")")
    end
end

-- Unmute all speakers (call at conversation end)
-- Looks up actors by name and unmutes their current AkComponent (handles NPC recreation)
function UnmuteAllSpeakers()
    if not _G.SonorusState then return end
    local mutedSpeakers = _G.SonorusState.mutedSpeakers or {}
    _G.SonorusState.mutedSpeakers = {}

    local count = 0
    for speakerName, _ in pairs(mutedSpeakers) do
        count = count + 1
    end

    if count > 0 then
        print("[Sonorus] Unmuting " .. count .. " speakers")
        for speakerName, _ in pairs(mutedSpeakers) do
            local actor = GetSpeakerActor(speakerName)
            if actor then
                UnmuteNPCAudioByActor(actor)
            end
        end
    end
end

-- ============================================
-- Playback State (for multi-NPC queue)
-- ============================================
_G.PlaybackState = _G.PlaybackState or {
    queue = {},           -- Items pushed via socket
    currentIndex = 1,     -- Which queue item we're playing (1-indexed)
    currentSegment = 1,   -- Which segment within current item
    playing = false,      -- Are we actively playing queue?
    serverState = "idle", -- Server state (idle/playing) from socket
}

-- ============================================
-- Dialogue/Voice Tracking (global for persistence)
-- ============================================
-- Note: Dialogue history is now managed by Python only
-- Lua sends entries via socket, Python handles persistence
_G.VoiceSamples = _G.VoiceSamples or {}
_G.PendingDialogue = _G.PendingDialogue or {}
_G.Subtitles = _G.Subtitles or {}
_G.SubtitlesLoaded = _G.SubtitlesLoaded or false
_G.Locations = _G.Locations or {}
_G.LocationsLoaded = _G.LocationsLoaded or false
_G.Localization = _G.Localization or {}
_G.LocalizationLoaded = _G.LocalizationLoaded or false
-- Voice manifest: maps voice IDs to their reference info
_G.VoiceManifest = _G.VoiceManifest or {}
_G.VoiceManifestLoaded = _G.VoiceManifestLoaded or false
-- NPC ID normalization: lowercase -> proper case mapping (e.g., "neridaroberts" -> "NeridaRoberts")
-- Built from voice_manifest.json voice keys which have proper casing
_G.VoiceIdNormalize = _G.VoiceIdNormalize or {}
-- Companion callout history for repeat blocking: {[voiceName] = {[text] = gameTimeMinutes}}
_G.CompanionCalloutHistory = _G.CompanionCalloutHistory or {}

-- ============================================
-- ============================================
-- Server Management
-- ============================================
function IsServerAlive()
    -- Check heartbeat file - if timestamp is recent, server is alive
    local content = ReadFile("sonorus\\server.heartbeat")
    if content == "" then
        print("[Sonorus] Heartbeat file empty or missing")
        return false
    end

    local timestamp = tonumber(content)
    if not timestamp then
        print("[Sonorus] Heartbeat file invalid: " .. tostring(content))
        return false
    end

    -- Server is alive if heartbeat is within last 10 seconds
    -- (Python writes every 1s, 10s gives margin for file system delays)
    local now = os.time()
    local age = now - timestamp
    local alive = age < 10

    if not alive then
        print("[Sonorus] Heartbeat stale: " .. age .. "s old")
    end

    -- Clear startup guard if server is confirmed alive
    if alive and _G.SonorusServerState.startupInProgress then
        _G.SonorusServerState.startupInProgress = false
        print("[Sonorus] Server confirmed alive")
    end

    return alive
end

function StartServer()
    local serverState = _G.SonorusServerState

    -- Check heartbeat file (non-blocking, no HTTP)
    if IsServerAlive() then
        print("[Sonorus] Server alive (heartbeat)")
        return true
    end

    -- Check lock file (contains time string like "16:45:30.50" from batch heartbeat)
    local lockContent = ReadFile("sonorus\\server.lock")
    local h, m, s = lockContent:match("(%d+):(%d+):(%d+)")
    if h and m and s then
        local lockTime = tonumber(h) * 3600 + tonumber(m) * 60 + tonumber(s)
        local t = os.date("*t")
        local now = t.hour * 3600 + t.min * 60 + t.sec
        -- Handle midnight wraparound
        local age = now - lockTime
        if age < 0 then age = age + 86400 end
        if age < 60 then  -- 60s = 2 missed heartbeats means dead
            print("[Sonorus] Server startup in progress (lock " .. age .. "s old), waiting...")
            return true
        else
            -- Lock is stale (process died), delete it and retry
            print("[Sonorus] Lock file stale (" .. age .. "s), removing and retrying...")
            os.remove("sonorus\\server.lock")
        end
    end

    -- In-memory guard (for rapid retries within same game session)
    if serverState.startupInProgress then
        local elapsed = os.time() - serverState.startupTime
        if elapsed < 30 then
            print("[Sonorus] Server startup in progress (" .. elapsed .. "s), waiting...")
            return true
        end
    end

    -- Set guard before spawning
    serverState.startupInProgress = true
    serverState.startupTime = os.time()

    print("[Sonorus] Starting server...")

    -- Force socket reconnect since server is restarting
    if _G.SocketClient then
        _G.SocketClient.close()
        print("[Sonorus] Socket closed for server restart")
    end

    -- Use batch file that knows its own location
    -- Close handle immediately since 'start' detaches the process
    local handle = io.popen('start "SonorusServer" sonorus\\start_server.bat --from-game')
    if handle then handle:close() end

    print("[Sonorus] Server process spawned")
    return true
end

function StopServer()
    if not _G.SonorusServerState.started then
        return
    end

    print("[Sonorus] Stopping server...")
    if _G.SocketClient then
        _G.SocketClient.send({type = "shutdown"})
    end
    _G.SonorusServerState.started = false
    print("[Sonorus] Server stopped")
end

-- Send dialogue entry to Python for persistence
-- Python is the sole writer for dialogue_history.json to avoid race conditions
local function sendDialogueEntry(entry)
    if not entry then return end

    -- Suppress all non-combat entries during combat
    -- Combat entries use direct socket send to bypass this check
    if _G.CombatStats and _G.CombatStats.active then
        local entryType = entry.type or "dialogue"
        -- Only allow combat entries through during combat
        if entryType ~= "combat" then
            -- Silently skip - don't log to avoid spam during combat
            return
        end
    end

    -- Send to Python via socket
    pcall(function()
        SocketClient.send({
            type = "record_dialogue",
            entry = entry
        })
    end)
end

-- Load subtitles.json (lazy load on first use)
-- Uses language-specific file if set (e.g., subtitles_de_de.json for German)
function LoadSubtitles()
    local path = GetLocalizedPath("subtitles", ".json")
    return FileIO.LoadJsonCached("Subtitles", path, "subtitles.json")
end

-- Load locations.json (lazy load on first use)
function LoadLocations()
    return FileIO.LoadJsonCached("Locations", FILES.locations, "locations.json")
end

-- Get display name for a location internal ID
-- Returns name string (or nil if not found)
-- Locations can be either strings or objects with {name, desc}
function GetLocationDisplayName(internalId)
    if not _G.LocationsLoaded then
        LoadLocations()
    end

    if not internalId or internalId == "" then
        return nil
    end

    -- Helper to extract name from location entry (handles both string and object formats)
    local function extractName(entry)
        if type(entry) == "string" then
            return entry
        elseif type(entry) == "table" and entry.name then
            return entry.name
        end
        return nil
    end

    -- Try exact match first
    local entry = _G.Locations[internalId]
    if entry then
        return extractName(entry)
    end

    -- Try without "Area" suffix (HogwartsArea -> Hogwarts)
    local withoutArea = internalId:gsub("Area$", "")
    if withoutArea ~= internalId then
        entry = _G.Locations[withoutArea]
        if entry then return extractName(entry) end
    end

    -- Try case-insensitive match
    local lowerKey = internalId:lower()
    for key, value in pairs(_G.Locations) do
        if key:lower() == lowerKey then
            return extractName(value)
        end
    end

    -- Try longest prefix match (e.g. "HOG_Class_Charms_Patrol_Prof" matches "HOG_Class_Charms")
    local bestMatch = nil
    local bestLen = 0
    for key, value in pairs(_G.Locations) do
        if #key > bestLen and internalId:sub(1, #key) == key then
            bestLen = #key
            bestMatch = value
        end
    end
    if bestMatch then return extractName(bestMatch) end

    -- Fallback to main_localization.json
    if not _G.LocalizationLoaded then
        LoadLocalization()
    end
    if _G.Localization then
        local locEntry = _G.Localization[internalId]
        if locEntry and type(locEntry) == "string" and locEntry ~= "" then
            return locEntry
        end
    end

    return nil
end

-- Get subtitle text for a lineID
function GetSubtitleText(lineID)
    if not _G.SubtitlesLoaded then
        LoadSubtitles()
    end

    -- Handle case where file doesn't exist (e.g., language changed but not yet extracted)
    if not _G.Subtitles then
        return ""
    end

    -- Try original key first (NPCs use TitleCase)
    local text = _G.Subtitles[lineID]
    if text then return text end

    -- Try Normalizing the NPC part of the key e.g. neridaroberts_10364 to NeridaRoberts_10364
    local npcId, lineNum = string.match(lineID, "^([^_]+)_(.+)$")
    if npcId and lineNum then
        local normalized = NormalizeNpcId(npcId)
        if normalized ~= npcId then
            local normalizedLineID = normalized .. "_" .. lineNum
            _G.DevPrint("normalizedLineID " .. normalizedLineID)

            text = _G.Subtitles[normalizedLineID]
            if text then return text end
        end
    end

    -- Player keys are lowercase in subtitles.json
    local key = string.lower(lineID or "")
    text = _G.Subtitles[key]
    if text then return text end

    -- Try swapping male/female for player dialogue
    local altKey = key:gsub("playermale", "playerfemale")
    if altKey == key then
        altKey = key:gsub("playerfemale", "playermale")
    end
    return _G.Subtitles[altKey] or ""
end

-- ============================================
-- Localization (character/item display names)
-- ============================================

-- Load main_localization.json (lazy load, ~3MB)
-- Uses language-specific file if set (e.g., main_localization_de_de.json for German)
function LoadLocalization()
    local path = GetLocalizedPath("main_localization", ".json")
    return FileIO.LoadJsonCached("Localization", path, "main_localization.json")
end

--- Normalize an NPC ID to proper case using voice manifest
--- If the ID has no uppercase letters, looks up the proper case from voice_manifest.json
--- This fixes IDs like "neridaroberts" -> "NeridaRoberts"
---@param npcId string The NPC ID to normalize (e.g., "neridaroberts" or "NeridaRoberts")
---@return string normalizedId The properly-cased ID, or original if not found/already proper
function NormalizeNpcId(npcId)
    if not npcId or npcId == "" then return npcId end

    -- Check if ID already has uppercase letters (proper casing)
    -- If it has ANY uppercase, assume it's already correct
    if npcId:match("[A-Z]") then
        return npcId
    end

    -- All lowercase - need to normalize
    -- Load voice manifest if not loaded
    if not _G.VoiceManifestLoaded then
        LoadVoiceManifest()
    end

    -- Look up in normalization map
    local properCase = _G.VoiceIdNormalize[npcId]
    if properCase then
        return properCase
    end

    -- Not found in voice manifest - return as-is
    return npcId
end
_G.NormalizeNpcId = NormalizeNpcId

-- Get localized display name for internal ID
-- Falls back to prettified name if not found
function GetDisplayName(internalName)
    if not internalName or internalName == "" then return "Unknown" end

    -- Load localization if not loaded
    if not _G.LocalizationLoaded then
        LoadLocalization()
    end

    -- Handle case where file doesn't exist (e.g., language changed but not yet extracted)
    if _G.Localization then
        -- Normalize because the localization has pascal case keys while some NPC ids do not e.g. "neridaroberts"
        local displayName = _G.Localization[NormalizeNpcId(internalName)]
        if displayName and displayName ~= "" then
            return displayName
        end
    end

    -- Fallback: prettify the internal name
    return string.gsub(NormalizeNpcId(internalName), "(%l)(%u)", "%1 %2")
end

-- ============================================
-- Spell Mappings (Blueprint name -> display name)
-- ============================================
_G.SpellMappings = _G.SpellMappings or {}
_G.SpellMappingsLoaded = _G.SpellMappingsLoaded or false

-- Load spell_mappings.json
function LoadSpellMappings()
    return FileIO.LoadJsonCached("SpellMappings", FILES.spellMappings, "spell_mappings.json")
end

-- Get spell info from Blueprint class name
-- Input: "BlueprintGeneratedClass /Game/Gameplay/ToolSet/Spells/Reparo/BP_ReparoSpell.BP_ReparoSpell_C"
-- Output: { name = "Reparo", displayName = "Reparo", category = "Tool", ... } or fallback
function GetSpellInfo(blueprintClassName)
    if not _G.SpellMappingsLoaded then
        LoadSpellMappings()
    end

    if not blueprintClassName or blueprintClassName == "" then
        return nil
    end

    -- Extract "BP_XxxSpell" from the full path
    -- Pattern: .../BP_ReparoSpell.BP_ReparoSpell_C -> BP_ReparoSpell
    local bpKey = blueprintClassName:match("BP_[%w_]+Spell")

    if not bpKey then
        -- Fallback: try to extract any BP_ prefix
        bpKey = blueprintClassName:match("BP_[%w_]+")
    end

    if bpKey and _G.SpellMappings[bpKey] then
        return _G.SpellMappings[bpKey]
    end

    -- Not found - return basic info parsed from class name
    local spellName = bpKey and bpKey:gsub("BP_", ""):gsub("Spell$", "") or "Unknown"
    return {
        name = spellName,
        displayName = spellName,
        category = "Unknown",
        curriculum = "Unknown",
        uiVisible = false,
        cooldown = 0
    }
end

-- Combat tracking utilities are now in Utils/Combat.lua

-- ============================================
-- Voice Manifest & NPC ID Normalization
-- ============================================

--- Load voice_manifest.json and build ID normalization map
--- The voice manifest contains properly-cased voice IDs (e.g., "NeridaRoberts")
--- We build a lowercase -> proper case map so we can normalize IDs from the game
--- which sometimes returns lowercase (e.g., "neridaroberts" -> "NeridaRoberts")
function LoadVoiceManifest()
    -- Already loaded check
    if _G.VoiceManifestLoaded then return true end

    print("[Sonorus] Loading voice_manifest.json...")

    local content = FileIO.ReadFile(FILES.voiceManifest)
    if content == "" then
        print("[Sonorus] Warning: voice_manifest.json not found or empty")
        return false
    end

    local ok, result = pcall(json.decode, content)
    if not ok or not result then
        print("[Sonorus] Error parsing voice_manifest.json")
        return false
    end

    _G.VoiceManifest = result
    _G.VoiceManifestLoaded = true

    -- Build lowercase -> proper case normalization map from voice keys
    local voices = result.voices or {}
    local normalizeMap = {}
    local count = 0

    for voiceId, _ in pairs(voices) do
        local lowerKey = voiceId:lower()
        normalizeMap[lowerKey] = voiceId
        count = count + 1
    end

    _G.VoiceIdNormalize = normalizeMap
    print(string.format("[Sonorus] Loaded %d voice entries, built ID normalization map", count))

    return true
end

-- Export globally
_G.LoadVoiceManifest = LoadVoiceManifest

-- ============================================
-- Utility
-- ============================================

local function calculateDistance(loc1, loc2)
    local dx = loc1.X - loc2.X
    local dy = loc1.Y - loc2.Y
    local dz = loc1.Z - loc2.Z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end

-- Prefixes that indicate generic/ambient NPCs (not named characters)
local GENERIC_NPC_PREFIXES = {
    "AdultMale", "AdultFemale", "ElderlyMale", "ElderlyFemale",
    "ChildMale", "ChildFemale", "TeenMale", "TeenFemale"
}

local function IsNamedNPC(voiceName)
    -- Return true if voiceName is a named NPC, not a generic townsperson
    if not voiceName or voiceName == "" then return false end
    for _, prefix in ipairs(GENERIC_NPC_PREFIXES) do
        if voiceName:sub(1, #prefix) == prefix then
            return false
        end
    end
    return true
end

local function GetEarshotWitnesses(speakerVoiceName, distanceOverride)
    -- Get list of named NPC IDs within earshot, excluding speaker and player
    -- Uses player-relative nearbyNPCs as a proxy for speaker-relative earshot
    -- distanceOverride: optional custom distance (e.g., 3000 for combat)
    local witnesses = {}

    -- Use larger search radius if custom distance requested
    local searchRadius = distanceOverride and math.max(distanceOverride, 2000) or 2000

    -- Get nearby NPCs (use cached result if available from recent WriteGameContext)
    local npcResult = nil
    pcall(function()
        npcResult = GetNearbyNPCs(searchRadius, 0.9)
    end)

    if not npcResult or not npcResult.nearbyList then
        return witnesses
    end

    -- Base earshot distance (can be overridden for combat, etc.)
    local earshotDistance = distanceOverride or 1000  -- ~10m normal, or custom

    -- Reduce earshot distance when player is invisible (Disillusionment)
    -- (proportional reduction even with override)
    if npcResult.playerInStealth then
        earshotDistance = earshotDistance * 0.3  -- 30% when invisible
    end

    for _, npc in ipairs(npcResult.nearbyList) do
        local npcName = npc.name or ""
        local distance = npc.distance or 99999

        -- Skip if too far
        if distance > earshotDistance then
            goto continue
        end

        -- Skip speaker
        if npcName == speakerVoiceName then
            goto continue
        end

        -- Skip player
        local npcLower = npcName:lower()
        if npcLower == "player" or npcLower == "playermale" or npcLower == "playerfemale" then
            goto continue
        end

        -- Skip generic NPCs (only track named characters)
        if not IsNamedNPC(npcName) then
            goto continue
        end

        table.insert(witnesses, npcName)

        ::continue::
    end

    return witnesses
end

-- ============================================
-- Game State Extraction
-- ============================================
-- Set to false to disable game context collection (if it causes freezes)
local ENABLE_GAME_CONTEXT = true

-- Try to get an object, return nil on failure
local function TryFindFirstOf(className)
    local success, result = pcall(function()
        return FindFirstOf(className)
    end)
    -- Check result is valid UObject (not nil, not function)
    if success and result and type(result) == "userdata" then
        if SafeIsValid(result) then
            return result
        end
    end
    return nil
end

-- Get player character info (name, house)
-- Uses UIManager which has clean GetPlayerName/GetPlayerHouse methods
function GetPlayerInfo()
    local info = {
        name = "Unknown",
        house = "Unknown",
    }

    local uiManager = TryFindFirstOf("UIManager")
    if not uiManager then return info end

    -- Get player name and house using safe FString helpers
    local name = Utils.SafeMethodToString(uiManager, "GetPlayerName")
    if name then info.name = name end

    local house = Utils.SafeMethodToString(uiManager, "GetPlayerHouse")
    if house then info.house = house end

    return info
end

-- Time cache: persists across hot reload, invalidated on load/fast travel
_G._TimeCache = _G._TimeCache or {
    lastHour = -1, dayOfWeek = 0, dayOfMonth = 1, month = 9, year = 1890, initialized = false
}

-- Refresh time cache from Scheduler. Call with fresh=true on save load / fast travel.
-- Normal calls: 1 UFunction call (GetMinuteOfTheDay). Fresh: 5 calls (all fields).
function RefreshTimeCache(fresh)
    local scheduler = Cache.Get("Scheduler", function()
        return TryFindFirstOf("Scheduler")
    end)
    if not scheduler then return end

    local tc = _G._TimeCache
    if fresh then
        tc.initialized = false
        tc.lastHour = -1
    end

    -- Always fetch time (1 UFunction call)
    local hour, minute = 12, 0
    pcall(function()
        local minuteOfDay = scheduler:GetMinuteOfTheDay() or 720
        hour = math.floor(minuteOfDay / 60)
        minute = minuteOfDay % 60
    end)
    tc.hour = hour
    tc.minute = minute

    -- Check if day might have changed: hour crossed midnight (was PM, now AM) or first run
    local dayMayHaveChanged = not tc.initialized
        or (tc.lastHour >= 12 and hour < 12)  -- PM → AM = new day
    tc.lastHour = hour

    if dayMayHaveChanged then
        if not tc.initialized then
            -- First run / fresh: fetch everything once
            pcall(function() tc.dayOfWeek = scheduler:GetDayOfTheWeek() or 0 end)
            pcall(function() tc.dayOfMonth = scheduler:GetDayOfTheMonth() or 1 end)
            pcall(function() tc.month = scheduler:GetMonthOfTheYear() or 9 end)
            pcall(function() tc.year = scheduler:GetCalendarYear() or 1890 end)
            tc.initialized = true
        else
            -- PM→AM flip: check if day actually changed
            local oldDay = tc.dayOfMonth
            local oldMonth = tc.month
            local newDay = nil
            pcall(function() newDay = scheduler:GetDayOfTheMonth() end)
            if newDay and newDay ~= oldDay then
                tc.dayOfMonth = newDay
                pcall(function() tc.dayOfWeek = scheduler:GetDayOfTheWeek() or 0 end)
                -- Day went down = new month
                if newDay < oldDay then
                    pcall(function() tc.month = scheduler:GetMonthOfTheYear() or 9 end)
                    -- Month went down = new year
                    if tc.month < oldMonth then
                        pcall(function() tc.year = scheduler:GetCalendarYear() or 1890 end)
                    end
                end
            end
        end
    end
end

-- Get current time using cached Scheduler data
function GetTimeOfDay()
    local result = {
        hour = 12,
        minute = 0,
        dayOfWeek = 0,      -- 0=Monday
        dayOfMonth = 1,
        month = 9,          -- September (school year)
        year = 1890,
        period = "Day",
        isDay = true,
        formatted = "12:00 PM",
        dateFormatted = "Monday, September 1st, 1890",
    }

    local tc = _G._TimeCache
    if not tc.initialized then
        RefreshTimeCache()
    end

    result.hour = tc.hour or 12
    result.minute = tc.minute or 0
    result.dayOfWeek = tc.dayOfWeek
    result.dayOfMonth = tc.dayOfMonth
    result.month = tc.month
    result.year = tc.year

    -- Format time string
    local h = result.hour
    local ampm = h >= 12 and "PM" or "AM"
    local h12 = h % 12
    if h12 == 0 then h12 = 12 end
    result.formatted = string.format("%d:%02d %s", h12, result.minute, ampm)

    -- Determine period from hour
    if h >= 5 and h < 7 then
        result.period = "Dawn"
        result.isDay = true
    elseif h >= 7 and h < 12 then
        result.period = "Morning"
        result.isDay = true
    elseif h >= 12 and h < 14 then
        result.period = "Noon"
        result.isDay = true
    elseif h >= 14 and h < 18 then
        result.period = "Afternoon"
        result.isDay = true
    elseif h >= 18 and h < 21 then
        result.period = "Evening"
        result.isDay = true
    else
        result.period = "Night"
        result.isDay = false
    end

    -- Day names
    local dayNames = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
    local monthNames = {"January", "February", "March", "April", "May", "June",
                        "July", "August", "September", "October", "November", "December"}

    -- Ordinal suffix
    local day = result.dayOfMonth
    local suffix = "th"
    if day == 1 or day == 21 or day == 31 then suffix = "st"
    elseif day == 2 or day == 22 then suffix = "nd"
    elseif day == 3 or day == 23 then suffix = "rd"
    end

    local dayName = dayNames[(result.dayOfWeek % 7) + 1] or "Monday"
    local monthName = monthNames[result.month] or "September"
    result.dateFormatted = string.format("%s, %s %d%s, %d", dayName, monthName, day, suffix, result.year)
    result.dateShort = string.format("%04d/%02d/%02d", result.year, result.month, day)

    return result
end

-- Convert game time to total minutes since epoch (for time comparisons)
-- Uses a simplified calculation: year * 365 * 24 * 60 + month * 30 * 24 * 60 + day * 24 * 60 + hour * 60 + minute
function GetGameTimeMinutes()
    local time = GetTimeOfDay()
    -- Approximate: treat all months as 30 days, all years as 365 days
    local totalMinutes = (time.year * 365 * 24 * 60) +
                         ((time.month - 1) * 30 * 24 * 60) +
                         ((time.dayOfMonth - 1) * 24 * 60) +
                         (time.hour * 60) +
                         time.minute
    return totalMinutes
end

-- Check if a companion callout should be blocked as a repeat
-- Returns true if blocked (duplicate within time window), false if allowed
-- blockMinutes: 0 = disabled (allow all), -1 = never repeat, >0 = block within N game minutes
function IsCompanionCalloutBlocked(voiceName, text, blockMinutes)
    if not voiceName or not text then return false end
    if blockMinutes == 0 then return false end  -- Feature disabled

    local history = _G.CompanionCalloutHistory[voiceName]
    if not history then
        -- First time seeing this companion's callouts
        _G.CompanionCalloutHistory[voiceName] = {[text] = GetGameTimeMinutes()}
        return false
    end

    local lastTime = history[text]
    if not lastTime then
        -- First time seeing this text from this companion
        history[text] = GetGameTimeMinutes()
        return false
    end

    local currentTime = GetGameTimeMinutes()
    local timeDiff = currentTime - lastTime

    -- blockMinutes < 0 means "never repeat" (always block duplicates)
    if blockMinutes < 0 then
        print(string.format("[CompanionCallout] Blocked repeat: %s", voiceName))
        return true
    end

    -- Check if within time window
    if timeDiff < blockMinutes then
        print(string.format("[CompanionCallout] Blocked repeat: %s", voiceName))
        return true
    end

    -- Enough time has passed, update timestamp and allow
    history[text] = currentTime
    -- print(string.format("[CompanionCallout] Allowing repeat (time diff=%d min): %s - '%s'",
    --     timeDiff, voiceName, text:sub(1,50)))
    return false
end

-- Initialize Combat module with dependencies (must be after GetTimeOfDay and GetDisplayName are defined)
Combat.init({
    getTimeOfDay = GetTimeOfDay,
    getDisplayName = GetDisplayName,
})

-- Get current location using game systems
function GetCurrentLocation()
    local location = "Hogwarts"
    local detailedLocation = nil

    -- Method 1: Try MapSubSystem.GetCurrentPlayerRegionInfo()
    pcall(function()
        local mapSubSystem = FindFirstOf("MapSubSystem")
        if mapSubSystem and mapSubSystem:IsValid() then
            local regionInfo = mapSubSystem:GetCurrentPlayerRegionInfo()
            if regionInfo then

                -- Get RegionName FString with proper nested pcall (CLAUDE.md pattern)
                pcall(function()
                    local regionNameFString = regionInfo.RegionName
                    if regionNameFString then
                        local str = nil
                        pcall(function()
                            str = regionNameFString:ToString()
                        end)

                        -- If still garbage, try getting the actual text differently
                        if str and #str > 0 and string.byte(str, 1) < 128 then
                            detailedLocation = str
                        end
                    end
                end)

                -- Helper to parse location from actor/object fullName
                local function parseLocationFromActor(actor, label)
                    if not actor then
                        return nil
                    end

                    -- Try IsValid first (AActor), but UObjects may not have it
                    local isValid = true
                    pcall(function()
                        if actor.IsValid then
                            isValid = actor:IsValid()
                        end
                    end)
                    if not isValid then
                        return nil
                    end

                    local fullName = nil
                    pcall(function() fullName = actor:GetFullName() end)
                    if not fullName then
                        return nil
                    end

                    -- Parse region name from actor path
                    -- Path format: "BP_RegionSpline_C /Engine/Transient...MapSubSystem.HogwartsArea"
                    local internalId = string.match(fullName, "%.([%w_]+)$") or
                                       string.match(fullName, "BP_([%w_]+)_Region") or
                                       string.match(fullName, "Region_([%w_]+)") or
                                       string.match(fullName, "/([%w_]+)_C_")
                    if internalId then
                        -- Strip common suffixes before lookup
                        local cleanId = internalId:gsub("Bounds$", ""):gsub("Area$", ""):gsub("Region$", "")
                        -- Look up display name from locations.json (try both original and cleaned)
                        local displayName = GetLocationDisplayName(internalId) or GetLocationDisplayName(cleanId)
                        if displayName then
                            return displayName
                        else
                            -- Fallback: clean up the ID as display name
                            local cleaned = cleanId:gsub("(%l)(%u)", "%1 %2"):gsub("_", " ")
                            return cleaned
                        end
                    end
                    return nil
                end

                -- Try regions in order of specificity: SubRegion > InnerLevelRegion > LevelRegion > Region
                if not detailedLocation then
                    pcall(function()
                        -- SubRegion is often invalid, skip it for now to avoid crashes
                        -- TODO: Find a safe way to access SubRegion for room-level detail

                        -- Try InnerLevelRegion (e.g., "Hogwarts Castle")
                        if not detailedLocation then
                            detailedLocation = parseLocationFromActor(regionInfo.InnerLevelRegion, "InnerLevelRegion")
                        end

                        -- Try LevelRegion (e.g., "Hogwarts")
                        if not detailedLocation then
                            detailedLocation = parseLocationFromActor(regionInfo.LevelRegion, "LevelRegion")
                        end

                        -- Fall back to Region (broadest)
                        if not detailedLocation then
                            detailedLocation = parseLocationFromActor(regionInfo.Region, "Region")
                        end
                    end)
                end
            end
        end
    end)

    -- Method 2: Try PhoenixGameInstance.GetCurrentWorldName()
    if not detailedLocation then
        pcall(function()
            local gameInstance = FindFirstOf("PhoenixGameInstance")
            if gameInstance and gameInstance:IsValid() then
                local name = Utils.SafeMethodToString(gameInstance, "GetCurrentWorldName")
                if name then
                    -- Convert internal names to readable names
                    local readableNames = {
                        ["Overland"] = "Scottish Highlands",
                        ["HogwartsCastle"] = "Hogwarts Castle",
                        ["Hogwarts"] = "Hogwarts",
                        ["Hogsmeade"] = "Hogsmeade Village",
                        ["Dungeon"] = "Underground",
                    }
                    detailedLocation = readableNames[name] or name
                end
            end
        end)
    end

    -- Method 3: Try MinimapManager to get active minimap type
    if not detailedLocation then
        pcall(function()
            local minimapMgr = FindFirstOf("MinimapManager")
            if minimapMgr and minimapMgr:IsValid() then
                -- Try GetActiveMiniMap
                local activeMinimap = minimapMgr:GetActiveMiniMap()
                if activeMinimap then
                    local mapName = activeMinimap:GetFullName()
                    -- Parse minimap type from name
                    if string.find(mapName, "Hogwarts") then detailedLocation = "Hogwarts"
                    elseif string.find(mapName, "Hogsmeade") then detailedLocation = "Hogsmeade"
                    elseif string.find(mapName, "Overland") then detailedLocation = "Highlands"
                    elseif string.find(mapName, "Dungeon") then detailedLocation = "Dungeon"
                    end
                end
            end
        end)
    end

    -- Method 4: Try MapHogwarts.GetMapLocationName() if in Hogwarts
    if not detailedLocation then
        pcall(function()
            local mapHogwarts = FindFirstOf("MapHogwarts")
            if mapHogwarts and mapHogwarts:IsValid() then
                local name = Utils.SafeMethodToString(mapHogwarts, "GetMapLocationName")
                if name then
                    detailedLocation = name
                end
            end
        end)
    end

    -- Method 5: Fallback to player's GetFullName path parsing
    if not detailedLocation then
        local staticData = Cache.GetStaticData()
        local player = staticData and staticData.player
        if player then
            pcall(function()
                local fullName = player:GetFullName()
                if fullName then
                    local levelMatch = string.match(fullName, "/Levels/([^/]+)/")
                    if levelMatch then
                        -- Make level names more readable
                        if levelMatch == "Overland" then detailedLocation = "Scottish Highlands"
                        elseif levelMatch == "HogwartsCastle" then detailedLocation = "Hogwarts Castle"
                        else detailedLocation = levelMatch
                        end
                    end
                end
            end)
        end
    end

    if detailedLocation then
        location = detailedLocation
    end

    return location
end

-- Export for NPCLock snap rotation location check
_G.GetCurrentLocation = GetCurrentLocation

-- ============================================
-- House Points Cache System
-- ============================================
-- Reads house points from StatsManager and caches them.
-- Only called at specific trigger points (not every context update).
-- Returns true if data was found, false otherwise.
function RefreshHousePoints()
    local housePoints = {}
    local hasData = false

    local ok, err = pcall(function()
        local statsManager = Cache.Get("StatsManager", function()
            return FindFirstOf("StatsManager")
        end)
        if not statsManager then
            DevPrint("[HousePoints] StatsManager not found")
            return
        end

        local houses = {"Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"}
        local periods = {"Season", "Month", "Week", "Day"}

        for _, house in ipairs(houses) do
            housePoints[house] = {}
            for _, period in ipairs(periods) do
                local statName = "Current_" .. period .. "_" .. house
                local statOk, statErr = pcall(function()
                    local statFName = FName(statName)
                    local exists = statsManager:StatExists(statFName)
                    if exists then
                        local points = statsManager:ReadStat(statFName)
                        housePoints[house][period:lower()] = points or 0
                        hasData = true
                    end
                end)
                if not statOk then
                    DevPrint("[HousePoints] Error reading " .. statName .. ": " .. tostring(statErr))
                end
            end
        end
    end)

    if not ok then
        DevPrint("[HousePoints] Refresh error: " .. tostring(err))
        return false
    end

    -- Update cache
    _G.CachedHousePoints.data = hasData and housePoints or nil
    _G.CachedHousePoints.lastRefresh = os.clock()

    if hasData then
        -- Log actual values for debugging
        local summary = {}
        for house, data in pairs(housePoints) do
            table.insert(summary, string.format("%s=%d", house:sub(1,4), data.season or 0))
        end
        DevPrint("[HousePoints] Cache refreshed: " .. table.concat(summary, ", "))
        -- Send to Python for live display updates
        if _G.SocketClient and _G.SocketClient.isConnected() then
            _G.SocketClient.send({
                type = "house_points_data",
                points = housePoints
            })
        end
    else
        DevPrint("[HousePoints] No stats found (mod may not be installed)")
    end

    return hasData
end

-- Get cached house points (for context injection)
-- Returns nil if no data cached
function GetCachedHousePoints()
    return _G.CachedHousePoints.data
end

-- Collect all game context and send to Python server
function WriteGameContext()
    local context = {
        playerName = "Unknown",
        playerHouse = "Unknown",
        hour = 12,
        minute = 0,
        timePeriod = "Day",
        isDay = true,
        timeFormatted = "12:00 PM",
        dateFormatted = "1890",
        location = "Hogwarts",
        wandEquipped = false,
        nearbyNpcs = {},  -- List of nearby NPCs with distances
        lookedAtNpcName = nil,  -- Name of NPC player is looking at
    }

    -- Player info from global state (set by Blueprint via setplayerinfo event)
    local state = _G.SonorusState or {}
    context.playerName = state.playerName or "Unknown"
    context.playerHouse = state.playerHouse or "Unknown"
    context.playerLoaded = state.playerLoaded or false

    if ENABLE_GAME_CONTEXT then
        -- NOTE: Caller (socket_client request_context) already wraps in ExecuteInGameThread
        -- so we execute directly here - no nested ExecuteInGameThread needed

        -- Time (Scheduler)
        pcall(function()
            local time = GetTimeOfDay()
            context.hour = time.hour
            context.minute = time.minute
            context.timePeriod = time.period
            context.isDay = time.isDay
            context.timeFormatted = time.formatted
            context.dateFormatted = time.dateFormatted
        end)

        -- Location (from player path)
        pcall(function()
            context.location = GetCurrentLocation()
        end)

        -- Gear (equipment info for LLM context)
        pcall(function()
            local gear = GetPlayerGear()
            if gear then
                context.hoodUp = gear.HOOD and gear.HOOD.up or false
                -- Full gear context string for LLM (pass gear to avoid second GetPlayerGear call)
                context.playerGear = FormatPlayerGearForContext(gear)
            end
        end)

        -- Player voice ID (for TTS)
        pcall(function()
            context.playerVoiceId = GetPlayerVoiceId()
        end)

        -- Scan for nearby NPCs (uses reactive cache)
        pcall(function()
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                local nearbyNpcsForContext = {}
                local seenNames = {}
                for _, entry in ipairs(npcResult.nearbyList) do
                    table.insert(nearbyNpcsForContext, {
                        name = entry.name,
                        distance = math.floor(entry.distance),
                        isLookedAt = entry.isLookedAt,
                        onScreen = entry.onScreen
                    })
                    seenNames[entry.name:lower()] = true
                end

                -- When on broom, check if companion is within extended range but outside normal range
                -- Only add companion specifically - don't extend range for other NPCs
                if _G.BroomState and _G.BroomState.mounted then
                    local _, companionId = Utils.GetCompanionNameAndId()
                    if companionId and not seenNames[companionId:lower()] then
                        -- Companion not in normal range, check extended broom range
                        local companionDist = Utils.GetCompanionDistance()
                        if companionDist and companionDist <= 10000 then
                            table.insert(nearbyNpcsForContext, {
                                name = companionId,
                                distance = math.floor(companionDist),
                                isLookedAt = false,
                                onScreen = false
                            })
                        end
                    end
                end

                context.nearbyNpcs = nearbyNpcsForContext
                if npcResult.lookedAtNpc then
                    context.lookedAtNpcName = npcResult.lookedAtNpc.name
                end
            end
        end)

        -- Get current mission/quest info
        pcall(function()
            local mission = Utils.GetCurrentMission()
            if mission.questName ~= "" or mission.objective ~= "" then
                context.currentQuest = mission.questName
                context.questObjective = mission.objective
            end
        end)

        -- Get zone location from HUD (actual room name like "Transfiguration Courtyard")
        pcall(function()
            local zone = Utils.GetZoneLocation()
            if zone.location ~= "" then
                context.zoneLocation = zone.location

                -- Detect location transitions and record to dialogue history
                local lastLoc = _G.LastTrackedLocation
                if lastLoc ~= zone.location then
                    -- Only record if we had a previous location (skip initial load)
                    if lastLoc ~= nil and RecordLocationTransition then
                        RecordLocationTransition(zone.location)
                    end
                    _G.LastTrackedLocation = zone.location

                    -- Clear NPC cache on location change - fresh NPCs will be fetched on next access
                    Cache.ClearEntities("NPC")
                    DevPrint(string.format("[Sonorus] Location changed to %s - NPC cache cleared", zone.location))

                    -- Emit location:change event for CommitmentManager etc.
                    pcall(function() Events.emit("location:change", { location = zone.location }) end)

                    -- Update cached reverb on location change
                    pcall(function()
                        local reverb = GetCurrentReverb()
                        if reverb then
                            _G.CachedReverb = reverb
                            -- Send reverb update to Python for live audio adjustment
                            if SocketClient and SocketClient.send then
                                SocketClient.send({
                                    type = "reverb_update",
                                    auxBus = reverb.auxBus,
                                    sendLevel = reverb.sendLevel,
                                    zone = reverb.zone,
                                    priority = reverb.priority
                                })
                            end
                            DevPrint(string.format("[Sonorus] Reverb cached: %s (zone=%s)", reverb.auxBus, reverb.zone))
                        end
                    end)
                end
            end
        end)

        -- Add pause state for Python-side detection
        context.isGamePaused = Utils.IsGamePaused()

        -- Add player position (x/y/z) for vision agent movement detection
        local staticData = Cache.GetStaticData()
        pcall(function()
            local player = staticData and staticData.player
            if player then
                local loc = player:K2_GetActorLocation()
                if loc then
                    context.x = loc.X
                    context.y = loc.Y
                    context.z = loc.Z
                end
                -- Combat mode
                pcall(function()
                    context.inCombat = player.bInCombatMode or false
                end)
                -- Cinematic mode
                pcall(function()
                    context.inCinematic = player.InCinematic or false
                end)
                -- Stealth/Disillusionment mode
                pcall(function()
                    context.inStealth = player.InStealthMode or false
                end)
                -- Swimming
                pcall(function()
                    context.isSwimming = player:IsSwimming() or false
                end)
                -- Companion info (companion shares player's stealth state via Disillusionment)
                local companionInfo = Utils.GetCompanionInfo(staticData, context.isOnBroom, context.inStealth, IsCompanionOnBroom, GetNearbyNPCs)
                if companionInfo then
                    for k, v in pairs(companionInfo) do
                        context[k] = v
                    end
                end
            end
        end)

        -- Check if player is on broom (now tracked via hooks in main.lua)
        -- Fallback to GearScreen if hooks haven't fired yet
        if _G.BroomState then
            context.isOnBroom = _G.BroomState.mounted or false
        end

        -- ============================================
        -- Game Mods Data Collection (uses cached data)
        -- ============================================
        context.mods = {}

        -- House Points - use cached data (refreshed at specific trigger points)
        local cachedHP = GetCachedHousePoints()
        if cachedHP then
            context.mods.housePoints = {
                points = cachedHP
            }
        end

        -- Send via socket (already on game thread so all data is ready)
        if _G.SocketClient and _G.SocketClient.isConnected() then
            _G.SocketClient.send({
                type = "game_context",
                data = context
            })
        end
    end

    return context
end

-- ============================================
-- Selective Context Gathering
-- ============================================
-- Groups:
--   position: x, y, z, location
--   state: inCombat, inCinematic, inStealth, isSwimming, isOnBroom, isGamePaused, playerLoaded
--   time: hour, minute, timePeriod, timeFormatted, dateFormatted, isDay
--   player: playerName, playerHouse, playerVoiceId
--   gear: hoodUp, playerGear (EXPENSIVE)
--   npcs: nearbyNpcs, lookedAtNpcName (EXPENSIVE)
--   zone: zoneLocation
--   mission: currentQuest, questObjective
--   companion: hasCompanion, companionId, companionInStealth, companionIsSwimming, companionIsOnBroom
--   mods: mods.housePoints (uses cached data, cheap)

function WriteSelectiveContext(groups)
    local context = {}

    -- Build group lookup set for O(1) checks
    local groupSet = {}
    for _, g in ipairs(groups or {}) do
        groupSet[g] = true
    end

    -- Player object - needed by position, state, gear, companion
    -- Get from cache if any of those groups are requested
    local player = nil
    if groupSet["position"] or groupSet["state"] or groupSet["gear"] or groupSet["companion"] then
        local staticData = Cache.GetStaticData()
        player = staticData and staticData.player
    end

    -- GROUP: player (cheap - from cached Blueprint state)
    if groupSet["player"] then
        local state = _G.SonorusState or {}
        context.playerName = state.playerName or "Unknown"
        context.playerHouse = state.playerHouse or "Unknown"
        context.playerLoaded = state.playerLoaded or false
        pcall(function()
            context.playerVoiceId = GetPlayerVoiceId()
        end)
    end

    -- GROUP: state (cheap - cached bools + player properties)
    if groupSet["state"] then
        local state = _G.SonorusState or {}
        context.playerLoaded = state.playerLoaded or false
        -- Auto-correct: if player actor exists but ClientRestart never fired, fix it
        if not context.playerLoaded and Utils.SafeIsValid(player) then
            print("[Sonorus] playerLoaded was false but player actor is valid - auto-correcting")
            state.playerLoaded = true
            context.playerLoaded = true
        end
        context.isGamePaused = Utils.IsGamePaused()

        -- Broom state from cached global
        if _G.BroomState then
            context.isOnBroom = _G.BroomState.mounted or false
        end

        -- Player state properties
        if player then
            pcall(function() context.inCombat = player.bInCombatMode or false end)
            pcall(function() context.inCinematic = player.InCinematic or false end)
            pcall(function() context.inStealth = player.InStealthMode or false end)
            pcall(function() context.isSwimming = player:IsSwimming() or false end)
        end
    end

    -- GROUP: position (cheap - player location)
    if groupSet["position"] then
        if player then
            pcall(function()
                local loc = player:K2_GetActorLocation()
                if loc then
                    context.x = loc.X
                    context.y = loc.Y
                    context.z = loc.Z
                end
            end)
        end
        -- Broad location from player path
        pcall(function()
            context.location = GetCurrentLocation()
        end)
    end

    -- GROUP: time (medium - Scheduler calls)
    if groupSet["time"] then
        pcall(function()
            local time = GetTimeOfDay()
            context.hour = time.hour
            context.minute = time.minute
            context.year = time.year
            context.month = time.month
            context.day = time.dayOfMonth
            context.dayOfWeek = time.dayOfWeek
            context.timePeriod = time.period
            context.isDay = time.isDay
            context.gameTime = time.formatted         -- e.g., "7:45 AM"
            context.timeFormatted = time.formatted
            context.dateFormatted = time.dateFormatted
        end)
    end

    -- GROUP: zone (medium - HUD widget read)
    if groupSet["zone"] then
        pcall(function()
            local zone = Utils.GetZoneLocation()
            if zone.location ~= "" then
                context.zoneLocation = zone.location

                -- Track location transitions for dialogue history
                local lastLoc = _G.LastTrackedLocation
                if lastLoc ~= zone.location then
                    if lastLoc ~= nil and RecordLocationTransition then
                        RecordLocationTransition(zone.location)
                    end
                    _G.LastTrackedLocation = zone.location

                    -- Clear NPC cache on location change - fresh NPCs will be fetched on next access
                    Cache.ClearEntities("NPC")
                    DevPrint(string.format("[Sonorus] Location changed to %s - NPC cache cleared", zone.location))

                    -- Emit location:change event for CommitmentManager etc.
                    pcall(function() Events.emit("location:change", { location = zone.location }) end)
                end
            end
        end)
    end

    -- GROUP: mission (medium - HUD widget read)
    if groupSet["mission"] then
        pcall(function()
            local mission = Utils.GetCurrentMission()
            if mission.questName ~= "" or mission.objective ~= "" then
                context.currentQuest = mission.questName
                context.questObjective = mission.objective
            end
        end)
    end

    -- GROUP: gear (EXPENSIVE - GetPlayerGear with 6 slot iterations)
    if groupSet["gear"] then
        pcall(function()
            local gear = GetPlayerGear()
            if gear then
                context.hoodUp = gear.HOOD and gear.HOOD.up or false
                context.playerGear = FormatPlayerGearForContext(gear)
            end
        end)
    end

    -- GROUP: npcs (EXPENSIVE - iterates all cached NPCs)
    if groupSet["npcs"] then
        pcall(function()
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                local nearbyNpcsForContext = {}
                local seenNames = {}
                for _, entry in ipairs(npcResult.nearbyList) do
                    table.insert(nearbyNpcsForContext, {
                        name = entry.name,
                        distance = math.floor(entry.distance),
                        isLookedAt = entry.isLookedAt,
                        onScreen = entry.onScreen
                    })
                    seenNames[entry.name:lower()] = true
                end

                -- When on broom, check if companion is within extended range but outside normal range
                -- Only add companion specifically - don't extend range for other NPCs
                if _G.BroomState and _G.BroomState.mounted then
                    local _, companionId = Utils.GetCompanionNameAndId()
                    if companionId and not seenNames[companionId:lower()] then
                        -- Companion not in normal range, check extended broom range
                        local companionDist = Utils.GetCompanionDistance()
                        if companionDist and companionDist <= 10000 then
                            table.insert(nearbyNpcsForContext, {
                                name = companionId,
                                distance = math.floor(companionDist),
                                isLookedAt = false,
                                onScreen = false
                            })
                        end
                    end
                end

                context.nearbyNpcs = nearbyNpcsForContext
                if npcResult.lookedAtNpc then
                    context.lookedAtNpcName = npcResult.lookedAtNpc.name
                end
            end
        end)

        -- Include preview lock info for target selection
        -- This tells Python which NPC the player locked before typing (more reliable than isLookedAt)
        if _G.ChatPreviewLock and _G.ChatPreviewLock.npcName then
            context.previewLockedNpc = _G.ChatPreviewLock.npcName
            context.previewLockState = _G.ChatPreviewLock.state
        elseif _G.STTPreviewLock and _G.STTPreviewLock.npcName then
            context.previewLockedNpc = _G.STTPreviewLock.npcName
            context.previewLockState = _G.STTPreviewLock.state
        end
    end

    -- GROUP: vision (for vision LLM - line trace visibility checks on on-screen NPCs)
    -- Note: No broom extension here - vision is about what's visually on-screen, not conversation range
    if groupSet["vision"] then
        pcall(function()
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                -- Collect on-screen NPCs for visibility check
                local onScreenNpcs = {}
                for _, entry in ipairs(npcResult.nearbyList) do
                    if entry.onScreen then
                        table.insert(onScreenNpcs, entry)
                    end
                end

                -- Run line trace visibility checks
                local visibleNpcs = {}
                if #onScreenNpcs > 0 and CheckNPCVisibility then
                    local visibilityResults = CheckNPCVisibility(onScreenNpcs)
                    for _, entry in ipairs(onScreenNpcs) do
                        if visibilityResults[entry.name] then
                            table.insert(visibleNpcs, {
                                name = entry.name,
                                distance = math.floor(entry.distance)
                            })
                        end
                    end
                end
                context.visibleNpcs = visibleNpcs
            end
        end)
    end

    -- GROUP: companion (cheap - uses cached CompanionManager)
    if groupSet["companion"] then
        -- Get player stealth for companion (shares state)
        local isPlayerInStealth = false
        if player then
            pcall(function() isPlayerInStealth = player.InStealthMode or false end)
        end

        local staticData = Cache.GetStaticData()
        local companionInfo = Utils.GetCompanionInfo(staticData, context.isOnBroom, isPlayerInStealth, IsCompanionOnBroom, GetNearbyNPCs)
        if companionInfo then
            for k, v in pairs(companionInfo) do
                context[k] = v
            end
        end
    end

    -- GROUP: mods (cheap - uses cached house points data)
    if groupSet["mods"] then
        context.mods = {}
        local cachedHP = GetCachedHousePoints()
        if cachedHP then
            context.mods.housePoints = {
                points = cachedHP
            }
        end
    end

    -- Send via socket
    if _G.SocketClient and _G.SocketClient.isConnected() then
        _G.SocketClient.send({
            type = "game_context",
            data = context
        })
    end

    return context
end

-- Queue updates and conversation state now handled via socket (socket_client.lua)
-- See handleMessage() for queue_item and conversation_state handlers

-- Start playing the queue
function StartQueuePlayback()
    local pState = _G.PlaybackState
    if #pState.queue == 0 then
        print("[Sonorus] No items in queue to play")
        return false
    end

    pState.playing = true
    pState.currentIndex = 1
    pState.currentSegment = 1
    print(string.format("[Sonorus] Starting queue playback with %d items", #pState.queue))
    return true
end

-- Reset playback state
function ResetPlaybackState()
    _G.PlaybackState = {
        queue = {},
        currentIndex = 1,
        currentSegment = 1,
        playing = false,
        serverState = "idle",
    }
end

-- ============================================
-- UI
-- ============================================

-- Input mode switching to block game input during chat
-- Uses WidgetBlueprintLibrary static functions to properly disable game input
-- This blocks Blueprint mods that poll input via IsInputKeyDown/GetAsyncKeyState
_G.InputModeBlockingEnabled = false  -- Can be disabled in settings if causing issues

function SetInputModeUIOnly()
    if not _G.InputModeBlockingEnabled then return end
    pcall(function()
        local staticData = Cache and Cache.GetStaticData()
        local pc = staticData and staticData.playerController
        if not pc then
            pc = FindFirstOf("PlayerController")
        end
        if not pc or not Utils.SafeIsValid(pc) then
            print("[InputMode] No valid PlayerController")
            return
        end

        local lib = StaticFindObject("/Script/UMG.Default__WidgetBlueprintLibrary")
        if lib and lib:IsValid() then
            -- SetInputMode_UIOnlyEx(PlayerController, InWidgetToFocus, InMouseLockMode)
            -- InMouseLockMode: 0 = DoNotLock (keep mouse working normally)
            lib:SetInputMode_UIOnlyEx(pc, nil, 0)
            print("[InputMode] Set to UI Only (game input blocked)")
        else
            print("[InputMode] WidgetBlueprintLibrary not found")
        end
    end)
end

function SetInputModeGameOnly()
    if not _G.InputModeBlockingEnabled then return end
    pcall(function()
        local staticData = Cache and Cache.GetStaticData()
        local pc = staticData and staticData.playerController
        if not pc then
            pc = FindFirstOf("PlayerController")
        end
        if not pc or not Utils.SafeIsValid(pc) then
            print("[InputMode] No valid PlayerController")
            return
        end

        local lib = StaticFindObject("/Script/UMG.Default__WidgetBlueprintLibrary")
        if lib and lib:IsValid() then
            lib:SetInputMode_GameOnly(pc)
            print("[InputMode] Set to Game Only (normal input restored)")
        else
            print("[InputMode] WidgetBlueprintLibrary not found")
        end
    end)
end

-- Chat input display using subtitle system (updates in place, no flashing)
-- State: tracks if we have an active subtitle displayed
_G.ChatInputSubtitleActive = _G.ChatInputSubtitleActive or false

-- Process chat input state changes (called from unified loop)
function ProcessChatInput()
    local state = _G.ChatInputState
    if not state or not state.dirty then return end

    state.dirty = false  -- Clear dirty flag

    local active = state.active
    print("[ChatInput] Processing: active=" .. tostring(active))
    local text = state.text or ""
    -- Use "Prompt: " for director mode, "You: " for normal chat mode
    local prefix = (state.mode == "prompt") and "Prompt: " or "You: "
    local displayText = prefix .. text .. "|"

    -- Check if subtitle HUD exists (required for subtitles to display)
    local subtitleHUD = Cache.Get("UI_BP_Subtitle_HUD_C", function()
        return FindFirstOf("UI_BP_Subtitle_HUD_C")
    end)
    if not subtitleHUD then
        print("[ChatInput] WARN: Subtitle HUD not found")
        return
    end
    local hudValid = false
    pcall(function() hudValid = subtitleHUD:IsValid() end)
    if not hudValid then
        print("[ChatInput] WARN: Subtitle HUD invalid")
        return
    end

    local subtitles = Cache.Get("Subtitles", function()
        return FindFirstOf("Subtitles")
    end)
    if not subtitles then
        print("[ChatInput] WARN: Subtitles object not found")
        return
    end
    local valid = false
    pcall(function() valid = subtitles:IsValid() end)
    if not valid then
        print("[ChatInput] WARN: Subtitles object invalid")
        return
    end

    if active then
        -- Check if text changed (for updates vs fresh add)
        local textChanged = (_G.ChatInputLastText ~= text)
        _G.ChatInputLastText = text

        -- Skip subtitle display in VR (immersive mode)
        if _G.VROffset then return end

        local ok, err = pcall(function()
            -- Always Remove+Add to guarantee subtitle shows (handles stale state)
            subtitles:BPRemoveStandaloneSubtitle()
            subtitles:BPAddStandaloneSubtitle(displayText)
            print("[ChatInput] Subtitle set: " .. displayText)
        end)
        if not ok then
            print("[ChatInput] ERROR: " .. tostring(err))
        end
    else
        -- Chat closing - clear state and remove subtitle
        _G.ChatInputLastText = nil
        local ok, err = pcall(function()
            subtitles:BPRemoveStandaloneSubtitle()
        end)
        if not ok then
            print("[ChatInput] ERROR removing: " .. tostring(err))
        end
        print("[ChatInput] Closed")
    end
end

function ShowMessage(message)
    -- Skip subtitle display in VR (immersive mode)
    if _G.VROffset then return end

    -- Convert *emphasis* to <i>emphasis</i> for UE4 rich text
    message = string.gsub(message, "%*([^%*]+)%*", "<i>%1</i>")

    -- Bump generation counter to cancel any pending player_message auto-hide timers
    _G.SubtitleGen = (_G.SubtitleGen or 0) + 1

    -- Mode 3 approach: Check Subtitle_HUD exists first for consistent display
    local subtitleHUD = Cache.Get("UI_BP_Subtitle_HUD_C", function()
        return FindFirstOf("UI_BP_Subtitle_HUD_C")
    end)
    if subtitleHUD and subtitleHUD:IsValid() then
        local subtitles = Cache.Get("Subtitles", function()
            return FindFirstOf("Subtitles")
        end)
        if subtitles and subtitles:IsValid() then
            pcall(function()
                -- Clear any existing subtitle first to avoid stacking
                subtitles:BPRemoveStandaloneSubtitle()
                subtitles:BPAddStandaloneSubtitle(message)
            end)
            return
        end
    end

    -- Fallback to hint message if subtitle HUD unavailable
    ShowHint(message, 3600)
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

function HideMessage()
    local subtitles = Cache.Get("Subtitles", function()
        return FindFirstOf("Subtitles")
    end)
    if subtitles and subtitles:IsValid() then
        pcall(function()
            subtitles:BPRemoveStandaloneSubtitle()
        end)
    end
end

function UpdateMessage(message)
    -- Skip subtitle display in VR (immersive mode)
    if _G.VROffset then return end

    -- Convert *emphasis* to <i>emphasis</i> for UE4 rich text
    message = string.gsub(message, "%*([^%*]+)%*", "<i>%1</i>")

    local subtitles = Cache.Get("Subtitles", function()
        return FindFirstOf("Subtitles")
    end)
    if subtitles and subtitles:IsValid() then
        pcall(function()
            subtitles:BPUpdateStandaloneSubtitle(message)
        end)
    end
end

-- Show notification toast (top-left notification panel)
function ShowNotification(text)
    if not text or text == "" then return end

    -- Use cached HUD lookup
    local hud = Cache.Get("PhoenixHUD", function()
        return FindFirstOf("PhoenixHUD")
    end)

    if not hud then
        print("[Notification] No HUD found, dropping: " .. tostring(text))
        return
    end

    print("[Notification] Sending to AddNotification: \"" .. tostring(text) .. "\"")
    local ok, err = pcall(function()
        hud.HUDWidgetRef.TextNotificationPanel:AddNotification(text)
    end)
    if not ok then
        print("[Notification] AddNotification error: " .. tostring(err))
    end
end

-- ============================================
-- Lip Sync (delegated to Utils/LipSync.lua)
-- ============================================
_G.ResetNearbyNPCLips = LipSync.ResetNearbyNPCLips  -- Also in _G for module access

-- ============================================
-- Position Writing (for 3D audio)
-- ============================================
local _lastPositionWriteTime = 0
local _lastNoActorLogTurn = nil  -- Throttle "no actor" log to once per turn

function WritePositions()
    -- Throttle writes to every 100ms
    local now = os.clock()
    if (now - _lastPositionWriteTime) < 0.1 then return end
    _lastPositionWriteTime = now

    -- Get turn ID and NPC actor
    local turnId = _G.SonorusState and _G.SonorusState.currentTurnId
    if not turnId then return end

    local npc = _G.TurnActorCache and _G.TurnActorCache[turnId]

    -- Check if cached actor is still valid (may become invalid after location change)
    if npc and not SafeIsValid(npc) then
        -- Actor became invalid - try to re-find by speakerId
        local speakerId = nil
        local pState = _G.PlaybackState
        if pState and pState.queue then
            for _, item in ipairs(pState.queue) do
                if item.turnId == turnId then
                    speakerId = item.speakerId
                    break
                end
            end
        end

        if speakerId and speakerId ~= "player" and GetSpeakerActor then
            local newActor = GetSpeakerActor(speakerId)
            if newActor and SafeIsValid(newActor) then
                -- Found valid actor - update cache
                _G.TurnActorCache[turnId] = newActor
                npc = newActor
                print(string.format("[WritePos] Re-acquired actor for %s (location change recovery)", speakerId))
            else
                -- Could not re-find actor
                npc = nil
                print(string.format("[WritePos] Actor invalid and re-lookup failed for %s", tostring(speakerId)))
            end
        else
            npc = nil
        end
    end

    if not npc then
        if _lastNoActorLogTurn ~= turnId then
            _lastNoActorLogTurn = turnId
            print(string.format("[WritePos] No actor for turn %s", tostring(turnId)))
        end
        return
    end

    -- Use unified static cache (already refreshed by GetNearbyNPCs)
    local staticData = Cache.GetStaticData()
    local cam = staticData and staticData.cameraManager
    if not cam then return end

    -- Get positions - wrapped in pcall since objects can become invalid anytime
    local ok, camPos, camRot, npcPos = pcall(function()
        return cam:GetCameraLocation(), cam:GetCameraRotation(), npc:K2_GetActorLocation()
    end)

    if not ok or not camPos or not camRot or not npcPos then return end

    -- Send via socket
    if _G.SocketClient and _G.SocketClient.send then
        _G.SocketClient.send({
            type = "positions",
            camX = camPos.X,
            camY = camPos.Y,
            camZ = camPos.Z,
            camYaw = camRot.Yaw,
            camPitch = camRot.Pitch,
            npcX = npcPos.X,
            npcY = npcPos.Y,
            npcZ = npcPos.Z + 60  -- Head height offset (~60cm above center)
        })
    end
end

-- NPC class paths for spawn hooks
local NPC_CLASS_PATHS = {
    "/Script/Phoenix.NPC_Character",
    "/Game/Characters/NPC_Character",
}

-- Register NPC spawn hooks (idempotent - only registers once)
Cache.RegisterSpawnHook("NPC", NPC_CLASS_PATHS)

-- NOTE: RefreshStaticData, GetCachedNPCs, GetStaticCache are defined earlier
-- (exported to _G for LipSync module and other cross-module access)

-- ============================================
-- Significant NPC Check (synced from Python)
-- ============================================

--- Check if an NPC name is significant (has voice reference on Python side)
--- Uses data synced from Python via sync_significant_npcs message
--- @param name string The NPC's name (accepts both voice IDs and display names)
--- @return boolean isSignificant true if NPC is significant
function IsSignificantNPC(name)
    if not name or name == "" then
        return false
    end

    -- Player is always significant (check voice name variants)
    local lower = name:lower()
    if lower == "player" or lower == "playermale" or lower == "playerfemale" then
        return true
    end

    -- Player is always significant (check actual player display name)
    local playerName = _G.SonorusState and _G.SonorusState.playerName
    if playerName and playerName ~= "" and name == playerName then
        return true
    end

    -- Check blacklist prefixes (T3, MidRes, etc.)
    -- These prefixes apply to voice names, not display names, but check anyway
    local prefixes = _G.InsignificantPrefixes or {"t3", "midres"}
    for _, prefix in ipairs(prefixes) do
        if lower:sub(1, #prefix) == prefix then
            return false
        end
    end

    -- Check if in significant NPCs set (synced from Python)
    -- Set contains BOTH voice names AND display names
    local significant = _G.SignificantNPCs or {}
    if significant[name] or significant[lower] then
        return true
    end

    return false
end

-- Export globally
_G.IsSignificantNPC = IsSignificantNPC

-- ============================================
-- Get Nearby NPCs (single iteration, returns list + looked-at)
-- Returns: { nearbyList = [{name, distance, actor, isLookedAt}], lookedAtNpc = {name, actor, distance} or nil, playerInStealth = bool }
-- ============================================
-- GetNearbyNPCs - MUST be called from game thread (inside ExecuteInGameThread or hook)
-- Returns: { nearbyList = [{...}], lookedAtNpc = {...} or nil, playerInStealth = bool }
function GetNearbyNPCs(maxDistance, lookDotThreshold)
    maxDistance = maxDistance or 2000  -- ~20 meters default
    lookDotThreshold = lookDotThreshold or 0.9  -- How centered in view to count as "looked at"

    -- Use cached static objects
    local staticData = GetStaticCache()

    -- Wrap IsValid in pcall - stale cached references can crash
    local pc = staticData.playerController
    local pcValid = false
    if pc then pcall(function() pcValid = pc:IsValid() end) end
    if not pcValid then
        print("[GetNearbyNPCs] EMPTY: PlayerController invalid")
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = false }
    end

    local cam = staticData.cameraManager
    local camValid = false
    if cam then pcall(function() camValid = cam:IsValid() end) end
    if not camValid then
        print("[GetNearbyNPCs] EMPTY: CameraManager invalid")
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = false }
    end

    local camLoc, camRot, camFOV
    pcall(function()
        camLoc = cam:GetCameraLocation()
        camRot = cam:GetCameraRotation()
        camFOV = cam:GetFOVAngle()  -- Get camera field of view
    end)
    if not camLoc or not camRot then
        print("[GetNearbyNPCs] EMPTY: Camera location/rotation nil")
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = false }
    end

    -- Default FOV if not available (90 degrees is common for third-person)
    camFOV = camFOV or 90

    -- Calculate on-screen threshold: cos(FOV/2)
    -- Using slightly smaller angle (0.9x) to account for character width
    local onScreenThreshold = math.cos(math.rad(camFOV * 0.45))

    local playerFullName = staticData.playerFullName
    local lib = staticData.bpLibrary

    -- Check player stealth status (Disillusionment charm)
    local playerInStealth = false
    local player = staticData.player
    local playerLoc = nil
    if player then
        pcall(function() playerInStealth = player.InStealthMode or false end)
        pcall(function() playerLoc = player:K2_GetActorLocation() end)
    end

    -- Use reactive NPC cache (no FindAllOf after first load)
    local npcs = GetCachedNPCs()
    if not npcs or #npcs == 0 then
        local cacheReady = Cache.IsEntityCacheReady("NPC")
        print("[GetNearbyNPCs] EMPTY: NPC cache has 0 entries (initialized=" .. tostring(cacheReady) .. ")")
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = playerInStealth }
    end

    -- Calculate forward vector from camera rotation (+ VR headset offset if active)
    local vrOff = _G.VROffset
    local pitch = math.rad(camRot.Pitch + (vrOff and vrOff.pitch or 0))
    local yaw = math.rad(camRot.Yaw + (vrOff and vrOff.yaw or 0))
    local forward = {
        X = math.cos(pitch) * math.cos(yaw),
        Y = math.cos(pitch) * math.sin(yaw),
        Z = math.sin(pitch)
    }

    -- Gaze origin: VR = player head position, flat = camera position
    local gazeOrigin = camLoc
    if vrOff and playerLoc then
        local playerHalfHeight = 88
        pcall(function()
            local capsule = player.CapsuleComponent
            if capsule and capsule.CapsuleHalfHeight then
                playerHalfHeight = capsule.CapsuleHalfHeight
            end
        end)
        gazeOrigin = { X = playerLoc.X, Y = playerLoc.Y, Z = playerLoc.Z + playerHalfHeight * 2 }
    end

    local nearbyList = {}
    local lookedAtNpc = nil
    local bestDot = lookDotThreshold

    -- Tracking stats for logging
    local stats = {
        total = #npcs,
        invalid = 0,
        outOfRange = 0,
        insignificant = 0,
        inRange = 0,
        insignificantNames = {}  -- Track first few filtered names for debugging
    }

    -- Single iteration through all NPCs
    for _, npc in pairs(npcs) do
        -- Wrap validity check in pcall - corrupted references crash on :IsValid() call
        if SafeIsValid(npc) then
            local fullName = nil
            pcall(function() fullName = npc:GetFullName() end)
            if fullName and fullName ~= playerFullName then
                local npcLoc = nil
                pcall(function() npcLoc = npc:K2_GetActorLocation() end)
                if not npcLoc then goto continue end

                -- Distance from PLAYER to NPC (not camera - camera is behind player in 3rd person)
                local distOrigin = playerLoc or camLoc
                local dx = npcLoc.X - distOrigin.X
                local dy = npcLoc.Y - distOrigin.Y
                local dz = npcLoc.Z - distOrigin.Z
                local dist = math.sqrt(dx * dx + dy * dy + dz * dz)

                -- Vector from gaze origin to NPC (for looked-at/on-screen checks)
                -- VR: player head position, flat: camera position
                local toNpc = {
                    X = npcLoc.X - gazeOrigin.X,
                    Y = npcLoc.Y - gazeOrigin.Y,
                    Z = npcLoc.Z - gazeOrigin.Z
                }
                local camDist = math.sqrt(toNpc.X * toNpc.X + toNpc.Y * toNpc.Y + toNpc.Z * toNpc.Z)

                if dist > 0 and dist <= maxDistance then
                    -- Get NPC id
                    local npcId = Utils.GetActorVoiceId(npc, staticData) or "Unknown"

                    -- Filter out insignificant NPCs (generic students, townspeople, etc.)
                    -- Checks against display names synced from Python (from voice references)
                    if not IsSignificantNPC(npcId) then
                        stats.insignificant = stats.insignificant + 1
                        if #stats.insignificantNames < 5 then
                            table.insert(stats.insignificantNames, npcId)
                        end
                        goto continue
                    end

                    stats.inRange = stats.inRange + 1

                    -- Normalize direction vector (using camera distance for look direction)
                    toNpc.X = toNpc.X / camDist
                    toNpc.Y = toNpc.Y / camDist
                    toNpc.Z = toNpc.Z / camDist

                    -- Dot product with forward (1.0 = perfectly aligned with camera)
                    local dot = forward.X * toNpc.X + forward.Y * toNpc.Y + forward.Z * toNpc.Z

                    -- Check if NPC is within camera FOV (on screen)
                    local onScreen = dot > onScreenThreshold

                    -- Check if this is the best "looked at" candidate
                    local isLookedAt = false
                    if dot > bestDot then
                        bestDot = dot
                        lookedAtNpc = { name = npcId, actor = npc, distance = dist }
                        isLookedAt = true
                    end

                    -- Add to nearby list
                    table.insert(nearbyList, {
                        name = npcId,
                        distance = dist,
                        actor = npc,
                        isLookedAt = isLookedAt,
                        onScreen = onScreen
                    })
                else
                    stats.outOfRange = stats.outOfRange + 1
                end
            end
        else
            stats.invalid = stats.invalid + 1
        end
        ::continue::
    end

    -- Log NPC filtering stats
    local afterDedup = #nearbyList
    local insignificantStr = ""
    if #stats.insignificantNames > 0 then
        insignificantStr = " [" .. table.concat(stats.insignificantNames, ", ") .. (stats.insignificant > 5 and "..." or "") .. "]"
    end
    print(string.format("[GetNearbyNPCs] Cache:%d | InRange:%d | Insignificant:%d%s | OutOfRange:%d | Invalid:%d",
        stats.total, stats.inRange, stats.insignificant, insignificantStr, stats.outOfRange, stats.invalid))

    -- Sort by distance (closest first)
    table.sort(nearbyList, function(a, b) return a.distance < b.distance end)

    -- Deduplicate by name (keep closest instance of each NPC name)
    local seenNames = {}
    local dedupedList = {}
    for _, entry in ipairs(nearbyList) do
        local nameLower = entry.name:lower()
        if not seenNames[nameLower] then
            seenNames[nameLower] = true
            table.insert(dedupedList, entry)
        end
    end
    nearbyList = dedupedList

    -- Mark the looked-at NPC in the list (update isLookedAt flags)
    if lookedAtNpc then
        for _, entry in ipairs(nearbyList) do
            entry.isLookedAt = (entry.actor == lookedAtNpc.actor)
        end
    end

    -- Filter out NPCs behind walls using line traces
    if #nearbyList > 0 then
        local visibilityResults = CheckNPCVisibility(nearbyList)
        local visibleList = {}
        for _, entry in ipairs(nearbyList) do
            if visibilityResults[entry.name] then
                table.insert(visibleList, entry)
            else
                print(string.format("[Sonorus] NPC excluded (behind wall): %s (%.0fm)", entry.name, entry.distance / 100))
            end
        end
        nearbyList = visibleList

        -- Update lookedAtNpc if it was filtered out
        if lookedAtNpc and not visibilityResults[lookedAtNpc.name] then
            lookedAtNpc = nil
            -- Find new best looked-at from remaining visible NPCs
            for _, entry in ipairs(nearbyList) do
                if entry.isLookedAt then
                    lookedAtNpc = { name = entry.name, actor = entry.actor, distance = entry.distance }
                    break
                end
            end
        end
    end

    -- Log final result
    if #nearbyList == 0 and stats.inRange > 0 then
        print("[GetNearbyNPCs] WARNING: All " .. stats.inRange .. " in-range NPCs were filtered (check visibility)")
    elseif #nearbyList > 0 then
        local names = {}
        for i, entry in ipairs(nearbyList) do
            if i <= 5 then table.insert(names, entry.name) end
        end
        print("[GetNearbyNPCs] Result: " .. #nearbyList .. " NPCs [" .. table.concat(names, ", ") .. (#nearbyList > 5 and "..." or "") .. "]")
    end

    return { nearbyList = nearbyList, lookedAtNpc = lookedAtNpc, playerInStealth = playerInStealth }
end

-- Export globally for LipSync module
_G.GetNearbyNPCs = GetNearbyNPCs

-- Legacy wrapper for compatibility
function GetLookedAtNPC(minDot, maxDistance)
    local result = GetNearbyNPCs(maxDistance, minDot)
    if result.lookedAtNpc then
        return result.lookedAtNpc.actor, result.lookedAtNpc.name, result.lookedAtNpc.distance
    end
    return nil
end

-- ============================================
-- Line Trace Visibility Check (for Vision LLM)
-- Returns list of NPC names that are actually visible (not occluded)
-- ============================================
--- Check if NPCs are visible (not blocked by walls)
--- Uses multi-ray traces ignoring all characters to detect only world geometry
--- @param npcList table List of NPC data with .actor and .name
--- @return table Map of npcName -> boolean (true = visible, false = behind wall)
function CheckNPCVisibility(npcList)
    if not npcList or #npcList == 0 then
        return {}
    end

    local staticData = GetStaticCache()
    local KismetSystem = staticData and staticData.kismetSystem
    local KismetMath = staticData and staticData.kismetMath

    if not KismetSystem or not KismetMath then
        -- Return all as visible if we can't do traces
        local result = {}
        for _, npc in ipairs(npcList) do
            result[npc.name] = true
        end
        return result
    end

    -- Get player position (trace from above player's head)
    local player = staticData and staticData.player
    local KismetMathLib = KismetMath

    if not player then
        return {}
    end

    local playerLoc = nil
    pcall(function() playerLoc = player:K2_GetActorLocation() end)
    if not playerLoc then
        return {}
    end

    -- Get player capsule height
    local playerHalfHeight = 88
    pcall(function()
        local capsule = player.CapsuleComponent
        if capsule and capsule.CapsuleHalfHeight then
            playerHalfHeight = capsule.CapsuleHalfHeight
        end
    end)

    -- Trace origin: above player's head (full capsule height + 20 units)
    local traceStart = nil
    pcall(function()
        traceStart = KismetMathLib:MakeVector(playerLoc.X, playerLoc.Y, playerLoc.Z + playerHalfHeight * 2 + 20)
    end)
    if not traceStart then
        return {}
    end

    -- Build ignore list: player + ALL NPCs (so trace only hits world geometry)
    local ActorsToIgnore = {}
    if player then
        table.insert(ActorsToIgnore, player)
    end
    for _, npcData in ipairs(npcList) do
        if npcData.actor then
            table.insert(ActorsToIgnore, npcData.actor)
        end
    end

    -- Trace settings
    local ETraceTypeQuery_Visibility = 0
    local EDrawDebugTrace_None = 0
    local TraceColor = { R = 0, G = 0, B = 0, A = 0 }

    local visibilityResults = {}

    for _, npcData in ipairs(npcList) do
        local npcActor = npcData.actor
        local npcName = npcData.name

        if not npcActor then
            visibilityResults[npcName] = false
            goto continue
        end

        -- Get NPC base location
        local npcLoc = nil
        pcall(function() npcLoc = npcActor:K2_GetActorLocation() end)
        if not npcLoc then
            visibilityResults[npcName] = false
            goto continue
        end

        -- Get NPC height from CapsuleComponent (scales with NPC size)
        local halfHeight = 88  -- Default adult height
        pcall(function()
            local capsule = npcActor.CapsuleComponent
            if capsule and capsule.CapsuleHalfHeight then
                halfHeight = capsule.CapsuleHalfHeight
            end
        end)

        -- Three trace points: head, chest, low (for sitting NPCs)
        local traceOffsets = {
            halfHeight * 2 - 15,  -- Head height (standing)
            halfHeight,           -- Chest height (standing) / Head (sitting)
            50                    -- Low point (catches sitting torso)
        }

        local isVisible = false

        for _, zOffset in ipairs(traceOffsets) do
            local EndVector = nil
            pcall(function()
                EndVector = KismetMath:MakeVector(npcLoc.X, npcLoc.Y, npcLoc.Z + zOffset)
            end)

            if not EndVector then
                goto nextRay
            end

            local HitResult = {}
            local WasHit = false

            pcall(function()
                WasHit = KismetSystem:LineTraceSingle(
                    player or npcActor,      -- WorldContextObject
                    traceStart,               -- Start (above player's head)
                    EndVector,                -- End (NPC body point)
                    ETraceTypeQuery_Visibility,
                    false,                    -- bTraceComplex
                    ActorsToIgnore,           -- Ignore player + all NPCs
                    EDrawDebugTrace_None,
                    HitResult,
                    true,                     -- bIgnoreSelf
                    TraceColor,
                    TraceColor,
                    0.0
                )
            end)

            -- If trace didn't hit anything, line of sight is clear
            if not WasHit then
                isVisible = true
                break  -- One clear ray is enough
            end

            ::nextRay::
        end

        visibilityResults[npcName] = isVisible
        ::continue::
    end

    return visibilityResults
end

-- ============================================
-- Tick Handler
-- ============================================

-- Static wrapper functions to avoid creating new closures in hot loops
-- (creating closures at 20Hz corrupts UE4SS Lua registry -> PANIC crash)
local _AnimateLipsCallCount = 0
local function _AnimateLipsWrapper()
    _AnimateLipsCallCount = _AnimateLipsCallCount + 1
    -- Log every 100 calls (~5 seconds at 20Hz) to track if we're in the right place
    if _G.DevPrint and _AnimateLipsCallCount % 100 == 0 then
        _G.DevPrint("[DEBUG] AnimateLips call #" .. _AnimateLipsCallCount)
    end
    local ok, err = pcall(AnimateLips)
    if not ok then
        print("[Sonorus] AnimateLips error: " .. tostring(err))
    end
end

function OnTick()
    if not _G.SonorusState then return end

    -- NOTE: Socket updates, position writes, and context writes are now handled by
    -- the unified 100ms loop (runs always). OnTick only handles lipsync/conversation logic.

    -- Queue updates now come via socket (queue_item messages)
    local pState = _G.PlaybackState

    -- NEW: Use phase-based state machine (with legacy fallback)
    local phase = _G.SonorusState.phase or "idle"
    if phase == "idle" and not _G.SonorusState.active then return end

    -- Handle closing phase - keep loop running to smoothly close mouth
    if phase == "closing" or _G.SonorusState.closing then
        -- Check if closing completed (flag set by CloseLips on game thread)
        if _G.CloseLipsComplete then
            -- Queue items now arrive via socket - no need to poll

            -- Check if there are more queue items to play
            if pState.playing and pState.currentIndex < #pState.queue and not _G.SonorusState.pendingIdle then
                -- More items in queue - advance to next
                pState.currentIndex = pState.currentIndex + 1
                pState.currentSegment = 1

                local nextItem = pState.queue[pState.currentIndex]
                if nextItem then
                    -- Update current turn ID for the next item
                    if nextItem.turnId then
                        _G.SonorusState.currentTurnId = nextItem.turnId
                    end
                    print(string.format("[Sonorus] Advancing to queue item %d/%d: %s (turn=%s)",
                        pState.currentIndex, #pState.queue,
                        nextItem.speaker or "Unknown",
                        tostring(nextItem.turnId)))
                end

                -- Reset for next turn (phase will become "playing" on lipsync_start)
                _G.SonorusState.phase = "preparing"
                _G.SonorusState.closing = false
                _G.SonorusState.pendingIdle = false  -- Clear deferred idle (continuing conversation)
                _G.CloseLipsComplete = false
                _G.SonorusState.lipsyncStarted = false
                _G.SonorusState.messageShown = false
            else
                -- No more items in local queue - check if server is still processing
                if pState.serverState == "playing" then
                    -- Server still working on interjections, wait for new items via socket
                    local now = os.clock()
                    if not _G.LastInterjectionWaitPrint or (now - _G.LastInterjectionWaitPrint) > 2 then
                        print("[Sonorus] Waiting for server to finish interjection...")
                        _G.LastInterjectionWaitPrint = now
                    end
                    return
                end

                -- Check if preview lock is waiting for server response
                -- This handles the race condition where player turn finishes before
                -- the server sends "playing" state for the NPC response
                local previewLock = _G.ChatPreviewLock or _G.STTPreviewLock
                if previewLock and previewLock.state == "submitted" then
                    local now = os.clock()
                    if not _G.LastPreviewLockWaitPrint or (now - _G.LastPreviewLockWaitPrint) > 2 then
                        print("[Sonorus] Waiting for server response (preview lock: " .. tostring(previewLock.npcName) .. ")")
                        _G.LastPreviewLockWaitPrint = now
                    end
                    return
                end

                -- Queue truly complete - reset everything
                _G.SonorusState.phase = "idle"
                _G.SonorusState.currentTurnId = nil
                _G.SonorusState.active = false
                _G.SonorusState.closing = false
                _G.SonorusState.pendingIdle = false  -- Clear deferred idle flag
                _G.CloseLipsComplete = false
                _G.TurnActorCache = {}  -- Clear turn-based cache
                ClearSpeakerCache()     -- Clear legacy cache
                UnmuteAllSpeakers()
                LingerAllNPCs()         -- NPCs stay frozen ~10s before returning to schedule
                ResetPlaybackState()
                -- Hide subtitles now that closing is complete
                if HideMessage then
                    HideMessage()
                end
                -- Time dilation: Restore day/night rate
                if TimeDilation then
                    TimeDilation.OnConversationEnd()
                end
                print("[Sonorus] Ready for next conversation")
            end
        else
            -- Still closing - call CloseLips directly (already on game thread)
            CloseLips()
        end
        return
    end

    -- Handle playing phase - show subtitles and animate
    -- Check for "playing" phase OR legacy lipsyncStarted
    if (phase == "playing" or _G.SonorusState.lipsyncStarted) and not _G.SonorusState.messageShown then

        -- Activate queue playback if we have items (pushed via socket) and not already playing
        if not pState.playing and #pState.queue > 0 then
            _G.PlaybackState.currentIndex = 1
            _G.PlaybackState.currentSegment = 1
            _G.PlaybackState.playing = true
            pState = _G.PlaybackState  -- Update local reference
            print(string.format("[Sonorus] Queue playback activated with %d items", #pState.queue))
        end

        -- Get message from queue item using turn-based lookup
        local displayMessage = nil
        local currentItem = nil

        -- Try turn-based lookup first (new system)
        if _G.SonorusState.currentTurnId then
            for _, item in ipairs(pState.queue or {}) do
                if item.turnId == _G.SonorusState.currentTurnId then
                    currentItem = item
                    break
                end
            end
        end

        -- Fall back to index-based lookup (legacy)
        if not currentItem and pState.playing and pState.queue[pState.currentIndex] then
            currentItem = pState.queue[pState.currentIndex]
        end

        if currentItem then
            -- Streaming subtitles: defer to subtitle_update messages from Python
            -- Each sentence is shown individually as TTS plays it
            if currentItem.streamingSubtitles and not currentItem._subtitleReceived then
                -- Record when we first tried to show subtitle (for fallback timer)
                if not currentItem._subtitleDeferredAt then
                    currentItem._subtitleDeferredAt = os.clock()
                end
                -- Fallback: if no subtitle_update arrives within 500ms, show full text
                if os.clock() - currentItem._subtitleDeferredAt < 0.5 then
                    -- Still waiting for subtitle_update — skip showing full text
                    displayMessage = nil
                else
                    -- Timeout: show full text as fallback
                    print("[Sonorus] Streaming subtitle timeout, showing full text")
                    -- Fall through to legacy behavior below
                    currentItem.streamingSubtitles = false
                end
            end

            if not currentItem.streamingSubtitles or not currentItem._subtitleDeferredAt then
                -- Legacy behavior: show full text at once (ElevenLabs, non-streaming, or fallback)
                local npcName = GetDisplayName(currentItem.speaker or "NPC")
                local text = currentItem.full_text

                -- Get text from current segment if available
                if currentItem.segments and currentItem.segments[pState.currentSegment] then
                    text = currentItem.segments[pState.currentSegment].text or text
                end

                -- Strip bracketed text like [sighs], [laughing] only when using cloud TTS
                -- Keep brackets for "none", "pocket" (local TTS can't express emotions well)
                local displayText = text or ""
                local ttsProvider = (_G.TtsProvider or ""):lower()
                local keepBrackets = ttsProvider == "" or ttsProvider == "none"
                    or ttsProvider == "pocket" or ttsProvider == "pocket_onnx"
                if not keepBrackets then
                    displayText = string.gsub(displayText, "%[[^%]]*%]", "")  -- Remove [...] content
                end
                displayText = string.gsub(displayText, "%s+", " ")  -- Collapse multiple spaces to single
                displayText = string.gsub(displayText, "^%s+", "")  -- Trim leading
                displayText = string.gsub(displayText, "%s+$", "")  -- Trim trailing
                displayMessage = npcName .. ": " .. displayText
            end
        end

        if displayMessage then
            print("[Sonorus] Showing message: " .. displayMessage)
            ShowMessage(displayMessage)
            _G.SonorusState.messageShown = true
        end
    end

    -- Animate lips while playing (viseme data populated by socket_client)
    -- Socket triggers phase="closing" on lipsync_stop, handled at top of OnTick
    if (phase == "playing" or _G.SonorusState.lipsyncStarted) and phase ~= "closing" and not _G.SonorusState.closing then
        -- Viseme data now comes via socket - no need for LoadVisemes()
        -- DISABLE LIPSYNC FOR TESTING: set _G.DisableLipsync = true
        if not _G.DisableLipsync then
            -- Already on game thread via LoopInGameThreadWithDelay
            _AnimateLipsWrapper()
        end
        -- DISABLE 3D AUDIO FOR TESTING: set _G.Disable3DAudio = true
        if not _G.Disable3DAudio then
            WritePositions()
        end
    end
end

-- ============================================
-- Reset State
-- ============================================
function ResetState()
    print("[Sonorus] Resetting state...")

    -- First: Reset blendshapes on ALL nearby NPCs (fixes stuck lip sync)
    -- This runs first so users can use F8 as a general "fix broken NPCs" button
    ResetNearbyNPCLips()

    if not _G.SonorusState then return end

    -- Reset Lua state (no UObject access)
    ResetPlaybackState()
    _G.SonorusState.active = false
    _G.SonorusState.closing = false  -- Must reset or next conversation breaks
    _G.SonorusState.pendingIdle = false  -- Clear deferred idle flag
    _G.SonorusState.lipsyncStarted = false
    _G.SonorusState.messageShown = false
    _G.SonorusState.playerMessageShown = false
    _G.SonorusState.playerMessage = nil
    _G.CloseLipsComplete = false  -- Reset async flag
    ClearSpeakerCache()

    -- Signal server to reset via socket
    if _G.SocketClient and _G.SocketClient.send then
        _G.SocketClient.send({type = "reset"})
    end

    -- Clear muted speakers tracking
    if _G.SonorusState then
        _G.SonorusState.mutedSpeakers = {}
    end

    -- Unmute ALL nearby NPCs (safety net - not just tracked participants)
    UnmuteAllNearbyNPCs()

    -- Release all locked NPCs
    ReleaseAllNPCs()

    -- Close lips
    CloseLips()

    -- Time dilation: Restore day/night rate
    if TimeDilation then
        TimeDilation.OnConversationEnd()
    end

    print("[Sonorus] Reset complete")
end

-- ============================================
-- Dialogue Blocker Hook Handlers
-- ============================================
function OnDialoguePreHook(Context)
    -- Just log when dialogue is blocked, muting is handled elsewhere
    if _G.SonorusState and _G.SonorusState.active then
        print("[Sonorus] [PRE] Blocking native dialogue - conversation active")
    end
end

function OnDialoguePostHook(Context, ReturnValue)
    if _G.SonorusState and _G.SonorusState.active then
        print("[Sonorus] [POST] Dialogue function returned")

        local handle = nil
        local getSuccess = pcall(function()
            handle = ReturnValue:get()
        end)

        if getSuccess and handle then
            print("[Sonorus] Got dialogue handle: " .. tostring(handle))

            -- DISABLED: Relying on muting instead of stopping dialogue
            -- pcall(function()
            --     local statics = StaticFindObject("/Script/Phoenix.Default__AvaAudioGameplayStatics")
            --     if statics and statics:IsValid() then
            --         statics:StopDialogue(handle)
            --         print("[Sonorus] StopDialogue called")
            --     end
            -- end)
        else
            print("[Sonorus] Could not get dialogue handle")
        end
    end
end

-- ============================================
-- Dialogue Tracking - Hook Handlers
-- ============================================

-- Track current Sonorus conversation target (set by StartConversation)
_G.CurrentSonorusTarget = _G.CurrentSonorusTarget or nil

--- Process dialogue line data from SubtitleElement:InitAudioDialogueLineData hook
--- Context is the SubtitleElement, which has ElementAudioDialogueLineData struct
---
--- AudioDialogueLineData struct properties:
---   lineID (StrProperty) - Dialogue line ID (e.g. "DuncanHobhouse_10383")
---   LocDirectKey (StrProperty) - Localization key
---   DurationSeconds (FloatProperty) - Audio duration in seconds
---   DialogueHandle (IntProperty) - Handle for audio system
---   SpeakingActor (WeakObjectProperty) - Direct reference to the NPC actor speaking
---   VoiceName (StrProperty) - Voice/character name (e.g. "DuncanHobhouse")
---   bIsFromConversation (BoolProperty) - True if from native conversation system
---   bIsEmote (BoolProperty) - True if this is an emote
---   bForceSubtitle (BoolProperty) - Force show subtitle
---   bSuppressSubtitle (BoolProperty) - Set true to hide subtitle
---   bSuppressSubtitleCharacterName (BoolProperty) - Hide character name in subtitle
---   EmotionHint (EnumProperty) - Emotion of the line
---   SocialSemanticEmotionHint (ByteProperty) - Social emotion hint
---   AudioPriority (ByteProperty) - Audio priority level
---   bNonSpatialized (BoolProperty) - Non-spatialized (2D) audio
function ProcessInitDialogueData(Context, AudioDialogueLineData)
    -- Skip dialogue tracking when mod is disabled
    if not _G.SonorusModEnabled then return end

    local elem = nil
    pcall(function() elem = Context:get() end)

    if not elem then return end

    ExecuteInGameThreadWithDelay(50, function()
        local lineID, voiceName, duration = "", "", 0

        pcall(function()
            local data = elem.ElementAudioDialogueLineData
            if data then
                pcall(function()
                    local id = data.lineID
                    if id then lineID = id:ToString() or "" end
                end)
                pcall(function()
                    local vn = data.VoiceName
                    if vn then voiceName = vn:ToString() or "" end
                end)
                pcall(function()
                    duration = data.DurationSeconds or 0
                end)
            end
        end)

        if lineID ~= "" then
            _G.PendingDialogue[lineID] = {
                voiceName = voiceName,
                duration = duration,
                timestamp = os.time()
            }
            print(string.format("[Sonorus] Dialogue: %s (%s, %.1fs)", lineID, voiceName, duration))

            -- Block ambient lip sync if NPC is in AI conversation
            if IsNPCInConversation and IsNPCInConversation(voiceName) then
                local npcActor = _G.SpeakerActorCache and _G.SpeakerActorCache[voiceName]
                if npcActor and StopNPCDialogueLipSync then
                    StopNPCDialogueLipSync(npcActor)
                    print(string.format("[Sonorus] Blocked ambient lip sync for %s", voiceName))
                end
            end

            -- Lookup subtitle text
            local subtitleText = ""
            if GetSubtitleText then
                subtitleText = GetSubtitleText(lineID) or ""
            end
            if subtitleText ~= "" then
                print(string.format("[Sonorus] Subtitle: \"%s\"", subtitleText))
            end

            -- Use _G lookup to survive F11 reload (closure captures stale ref)
            if _G.RecordDialogueLine then
                _G.RecordDialogueLine(voiceName, lineID, duration, "", nil, nil)
            end

            -- Suppress native subtitle in VR (immersive mode)
            if false and _G.VROffset then
                pcall(function() elem:SetVisibility(1) end)  -- ESlateVisibility::Collapsed
            end
        end
    end)
end

-- Activity state helpers (for ambient dialog gating)
-- These check globals set by Python via socket (see socket_client.lua activity_state handler)
function IsPlayerIdle()
    return _G.PlayerIdleState == true
end
_G.IsPlayerIdle = IsPlayerIdle

function IsGameWindowActive()
    return _G.GameWindowForeground ~= false  -- Default to true if unset
end
_G.IsGameWindowActive = IsGameWindowActive

function RecordDialogueLine(voiceName, lineID, duration, subtitleText, speakingActor, targetName)
    -- Skip recording when game is paused/menu open
    if Utils.IsGamePaused() then
        return
    end

    -- Skip recording when game window is not active (minimized/tabbed out)
    if not IsGameWindowActive() then
        return
    end

    -- Skip recording when player is idle (AFK detection from Python)
    if IsPlayerIdle() then
        return
    end

    -- Check cinematic state and apply tracking settings from config
    local inCinematic = _G.CinematicState and _G.CinematicState.active or false
    if inCinematic then
        -- In cutscene: skip if track_cutscene is disabled
        if _G.TrackCutsceneDialogue == false then
            return
        end
    else
        -- Not in cutscene (ambient chatter): skip if track_ambient is disabled
        if _G.TrackAmbientDialogue == false then
            return
        end

        -- Skip all ambient dialogue when AI conversation is active
        local serverState = _G.PlaybackState and _G.PlaybackState.serverState
        if serverState and serverState ~= "idle" then
            return
        end

        -- Track companion callout state for later repeat checking (after we have text)
        -- Note: actual blocking is deferred until we have subtitle text
    end

    -- Check if this is a companion callout outside combat/broom (for repeat blocking)
    local isCompanionCallout = false
    local inCombat = _G.CombatState and _G.CombatState.active or false
    local onBroom = _G.BroomState and _G.BroomState.mounted or false
    if not inCinematic and not inCombat and not onBroom then
        isCompanionCallout = IsCompanion(speakingActor) or IsCompanion(voiceName)
    end

    local timestamp = os.time()

    local speakerName = "Unknown"
    if speakingActor and SafeIsValid(speakingActor) then
        local actorName = Utils.GetActorDisplayName(speakingActor)
        if actorName then
            speakerName = actorName
        end
    end

    -- Fallback to prettified voiceName if speaker is still Unknown
    if speakerName == "Unknown" and voiceName and voiceName ~= "" and voiceName ~= "Unknown" then
        speakerName = GetDisplayName(voiceName)
    end

    -- Skip logging ambient dialogue from NPCs currently in an AI conversation
    -- Check both voiceName and speakerName since either could match
    if IsNPCInConversation(voiceName) or IsNPCInConversation(speakerName) then
        -- print("[Sonorus] Skipping ambient dialogue from conversation participant: " .. (speakerName or voiceName))
        return
    end

    -- If no subtitle text provided, look it up from subtitles.json
    local text = subtitleText or ""
    if text == "" and lineID and lineID ~= "" and lineID ~= "Unknown" then
        text = GetSubtitleText(lineID)
        if text ~= "" then
            print(string.format("[Sonorus] Subtitle: \"%s\"", text))
        end
    end

    -- Skip player spell incantations (e.g., "<i>Revelio!</i>") - tracked separately via LogSpellCast
    if text and text:match("^<i>%a+!</i>$") then
        -- print("[Sonorus] Skipping player spell incantation: " .. text)
        return
    end

    -- Strip HTML tags like <i>, </i>, <b>, </b>
    if text and text ~= "" then
        text = text:gsub("<[^>]+>", "")
    end

    -- Skip logging if no text content
    if not text or text == "" then
        return
    end

    -- Block repeated companion callouts within time window (if enabled)
    -- _G.CompanionCalloutBlockMinutes: 0 = disabled, -1 = never repeat, >0 = block within N game minutes
    local blockMinutes = _G.CompanionCalloutBlockMinutes or 0
    if isCompanionCallout and blockMinutes ~= 0 then
        if IsCompanionCalloutBlocked(voiceName, text, blockMinutes) then
            -- Stop the dialogue lip sync if we have the actor
            if StopNPCDialogueLipSync and speakingActor then
                StopNPCDialogueLipSync(speakingActor)
            end
            -- Don't log to history
            return
        end
    end

    -- Get game time
    local gameTime = GetTimeOfDay()

    -- Check if this is the player speaking (compare against known player name)
    local isPlayer = false
    local playerName = _G.SonorusState and _G.SonorusState.playerName or ""
    if playerName ~= "" then
        -- Compare without spaces (voiceName is often "AdriValter" vs "Adri Valter")
        local playerNameNoSpace = playerName:gsub(" ", "")
        local voiceNameClean = (voiceName or ""):gsub(" ", "")
        local speakerNameClean = (speakerName or ""):gsub(" ", "")
        if voiceNameClean:lower() == playerNameNoSpace:lower() or
           speakerNameClean:lower() == playerNameNoSpace:lower() or
           voiceName == "Player" then
            isPlayer = true
        end
    end

    -- If this is the player, use their actual name for display
    if isPlayer and playerName ~= "" then
        speakerName = playerName
    end

    -- Get earshot witnesses (named NPCs within range, excluding speaker)
    local speakerVoiceId = isPlayer and "Player" or (voiceName or "Unknown")
    local earshot = GetEarshotWitnesses(speakerVoiceId)

    local entry = {
        timestamp = timestamp,
        gameTime = gameTime.formatted,
        gameDate = gameTime.dateShort or gameTime.dateFormatted,
        speaker = speakerName,
        voiceName = speakerVoiceId,
        lineID = lineID or "Unknown",
        text = text,
        duration = duration or 0,
        target = targetName or "Unknown",
        isAIResponse = false,
        isPlayer = isPlayer,
        type = inCinematic and "cutscene" or "chatter",  -- Cutscene dialogue vs ambient NPC chatter
        earshot = earshot,
    }

    -- Send to Python for persistence
    sendDialogueEntry(entry)

    if voiceName and voiceName ~= "" and voiceName ~= "Unknown" then
        _G.VoiceSamples[voiceName] = _G.VoiceSamples[voiceName] or {}

        local exists = false
        for _, sample in ipairs(_G.VoiceSamples[voiceName]) do
            if sample.lineID == lineID then
                exists = true
                break
            end
        end

        if not exists and lineID ~= "Unknown" then
            table.insert(_G.VoiceSamples[voiceName], {
                lineID = lineID or "Unknown",
                duration = duration or 0,
                text = subtitleText or ""
            })
        end
    end
end
_G.RecordDialogueLine = RecordDialogueLine

-- ============================================
-- Spell Event Recording
-- ============================================

-- Hook handler for SpellTool:Start - records spell casts to dialogue history
-- Called from main.lua hook
function OnSpellToolStart(Context)
    local spellTool = Context:get()
    if not spellTool then return end
    if not SafeIsValid(spellTool) then return end

    -- Skip during combat (use cached state)
    if _G.CombatState and _G.CombatState.active then return end

    -- Get the spell class name
    local spellClass = nil
    pcall(function()
        spellClass = spellTool:GetClass():GetFullName()
    end)

    -- Record the spell cast to dialogue history (checks earshot internally)
    if spellClass then
        RecordSpellCast(spellClass)
    end

    -- Dev mode: additional debug logging
    if not _G.SonorusDevMode then return end

    print("[Sonorus] === SPELL_CAST EVENT ===")
    print("[Sonorus] Spell: " .. (spellClass or "Unknown"))

    -- Iterate all ObjectProperty fields to find potential caster references
    local objectProps = {}
    pcall(function()
        local objClass = spellTool:GetClass()
        while objClass and objClass:IsValid() do
            objClass:ForEachProperty(function(prop)
                local propName = nil
                local propType = nil

                pcall(function() propName = prop:GetFName():ToString() end)
                pcall(function() propType = prop:GetClass():GetFName():ToString() end)

                -- Only log ObjectProperty types (potential actor references)
                if propName and propType == "ObjectProperty" then
                    local valName = "nil"
                    local valClass = ""
                    pcall(function()
                        local val = spellTool[propName]
                        if val and val:IsValid() then
                            valName = val:GetName()
                            valClass = val:GetClass():GetName()
                        end
                    end)
                    if valName ~= "nil" then
                        table.insert(objectProps, propName .. "=" .. valName .. " (" .. valClass .. ")")
                    end
                end
            end)
            objClass = objClass:GetSuperStruct()
        end
    end)

    -- Print found object properties
    if #objectProps > 0 then
        for _, prop in ipairs(objectProps) do
            print("[Sonorus]   " .. prop)
        end
    else
        print("[Sonorus]   No ObjectProperty refs found")
    end
end

-- ============================================
-- TEST: Death & Damage Hook Handlers
-- ============================================

-- Helper to dump all properties of an object using ForEachProperty
local function DumpObjectProperties(obj, label)
    if not obj then
        print("[Sonorus] " .. label .. ": nil")
        return
    end

    -- Unwrap if needed
    local unwrapped = obj
    pcall(function()
        if obj.get then unwrapped = obj:get() end
    end)

    if not unwrapped then
        print("[Sonorus] " .. label .. ": nil after :get()")
        return
    end

    -- Check validity
    if not SafeIsValid(unwrapped) then
        print("[Sonorus] " .. label .. ": invalid")
        return
    end

    -- Get basic info
    local name, className = "?", "?"
    pcall(function() name = unwrapped:GetName() end)
    pcall(function() className = unwrapped:GetClass():GetName() end)
    print(string.format("[Sonorus] %s: %s (%s)", label, name, className))

    -- Iterate all properties using ForEachProperty
    pcall(function()
        local objClass = unwrapped:GetClass()
        while objClass and objClass:IsValid() do
            objClass:ForEachProperty(function(prop)
                local propName = nil
                local propType = nil
                local propValue = "?"

                pcall(function() propName = prop:GetFName():ToString() end)
                pcall(function() propType = prop:GetClass():GetFName():ToString() end)

                -- Try to get value based on type
                if propName then
                    pcall(function()
                        local val = unwrapped[propName]
                        if val == nil then
                            propValue = "nil"
                        elseif propType == "ObjectProperty" or propType == "WeakObjectProperty" then
                            if val:IsValid() then
                                propValue = val:GetName()
                            else
                                propValue = "invalid"
                            end
                        elseif propType == "BoolProperty" then
                            propValue = val and "true" or "false"
                        elseif propType == "FloatProperty" or propType == "IntProperty" or propType == "ByteProperty" then
                            propValue = tostring(val)
                        elseif propType == "NameProperty" then
                            propValue = val:ToString()
                        elseif propType == "StrProperty" then
                            propValue = val:ToString()
                        else
                            propValue = "<" .. propType .. ">"
                        end
                    end)
                end

                if propName then
                    print(string.format("[Sonorus]   .%s (%s) = %s", propName, propType or "?", propValue))
                end
            end)
            objClass = objClass:GetSuperStruct()
        end
    end)
end

-- Combat hook handlers - delegated to Combat module
function OnNPCDied(Context)
    if not _G.SonorusModEnabled then return end
    Combat.OnNPCDied(Context)
end

function OnCompanionDamaged(Context, InActor, InInstigator, InDamage, InHit)
    if not _G.SonorusModEnabled then return end
    Combat.OnCompanionDamaged(Context, InActor, InInstigator, InDamage, InHit)
end

function OnEnemyDamaged(Context, InActor, InInstigator, InDamage, InHit)
    if not _G.SonorusModEnabled then return end
    Combat.OnEnemyDamaged(Context, InActor, InInstigator, InDamage, InHit)
end

-- Record a spell cast event to DialogueHistory
function RecordSpellCast(blueprintClassName)
    local timestamp = os.time()
    local gameTime = GetTimeOfDay()

    -- Get spell info from mappings
    local spellInfo = GetSpellInfo(blueprintClassName)
    local spellName = spellInfo and spellInfo.displayName or "Unknown Spell"
    local category = spellInfo and spellInfo.category or "Unknown"

    -- Get player name
    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
    end

    -- Get earshot witnesses (nearby named NPCs) - skip if no one around
    local earshot = GetEarshotWitnesses("Player")
    if not earshot or #earshot == 0 then return end

    -- Create spell event entry
    local entry = {
        timestamp = timestamp,
        gameTime = gameTime.formatted,
        gameDate = gameTime.dateShort or gameTime.dateFormatted,
        speaker = playerName,
        voiceName = "Player",
        lineID = "spell_" .. (spellInfo and spellInfo.name or "unknown"),
        text = "Cast " .. spellName,
        duration = 0,
        target = "Unknown",  -- Could be enhanced with target detection
        isAIResponse = false,
        isPlayer = true,
        type = "spell",  -- Spell cast event
        spellCategory = category,  -- Additional spell metadata
        earshot = earshot,
    }

    -- Send to Python for persistence
    sendDialogueEntry(entry)

    -- Log for debugging
    print(string.format("[Sonorus] Spell: %s cast %s (%s)",
        playerName, spellName, category))
end

-- Record a broom mount/dismount event to DialogueHistory
-- Called from the broom tracker hooks in main.lua
function RecordBroomEvent(broomAction)
    local timestamp = os.time()
    local gameTime = GetTimeOfDay()

    -- Get player name
    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
    end

    -- Create broom event entry
    local actionText = broomAction == "mounted" and "Mounted broom" or "Dismounted from broom"

    -- Get earshot witnesses (nearby named NPCs)
    local earshot = GetEarshotWitnesses("Player")

    local entry = {
        timestamp = timestamp,
        gameTime = gameTime.formatted,
        gameDate = gameTime.dateShort or gameTime.dateFormatted,
        speaker = playerName,
        voiceName = "Player",
        lineID = "broom_" .. broomAction,
        text = actionText,
        duration = 0,
        isAIResponse = false,
        isPlayer = true,
        type = "broom",  -- Broom event
        earshot = earshot,
    }

    -- Send to Python for persistence
    sendDialogueEntry(entry)

    -- Log for debugging
    print(string.format("[Sonorus] Broom: %s %s", playerName, actionText:lower()))
end

-- ============================================
-- Location Transition Recording
-- ============================================
-- Track the last recorded location for dedup (simple string, not full history)
_G.LastRecordedLocation = _G.LastRecordedLocation or nil

-- Record a location transition event to DialogueHistory
-- Called when the zone/location changes (detected in WriteGameContext)
function RecordLocationTransition(newLocation)
    if not newLocation or newLocation == "" then return end

    -- Simple dedup: skip if same as last recorded location
    if _G.LastRecordedLocation == newLocation then
        print(string.format("[Sonorus] Location: Skipping duplicate entry for %s", newLocation))
        return
    end

    -- Update last recorded location
    _G.LastRecordedLocation = newLocation

    local timestamp = os.time()
    local gameTime = GetTimeOfDay()

    -- Get player name
    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
    end

    -- Get earshot witnesses (nearby named NPCs)
    local earshot = GetEarshotWitnesses("Player")

    -- Get companion if actively following (not in forced wait from quest/puzzle)
    -- Also ensure companion is in earshot (wall trace can fail at door transitions)
    local companions = {}
    local compOk, compErr = pcall(function()
        local isFollowing, voiceId, displayName = Utils.IsCompanionActivelyFollowing()
        if isFollowing and displayName and voiceId then
            table.insert(companions, displayName)
            -- Companion is always in earshot for location transitions
            local found = false
            for _, w in ipairs(earshot) do
                if w == voiceId then found = true; break end
            end
            if not found then
                table.insert(earshot, voiceId)
            end
        end
    end)
    if not compOk then
        print(string.format("[Sonorus] Location: companion lookup failed: %s", tostring(compErr)))
    end

    -- Create location transition entry
    local entry = {
        timestamp = timestamp,
        gameTime = gameTime.formatted,
        gameDate = gameTime.dateShort or gameTime.dateFormatted,
        speaker = playerName,
        voiceName = "Player",
        lineID = "location_" .. newLocation:gsub("%s+", "_"):lower(),
        text = "Entered " .. newLocation,
        duration = 0,
        isAIResponse = false,
        isPlayer = true,
        type = "location",  -- Location transition event
        location = newLocation,  -- Store the raw location name
        earshot = earshot,
        companions = #companions > 0 and companions or nil,
    }

    -- Send to Python for persistence
    sendDialogueEntry(entry)

    -- Log for debugging
    local compStr = #companions > 0 and (" with " .. table.concat(companions, ", ")) or ""
    print(string.format("[Sonorus] Location: %s%s entered %s", playerName, compStr, newLocation))
end

-- ============================================
-- Server Heartbeat Monitor
-- ============================================
_G.ServerMonitor = _G.ServerMonitor or {
    lastRestartAttempt = 0,
    cooldown = 15,  -- seconds between restart attempts
    loopStarted = false,
}

function MonitorServerHeartbeat()
    local now = os.time()
    local monitor = _G.ServerMonitor

    -- Check if server heartbeat is stale
    if not IsServerAlive() and (now - monitor.lastRestartAttempt) >= monitor.cooldown then
        print("[Sonorus] Server heartbeat stale, restarting...")
        monitor.lastRestartAttempt = now
        StartServer()
    end
    return false  -- Continue loop
end

-- Start monitor loop only once (survives hot reload)
if not _G.ServerMonitor.loopStarted then
    _G.ServerMonitor.loopStarted = true
    _G.ServerMonitor.loopHandle = LoopInGameThreadWithDelay(5000, function()
        MonitorServerHeartbeat()
    end)
    print("[Sonorus] Server heartbeat monitor started (5s interval)")
end

-- Unified loop - handles socket updates and periodic game context
-- CRITICAL: This loop handles socket reconnection when not in conversation
-- Benefits: Single timer, consistent reconnection, no concurrency issues
-- Version-based loop management: old loops exit when version increments

-- Static wrappers for unified loop (avoid creating closures in hot loop)
local function _ProcessChatInputWrapper()
    local ok, err = pcall(ProcessChatInput)
    if not ok then
        print("[Sonorus] ProcessChatInput error: " .. tostring(err))
    end
end

-- Static wrapper for NPC lock check (runs every 1s when NPCs locked)
local function _NPCLockCheckWrapper()
    local ok, err = pcall(function()
        -- First check combat/broom
        local canLock, reason = CanLockNPCs()
        if not canLock then
            print("[NPCLock] Releasing NPCs: " .. tostring(reason))
            pcall(ReleaseAllNPCs)
            return
        end

        -- Check if any locked NPC needs to re-face their target (angle > 45 degrees)
        -- Collect NPCs that need re-facing first (can't modify table during iteration)
        -- Skip companions, static locks, and lingering NPCs (they're frozen in place)
        -- Snap-locked NPCs re-face via direct rotation (no release/re-lock cycle)
        local needsReface = {}
        local snapReface = {}
        for lockId, data in pairs(_G.LockedNPCs) do
            if data.locked and data.npc and data.targetActor
               and not data.isCompanionLock and not data.isStaticLock
               and not data.lingering then
                pcall(function()
                    -- Check target is still valid
                    if not data.targetActor:IsValid() then return end

                    local npcPos = data.npc:K2_GetActorLocation()
                    local npcRot = data.npc:K2_GetActorRotation()
                    local targetPos = data.targetActor:K2_GetActorLocation()

                    -- Direction to target
                    local toTargetX = targetPos.X - npcPos.X
                    local toTargetY = targetPos.Y - npcPos.Y
                    local dist = math.sqrt(toTargetX * toTargetX + toTargetY * toTargetY)
                    if dist < 1 then return end

                    -- Angle to target (degrees)
                    local angleToTarget = math.atan(toTargetY / toTargetX) * 180 / math.pi
                    if toTargetX < 0 then
                        angleToTarget = angleToTarget + 180
                    end

                    -- NPC's current yaw
                    local npcYaw = npcRot.Yaw or 0

                    -- Angle difference (normalize to -180 to 180)
                    local diff = angleToTarget - npcYaw
                    while diff > 180 do diff = diff - 360 end
                    while diff < -180 do diff = diff + 360 end

                    -- If angle > 45 degrees, mark for re-facing
                    if math.abs(diff) > 45 then
                        if data.isSnapLock then
                            -- Snap lock: direct rotation, no release/re-lock
                            table.insert(snapReface, { lockId = lockId, data = data })
                        else
                            table.insert(needsReface, {
                                lockId = lockId,
                                npc = data.npc,
                                target = data.targetActor,
                                angle = math.floor(diff)
                            })
                        end
                    end
                end)
            end
        end

        -- Snap re-face: rotate in place instantly (no release/re-lock cycle)
        for _, item in ipairs(snapReface) do
            NPCLock.SnapRefaceNPC(item.data)
            print("[NPCLock] Snap re-face (id=" .. item.lockId .. ")")
        end

        -- Normal re-face: release and re-lock (animated turn)
        for _, item in ipairs(needsReface) do
            print("[NPCLock] Re-facing NPC (angle=" .. item.angle .. ")")

            -- Check if this is a preview lock before releasing
            local wasPreviewLock = false
            local oldLockId = item.lockId
            if _G.LockedNPCs[oldLockId] and _G.LockedNPCs[oldLockId].isPreviewLock then
                wasPreviewLock = true
            end

            ReleaseNPC(item.lockId)
            local newLockId = LockNPCToTarget(item.npc, item.target)

            -- If it was a preview lock, update the preview lock reference with new lockId
            if wasPreviewLock and newLockId then
                -- Mark new lock as preview lock
                if _G.LockedNPCs[newLockId] then
                    _G.LockedNPCs[newLockId].isPreviewLock = true
                end

                -- Update preview lock lockId references so they release the correct lock
                if _G.ChatPreviewLock and _G.ChatPreviewLock.lockId == oldLockId then
                    _G.ChatPreviewLock.lockId = newLockId
                    DevPrint("[NPCLock] Updated ChatPreviewLock lockId: " .. oldLockId .. " -> " .. newLockId)
                end
                if _G.STTPreviewLock and _G.STTPreviewLock.lockId == oldLockId then
                    _G.STTPreviewLock.lockId = newLockId
                    DevPrint("[NPCLock] Updated STTPreviewLock lockId: " .. oldLockId .. " -> " .. newLockId)
                end
            end
        end
    end)
    if not ok then
        print("[Sonorus] NPCLockCheck error: " .. tostring(err))
    end
    -- if _G.DevPrint then _G.DevPrint("[DEBUG] NPCLockCheck END") end
end

_G.UnifiedLoop = _G.UnifiedLoop or { version = 0, lastContextWrite = 0 }
_G.UnifiedLoop.interval = _G.UnifiedLoop.interval or 100  -- Default 100ms, configurable via config page

-- ============================================
-- Event Handlers (re-registered on each reload)
-- ============================================
-- Events.clear() is called above to prevent duplicate handlers on F11 reload

-- Cinematic: Stop active conversation when entering cinematic
Events.on("cinematic:start", function(data)
    print("[Sonorus] Cinematic started - stopping conversation")
    -- Tell Python to stop conversation immediately (with history trimming)
    -- Python's stop_conversation will send reset back to Lua, which calls ResetState
    pcall(function()
        SocketClient.send({ type = "interrupt_conversation", reason = "cinematic" })
    end)
    -- Send state update so Python knows we're in cinematic
    pcall(function() WriteSelectiveContext({"state"}) end)
end)

Events.on("cinematic:end", function(data)
    print("[Sonorus] Cinematic ended")
    pcall(function() WriteSelectiveContext({"state"}) end)
    -- Refresh house points after 2s (quests may award points on completion)
    ExecuteInGameThreadWithDelay(2000, function()
        RefreshHousePoints()
    end)
end)

-- Helper: Get companion if within combat range (no visibility requirement)
local function GetCompanionInRange(maxDistance)
    local companionId = nil
    pcall(function()
        local _, id = Utils.GetCompanionNameAndId()
        if not id then return end

        -- Check if companion is within range
        local staticData = Cache.GetStaticData()
        local player = staticData and staticData.player
        local companionMgr = staticData and staticData.companionManager
        if not player or not companionMgr then return end

        local companionPawn = companionMgr:GetPrimaryCompanionPawn()
        if not companionPawn or not Utils.SafeIsValid(companionPawn) then return end

        local playerLoc = player:K2_GetActorLocation()
        local companionLoc = companionPawn:K2_GetActorLocation()
        if not playerLoc or not companionLoc then return end

        local dx = playerLoc.X - companionLoc.X
        local dy = playerLoc.Y - companionLoc.Y
        local dz = playerLoc.Z - companionLoc.Z
        local dist = math.sqrt(dx*dx + dy*dy + dz*dz)

        if dist <= maxDistance then
            companionId = id
        end
    end)
    return companionId
end

-- Helper: Get combat witnesses (NPCs via visibility + companion via range only)
local function GetCombatWitnesses()
    local witnesses = {}
    local witnessSet = {}  -- For deduplication

    -- Get NPCs within combat earshot (3x normal, with visibility)
    local npcWitnesses = GetEarshotWitnesses("Player", 3000)
    for _, id in ipairs(npcWitnesses) do
        if not witnessSet[id] then
            witnessSet[id] = true
            table.insert(witnesses, id)
        end
    end

    -- Add companion if in range (no visibility requirement - they might be behind player)
    local companionId = GetCompanionInRange(3000)
    if companionId and not witnessSet[companionId] then
        witnessSet[companionId] = true
        table.insert(witnesses, companionId)
    end

    return witnesses, witnessSet
end

-- Combat: Track combat stats and create summary entry on end
Events.on("combat:start", function(data)
    print("[Sonorus] Combat started")

    -- Check if this should merge with previous combat (within 60s)
    local isMerge = Combat.ShouldMergeWithPrevious()
    if isMerge then
        -- Merge: just reactivate tracking, keep existing stats and witnesses
        Combat.ReactivateTracking()
        print("[Combat] Merging with previous combat (within 60s window)")
    else
        -- New combat: reset stats (also clears startWitnesses)
        Combat.ResetStats()
        print("[Combat] New combat encounter started")
    end

    -- Capture witnesses at combat START
    local newWitnesses, newWitnessSet = GetCombatWitnesses()

    if isMerge and _G.CombatStats.startWitnesses then
        -- Merge new witnesses with existing ones (for continued combat)
        for _, id in ipairs(_G.CombatStats.startWitnesses) do
            if not newWitnessSet[id] then
                newWitnessSet[id] = true
                table.insert(newWitnesses, id)
            end
        end
    end

    _G.CombatStats.startWitnesses = newWitnesses
    print(string.format("[Combat] Start witnesses: %d%s", #newWitnesses, isMerge and " (merged)" or ""))

    pcall(function() WriteSelectiveContext({"state"}) end)
end)

Events.on("combat:end", function(data)
    print("[Sonorus] Combat ended")

    -- Mark combat as ended
    Combat.EndCombat()

    -- Create combat entry if there was any activity
    local stats = Combat.GetStats()
    local totalDamage = stats.playerDamage + stats.companionDamage
    local totalKills = stats.playerKills + stats.companionKills
    if totalDamage > 0 or totalKills > 0 then
        -- Get witnesses at combat END
        local endWitnesses, witnessSet = GetCombatWitnesses()

        -- Merge with witnesses from combat START (deduped)
        local startWitnesses = _G.CombatStats.startWitnesses or {}
        for _, id in ipairs(startWitnesses) do
            if not witnessSet[id] then
                witnessSet[id] = true
                table.insert(endWitnesses, id)
            end
        end

        local entry = Combat.CreateEntry(endWitnesses)
        -- Use direct socket send (bypass suppression since this IS the combat entry)
        pcall(function()
            SocketClient.send({
                type = "record_dialogue",
                entry = entry
            })
        end)
        print(string.format("[Combat] Summary: %s (witnesses: %d from start+end)", entry.text, #endWitnesses))
    else
        print("[Combat] No combat activity to record")
    end

    -- Clear start witnesses
    _G.CombatStats.startWitnesses = nil

    pcall(function() WriteSelectiveContext({"state"}) end)
end)

-- Broom: Release NPCs when mounting, log events
-- Note: setState("broom", true) emits broom:start, setState("broom", false) emits broom:end
Events.on("broom:start", function(data)
    print("[Sonorus] Player mounted broom")
    if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
    if RecordBroomEvent then pcall(function() RecordBroomEvent("mounted") end) end
end)

Events.on("broom:end", function(data)
    print("[Sonorus] Player dismounted broom")
    if RecordBroomEvent then pcall(function() RecordBroomEvent("dismounted") end) end

    -- Delay context update to let floo mod restore companion
    ExecuteWithDelay(500, function()
        pcall(function() WriteSelectiveContext({"companion"}) end)
    end)
end)

-- Stealth: Log state changes, send context updates
Events.on("stealth:start", function(data)
    print("[Sonorus] Player entered stealth/disillusionment")
    pcall(function() WriteSelectiveContext({"state"}) end)
end)

Events.on("stealth:end", function(data)
    print("[Sonorus] Player left stealth/disillusionment")
    pcall(function() WriteSelectiveContext({"state"}) end)
end)

-- Function to start/restart the unified loop with current interval
-- Called on init and when interval changes via config
function _G.StartUnifiedLoop(newInterval)
    -- Update interval if provided
    if newInterval then
        _G.UnifiedLoop.interval = newInterval
    end

    -- Cancel existing loop if running
    if _G.UnifiedLoop.handle and IsValidDelayedActionHandle(_G.UnifiedLoop.handle) then
        CancelDelayedAction(_G.UnifiedLoop.handle)
    end

    -- Increment version for logging
    _G.UnifiedLoop.version = (_G.UnifiedLoop.version or 0) + 1
    local myLoopVersion = _G.UnifiedLoop.version
    print("[Sonorus] Starting unified loop v" .. myLoopVersion .. " (" .. _G.UnifiedLoop.interval .. "ms)")

    -- LoopInGameThreadWithDelay runs ON game thread - UObject access is safe
    _G.UnifiedLoop.handle = LoopInGameThreadWithDelay(_G.UnifiedLoop.interval, function()
        local devMode = _G.SonorusDevMode
        local t0, t1, t2, t3, t4, t5

        if devMode then t0 = os.clock() end

        local now = os.clock()
        if devMode then t1 = os.clock() end

        -- Socket update EVERY tick - handles reconnection and message processing
        -- This is CRITICAL - socket must update frequently even when mod is disabled
        -- NOTE: Pure LuaSocket, no UObjects
        -- Skip if fast poll loop is active AND we're connected (it handles socket updates at 25ms)
        -- Always run here when disconnected so reconnection logic isn't blocked
        local fp = _G._FastPoll
        local fastPollActive = fp and fp.handle and os.clock() < fp.expiry
        if not fastPollActive or not (_G.SocketClient and _G.SocketClient.isConnected()) then
            if _G.SocketClient then
                pcall(_G.SocketClient.update)
            else
                print("No socket client!")
            end
        end
        if devMode then t2 = os.clock() end

        -- Skip all mod functionality when disabled (except socket communication above)
        if not _G.SonorusModEnabled then
            return  -- Early exit - only socket update runs when mod disabled
        end

        -- Process chat input display (already on game thread, call directly)
        if _G.ChatInputState and _G.ChatInputState.dirty then
            _ProcessChatInputWrapper()
        end
        if devMode then t3 = os.clock() end

        -- Send lightweight time + zone updates every 5 seconds (for config page display + location tracking)
        _G.UnifiedLoop.lastTimeUpdate = _G.UnifiedLoop.lastTimeUpdate or 0
        if (now - _G.UnifiedLoop.lastTimeUpdate) >= 5.0 then
            _G.UnifiedLoop.lastTimeUpdate = now
            pcall(RefreshTimeCache)
            pcall(function() WriteSelectiveContext({"time", "zone"}) end)
            pcall(function() Events.emit("timeUpdated") end)
        end
        if devMode then t4 = os.clock() end

        -- Broom state polling every 2 seconds (replaces ReceiveTick hooks)
        -- Already on game thread, UObject access is safe
        _G.UnifiedLoop.lastBroomCheck = _G.UnifiedLoop.lastBroomCheck or 0
        if (now - _G.UnifiedLoop.lastBroomCheck) >= 2.0 then
            _G.UnifiedLoop.lastBroomCheck = now
            local onBroom = false
            pcall(function()
                local staticData = GetStaticCache()
                local gearScreen = staticData.gearScreen
                if gearScreen then
                    onBroom = gearScreen:IsPlayerOnBroom() or false
                end
            end)
            -- Update state - events fire automatically on change
            -- Uses "broom" state with mount/dismount mapping to :start/:end
            if Events.setState("broom", onBroom) then
                -- Also update legacy _G.BroomState for compatibility
                _G.BroomState = _G.BroomState or {}
                _G.BroomState.mounted = onBroom
            end
        end

        -- Cinematic, combat, and stealth state polling every 1 second
        _G.UnifiedLoop.lastStateCheck = _G.UnifiedLoop.lastStateCheck or 0
        if (now - _G.UnifiedLoop.lastStateCheck) >= 1.0 then
            _G.UnifiedLoop.lastStateCheck = now

            local inCinematic = false
            local inCombat = false
            local inStealth = false
            pcall(function()
                local staticData = GetStaticCache()
                local player = staticData.player
                if player then
                    inCinematic = player.InCinematic or false
                    inCombat = player.bInCombatMode or false
                    inStealth = player.InStealthMode or false
                end
            end)

            -- Update states - events fire automatically on change
            -- Also update legacy _G.*State for compatibility with existing code
            if Events.setState("cinematic", inCinematic) then
                _G.CinematicState = _G.CinematicState or {}
                _G.CinematicState.active = inCinematic
            end

            if Events.setState("combat", inCombat) then
                _G.CombatState = _G.CombatState or {}
                _G.CombatState.active = inCombat
            end

            if Events.setState("stealth", inStealth) then
                _G.StealthState = _G.StealthState or {}
                _G.StealthState.active = inStealth
            end
        end

        -- Idle detection every 30 seconds (for ambient dialog gating)
        -- Tracks player position and sets _G.PlayerIdleState if no movement for idle_timeout_minutes
        _G.UnifiedLoop.lastIdleCheck = _G.UnifiedLoop.lastIdleCheck or 0
        if (now - _G.UnifiedLoop.lastIdleCheck) >= 30.0 then
            _G.UnifiedLoop.lastIdleCheck = now

            -- Initialize idle tracking state
            _G.IdleState = _G.IdleState or {
                lastPos = nil,
                lastMovementTime = os.time(),
                idleTimeoutMinutes = 20,  -- Default, could be made configurable
            }

            -- Get current player position
            local currentPos = nil
            pcall(function()
                local staticData = GetStaticCache()
                local player = staticData.player
                if player and player:IsValid() then
                    local loc = player:K2_GetActorLocation()
                    if loc then
                        currentPos = { x = loc.X, y = loc.Y, z = loc.Z }
                    end
                end
            end)

            if currentPos then
                -- Check for movement
                local moved = false
                if _G.IdleState.lastPos then
                    local dx = currentPos.x - _G.IdleState.lastPos.x
                    local dy = currentPos.y - _G.IdleState.lastPos.y
                    local dz = currentPos.z - _G.IdleState.lastPos.z
                    local dist = math.sqrt(dx*dx + dy*dy + dz*dz)
                    if dist > 50 then  -- Moved more than 50 units (~0.5m)
                        moved = true
                    end
                else
                    moved = true  -- First check, consider as movement
                end

                _G.IdleState.lastPos = currentPos

                if moved then
                    _G.IdleState.lastMovementTime = os.time()
                    if _G.PlayerIdleState then
                        print("[Sonorus] Movement detected - resuming ambient dialog recording")
                        _G.PlayerIdleState = false
                    end
                else
                    -- Check if idle timeout exceeded
                    local idleSeconds = os.time() - _G.IdleState.lastMovementTime
                    local timeoutSeconds = _G.IdleState.idleTimeoutMinutes * 60
                    if timeoutSeconds > 0 and idleSeconds > timeoutSeconds and not _G.PlayerIdleState then
                        print(string.format("[Sonorus] Player idle for %d minutes - pausing ambient dialog recording", _G.IdleState.idleTimeoutMinutes))
                        _G.PlayerIdleState = true
                    end
                end
            end
        end

        -- Check locked NPCs every 1 second: combat/broom release, angle refresh
        _G.UnifiedLoop.lastLockCheck = _G.UnifiedLoop.lastLockCheck or 0
        if next(_G.LockedNPCs) and (now - _G.UnifiedLoop.lastLockCheck) >= 1.0 then
            _G.UnifiedLoop.lastLockCheck = now
            -- Already on game thread, call directly
            _NPCLockCheckWrapper()
        end

        -- Preview lock timeout check every 1 second
        -- Release locks stuck in 'submitted' or 'processing' state for over 15 seconds
        _G.UnifiedLoop.lastPreviewLockCheck = _G.UnifiedLoop.lastPreviewLockCheck or 0
        if (now - _G.UnifiedLoop.lastPreviewLockCheck) >= 1.0 then
            _G.UnifiedLoop.lastPreviewLockCheck = now
            local PREVIEW_LOCK_TIMEOUT = 15  -- seconds

            -- Check ChatPreviewLock timeout
            if _G.ChatPreviewLock and _G.ChatPreviewLock.startTime then
                local elapsed = now - _G.ChatPreviewLock.startTime
                local lockState = _G.ChatPreviewLock.state
                -- Only timeout transitioning states (submitted), not typing state
                if lockState == "submitted" and elapsed > PREVIEW_LOCK_TIMEOUT then
                    print("[Chat] Preview lock timeout (" .. string.format("%.0f", elapsed) .. "s) - releasing")
                    if ReleaseNPC and _G.ChatPreviewLock.lockId then
                        pcall(function() ReleaseNPC(_G.ChatPreviewLock.lockId) end)
                    end
                    _G.ChatPreviewLock = nil
                end
            end

            -- Check STTPreviewLock timeout
            if _G.STTPreviewLock and _G.STTPreviewLock.startTime then
                local elapsed = now - _G.STTPreviewLock.startTime
                local lockState = _G.STTPreviewLock.state
                -- Only timeout transitioning states (processing), not speaking state
                if lockState == "processing" and elapsed > PREVIEW_LOCK_TIMEOUT then
                    print("[STT] Preview lock timeout (" .. string.format("%.0f", elapsed) .. "s) - releasing")
                    if ReleaseNPC and _G.STTPreviewLock.lockId then
                        pcall(function() ReleaseNPC(_G.STTPreviewLock.lockId) end)
                    end
                    _G.STTPreviewLock = nil
                end
            end
        end

        -- Time dilation: Check for day/night transitions every 5 seconds
        _G.UnifiedLoop.lastTimeDilationCheck = _G.UnifiedLoop.lastTimeDilationCheck or 0
        if TimeDilation and TimeDilation.IsActive() and (now - _G.UnifiedLoop.lastTimeDilationCheck) >= 5.0 then
            _G.UnifiedLoop.lastTimeDilationCheck = now
            TimeDilation.OnTick()
        end
        if devMode then t5 = os.clock() end

        -- Log timing when devMode enabled (times in ms)
        if true and devMode and t0 then
            local total = (t5 - t0) * 1000
            if total >= 100 then  -- Only log if tick took > 1ms
                print(string.format("[Perf] Tick: %.2fms (clock:%.2f socket:%.2f chat:%.2f context:%.2f rest:%.2f)",
                    total,
                    (t1 - t0) * 1000,
                    (t2 - t1) * 1000,
                    (t3 - t2) * 1000,
                    (t4 - t3) * 1000,
                    (t5 - t4) * 1000
                ))
            end
        end
    end)
end

-- Start the unified loop on init
_G.StartUnifiedLoop()

-- ============================================
-- Combat/Spell Event Hooks (TEST)
-- ============================================
-- These hooks capture spell casting, impacts, and combat events
-- Toggle with _G.EnableCombatHooks = true/false

_G.EnableCombatHooks = false  -- Testing combat hooks

-- Unregister previous hooks on reload (per CLAUDE.md hot reload pattern)
local combatHookPaths = {
    "/Script/Phoenix.SpellTool:OnMunitionImpact",
    "/Script/Phoenix.SpellTool:OnMunitionImpactDamage",
    "/Script/Phoenix.SpellTool:OnMunitionDestroyed",
}
for _, path in ipairs(combatHookPaths) do
    pcall(function() UnregisterHook(path) end)
end

-- Helper to safely get spell name from SpellTool (uses nested pcall per CLAUDE.md)
local function GetSpellName(spellTool)
    if not SafeIsValid(spellTool) then return "unknown" end

    local spellName = "unknown"
    pcall(function()
        local nameResult = spellTool:GetSpellType()
        if nameResult then
            pcall(function() spellName = nameResult:ToString() end)  -- Nested pcall required
        end
    end)
    return spellName
end

-- Helper to safely get actor name (uses nested pcall per CLAUDE.md)
local function GetActorName(actor)
    if not actor then return "nil" end
    if not SafeIsValid(actor) then return "invalid" end

    local name = "unknown"
    pcall(function()
        local fullName = nil
        pcall(function() fullName = actor:GetFullName() end)  -- Nested pcall
        if fullName then
            -- Extract just the last part of the path
            local lastSlash = fullName:match(".*/(.*)")
            if lastSlash then name = lastSlash else name = fullName end
        end
    end)
    return name
end

-- ============================================
-- Station Exit Hook (for GracefulStationExit detection)
-- ============================================
_G.StationExitWatchers = _G.StationExitWatchers or {}
if not _G.StationExitHookRegistered then
    local hookOk, hookErr = pcall(function()
        RegisterHook("/Script/Phoenix.NPC_Character:OnStationOnFinishedExit", function(Context)
            local actor = Context:get()
            if not actor then return end
            local fullName = nil
            pcall(function() fullName = actor:GetFullName() end)
            if not fullName then return end

            local watcher = _G.StationExitWatchers[fullName]
            if watcher then
                _G.StationExitWatchers[fullName] = nil
                local elapsed = os.clock() - watcher.startTime
                print(string.format("[StationExit] HOOK fired: %s (%.1fs)",
                    fullName:match("([^%.]+)$") or fullName, elapsed))
                if ShowHint then ShowHint(string.format("EXIT HOOK %.1fs", elapsed), 3) end
                if watcher.timeoutHandle then
                    pcall(function() CancelDelayedAction(watcher.timeoutHandle) end)
                end
                if watcher.callback then pcall(watcher.callback) end
            else
                -- Log ANY exit event for research (even if we're not watching this NPC)
                print(string.format("[StationExit] HOOK fired (unwatched): %s",
                    fullName:match("([^%.]+)$") or fullName))
            end
        end)
    end)
    if hookOk then
        _G.StationExitHookRegistered = true
        print("[StationExit] RegisterHook OK")
    else
        print("[StationExit] RegisterHook FAILED: " .. tostring(hookErr))
        _G.StationExitHookRegistered = "failed"
    end
end

-- ============================================
-- Combat Hooks (TEST)
-- ============================================

RegisterHook("/Script/Phoenix.SpellTool:OnMunitionImpact", function(self, MunitionInstance, MunitionImpactData)
    if not _G.EnableCombatHooks then return end
    pcall(function()
        local spellTool = self:get()
        local spellName = GetSpellName(spellTool)
        local munition = MunitionInstance:get()

        -- Dump munition actor properties once for discovery
        if munition and not _G.MunitionActorDumped then
            _G.MunitionActorDumped = true
            print("[Combat] Dumping Munition actor ObjectProperties:")
            pcall(function()
                local objClass = munition:GetClass()
                while objClass and objClass:IsValid() do
                    local className = nil
                    pcall(function() className = objClass:GetFName():ToString() end)
                    if className then print("  === " .. className .. " ===") end

                    objClass:ForEachProperty(function(prop)
                        local propName = nil
                        local propType = nil
                        pcall(function() propName = prop:GetFName():ToString() end)
                        pcall(function() propType = prop:GetClass():GetFName():ToString() end)
                        -- Only show ObjectProperty (actors/components)
                        if propName and propType == "ObjectProperty" then
                            print(string.format("    %s", propName))
                        end
                    end)

                    pcall(function() objClass = objClass:GetSuperStruct() end)
                end
            end)
        end

        -- Try to get target from munition or spell tool
        local targetName = "unknown"

        -- Try SubsonicProximityActor on munition
        if munition then
            pcall(function()
                if munition.SubsonicProximityActor then
                    targetName = GetActorName(munition.SubsonicProximityActor)
                end
            end)
        end

        -- Try GetActiveTarget() on SpellTool
        if targetName == "unknown" and spellTool then
            pcall(function()
                local activeTarget = spellTool:GetActiveTarget()
                if activeTarget then
                    targetName = GetActorName(activeTarget)
                end
            end)
        end

        print(string.format("[Combat] IMPACT: %s -> %s", spellName, targetName))
    end)
end)

-- ============================================
-- Voice Spell Casting System
-- ============================================
-- Detects spell names in speech input and triggers casting if unlocked
-- Based on SpellHotkeys mod approach by olegbl

-- SpellToolRecord paths (from F7 dump)
-- Format: /Game/Gameplay/ToolSet/Spells/<SpellName>/DA_<SpellName>SpellRecord.DA_<SpellName>SpellRecord
local SPELL_TOOL_RECORDS = {
    -- Control (Yellow)
    ArrestoMomentum = "/Game/Gameplay/ToolSet/Spells/ArrestoMomentum/DA_ArrestoMomentumSpellRecord.DA_ArrestoMomentumSpellRecord",
    Glacius = "/Game/Gameplay/ToolSet/Spells/Glacius/DA_GlaciusSpellRecord.DA_GlaciusSpellRecord",
    Levioso = "/Game/Gameplay/ToolSet/Spells/Levioso/DA_LeviosoSpellRecord.DA_LeviosoSpellRecord",
    Transformation = "/Game/Gameplay/ToolSet/Spells/Transformation/DA_TransformationSpellRecord.DA_TransformationSpellRecord",

    -- Force (Purple)
    Accio = "/Game/Gameplay/ToolSet/Spells/Accio/DA_AccioSpellRecord.DA_AccioSpellRecord",
    Depulso = "/Game/Gameplay/ToolSet/Spells/Depulso/DA_DepulsoSpellRecord.DA_DepulsoSpellRecord",
    Descendo = "/Game/Gameplay/ToolSet/Spells/Descendo/DA_DescendoSpellRecord.DA_DescendoSpellRecord",
    Flipendo = "/Game/Gameplay/ToolSet/Spells/Flipendo/DA_FlipendoSpellRecord.DA_FlipendoSpellRecord",

    -- Damage (Red)
    Confringo = "/Game/Gameplay/ToolSet/Spells/Confringo/DA_ConfringoSpellRecord.DA_ConfringoSpellRecord",
    Diffindo = "/Game/Gameplay/ToolSet/Spells/Diffindo/DA_DiffindoSpellRecord.DA_DiffindoSpellRecord",
    Expelliarmus = "/Game/Gameplay/ToolSet/Spells/Expelliarmus/DA_ExpelliarmusSpellRecord.DA_ExpelliarmusSpellRecord",
    Incendio = "/Game/Gameplay/ToolSet/Spells/Incendio/DA_IncendioSpellRecord.DA_IncendioSpellRecord",
    Expulso = "/Game/Gameplay/ToolSet/Spells/Expulso/DA_ExpulsoSpellRecord.DA_ExpulsoSpellRecord",

    -- Utility
    Disillusionment = "/Game/Gameplay/ToolSet/Spells/Disillusionment/DA_DisillusionmentSpellRecord.DA_DisillusionmentSpellRecord",
    Lumos = "/Game/Gameplay/ToolSet/Spells/Lumos/DA_LumosSpellRecord.DA_LumosSpellRecord",
    Reparo = "/Game/Gameplay/ToolSet/Spells/Reparo/DA_ReparoSpellRecord.DA_ReparoSpellRecord",
    WingardiumLeviosa = "/Game/Gameplay/ToolSet/Spells/Wingardium/DA_WingardiumSpellRecord.DA_WingardiumSpellRecord",
    Conjuration = "/Game/Gameplay/ToolSet/Spells/Conjuration/DA_ConjurationSpellRecord.DA_ConjurationSpellRecord",
    Vanishment = "/Game/Gameplay/ToolSet/Spells/Vanishment/DA_VanishmentSpellRecord.DA_VanishmentSpellRecord",

    -- Unforgivable Curses
    AvadaKedavra = "/Game/Gameplay/ToolSet/Spells/AvadaKedavra/DA_AvadaKedavraSpellRecord.DA_AvadaKedavraSpellRecord",
    Crucio = "/Game/Gameplay/ToolSet/Spells/Crucio/DA_CrucioSpellRecord.DA_CrucioSpellRecord",
    Imperio = "/Game/Gameplay/ToolSet/Spells/Imperious/DA_ImperiusSpellRecord.DA_ImperiusSpellRecord",

    -- Essential
    Revelio = "/Game/Gameplay/ToolSet/Spells/Revelio/DA_RevelioSpellRecord.DA_RevelioSpellRecord",
    Protego = "/Game/Gameplay/ToolSet/Spells/Protego/DA_ProtegoSpellRecord.DA_ProtegoSpellRecord",
    Stupefy = "/Game/Gameplay/ToolSet/Spells/Stupefy/DA_StupefySpellRecord.DA_StupefySpellRecord",
    PetrificusTotalus = "/Game/Gameplay/ToolSet/Spells/Petrificus/DA_PetrificusSpellRecord.DA_PetrificusSpellRecord",

    -- Other spells found
    Confundo = "/Game/Gameplay/ToolSet/Spells/Confundo/DA_ConfundoSpellRecord.DA_ConfundoSpellRecord",
    Oppugno = "/Game/Gameplay/ToolSet/Spells/Oppugno/DA_OppugnoSpellRecord.DA_OppugnoSpellRecord",
    Obliviate = "/Game/Gameplay/ToolSet/Spells/Obliviate/DA_ObliviateSpellRecord.DA_ObliviateSpellRecord",
    Episkey = "/Game/Gameplay/ToolSet/Spells/Episkey/DA_EpiskeySpellRecord.DA_EpiskeySpellRecord",
}

-- Spell index: normalized spoken name -> internal spell name
local SPELL_INDEX = {
    -- Control spells (Yellow)
    ["arresto momentum"] = "ArrestoMomentum",
    ["glacius"] = "Glacius",
    ["levioso"] = "Levioso",
    ["transformation"] = "Transformation",

    -- Force spells (Purple)
    ["accio"] = "Accio",
    ["depulso"] = "Depulso",
    ["descendo"] = "Descendo",
    ["flipendo"] = "Flipendo",

    -- Damage spells (Red)
    ["confringo"] = "Confringo",
    ["diffindo"] = "Diffindo",
    ["expelliarmus"] = "Expelliarmus",
    ["incendio"] = "Incendio",
    ["expulso"] = "Expulso",

    -- Utility spells
    ["disillusionment"] = "Disillusionment",
    ["lumos"] = "Lumos",
    ["reparo"] = "Reparo",
    ["wingardium leviosa"] = "WingardiumLeviosa",
    ["conjuration"] = "Conjuration",
    ["evanesco"] = "Vanishment",
    ["vanishment"] = "Vanishment",

    -- Unforgivable Curses
    ["avada kedavra"] = "AvadaKedavra",
    ["crucio"] = "Crucio",
    ["imperio"] = "Imperio",

    -- Essential spells
    ["revelio"] = "Revelio",
    ["protego"] = "Protego",
    ["stupefy"] = "Stupefy",
    ["petrificus totalus"] = "PetrificusTotalus",
    ["petrificus"] = "PetrificusTotalus",

    -- Other spells
    ["confundo"] = "Confundo",
    ["oppugno"] = "Oppugno",
    ["obliviate"] = "Obliviate",
    ["episkey"] = "Episkey",

    -- Common mispronunciations/alternatives
    ["stupify"] = "Stupefy",
    ["stupiphy"] = "Stupefy",
    ["expeliarmus"] = "Expelliarmus",
    ["avada cadavra"] = "AvadaKedavra",
    ["wingardium"] = "WingardiumLeviosa",
    ["leviosa"] = "Levioso",
    ["aresto momentum"] = "ArrestoMomentum",
    ["arresto"] = "ArrestoMomentum",
    ["nox"] = "Lumos",  -- Nox cancels Lumos (toggle)
    -- Note: Bombarda appears to be a talent upgrade for Confringo, not a separate spell
}

-- Normalize text for spell matching (lowercase, strip punctuation, trim whitespace)
local function NormalizeSpellText(text)
    if not text then return "" end
    return text:lower()
        :gsub("[%p]", "")           -- Strip all punctuation
        :gsub("^%s+", "")           -- Trim leading whitespace
        :gsub("%s+$", "")           -- Trim trailing whitespace
        :gsub("%s+", " ")           -- Normalize multiple spaces
end

-- Find spell name in text (returns internal spell name or nil)
-- Checks if text contains any known spell name
function DetectSpellInText(text)
    if not text or text == "" then return nil end

    local normalized = NormalizeSpellText(text)

    -- Check exact match first (just the spell name)
    if SPELL_INDEX[normalized] then
        return SPELL_INDEX[normalized], normalized
    end

    -- Check if text contains a spell name (longer names first to avoid partial matches)
    -- Sort keys by length descending
    local keys = {}
    for k in pairs(SPELL_INDEX) do
        table.insert(keys, k)
    end
    table.sort(keys, function(a, b) return #a > #b end)

    for _, spellName in ipairs(keys) do
        if normalized:find(spellName, 1, true) then
            return SPELL_INDEX[spellName], spellName
        end
    end

    return nil
end

-- Map internal spell names to SpellLockName format
-- Most are "Spell_" + name, but some have different casing/naming
local SPELL_LOCK_NAMES = {
    -- Exceptions with different naming
    ["AvadaKedavra"] = "Spell_Avadakedavra",  -- lowercase 'kedavra'
    ["WingardiumLeviosa"] = "Spell_Wingardium",  -- shortened name
    ["PetrificusTotalus"] = "Spell_Petrificus",  -- shortened name
    ["Vanishment"] = "Spell_Vanishment",
    ["Imperio"] = "Spell_Imperius",  -- different name
}

-- Check if a spell is unlocked using Blueprint bridge
_G.IsSpellUnlocked = function(internalSpellName)
    if not internalSpellName then return false end

    local mod = GetSonorusModActor()
    if not mod then return true end  -- Fail open if no Blueprint

    -- Look up SpellLockName, default to "Spell_" + internalName
    local spellLockName = SPELL_LOCK_NAMES[internalSpellName] or ("Spell_" .. internalSpellName)

    local out = {}
    local ok, err = pcall(function()
        mod:isspellunlocked(spellLockName, out)
    end)

    if ok then
        return out.Unlocked == true
    else
        print("[VoiceSpell] IsSpellUnlocked error: " .. tostring(err))
        return true  -- Fail open on error
    end
end

-- Attempt to cast a spell by name using WandTool (SpellHotkeys approach)
-- Returns true if cast successful, false if failed
function CastSpellByName(internalSpellName)
    if not internalSpellName then
        print("[VoiceSpell] No spell name provided")
        return false
    end

    -- Get the SpellToolRecord path
    local recordPath = SPELL_TOOL_RECORDS[internalSpellName]
    if not recordPath then
        print("[VoiceSpell] No SpellToolRecord path for: " .. internalSpellName)
        return false
    end

    print("[VoiceSpell] Casting: " .. internalSpellName)

    -- Find WandTool
    local wandTool = nil
    pcall(function()
        wandTool = FindFirstOf("WandTool")
    end)

    if not wandTool then
        print("[VoiceSpell] WandTool not found")
        return false
    end

    local wandValid = false
    pcall(function() wandValid = wandTool:IsValid() end)
    if not wandValid then
        print("[VoiceSpell] WandTool invalid")
        return false
    end

    -- Get SpellToolRecord via StaticFindObject
    local spellToolRecord = nil
    pcall(function()
        spellToolRecord = StaticFindObject(recordPath)
    end)

    if not spellToolRecord then
        print("[VoiceSpell] SpellToolRecord not found: " .. recordPath)
        return false
    end

    local recordValid = false
    pcall(function() recordValid = spellToolRecord:IsValid() end)
    if not recordValid then
        print("[VoiceSpell] SpellToolRecord invalid")
        return false
    end

    -- Skip IsSpellToolAvailable check - it only checks if spell is in hotkey bar
    -- We want to cast any unlocked spell regardless of hotkey bar

    -- Special handling for Lumos toggle (casting when active = cancel)
    if internalSpellName == "Lumos" then
        local spellTool = nil
        pcall(function()
            spellTool = wandTool:GetSpellTool(spellToolRecord)
        end)
        if spellTool then
            local lumosActive = false
            pcall(function() lumosActive = spellTool:IsLumosActive() end)
            if lumosActive then
                print("[VoiceSpell] Lumos active, cancelling (Nox)")
                pcall(function() wandTool:CancelCurrentSpell() end)
                return true
            end
        end
    end

    -- Direct cast: GetSpellTool + CastSpell(tool, true) — immediate, no delay
    local spellTool = nil
    pcall(function()
        spellTool = wandTool:GetSpellTool(spellToolRecord)
    end)

    local toolValid = false
    if spellTool then
        pcall(function() toolValid = spellTool:IsValid() end)
    end

    if toolValid then
        -- Activate spell tool first so wand has correct effects/animations
        pcall(function()
            wandTool:ActivateSpellTool(spellToolRecord, false)
        end)
        local castOk, castErr = pcall(function()
            wandTool:CastSpell(spellTool, true)
        end)
        if castOk then
            print("[VoiceSpell] Cast successful: " .. internalSpellName)
            return true
        else
            print("[VoiceSpell] CastSpell error: " .. tostring(castErr))
            return false
        end
    end

    -- Fallback: old Activate -> delay -> CastActive sequence
    -- (GetSpellTool can return invalid in some edge cases)
    print("[VoiceSpell] GetSpellTool invalid, falling back to delayed cast: " .. internalSpellName)
    if _G.PendingSpellCastHandle then
        pcall(function() CancelDelayedAction(_G.PendingSpellCastHandle) end)
        _G.PendingSpellCastHandle = nil
    end

    local activateOk, activateErr = pcall(function()
        wandTool:CancelCurrentSpell()
        wandTool:ActivateSpellTool(spellToolRecord, false)
    end)

    if not activateOk then
        print("[VoiceSpell] Activate error: " .. tostring(activateErr))
        return false
    end

    local capturedName = internalSpellName
    _G.PendingSpellCastHandle = ExecuteInGameThreadWithDelay(200, function()
        _G.PendingSpellCastHandle = nil
        local castOk, castErr = pcall(function()
            wandTool:CastActiveSpell()
        end)
        if castOk then
            print("[VoiceSpell] Cast successful (fallback): " .. capturedName)
        else
            print("[VoiceSpell] Cast error (fallback): " .. tostring(castErr))
        end
    end)

    return true
end

-- ============================================
-- Mod Initialization
-- ============================================

print("[Sonorus] logic.lua ready!")
