-- logic.lua - Reloadable logic (press F11 to reload)
print("[Sonorus] logic.lua starting...")

-- DevPrint is defined in main.lua as _G.DevPrint (survives hot reload)
-- _G.SonorusDevMode is also in main.lua - set to true to enable debug output

-- Clear module caches so they reload with logic.lua (F11)
-- Note: Cache.lua uses _G.CacheStore for data persistence, so clearing
-- the module only reloads code, not cached data
-- NOTE: package.loaded clearing alone is NOT sufficient - UE4SS caches file
-- contents at a lower level. Modules that need reliable hot reload use dofile().
local LOAD_PRESENCE_LEDGER_MODULES = false

_G.PresenceLedgerPhaseFlags = {
    scheduleDump = false,
    presenceWatcher = false,
}

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
package.loaded["Utils.FirstPerson"] = nil
package.loaded["Utils.PathNav"] = nil
package.loaded["Utils.TickScheduler"] = nil

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

-- Shared main-loop scheduler (single UE4SS timer for recurring Lua tasks)
local TickScheduler = require "Utils.TickScheduler"

if LOAD_PRESENCE_LEDGER_MODULES then
    _G.ScheduleDump = require "Utils.ScheduleDump"
    _G.PresenceWatcher = require "Utils.PresenceWatcher"
end

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
_G.AudioMute = AudioMute

-- NPC facial component helpers
local NPCFacial = require "Utils.NPCFacial"

-- Audio zone/reverb detection
local AudioZone = require "Utils.AudioZone"

-- Lip sync system
local LipSync = require "Utils.LipSync"

-- NPC attention lock system (dofile forces fresh file read on F11 - require uses stale cache)
local NPCLock = dofile(_G.SonorusScriptsPath .. "Utils/NPCLock.lua")
_G.NPCLockModule = NPCLock
local CompanionFollow = dofile(_G.SonorusScriptsPath .. "Utils/CompanionFollow.lua")
local FOLLOWING_COMPANION_EARSHOT_DISTANCE = 3000
local BROOM_COMPANION_EARSHOT_DISTANCE = 10000

-- Attention meter system
local AttentionMeter = require "Utils.AttentionMeter"

-- Player gear system
local PlayerGear = require "Utils.PlayerGear"

-- Combat tracking system
local Combat = require "Utils.Combat"

-- UE Helpers
local UEHelpers = require("UEHelpers")

-- Time dilation system
local TimeDilation = require "Utils.TimeDilation"

-- Facial emotes system (dofile forces fresh file read on F11 - require uses stale cache)
local Emotes = dofile(_G.SonorusScriptsPath .. "Utils/Emotes.lua")

-- First-person view camera system (helpers only - no auto-register)
local FirstPerson = require "Utils.FirstPerson"
_G.FirstPerson = FirstPerson  -- Expose for socket_client.lua

-- Location registry (mod key lookups, display names, schedule IDs)
-- dofile forces fresh file read on F11 so commitment_spots.json changes take effect
local LocationRegistry = dofile(_G.SonorusScriptsPath .. "Utils/LocationRegistry.lua")
_G.LocationRegistryModule = LocationRegistry

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

-- Local alias for the shared angle utility (used by NPC lock facing checks)
local GetAngleToTarget = Utils.GetAngleToTarget

-- Debug system (dofile forces fresh file read on F11 - require uses stale cache)
local Debug = dofile(_G.SonorusScriptsPath .. "Utils/Debug.lua")

-- Auto-expose module functions as globals (import *)
expose(BlueprintHelpers, AudioZone, PlayerGear, NPCLock, NPCFacial, FileIO, AudioMute, LipSync, TimeDilation, Debug)

-- Safe IsValid check (from BlueprintHelpers)
local SafeIsValid = BlueprintHelpers.SafeIsValid

-- Clear event listeners on reload (states persist, handlers re-register below)
Events.clear()

-- Re-apply FPV if it was active before F11 reload (no-op on fresh start)
FirstPerson.onReload()

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
_G.CommitmentManager = dofile(_G.SonorusScriptsPath .. "Utils/CommitmentManager.lua")

-- Path navigation for guiding player to committed NPCs (dofile for hot reload)
_G.PathNav = dofile(_G.SonorusScriptsPath .. "Utils/PathNav.lua")

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
local function RefreshStaticData(data, force)
    if not force and not _G.SonorusState.playerLoaded then
        print("[Sonorus] Player not loaded, skipping RefreshStaticData")
        return
    end
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
    pcall(function() data.missionManager = FindFirstOf("MissionManager") end)

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

-- Initialize Emotes module
Emotes.init()

-- Export GetStaticCache and GetCachedNPCs globally (needed by LipSync, AudioMute, and other modules)
_G.GetStaticCache = GetStaticCache
_G.GetCachedNPCs = GetCachedNPCs
-- Force-refresh static cache, bypassing playerLoaded guard
_G.ForceRefreshStaticCache = function()
    Cache.GetStatic(function(data) RefreshStaticData(data, true) end, 0)
end

--- Check if player is in a cinematic state
--- @param player UObject|nil The Biped_Player actor
--- @return boolean
function IsInCinematicState(player)
    if player then
        local native = false
        pcall(function() native = player.InCinematic or false end)
        if native then return true end
    end
    return false
end

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
-- Player Voice ID Detection
-- ============================================
-- Returns "PlayerMale" or "PlayerFemale" based on character gender
-- Must be called from game thread (or inside ExecuteInGameThread)
-- Cached in _G.SonorusState.playerVoiceId; cleared in InvalidateWorld
function GetPlayerVoiceId()
    local state = _G.SonorusState or {}
    if state.playerVoiceId and state.playerVoiceId ~= "" then
        return state.playerVoiceId
    end

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

    state.playerVoiceId = voiceId
    return voiceId
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

    local blocklist = _G._AmbientBlocklist
    if count > 0 then
        print("[Sonorus] Unmuting " .. count .. " speakers")
        for speakerName, _ in pairs(mutedSpeakers) do
            -- Skip blocklist NPCs — they should stay muted
            if blocklist and blocklist[speakerName] then
                print("[Sonorus] Skipping unmute for blocklist NPC: " .. speakerName)
            else
                local actor = GetSpeakerActor(speakerName)
                if actor then
                    UnmuteNPCAudioByActor(actor)
                end
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
_G.Localization = _G.Localization or {}
_G.LocalizationLoaded = _G.LocalizationLoaded or false
-- Voice manifest: maps voice IDs to their reference info
_G.VoiceManifest = _G.VoiceManifest or {}
_G.VoiceManifestLoaded = _G.VoiceManifestLoaded or false
-- NPC ID normalization: lowercase -> proper case mapping (e.g., "neridaroberts" -> "NeridaRoberts")
-- Built from voice_manifest.json voice keys which have proper casing
_G.VoiceIdNormalize = _G.VoiceIdNormalize or {}
-- Companion callout history (deprecated — ambient blocklist system replaces this)

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

-- Get display name for a location internal ID (mod key or game localization key).
-- Delegates to LocationRegistry which uses location_registry.json + main_localization.json.
function GetLocationDisplayName(internalId)
    if not internalId or internalId == "" then return nil end
    return LocationRegistry.ResolveDisplayName(internalId)
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

    local uiManager = Cache.Get("UIManager", function() return FindFirstOf("UIManager") end)
    if not uiManager or not Utils.SafeIsValid(uiManager) then return info end

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
    -- print("[TimeCache] Refresh Time Cache")
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

-- Initialize Combat module with dependencies (must be after GetTimeOfDay and GetDisplayName are defined)
Combat.init({
    getTimeOfDay = GetTimeOfDay,
    getDisplayName = GetDisplayName,
})

-- Get current location using game systems
function GetCurrentLocation()
    local location = "Hogwarts"
    local detailedLocation = nil
    local detailedLocationId = nil

    -- Method 1: Try MapSubSystem.GetCurrentPlayerRegionInfo()
    local m1ok, m1err = pcall(function()
        local mapSubSystem = FindFirstOf("MapSubSystem")
        if mapSubSystem and mapSubSystem:IsValid() then
            local regionInfo = mapSubSystem:GetCurrentPlayerRegionInfo()
            if regionInfo then

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
                        -- Look up display name from location registry (try both original and cleaned)
                        local displayName = GetLocationDisplayName(internalId) or GetLocationDisplayName(cleanId)
                        if displayName then
                            -- print("[Location] Method 1b (" .. label .. "): displayName='" .. displayName .. "' (internalId='" .. internalId .. "')\n")
                            return displayName, cleanId
                        else
                            -- Fallback: clean up the ID as display name
                            local cleaned = cleanId:gsub("(%l)(%u)", "%1 %2"):gsub("_", " ")
                            -- print("[Location] Method 1b (" .. label .. "): fallback cleaned='" .. cleaned .. "' (internalId='" .. internalId .. "')\n")
                            return cleaned, cleanId
                        end
                    end
                    return nil
                end

                -- Try regions in order of specificity: SubRegion > InnerLevelRegion > LevelRegion > Region
                pcall(function()
                    -- SubRegion is often invalid, skip it for now to avoid crashes

                    -- Try InnerLevelRegion (e.g., "Hogwarts Castle")
                    if not detailedLocation then
                        detailedLocation, detailedLocationId = parseLocationFromActor(regionInfo.InnerLevelRegion, "InnerLevelRegion")
                    end

                    -- Try LevelRegion (e.g., "Hogwarts")
                    if not detailedLocation then
                        detailedLocation, detailedLocationId = parseLocationFromActor(regionInfo.LevelRegion, "LevelRegion")
                    end

                    -- Fall back to Region (broadest)
                    if not detailedLocation then
                        detailedLocation, detailedLocationId = parseLocationFromActor(regionInfo.Region, "Region")
                    end
                end)
            else
                print("[Location] Method 1: regionInfo is nil\n")
            end
        else
            print("[Location] Method 1: MapSubSystem not found or invalid\n")
        end
    end)
    if not m1ok then
        print("[Location] Method 1: ERROR - " .. tostring(m1err) .. "\n")
    end

    -- print("[Location] FINAL result: '" .. location .. "'\n")
    return detailedLocation or location, detailedLocationId
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

    if not context.playerLoaded then
        context.isGamePaused = true
        return context
    end
    
    -- Add pause state for Python-side detection
    context.isGamePaused = Utils.IsGamePaused()

    if not context.isGamePaused then
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

                -- Only extend range for the active companion, not all NPCs.
                -- This keeps conversations alive if they trail behind while walking,
                -- and preserves the larger broom range when flying together.
                local isFollowing, companionId = Utils.IsCompanionActivelyFollowing()
                if isFollowing and companionId and not seenNames[companionId:lower()] then
                    local companionDist = Utils.GetCompanionDistance()
                    local maxCompanionRange = (_G.MountState and _G.MountState.mounted)
                        and BROOM_COMPANION_EARSHOT_DISTANCE
                        or FOLLOWING_COMPANION_EARSHOT_DISTANCE
                    if companionDist and companionDist <= maxCompanionRange then
                        table.insert(nearbyNpcsForContext, {
                            name = companionId,
                            distance = math.floor(companionDist),
                            isLookedAt = false,
                            onScreen = false
                        })
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
                    pcall(function()
                        if SocketClient and SocketClient.send then
                            SocketClient.send({
                                type = "game_event",
                                event = "location:change",
                                data = {
                                    oldLocation = lastLoc or "",
                                    newLocation = zone.location
                                }
                            })
                        end
                    end)

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
                    context.inCinematic = IsInCinematicState(player)
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
                local companionInfo = Utils.GetCompanionInfo(staticData, context.isOnMount, context.inStealth, IsCompanionOnBroom, GetNearbyNPCs)
                if companionInfo then
                    for k, v in pairs(companionInfo) do
                        context[k] = v
                    end
                end
            end
        end)

        -- Check if player is on a mount (tracked via polling in unified loop)
        if _G.MountState then
            context.isOnMount = _G.MountState.mounted or false
            if context.isOnMount and _G.MountState.mountType then
                context.mountType = _G.MountState.mountType
            end
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

-- ============================================================
-- World Facts: cached mission statuses (invalidated on loading screen)
-- Probes MissionManager once per load, results cached until InvalidateWorld
-- ============================================================
_G._MissionStatusCache = _G._MissionStatusCache or nil

local function ProbeMissionStatuses(staticData)
    if _G._MissionStatusCache then return _G._MissionStatusCache end

    local mm = staticData and staticData.missionManager
    if not mm or not SafeIsValid(mm) then return nil end

    -- Quest chains to probe (must match sonorus/data/world_facts.json probe_chains)
    -- Each chain is sequential: if a quest isn't done, skip the rest of the chain
    local probeChains = {
        { "FGS_01", "EVZ_01" },
        { "CNF_01", "EVL_01", "EVL_02", "EVL_03" },
        { "HER_01", "HER_02", "HER_03" },
        { "AVM_02", "NTR_01", "NTR_02", "NTR_03" },
        { "GDW_01", "DMS_01" },
        { "COM_01" },
        { "COM_19" },
    }

    local statuses = {}
    for _, chain in ipairs(probeChains) do
        for _, id in ipairs(chain) do
            local status = nil
            pcall(function()
                local fname = FName(id, FNAME_Find)
                if fname and tostring(fname) ~= "None" then
                    status = mm:GetMissionStatusBP(fname)
                end
            end)
            if status and (status == 3 or status == 4) then
                statuses[id] = status
            else
                break
            end
        end
    end

    _G._MissionStatusCache = statuses
    return statuses
end

-- ============================================
-- Selective Context Gathering
-- ============================================
-- Groups:
--   position: x, y, z, location
--   state: inCombat, inCinematic, inStealth, isSwimming, isOnMount, mountType, isGamePaused, playerLoaded
--   time: hour, minute, timePeriod, timeFormatted, dateFormatted, isDay
--   player: playerName, playerHouse, playerVoiceId
--   gear: hoodUp, playerGear (EXPENSIVE)
--   npcs: nearbyNpcs, lookedAtNpcName (EXPENSIVE)
--   zone: zoneLocation
--   mission: currentQuest, questObjective
--   companion: hasCompanion, companionId, companionInStealth, companionIsSwimming, companionIsOnBroom
--   world_facts: missionStatuses (cached, cheap after first probe)
--   mods: mods.housePoints (uses cached data, cheap)

function WriteSelectiveContext(groups, params)
    local context = {}
    
    local state = _G.SonorusState or {}
    context.playerLoaded = state.playerLoaded or false
    context.isGamePaused = Utils.IsGamePaused()

    if (not context.playerLoaded or context.isGamePaused) then
        return context
    end

    -- Build group lookup set for O(1) checks
    local groupSet = {}
    for _, g in ipairs(groups or {}) do
        groupSet[g] = true
    end

    -- Player object + full name - needed by position, state, gear, companion
    -- Capture full name once (crash-safe) for native calls
    local player = nil
    local playerFullName = nil
    if groupSet["position"] or groupSet["state"] or groupSet["gear"] or groupSet["companion"] then
        local staticData = Cache.GetStaticData()
        player = staticData and staticData.player
        if player then
            pcall(function() playerFullName = player:GetFullName() end)
        end
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

    -- GROUP: state (cheap - cached bools + player properties via native C++)
    if groupSet["state"] then

        -- Mount state from cached global
        if _G.MountState then
            context.isOnMount = _G.MountState.mounted or false
            if context.isOnMount and _G.MountState.mountType then
                context.mountType = _G.MountState.mountType
            end
        end

        -- Player state via ModActor Blueprint cache.
        local ps = BlueprintHelpers.GetPlayerState()
        if ps then
            if not _G._playerDebugDone then
                _G._playerDebugDone = true
                local parts = {}
                for k, v in pairs(ps) do
                    table.insert(parts, k .. "=" .. tostring(v))
                end
                print("[DEBUG] GetPlayerState: " .. table.concat(parts, ", "))
            end
            context.inCombat = ps.inCombat
            context.inCinematic = ps.inCinematic
            context.inStealth = ps.inStealth
            context.isSwimming = ps.isSwimming
        elseif not _G._playerDebugDone then
            _G._playerDebugDone = true
            print("[DEBUG] GetPlayerState returned nil")
        end
    end

    -- GROUP: position (cheap - player location via native C++)
    if groupSet["position"] then
        local ps = BlueprintHelpers.GetPlayerState()
        if ps then
            context.x = ps.x
            context.y = ps.y
            context.z = ps.z
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
                    pcall(function()
                        if SocketClient and SocketClient.send then
                            SocketClient.send({
                                type = "game_event",
                                event = "location:change",
                                data = {
                                    oldLocation = lastLoc or "",
                                    newLocation = zone.location
                                }
                            })
                        end
                    end)
                end
            end
        end)
    end

    -- GROUP: mission (medium - HUD widget read)
    if groupSet["mission"] then
        DevPrint("[DEBUG] WriteSelectiveContext: mission START")
        pcall(function()
            local mission = Utils.GetCurrentMission()
            if mission.questName ~= "" or mission.objective ~= "" then
                context.currentQuest = mission.questName
                context.questObjective = mission.objective
            end
        end)
        DevPrint("[DEBUG] WriteSelectiveContext: mission END")
    end

    -- GROUP: gear (EXPENSIVE - GetPlayerGear with 6 slot iterations)
    if groupSet["gear"] then
        DevPrint("[DEBUG] WriteSelectiveContext: gear START")
        pcall(function()
            local gear = GetPlayerGear()
            if gear then
                context.hoodUp = gear.HOOD and gear.HOOD.up or false
                context.playerGear = FormatPlayerGearForContext(gear)
            end
        end)
        DevPrint("[DEBUG] WriteSelectiveContext: gear END")
    end

    -- GROUP: npcs (EXPENSIVE - iterates all cached NPCs)
    if groupSet["npcs"] then
        DevPrint("[DEBUG] WriteSelectiveContext: npcs START")
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

                -- Only extend range for the active companion, not all NPCs.
                -- This keeps conversations alive if they trail behind while walking,
                -- and preserves the larger broom range when flying together.
                local isFollowing, companionId = Utils.IsCompanionActivelyFollowing()
                if isFollowing and companionId and not seenNames[companionId:lower()] then
                    local companionDist = Utils.GetCompanionDistance()
                    local maxCompanionRange = (_G.MountState and _G.MountState.mounted)
                        and BROOM_COMPANION_EARSHOT_DISTANCE
                        or FOLLOWING_COMPANION_EARSHOT_DISTANCE
                    if companionDist and companionDist <= maxCompanionRange then
                        table.insert(nearbyNpcsForContext, {
                            name = companionId,
                            distance = math.floor(companionDist),
                            isLookedAt = false,
                            onScreen = false
                        })
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
        if _G.ChatPreviewLock and _G.ChatPreviewLock.npcName and not _G.ChatPreviewLock.excludeFromTargetSelection then
            context.previewLockedNpc = _G.ChatPreviewLock.npcName
            context.previewLockState = _G.ChatPreviewLock.state
        elseif _G.STTPreviewLock and _G.STTPreviewLock.npcName then
            context.previewLockedNpc = _G.STTPreviewLock.npcName
            context.previewLockState = _G.STTPreviewLock.state
        end
        DevPrint("[DEBUG] WriteSelectiveContext: npcs END")
    end

    -- GROUP: nearby_lean (CHEAP - distance only, no camera/LOS/screen)
    if groupSet["nearby_lean"] then
        pcall(function()
            local maxDist = (params and params.nearby_lean_distance) or 10000
            context.nearbyNpcs = Utils.ScanNearbyLean(maxDist)
        end)
    end

    -- GROUP: vision (for vision LLM - line trace visibility checks on on-screen NPCs)
    -- Note: No broom extension here - vision is about what's visually on-screen, not conversation range
    if groupSet["vision"] then
        DevPrint("[DEBUG] WriteSelectiveContext: vision START")
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
                    DevPrint("[DEBUG] WriteSelectiveContext: vision traces (" .. #onScreenNpcs .. " on-screen)")
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
        DevPrint("[DEBUG] WriteSelectiveContext: vision END")
    end

    -- GROUP: companion (via native C++ — crash-safe)
    if groupSet["companion"] then
        DevPrint("[DEBUG] WriteSelectiveContext: companion START")
        local ps = BlueprintHelpers.GetPlayerState()
        local companionInfo = BlueprintHelpers.GetCompanionInfo()
        if companionInfo then
            if not _G._companionDebugDone then
                _G._companionDebugDone = true
                local parts = {}
                for k, v in pairs(companionInfo) do
                    table.insert(parts, k .. "=" .. tostring(v))
                end
                print("[DEBUG] GetCompanionInfo: " .. table.concat(parts, ", "))
            end
            for k, v in pairs(companionInfo) do
                context[k] = v
            end
            context.companionInStealth = ps and ps.inStealth or false
            context.companionIsOnBroom =
                (context.isOnMount == true)
                and (context.hasCompanion == true)
                and (context.companionForcedWaiting ~= true)
                and (_G.FlooCompanionsInstalled == true)
        elseif not _G._companionDebugDone then
            _G._companionDebugDone = true
            print("[DEBUG] GetCompanionInfo returned nil")
        end

        -- Include NPC followers (pure Lua table, no UObject access)
        local followers = {}
        if _G.NPCFollowers then
            for voiceName, _ in pairs(_G.NPCFollowers) do
                table.insert(followers, voiceName)
            end
        end
        context.followers = followers
        DevPrint("[DEBUG] WriteSelectiveContext: companion END")
    end

    -- GROUP: world_facts (cached - probes MissionManager once per load)
    if groupSet["world_facts"] then
        local staticData = Cache.GetStaticData()
        if staticData then
            local statuses = ProbeMissionStatuses(staticData)
            if statuses then
                context.missionStatuses = statuses
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

_G.WriteSelectiveContext = WriteSelectiveContext

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

local function SubtitleDevPrint(...)
    if _G.DevPrint then
        _G.DevPrint("[Subtitle]", ...)
    end
end

local function SelectBestSubtitlesObject(subtitleHUD)
    local allObjs = {}
    pcall(function() allObjs = FindAllOf("Subtitles") or {} end)

    if #allObjs == 0 then
        return nil
    end

    local hudFullName = nil
    if subtitleHUD then
        pcall(function() hudFullName = subtitleHUD:GetFullName() end)
    end

    -- Best case: pick the subtitle widget that belongs to the resolved live HUD.
    if hudFullName and hudFullName ~= "" then
        for _, obj in ipairs(allObjs) do
            local objFullName = nil
            pcall(function() objFullName = obj:GetFullName() end)
            if objFullName and string.find(objFullName, hudFullName, 1, true) == 1 then
                return obj
            end
        end
    end

    -- Next best: prefer the live transient widget over the /Game blueprint template.
    for _, obj in ipairs(allObjs) do
        local objFullName = nil
        pcall(function() objFullName = obj:GetFullName() end)
        if objFullName and string.find(objFullName, "/Engine/Transient", 1, true) then
            return obj
        end
    end

    -- Last resort: keep the old behavior.
    return allObjs[1]
end

local function ResolveSubtitleObjects(sourceTag, requireHUD)
    SubtitleDevPrint("Resolve ENTER", tostring(sourceTag), "requireHUD=" .. tostring(requireHUD))
    local subtitleHUD = nil
    if requireHUD then
        SubtitleDevPrint("Resolve HUD Cache PRE", tostring(sourceTag))
        subtitleHUD = Cache.Get("UI_BP_Subtitle_HUD_C", function()
            return FindFirstOf("UI_BP_Subtitle_HUD_C")
        end)
        SubtitleDevPrint("Resolve HUD Cache POST", tostring(sourceTag), tostring(subtitleHUD))
        if not subtitleHUD then
            SubtitleDevPrint("Resolve no HUD", tostring(sourceTag))
            return nil, nil
        end

        local hudValid = false
        SubtitleDevPrint("Resolve HUD IsValid PRE", tostring(sourceTag), tostring(subtitleHUD))
        pcall(function() hudValid = subtitleHUD:IsValid() end)
        SubtitleDevPrint("Resolve HUD IsValid POST", tostring(sourceTag), tostring(hudValid))
        if not hudValid then
            SubtitleDevPrint("Resolve invalid HUD", tostring(sourceTag))
            return nil, nil
        end
    end

    SubtitleDevPrint("Resolve Subtitles Cache PRE", tostring(sourceTag))
    local subtitles = Cache.Get("Subtitles", function()
        return SelectBestSubtitlesObject(subtitleHUD)
    end, requireHUD and "UI_BP_Subtitle_HUD_C" or nil)
    SubtitleDevPrint("Resolve Subtitles Cache POST", tostring(sourceTag), tostring(subtitles))
    if not subtitles then
        SubtitleDevPrint("Resolve no Subtitles", tostring(sourceTag))
        return subtitleHUD, nil
    end

    local subtitlesValid = false
    SubtitleDevPrint("Resolve Subtitles IsValid PRE", tostring(sourceTag), tostring(subtitles))
    pcall(function() subtitlesValid = subtitles:IsValid() end)
    SubtitleDevPrint("Resolve Subtitles IsValid POST", tostring(sourceTag), tostring(subtitlesValid))
    if not subtitlesValid then
        SubtitleDevPrint("Resolve invalid Subtitles", tostring(sourceTag))
        return subtitleHUD, nil
    end

    SubtitleDevPrint("Resolve EXIT", tostring(sourceTag), tostring(subtitleHUD), tostring(subtitles))
    return subtitleHUD, subtitles
end

local function RunSubtitleAction(sourceTag, actionName, action, opts)
    opts = opts or {}

    SubtitleDevPrint("Action ENTER", tostring(sourceTag), tostring(actionName))
    local _, subtitles = ResolveSubtitleObjects(sourceTag, opts.requireHUD)
    if not subtitles then
        SubtitleDevPrint("Action no subtitles", tostring(sourceTag), tostring(actionName))
        return false
    end

    SubtitleDevPrint("Action CALL PRE", tostring(sourceTag), tostring(actionName), tostring(subtitles))
    local ok, err = pcall(action, subtitles)
    SubtitleDevPrint("Action CALL POST", tostring(sourceTag), tostring(actionName), tostring(ok), tostring(err))
    return ok, err
end

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

    if active then
        -- Check if text changed (for updates vs fresh add)
        local textChanged = (_G.ChatInputLastText ~= text)
        _G.ChatInputLastText = text

        -- Skip subtitle display in VR (immersive mode)
        if _G.VROffset then return end

        RunSubtitleAction("ChatInput", "show", function(subtitles)
            -- Always Remove+Add to guarantee subtitle shows (handles stale state)
            SubtitleDevPrint("ChatInput BPRemove PRE", displayText)
            subtitles:BPRemoveStandaloneSubtitle()
            SubtitleDevPrint("ChatInput BPRemove POST", displayText)
            SubtitleDevPrint("ChatInput BPAdd PRE", displayText)
            subtitles:BPAddStandaloneSubtitle(displayText)
            SubtitleDevPrint("ChatInput BPAdd POST", displayText)
            print("[ChatInput] Subtitle set: " .. displayText)
        end, { requireHUD = true })
    else
        -- Chat closing - clear state and remove subtitle
        _G.ChatInputLastText = nil
        RunSubtitleAction("ChatInput", "hide", function(subtitles)
            SubtitleDevPrint("ChatInput hide BPRemove PRE")
            subtitles:BPRemoveStandaloneSubtitle()
            SubtitleDevPrint("ChatInput hide BPRemove POST")
        end, { requireHUD = false })
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

    local ok = RunSubtitleAction("ShowMessage", "show", function(subtitles)
        -- Clear any existing subtitle first to avoid stacking
        subtitles:BPRemoveStandaloneSubtitle()
        subtitles:BPAddStandaloneSubtitle(message)
    end, { requireHUD = true })
    if ok then
        return
    end

    -- Fallback to hint message if subtitle HUD unavailable
    ShowHint(message, 3600)
end

function ShowAIMessage(message)
    if not AreSubtitlesEnabled() then return end
    ShowMessage(message)
end

function UpdateAIMessage(message)
    if not AreSubtitlesEnabled() then return end
    UpdateMessage(message)
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
    RunSubtitleAction("HideMessage", "hide", function(subtitles)
        subtitles:BPRemoveStandaloneSubtitle()
    end, { requireHUD = false })
end

function UpdateMessage(message)
    -- Skip subtitle display in VR (immersive mode)
    if _G.VROffset then return end

    -- Convert *emphasis* to <i>emphasis</i> for UE4 rich text
    message = string.gsub(message, "%*([^%*]+)%*", "<i>%1</i>")

    RunSubtitleAction("UpdateMessage", "update", function(subtitles)
        subtitles:BPUpdateStandaloneSubtitle(message)
    end, { requireHUD = false })
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

if _G.PresenceWatcher and _G.PresenceLedgerPhaseFlags.presenceWatcher then
    _G.PresenceWatcher.Init({
        getCachedNPCs = GetCachedNPCs,
        getStaticData = GetStaticCache,
        getVoiceId = Utils.GetActorVoiceId,
        isSignificant = IsSignificantNPC,
        safeIsValid = SafeIsValid,
        getGameDateTime = function()
            local gt = GetTimeOfDay()
            return {
                gameDate = gt.dateShort or gt.dateFormatted,
                gameTime = gt.formatted,
            }
        end,
        send = function(tbl)
            if _G.SocketClient and _G.SocketClient.send then
                _G.SocketClient.send(tbl)
            end
        end,
    })
elseif _G.PresenceWatcher then
    _G.PresenceWatcher.Stop()
    print("[PresenceWatcher] disabled by phase gate\n")
end

-- ============================================
-- Get Nearby NPCs (single iteration, returns list + looked-at)
-- Returns: { nearbyList = [{name, distance, actor, isLookedAt}], lookedAtNpc = {name, actor, distance} or nil, playerInStealth = bool }
-- ============================================
-- GetNearbyNPCs - MUST be called from game thread (inside ExecuteInGameThread or hook)
-- Returns: { nearbyList = [{...}], lookedAtNpc = {...} or nil, playerInStealth = bool }
function GetNearbyNPCs(maxDistance, lookDotThreshold)
    if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then
        print("[GetNearbyNPCs] EMPTY: Player not loaded")
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = false }
    end

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

    -- FOV: VR uses at least headset FOV (~110°), flat uses game camera FOV
    camFOV = camFOV or 90
    if _G.VRCamRot and camFOV < 110 then
        camFOV = 110
    end

    -- Use shared helper for viewport + aim center (non-VR path updates screenCenterX below)
    local fpAimInfo = _G.FirstPerson and _G.FirstPerson.getScreenAimInfo(pc, cam, camLoc, camRot)
    local viewportX = fpAimInfo and fpAimInfo.viewportX or 1920
    local viewportY = fpAimInfo and fpAimInfo.viewportY or 1080
    local screenCenterX = viewportX * 0.5
    local playerFullName = staticData.playerFullName

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

    -- Calculate forward vector from camera rotation
    -- VR: use world-space HMD rotation from UEVR stereo callback (no offset math needed)
    -- Flat: use camera rotation directly
    local vrCam = _G.VRCamRot
    local gazeRot = vrCam or camRot
    local pitch = math.rad(gazeRot.Pitch)
    local yaw = math.rad(gazeRot.Yaw)
    local forward = {
        X = math.cos(pitch) * math.cos(yaw),
        Y = math.cos(pitch) * math.sin(yaw),
        Z = math.sin(pitch)
    }
    if vrCam then
        forward.X = math.cos(yaw)
        forward.Y = math.sin(yaw)
        forward.Z = 0
    end

    -- Gaze origin: always use camera position (tracks HMD in VR)
    local gazeOrigin = camLoc

    if not vrCam and fpAimInfo then
        screenCenterX = fpAimInfo.screenCenterX
    end

    local nearbyList = {}
    local lookedAtNpc = nil
    local bestCenterDist = vrCam and lookDotThreshold or math.huge

    -- Tracking stats for warning/diagnostic cases
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

                    local npcHH = 88
                    pcall(function()
                        local cap = npc.CapsuleComponent
                        if cap and cap.CapsuleHalfHeight then npcHH = cap.CapsuleHalfHeight end
                    end)
                    local feetScreenX, feetScreenY = nil, nil
                    local topScreenX, topScreenY = nil, nil
                    local projected = false

                    local feetProj = _G.FirstPerson.projectToScreen(pc, {X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z}, viewportX, viewportY)
                    local topProj = _G.FirstPerson.projectToScreen(pc, {X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z + npcHH * 2.0}, viewportX, viewportY)

                    if feetProj and topProj then
                        projected = true
                        feetScreenX, feetScreenY = feetProj.x, feetProj.y
                        topScreenX, topScreenY = topProj.x, topProj.y
                    end

                    if vrCam then
                        toNpc.Z = 0
                    end

                    -- Normalize direction vector
                    local camDist2 = math.sqrt(toNpc.X * toNpc.X + toNpc.Y * toNpc.Y + toNpc.Z * toNpc.Z)
                    toNpc.X = toNpc.X / camDist2
                    toNpc.Y = toNpc.Y / camDist2
                    toNpc.Z = toNpc.Z / camDist2

                    -- Dot product with forward (1.0 = perfectly aligned with camera)
                    local dot = forward.X * toNpc.X + forward.Y * toNpc.Y + forward.Z * toNpc.Z
                    if dot > 1.0 then dot = 1.0 end
                    if dot < -1.0 then dot = -1.0 end
                    local bandCenterX = nil
                    local bandMinY = nil
                    local bandMaxY = nil
                    local horizontalErrorPx = math.huge
                    local horizontalTolerancePx = 0
                    local onScreen = false
                    if projected and feetScreenX and topScreenX and feetScreenY and topScreenY then
                        bandCenterX = (feetScreenX + topScreenX) * 0.5
                        bandMinY = math.min(feetScreenY, topScreenY)
                        bandMaxY = math.max(feetScreenY, topScreenY)
                        horizontalErrorPx = math.abs(bandCenterX - screenCenterX)
                        horizontalTolerancePx = math.abs(bandMaxY - bandMinY) * 0.25
                        onScreen =
                            feetScreenX >= -viewportX and feetScreenX <= viewportX * 2 and
                            topScreenX >= -viewportX and topScreenX <= viewportX * 2 and
                            bandMaxY >= 0 and bandMinY <= viewportY
                    end

                    local isLookedAt = false
                    if vrCam then
                        if dot > bestCenterDist
                            or (dot > lookDotThreshold and bestCenterDist > lookDotThreshold and dist < (lookedAtNpc and lookedAtNpc.distance or math.huge)) then
                            bestCenterDist = dot
                            lookedAtNpc = { name = npcId, actor = npc, distance = dist }
                            isLookedAt = true
                        end
                        onScreen = dot > math.cos(math.rad(camFOV * 0.45))
                    elseif onScreen
                        and horizontalErrorPx <= horizontalTolerancePx
                        and (horizontalErrorPx < bestCenterDist
                            or (math.abs(horizontalErrorPx - bestCenterDist) < 10.0 and dist < (lookedAtNpc and lookedAtNpc.distance or math.huge))) then
                        bestCenterDist = horizontalErrorPx
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

    if #nearbyList == 0 and stats.inRange > 0 then
        print("[GetNearbyNPCs] WARNING: All " .. stats.inRange .. " in-range NPCs were filtered (check visibility)")
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
-- Portrait NPCs are embedded in walls; their WallFrame mesh blocks LOS traces
local PORTRAIT_NPCS = {
    ["FerdinandOctaviusPratt"] = true,
    ["FatLady"] = true,
    ["MaryDunne"] = true,
    ["LethiaBurbley"] = true,
    ["SirCadogan"] = true,
    ["MusicConductor"] = true,
    ["SylviaPembroke"] = true,
    ["OgleThePortrait"] = true,
}

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

    -- Pre-validate all actors once (same game frame, avoids per-trace revalidation)
    -- Filter out stale actors BEFORE building ignore list or doing traces,
    -- since passing dangling pointers to C++ LineTraceSingle crashes uncatchably
    local validNpcList = {}
    local droppedCount = 0
    for _, npcData in ipairs(npcList) do
        if npcData.actor and SafeIsValid(npcData.actor) then
            table.insert(validNpcList, npcData)
        else
            droppedCount = droppedCount + 1
            DevPrint("[Visibility] Dropped stale actor: " .. tostring(npcData.name))
        end
    end
    if droppedCount > 0 then
        print("[Sonorus] CheckNPCVisibility: dropped " .. droppedCount .. " stale actors")
    end

    -- Build ignore list: player + validated NPCs (so trace only hits world geometry)
    local ActorsToIgnore = {}
    if player then
        table.insert(ActorsToIgnore, player)
    end
    for _, npcData in ipairs(validNpcList) do
        table.insert(ActorsToIgnore, npcData.actor)
    end

    -- Trace settings
    local ETraceTypeQuery_Visibility = 0
    local EDrawDebugTrace_None = 0
    local TraceColor = { R = 0, G = 0, B = 0, A = 0 }

    local visibilityResults = {}

    -- Also mark any dropped actors as not visible
    for _, npcData in ipairs(npcList) do
        if not npcData.actor or not SafeIsValid(npcData.actor) then
            visibilityResults[npcData.name] = false
        end
    end

    for _, npcData in ipairs(validNpcList) do
        local npcActor = npcData.actor
        local npcName = npcData.name

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
            elseif PORTRAIT_NPCS[npcName] then
                -- Portrait NPCs sit inside wall frames; check if the hit is just their frame
                local hitActorName = nil
                pcall(function()
                    local a = HitResult.Actor
                    if a then
                        local obj = nil
                        pcall(function() obj = a:Get() end)
                        if obj then
                            hitActorName = obj:GetFullName()
                        else
                            hitActorName = a:GetFullName()
                        end
                    end
                end)
                if hitActorName and hitActorName:find("WallFrame") then
                    isVisible = true
                    break
                end
            end

            ::nextRay::
        end

        visibilityResults[npcName] = isVisible
        ::continue::
    end

    DevPrint("[DEBUG] CheckNPCVisibility: " .. #validNpcList .. " traced, " .. droppedCount .. " dropped")
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

function OnTick(phase, emoteActive)
    -- NOTE: Socket updates, position writes, and context writes are now handled by
    -- the unified 100ms loop (runs always). OnTick only handles lipsync/conversation logic.

    -- Tick emotes if active (runs even during idle for fade-out)
    if emoteActive then
        if not _G._emoteFirstTickLogged then
            _G._emoteFirstTickLogged = true
            print("[Emotes] First OnTick emote tick START")
        end
        pcall(Emotes._Tick)
        if _G._emoteFirstTickLogged == true then
            _G._emoteFirstTickLogged = "done"
            print("[Emotes] First OnTick emote tick END")
        end
    else
        _G._emoteFirstTickLogged = nil
    end

    -- Queue updates now come via socket (queue_item messages)
    if phase == "idle" and not _G.SonorusState.active then return end

    -- If idle arrived while we were handing off to the next queued turn, give
    -- lipsync_start a brief grace window before final cleanup.
    if phase == "preparing" and _G.SonorusState.pendingIdle then
        local pendingIdleAt = _G.SonorusState.pendingIdleAt or 0
        if pendingIdleAt > 0 and (os.clock() - pendingIdleAt) >= 1.0 then
            print("[Sonorus] Deferred idle expired during preparing - finalizing conversation")
            _G.SonorusState.phase = "idle"
            _G.SonorusState.currentTurnId = nil
            _G.SonorusState.active = false
            _G.SonorusState.closing = false
            _G.SonorusState.pendingIdle = false
            _G.SonorusState.pendingIdleAt = 0
            local endBehavior = _G.SonorusState.pendingEndBehavior or "linger"
            _G.SonorusState.pendingEndBehavior = nil
            _G.CloseLipsComplete = false
            _G.CloseLipsIterations = 0
            _G.TurnActorCache = {}
            ClearSpeakerCache()
            UnmuteAllSpeakers()
            if endBehavior == "release_all" and ReleaseAllNPCs then
                ReleaseAllNPCs()
            else
                LingerAllNPCs()
            end
            ResetPlaybackState()
            if HideMessage and not (_G.ChatInputState and _G.ChatInputState.active) then
                HideMessage()
            end
            if TimeDilation then
                TimeDilation.OnConversationEnd()
            end
            if _G.ConversationFPVActive and _G.FirstPerson then
                _G.FirstPerson.disable()
                _G.ConversationFPVActive = false
            end
            print("[Sonorus] Ready for next conversation")
        end
        return
    end

    local pState = _G.PlaybackState
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
                _G.SonorusState.pendingIdleAt = 0
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
                _G.SonorusState.pendingIdleAt = 0
                local endBehavior = _G.SonorusState.pendingEndBehavior or "linger"
                _G.SonorusState.pendingEndBehavior = nil
                _G.CloseLipsComplete = false
                _G.TurnActorCache = {}  -- Clear turn-based cache
                ClearSpeakerCache()     -- Clear legacy cache
                UnmuteAllSpeakers()
                if endBehavior == "release_all" and ReleaseAllNPCs then
                    ReleaseAllNPCs()
                else
                    LingerAllNPCs()     -- NPCs stay frozen ~10s before returning to schedule
                end
                ResetPlaybackState()
                -- Hide subtitles now that closing is complete
                if HideMessage then
                    HideMessage()
                end
                -- Time dilation: Restore day/night rate
                if TimeDilation then
                    TimeDilation.OnConversationEnd()
                end
                -- Auto first-person view: restore third-person
                if _G.ConversationFPVActive and _G.FirstPerson then
                    _G.FirstPerson.disable()
                    _G.ConversationFPVActive = false
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
            ShowAIMessage(displayMessage)
            _G.SonorusState.messageShown = AreSubtitlesEnabled()
        end
    end

    -- Animate lips while playing (viseme data populated by socket_client)
    -- Socket triggers phase="closing" on lipsync_stop, handled at top of OnTick
    if (phase == "playing" or _G.SonorusState.lipsyncStarted) and phase ~= "closing" and not _G.SonorusState.closing then
        -- Viseme data now comes via socket - no need for LoadVisemes()
        -- DISABLE LIPSYNC FOR TESTING: set _G.DisableLipsync = true
        if not _G.DisableLipsync then
            if not _G._lipsyncFirstTickLogged then
                _G._lipsyncFirstTickLogged = true
                print("[LipSync] First OnTick lipsync tick START")
            end
            -- Already on game thread via shared TickScheduler
            _AnimateLipsWrapper()
            if _G._lipsyncFirstTickLogged == true then
                _G._lipsyncFirstTickLogged = "done"
                print("[LipSync] First OnTick lipsync tick END")
            end
        end
        -- DISABLE 3D AUDIO FOR TESTING: set _G.Disable3DAudio = true
        if not _G.Disable3DAudio then
            if not _G._writePositionsFirstTickLogged then
                _G._writePositionsFirstTickLogged = true
                print("[WritePositions] First OnTick WritePositions START")
            end
            WritePositions()
            if _G._writePositionsFirstTickLogged == true then
                _G._writePositionsFirstTickLogged = "done"
                print("[WritePositions] First OnTick WritePositions END")
            end
        end
    end
end

-- ============================================
-- Reset State
-- ============================================
function ResetState()
    print("[Sonorus] Resetting state...")

    -- First: Reset blendshapes on ALL nearby NPCs (fixes stuck lip sync / emotes)
    -- This runs first so users can use F8 as a general "fix broken NPCs" button
    ResetNearbyNPCLips()
    -- Stop any active emote and reset emote morph targets
    if _G.EmoteState and _G.EmoteState.active then
        Emotes._Finish()
    end

    if not _G.SonorusState then return end

    -- Reset Lua state (no UObject access)
    ResetPlaybackState()
    _G.SonorusState.active = false
    _G.SonorusState.closing = false  -- Must reset or next conversation breaks
    _G.SonorusState.pendingIdle = false  -- Clear deferred idle flag
    _G.SonorusState.pendingIdleAt = 0
    -- If conversation auto-enabled FPV, disable it now
    if _G.ConversationFPVActive and _G.FirstPerson then
        pcall(function() _G.FirstPerson.disable() end)
    end
    _G.ConversationFPVActive = false
    _G.SonorusState.lipsyncStarted = false
    _G._lipsyncFirstTickLogged = nil
    _G._writePositionsFirstTickLogged = nil
    _G.SonorusState.messageShown = false
    _G.SonorusState.playerMessageShown = false
    _G.SonorusState.playerMessage = nil
    _G.CloseLipsComplete = false  -- Reset async flag
    _G.CurrentSonorusTarget = nil
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

    -- Stop any ambient eye-contact override before releasing/clearing state
    if StopAmbientGaze then
        pcall(function() StopAmbientGaze("reset state") end)
    end

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

    ExecuteInGameThreadAfterFrames(1, function()
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

            local lineBlocked = false
            local convoBlock = false

            -- Block ambient if NPC is in AI conversation
            if IsNPCInConversation and IsNPCInConversation(voiceName) then
                lineBlocked = true
                convoBlock = true
                print(string.format("[Sonorus] Blocked ambient for in-conversation NPC: %s", voiceName))
            end

            -- Ambient blocklist check (server-driven)
            -- Skip during cinematics and post-load cooldown
            local inCinematic = _G.CinematicState and _G.CinematicState.active or false
            local inLoadCooldown = AudioMute and AudioMute.InLoadCooldown() or false
            local isBlocklistNPC = not inCinematic and not inLoadCooldown and _G._AmbientBlocklist and _G._AmbientBlocklist[voiceName]
            if not lineBlocked and isBlocklistNPC then
                -- Check if this specific lineID number is blocked
                -- Extract numeric suffix: "GarrethWeasley_10946" -> 10946
                local lineNum = lineID and tonumber(lineID:match("_(%d+)$"))
                if lineNum then
                    lineBlocked = isBlocklistNPC[lineNum] == true
                else
                    -- Can't extract number — block by default
                    lineBlocked = true
                end
            end
            if lineBlocked then
                -- Heard before — block everything
                local npcActor = GetSpeakerActor(voiceName)
                if npcActor then
                    NPCFacial.StopNPCDialogueLipSync(npcActor)
                    -- Ensure muted (covers fresh spawns not yet muted)
                    AudioMute.MuteNPCAudio(npcActor)
                end
                pcall(function()
                    if elem and elem:IsValid() then
                        elem:SetVisibility(1)
                    end
                end)
                -- Capture stable identifiers while objects are valid (before the delay)
                local capElemPath = nil
                pcall(function()
                    local fn = elem:GetFullName()
                    capElemPath = fn:match(" (.+)$") or fn
                end)
                -- Backup mute/lip sync stop + elem visibility at 50ms.
                -- Use voiceName -> BP volume path so we don't rely on native delayed volume.
                ExecuteInGameThreadWithDelay(50, function()
                    if not _G.SonorusState or not _G.SonorusState.playerLoaded then return end
                    AudioMute.MuteNPCAudio(voiceName)
                    local actor = GetSpeakerActor(voiceName)
                    if actor then
                        NPCFacial.StopNPCDialogueLipSync(actor)
                    end
                    -- Fresh lookup for elem (never touch the stale closure ref)
                    if capElemPath then
                        pcall(function()
                            local freshElem = StaticFindObject(capElemPath)
                            if freshElem and freshElem:IsValid() then
                                freshElem:SetVisibility(1)
                            end
                        end)
                    end
                end)
                if not convoBlock then
                    print(string.format("[Sonorus] Ambient BLOCKED: %s (heard before)", voiceName))
                end
                return
            elseif isBlocklistNPC then
                -- New line from blocklist NPC — unmute, let it play, re-mute after duration + buffer
                local npcActor = GetSpeakerActor(voiceName)
                if npcActor then
                    AudioMute.UnmuteNPCAudioByActor(npcActor)
                end
                local remutDelay = math.floor(math.max((duration or 2) + 2, 4) * 1000)  -- duration + 2s buffer, min 4s
                ExecuteInGameThreadWithDelay(remutDelay, function()
                    if not _G.SonorusState or not _G.SonorusState.playerLoaded then return end
                    local inCutscene = _G.CinematicState and _G.CinematicState.active or false
                    if inCutscene then return end
                    local inConvo = IsNPCInConversation and IsNPCInConversation(voiceName)
                    if inConvo then return end
                    if _G._AmbientBlocklist and _G._AmbientBlocklist[voiceName] then
                        local actor = GetSpeakerActor(voiceName)
                        if actor then
                            AudioMute.MuteNPCAudio(actor)
                        end
                    end
                end)
                print(string.format("[Sonorus] Ambient ALLOWED (new line): %s | re-mute in %.1fs", voiceName, remutDelay / 1000))
                -- Don't return — let recording proceed so server learns this line
            end

            -- Use _G lookup to survive F11 reload (closure captures stale ref)
            if _G.RecordDialogueLine then
                -- Lookup subtitle text (needed early for blocklist hash)
                local subtitleText = ""
                if GetSubtitleText and not convoBlock then
                    subtitleText = GetSubtitleText(lineID) or ""
                end
                if subtitleText ~= "" then
                    print(string.format("[Sonorus] Subtitle: \"%s\"", subtitleText))
                end
                _G.RecordDialogueLine(voiceName, lineID, duration, subtitleText, nil, nil)
            end

            -- Suppress native subtitle in VR (immersive mode)
            if false and _G.VROffset then
                pcall(function()
                    if elem and elem:IsValid() then
                        elem:SetVisibility(1)
                    end
                end)
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

function AreSubtitlesEnabled()
    return _G.GameSubtitlesEnabled ~= false  -- Default true if unknown/unset
end
_G.AreSubtitlesEnabled = AreSubtitlesEnabled

function RecordDialogueLine(voiceName, lineID, duration, subtitleText, speakingActor, targetName)
    -- Skip recording when game is paused/menu open
    if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then
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

    -- Reset attention meter for the speaking NPC so they don't immediately re-trigger
    if not isPlayer and not inCinematic then
        pcall(function() AttentionMeter.OnAmbientDialogue(speakerVoiceId) end)
    end

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

-- Record a mount/dismount event to DialogueHistory
function RecordMountEvent(mountAction, mountType)
    local timestamp = os.time()
    local gameTime = GetTimeOfDay()

    -- Get player name
    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
    end

    -- Create mount event entry with specific mount name
    local mountName = mountType or "broom"
    local actionText = mountAction == "mounted"
        and ("Mounted " .. mountName)
        or ("Dismounted from " .. mountName)

    -- Get earshot witnesses (nearby named NPCs)
    local earshot = GetEarshotWitnesses("Player")

    local entry = {
        timestamp = timestamp,
        gameTime = gameTime.formatted,
        gameDate = gameTime.dateShort or gameTime.dateFormatted,
        speaker = playerName,
        voiceName = "Player",
        lineID = "mount_" .. mountAction,
        text = actionText,
        duration = 0,
        isAIResponse = false,
        isPlayer = true,
        type = "mount",
        earshot = earshot,
    }

    -- Send to Python for persistence
    sendDialogueEntry(entry)

    -- Log for debugging
    print(string.format("[Sonorus] Mount: %s %s", playerName, actionText:lower()))
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

-- Registered on the shared tick scheduler; re-registers on hot reload.
_G.ServerMonitor.loopStarted = true
_G.ServerMonitor.loopHandle = nil
TickScheduler.Register("server_heartbeat", 4987, function()
    MonitorServerHeartbeat()
end)
print("[Sonorus] Server heartbeat monitor registered (5s interval)")

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
        -- First check combat/mount
        local canLock, reason = CanLockNPCs()
        if not canLock then
            print("[NPCLock] Releasing NPCs: " .. tostring(reason))
            pcall(ReleaseAllNPCs)
            return
        end

        -- Check if any locked NPC needs to re-face their target (angle > 50 degrees)
        -- Collect NPCs that need re-facing first (can't modify table during iteration)
        -- Skip companions, static locks, and lingering NPCs (they're frozen in place)
        -- Snap-locked NPCs re-face through Blueprint by stable IDs.
        local needsReface = {}
        local snapReface = {}
        for lockId, data in pairs(_G.LockedNPCs) do
            if data.locked and data.npc and data.targetActor
               and not data.isCompanionLock and not data.isStaticLock
               and not data.lingering and not data.isPreviewLock then
                if data.isSnapLock then
                    -- Do not dereference retained UObject wrappers for snap locks.
                    -- Blueprint resolves fresh actors from these stable IDs.
                    if data.npcName and data.targetId then
                        table.insert(snapReface, { lockId = lockId, data = data })
                    end
                else
                    pcall(function()
                        if not SafeIsValid(data.npc) or not SafeIsValid(data.targetActor) then return end

                        local npcPos = data.npc:K2_GetActorLocation()
                        local npcRot = data.npc:K2_GetActorRotation()
                        local targetPos = data.targetActor:K2_GetActorLocation()

                        local angleDiff = GetAngleToTarget(npcPos, npcRot, targetPos)

                        -- If angle > 50 degrees, mark for re-facing
                        if angleDiff > 50 then
                            table.insert(needsReface, {
                                lockId = lockId,
                                npc = data.npc,
                                target = data.targetActor,
                                angle = math.floor(angleDiff)
                            })
                        end
                    end)
                end
            end
        end

        -- Refresh snap locks by ID without touching retained actor wrappers.
        for _, item in ipairs(snapReface) do
            NPCLock.SnapRefaceNPC(item.data)
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
_G.UnifiedLoop.interval = _G.UnifiedLoop.interval or 97  -- Default ~100ms (jittered to avoid UE4SS timer collisions)

-- ============================================
-- Event Handlers (re-registered on each reload)
-- ============================================
-- Events.clear() is called above to prevent duplicate handlers on F11 reload

-- Cinematic: Stop active conversation when entering cinematic
Events.on("cinematic:start", function(data)
    if not _G.SonorusState.playerLoaded then return end
    print("[Sonorus] Cinematic started - stopping conversation")
    if StopAmbientGaze then pcall(function() StopAmbientGaze("cinematic start") end) end
    -- Tell Python to stop conversation immediately (with history trimming)
    -- Python's stop_conversation will send reset back to Lua, which calls ResetState
    pcall(function()
        SocketClient.send({ type = "interrupt_conversation", reason = "cinematic" })
    end)
    -- Send state update so Python knows we're in cinematic
    pcall(function() WriteSelectiveContext({"state"}) end)
    -- Unmute blocklist NPCs so native cutscene dialogue plays
    pcall(function() AudioMute.UnmuteBlocklistNPCs() end)
end)

-- Cinematic: Suspend FPV when entering cinematic
Events.on("cinematic:start", function(data)
    print("[Sonorus] Cinematic started - suspending FPV")
    -- Suspend FPV during cutscene
    if _G.FirstPerson then
        pcall(function() _G.FirstPerson.suspend("cinematic", true) end)
    end
end)

Events.on("cinematic:end", function(data)
    print("[Sonorus] Cinematic ended")
    pcall(function() WriteSelectiveContext({"state"}) end)
    pcall(function()
        if SocketClient and SocketClient.send then
            SocketClient.send({
                type = "game_event",
                event = "cinematic:end",
                data = {}
            })
        end
    end)
    -- Re-mute blocklist NPCs after cutscene
    pcall(function() AudioMute.MuteBlocklistNPCs() end)
    -- Refresh house points after 2s (quests may award points on completion)
    ExecuteInGameThreadWithDelay(2000, function()
        RefreshHousePoints()
    end)
end)

-- Cinematic: Resume FPV when exiting cinematic
Events.on("cinematic:end", function(data)
    print("[Sonorus] Cinematic ended - resuming FPV")
    -- Resume FPV after cutscene
    if _G.FirstPerson then
        pcall(function() _G.FirstPerson.resume("cinematic", true) end)
    end
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

        local companionPawn = nil
        pcall(function() companionPawn = companionMgr:GetPrimaryCompanionPawn() end)
        if not companionPawn or not Utils.SafeIsValid(companionPawn) then return end

        local playerLoc, companionLoc = nil, nil
        pcall(function()
            playerLoc = player:K2_GetActorLocation()
            companionLoc = companionPawn:K2_GetActorLocation()
        end)
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
    if StopAmbientGaze then pcall(function() StopAmbientGaze("combat start") end) end
    -- Unmute companion so combat grunts play
    pcall(function() AudioMute.UnmuteBlocklistCompanion() end)

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
    -- Re-mute companion after combat
    pcall(function() AudioMute.MuteBlocklistCompanion() end)

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
        pcall(function()
            if SocketClient and SocketClient.send then
                SocketClient.send({
                    type = "game_event",
                    event = "combat:end",
                    data = {
                        summary = entry.text or "",
                        enemyCounts = stats.enemies or {},
                        playerDamagePct = totalDamage > 0 and math.floor((stats.playerDamage / totalDamage) * 100 + 0.5) or 0,
                        companionDamagePct = totalDamage > 0 and (100 - (math.floor((stats.playerDamage / totalDamage) * 100 + 0.5) or 0)) or 0
                    }
                })
            end
        end)
        print(string.format("[Combat] Summary: %s (witnesses: %d from start+end)", entry.text, #endWitnesses))
    else
        print("[Combat] No combat activity to record")
    end

    -- Clear start witnesses
    _G.CombatStats.startWitnesses = nil

    pcall(function() WriteSelectiveContext({"state"}) end)
end)

-- Mount: Release NPCs when mounting, log events, suspend/resume FPV
-- Note: setState("mount", true) emits mount:start, setState("mount", false) emits mount:end
Events.on("mount:start", function(data)
    if StopAmbientGaze then pcall(function() StopAmbientGaze("mount start") end) end
    if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
    if _G.FirstPerson then pcall(function() _G.FirstPerson.suspend("mount", true) end) end

    -- Delay recording so mount type recheck (1s) has time to identify creature mounts
    ExecuteInGameThreadWithDelay(1500, function()
        local mountType = (_G.MountState and _G.MountState.mountType) or "mount"
        print("[Sonorus] Player mounted " .. mountType)
        if RecordMountEvent then pcall(function() RecordMountEvent("mounted", mountType) end) end
    end)
end)

Events.on("mount:end", function(data)
    local mountType = (_G.MountState and _G.MountState.mountType) or "mount"
    print("[Sonorus] Player dismounted " .. mountType)
    if RecordMountEvent then pcall(function() RecordMountEvent("dismounted", mountType) end) end
    if _G.FirstPerson then pcall(function() _G.FirstPerson.resume("mount", true) end) end

    -- Delay context update to let floo mod restore companion
    ExecuteWithDelay(500, function()
        pcall(function() WriteSelectiveContext({"companion"}) end)
    end)
end)

-- Stealth: Log state changes, send context updates
Events.on("stealth:start", function(data)
    print("[Sonorus] Player entered stealth/disillusionment")
    if StopAmbientGaze then pcall(function() StopAmbientGaze("stealth start") end) end
    pcall(function() WriteSelectiveContext({"state"}) end)
end)

Events.on("stealth:end", function(data)
    print("[Sonorus] Player left stealth/disillusionment")
    pcall(function() WriteSelectiveContext({"state"}) end)
end)

-- Swimming: Suspend/resume FPV
Events.on("swimming:start", function(data)
    print("[Sonorus] Player started swimming")
    if StopAmbientGaze then pcall(function() StopAmbientGaze("swimming start") end) end
    if _G.FirstPerson then pcall(function() _G.FirstPerson.suspend("swimming") end) end
end)

Events.on("swimming:end", function(data)
    print("[Sonorus] Player stopped swimming")
    if _G.FirstPerson then pcall(function() _G.FirstPerson.resume("swimming") end) end
end)

-- Function to start/restart the unified loop with current interval
-- Called on init and when interval changes via config
function _G.StartUnifiedLoop(newInterval)
    -- Update interval if provided
    if newInterval then
        _G.UnifiedLoop.interval = newInterval
    end

    -- Replace the shared scheduler task if this is a reload or interval update.
    TickScheduler.Unregister("sonorus_unified")
    _G.UnifiedLoop.handle = nil

    -- Increment version for logging
    _G.UnifiedLoop.version = (_G.UnifiedLoop.version or 0) + 1
    local myLoopVersion = _G.UnifiedLoop.version
    print("[Sonorus] Starting unified loop v" .. myLoopVersion .. " (" .. _G.UnifiedLoop.interval .. "ms)")

    -- TickScheduler runs ON the game thread - UObject access is safe.
    TickScheduler.Register("sonorus_unified", _G.UnifiedLoop.interval, function()
        -- Socket update EVERY tick - handles reconnection and message processing
        -- This is CRITICAL - socket must update frequently even when mod is disabled
        -- Must run BEFORE playerLoaded guard so player_ready can be received
        -- NOTE: Pure LuaSocket, no UObjects
        -- Skip if fast poll loop is active AND we're connected (it handles socket updates at 25ms)
        -- Always run here when disconnected so reconnection logic isn't blocked
        local fp = _G._FastPoll
        local fastPollActive = fp and os.clock() < (fp.expiry or 0)
        if not fastPollActive or not (_G.SocketClient and _G.SocketClient.isConnected()) then
            if _G.SocketClient then
                pcall(_G.SocketClient.update)
            else
                print("No socket client!")
            end
        end

        if not _G.SonorusState.playerLoaded then return end
        local devMode = _G.SonorusDevMode
        local t0, t1, t2, t3, t4, t5

        if devMode then t0 = os.clock() end

        local now = os.clock()
        if devMode then t1 = os.clock() end
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
        if (now - _G.UnifiedLoop.lastTimeUpdate >= 5.0) then
            _G.UnifiedLoop.lastTimeUpdate = now
            pcall(RefreshTimeCache)
            pcall(function() WriteSelectiveContext({"time", "zone", "companion", "player", "world_facts"}) end)
            pcall(function() Events.emit("timeUpdated") end)
        end
        if devMode then t4 = os.clock() end

        -- Mount state polling every 2 seconds (replaces ReceiveTick hooks)
        -- Already on game thread, UObject access is safe
        -- Guard: skip during loading screens to avoid phantom mount/dismount from transitional states
        _G.UnifiedLoop.lastMountCheck = _G.UnifiedLoop.lastMountCheck or 0
        if (_G.SonorusState.playerLoaded and now - _G.UnifiedLoop.lastMountCheck >= 2.0) then
            _G.UnifiedLoop.lastMountCheck = now
            local onMount = false
            pcall(function()
                local staticData = GetStaticCache()
                local gearScreen = staticData.gearScreen
                if gearScreen then
                    -- IsPlayerOnBroom is the only confirmed working GearScreen mount check.
                    -- Also check GetIsOnAMountOrInTransition on player for other mount types.
                    onMount = gearScreen:IsPlayerOnBroom() or false
                    if not onMount then
                        local player = staticData.player
                        if player and player:IsValid() and player.GetIsOnAMountOrInTransition then
                            onMount = player:GetIsOnAMountOrInTransition() or false
                        end
                    end
                end
            end)
            -- Seed Events store so first real transition fires :start/:end (not treated as init)
            if not _G._mountStateSeeded then
                _G._mountStateSeeded = true
                Events.setState("mount", false)
            end

            -- Update MountState BEFORE firing events so handlers read correct values
            if onMount ~= (_G.MountState.mounted or false) then
                _G.MountState = _G.MountState or {}

                -- Only detect specific mount type on mount (not every poll)
                if onMount then
                    local mountTypeName = nil

                    -- Check if on broom specifically
                    pcall(function()
                        local staticData = GetStaticCache()
                        local gearScreen = staticData.gearScreen
                        if gearScreen and gearScreen:IsPlayerOnBroom() then
                            mountTypeName = "broom"
                        end
                    end)

                    -- Not broom — find creature mount via RiderCharacter match
                    if not mountTypeName then
                        pcall(function()
                            local player = FindFirstOf("Biped_Player")
                            if not player or not player:IsValid() then return end

                            local creatures = FindAllOf("Creature_Character")
                            if not creatures then return end
                            for _, creature in pairs(creatures) do
                                pcall(function()
                                    if not SafeIsValid(creature) then return end
                                    local mountComp = creature:GetMountComponent()
                                    if not mountComp then return end
                                    local rider = mountComp.RiderCharacter
                                    if not rider or rider:GetAddress() ~= player:GetAddress() then return end

                                    local cl = creature:GetClass():GetFName():ToString():lower()
                                    if cl:find("graphorn") then mountTypeName = "graphorn"
                                    elseif cl:find("hippogriff") then mountTypeName = "hippogriff"
                                    elseif cl:find("niffler") then mountTypeName = "niffler"
                                    elseif cl:find("thestral") then mountTypeName = "thestral"
                                    else mountTypeName = "creature"
                                    end
                                end)
                                if mountTypeName then break end
                            end
                        end)
                    end

                    -- Commit mount immediately so FPV suspends right away
                    _G.MountState.mounted = true
                    _G.MountState.mountType = mountTypeName or "mount"
                    print("[Sonorus] Mount type: " .. _G.MountState.mountType)
                    Events.setState("mount", true)

                    -- If type unknown, do a delayed recheck to identify the creature
                    if not mountTypeName then
                        ExecuteInGameThreadWithDelay(1000, function()
                            if not (_G.MountState and _G.MountState.mounted) then return end
                            pcall(function()
                                local player = FindFirstOf("Biped_Player")
                                if not player or not player:IsValid() then return end
                                local creatures = FindAllOf("Creature_Character")
                                if not creatures then return end
                                for _, creature in pairs(creatures) do
                                    pcall(function()
                                        if not SafeIsValid(creature) then return end
                                        local mountComp = creature:GetMountComponent()
                                        if not mountComp then return end
                                        local rider = mountComp.RiderCharacter
                                        if not rider or rider:GetAddress() ~= player:GetAddress() then return end
                                        local className = creature:GetClass():GetFName():ToString()
                                        local cl = className:lower()
                                        local name = "creature"
                                        if cl:find("graphorn") then name = "graphorn"
                                        elseif cl:find("hippogriff") then name = "hippogriff"
                                        elseif cl:find("niffler") then name = "niffler"
                                        elseif cl:find("thestral") then name = "thestral"
                                        end
                                        _G.MountState.mountType = name
                                        print("[Sonorus] Mount type: " .. name)
                                    end)
                                end
                            end)
                        end)
                    end
                else
                    -- Dismounting — commit and fire
                    _G.MountState.mounted = false
                    -- Note: keep mountType set so mount:end handler can read it
                    Events.setState("mount", false)
                end
            end
        end

        -- Teleport commitment proximity check every 2 seconds
        -- Pure Lua distance math + one K2_GetActorLocation; skips if no unplaced teleports
        _G.UnifiedLoop.lastCommitProximity = _G.UnifiedLoop.lastCommitProximity or 0
        if (now - _G.UnifiedLoop.lastCommitProximity >= 2.0) then
            _G.UnifiedLoop.lastCommitProximity = now
            if _G.CommitmentManager and _G.CommitmentManager.ProximityCheck then
                pcall(_G.CommitmentManager.ProximityCheck)
            end
        end

        -- Combat, stealth, swimming state polling every 1 second
        -- Note: Cinematic + pause detection lives in the 500ms pause monitor
        -- (socket_client.lua) so ViewTarget cinematic check always has fresh
        -- pause state — no race with pause menu ViewTarget swaps.
        _G.UnifiedLoop.lastStateCheck = _G.UnifiedLoop.lastStateCheck or 0
        if (_G.SonorusState.playerLoaded and now - _G.UnifiedLoop.lastStateCheck >= 1.0) then
            _G.UnifiedLoop.lastStateCheck = now

            local inCombat = false
            local inStealth = false
            local isSwimming = false
            pcall(function()
                local staticData = GetStaticCache()
                local player = staticData.player
                if player and player:IsValid() then
                    inCombat = player.bInCombatMode or false
                    inStealth = player.InStealthMode or false
                    if player.IsSwimming then
                        isSwimming = player:IsSwimming() or false
                    end
                end
            end)

            if Events.setState("combat", inCombat) then
                _G.CombatState = _G.CombatState or {}
                _G.CombatState.active = inCombat
            end

            if Events.setState("stealth", inStealth) then
                _G.StealthState = _G.StealthState or {}
                _G.StealthState.active = inStealth
            end

            Events.setState("swimming", isSwimming)
        end

        -- Idle detection every 30 seconds (for ambient dialog + attention meter gating)
        -- Tracks camera angle and sets _G.PlayerIdleState if no camera movement for 10 minutes
        _G.UnifiedLoop.lastIdleCheck = _G.UnifiedLoop.lastIdleCheck or 0
        if (now - _G.UnifiedLoop.lastIdleCheck >= 30.0) then
            _G.UnifiedLoop.lastIdleCheck = now

            -- Initialize idle tracking state
            _G.IdleState = _G.IdleState or {
                lastCamYaw = nil,
                lastCamPitch = nil,
                lastMovementTime = os.time(),
                idleTimeoutMinutes = 10,
            }

            -- Get current camera rotation
            local camYaw, camPitch = nil, nil
            pcall(function()
                local staticData = GetStaticCache()
                local cam = staticData.cameraManager
                if cam then
                    local rot = cam:GetCameraRotation()
                    if rot then
                        camYaw = rot.Yaw
                        camPitch = rot.Pitch
                    end
                end
            end)

            if camYaw and camPitch then
                local moved = false
                if _G.IdleState.lastCamYaw then
                    local dYaw = math.abs(camYaw - _G.IdleState.lastCamYaw)
                    if dYaw > 180 then dYaw = 360 - dYaw end
                    local dPitch = math.abs(camPitch - _G.IdleState.lastCamPitch)
                    if dYaw > 1 or dPitch > 1 then
                        moved = true
                    end
                else
                    moved = true  -- First check
                end

                _G.IdleState.lastCamYaw = camYaw
                _G.IdleState.lastCamPitch = camPitch

                if moved then
                    _G.IdleState.lastMovementTime = os.time()
                    if _G.PlayerIdleState then
                        print("[Sonorus] Camera movement detected - player no longer idle")
                        _G.PlayerIdleState = false
                    end
                else
                    local idleSeconds = os.time() - _G.IdleState.lastMovementTime
                    local timeoutSeconds = _G.IdleState.idleTimeoutMinutes * 60
                    if timeoutSeconds > 0 and idleSeconds > timeoutSeconds and not _G.PlayerIdleState then
                        print(string.format("[Sonorus] Player idle for %d minutes (no camera movement) - pausing", _G.IdleState.idleTimeoutMinutes))
                        _G.PlayerIdleState = true
                    end
                end
            end
        end

        -- Check locked NPCs every 1 second: combat/mount release, angle refresh
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

        -- Attention meter: single camera trace every 1s
        _G.UnifiedLoop.lastAttentionCheck = _G.UnifiedLoop.lastAttentionCheck or 0
        if (now - _G.UnifiedLoop.lastAttentionCheck) >= 1.0 then
            _G.UnifiedLoop.lastAttentionCheck = now
            pcall(AttentionMeter.Update)
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
    _G.PendingSpellCastHandle = ExecuteInGameThreadWithDelay(250, function()
        _G.PendingSpellCastHandle = nil
        local castOk, castErr = pcall(function()
            wandTool:ActivateSpellTool(spellToolRecord, false)
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
