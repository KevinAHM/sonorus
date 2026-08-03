-- Utils/NPCLock.lua
-- NPC Attention Lock System - makes NPCs walk towards and face conversation targets
-- Extracted from logic.lua for modularity

local NPCLock = {}

-- Module requires
local BlueprintHelpers = require("Utils.BlueprintHelpers")
local Cache = require("Utils.Cache")
local Utils = require("Utils.Utils")
local NPCFacial = require("Utils.NPCFacial")
local TickScheduler = require("Utils.TickScheduler")

local DevLog = {
    Log = function(tag, ...)
        if _G.DevPrint then
            _G.DevPrint("[" .. tostring(tag or "DevLog") .. "]", ...)
        end
    end
}

-- ============================================
-- State (persisted in _G for hot reload)
-- ============================================
_G.LockedNPCs = _G.LockedNPCs or {}
_G.LockedNPCNames = _G.LockedNPCNames or {}  -- lockId -> normalized name (for thread-safe lookup)

-- Linger timeout: how long NPCs stay frozen after conversation ends (ms)
local LINGER_TIMEOUT_MS = 20000
local GOODBYE_CLAIM_TIMEOUT_MS = 1500

-- Linger state (persisted in _G for hot reload)
_G.LingerState = _G.LingerState or {
    active = false,
    timerHandle = nil,
    locks = {},  -- normalized npcName -> lockId
    generation = 0,
    goodbyePending = nil,
    goodbyeTimerHandle = nil,
}

_G.NPCLockGazeState = _G.NPCLockGazeState or {
    loopHandle = nil,
    active = {},
}
if _G.NPCLockGazeState.loopHandle and _G.NPCLockGazeState.loopHandle ~= true then
    pcall(function() CancelDelayedAction(_G.NPCLockGazeState.loopHandle) end)
    _G.NPCLockGazeState.loopHandle = nil
end
_G.NPCLockGazeState.loopHandle = nil
TickScheduler.Unregister("npc_lock_gaze")

-- Clear stale active entries on reload — UObject refs from previous session are invalid
_G.NPCLockGazeState.active = {}

_G.NPCAmbientGazeEnabled = false

_G.NPCAmbientGazeState = _G.NPCAmbientGazeState or {
    loopHandle = nil,
    npc = nil,
    targetActor = nil,
    refs = nil,
    npcKey = nil,
    targetKey = nil,
    holdUntil = 0,
    holdSeconds = 0,
}
if _G.NPCAmbientGazeState.loopHandle and _G.NPCAmbientGazeState.loopHandle ~= true then
    pcall(function() CancelDelayedAction(_G.NPCAmbientGazeState.loopHandle) end)
    _G.NPCAmbientGazeState.loopHandle = nil
end
_G.NPCAmbientGazeState.loopHandle = nil
TickScheduler.Unregister("npc_ambient_gaze")
-- Clear stale UObject refs on reload
_G.NPCAmbientGazeState.npc = nil
_G.NPCAmbientGazeState.targetActor = nil
_G.NPCAmbientGazeState.refs = nil
_G.NPCAmbientGazeState.npcKey = nil
_G.NPCAmbientGazeState.targetKey = nil

-- Local state (resets on hot reload, which is fine for counter)
local lockIdCounter = 0
local GAZE_UPDATE_MS = 250
local AMBIENT_GAZE_MIN_HOLD_S = 4.0
local AMBIENT_GAZE_MAX_HOLD_S = 10.0
local EnsureLockGazeLoop
local EnsureAmbientGazeLoop
local AbsorbLingeringLock
local GetActorIdentityKey

-- Static cache getter - set via init()
local getStaticCache = nil
local BumpLingerGeneration
local ClearGoodbyeTimer
local ClearGoodbyePending
local ReleaseGoodbyePendingBatch
local QueueLingerGoodbyeBatch

-- Graceful station exit delay (ms) before AbandonStations takes control.
-- 2s gives enough natural exit animation before we force-lock.
-- Longer delays cause issues: NPC walks back before lock engages.
local STATION_EXIT_DELAY_MS = 2200

-- Follow mission cache (persisted in _G for hot reload)
-- Updated periodically, not on every lock check
_G.FollowMissionCache = _G.FollowMissionCache or {
    isFollowMission = false,
    lastCheck = 0,
    checkInterval = 2,  -- seconds between checks
}

-- Localized "Follow" words for multi-language support
-- prefix = check starts with (SVO languages like English)
-- contains = check anywhere (SOV languages like Japanese/Korean, or RTL like Arabic)
-- NOTE: Lua's :lower() only works on ASCII, so we include both cases for non-ASCII
local FOLLOW_WORDS_PREFIX = {
    "follow",       -- EN_US (lowercase handled by :lower())
    "sigue",        -- ES_ES / ES_MX
    "suis",         -- FR_FR
    "segui",        -- IT_IT
    "folge",        -- DE_DE
    "siga",         -- PT_BR
    "podążaj",      -- PL_PL (lowercase)
    "Podążaj",      -- PL_PL (capitalized - Lua :lower() doesn't handle Polish)
    "следуй",       -- RU_RU (lowercase)
    "Следуй",       -- RU_RU (capitalized - Lua :lower() doesn't handle Cyrillic)
    "跟随",         -- ZH_CN (Simplified - no case)
    "跟隨",         -- ZH_TW (Traditional - no case)
}

local FOLLOW_WORDS_CONTAINS = {
    "ついていく",    -- JA_JP (verb often at end, no case)
    "ついて",        -- JA_JP alternate
    "따라가",        -- KO_KR (SOV language, no case)
    "اتبع",         -- AR_AE (RTL, verb position varies)
    "تابع",         -- AR_AE alternate
}

-- Static NPCs - can't or shouldn't move to face you, use no-op lock
-- Check is by voice ID (lowercase for case-insensitive lookup).
-- Station-based checks (e.g., desks) are handled separately.
local STATIC_NPCS = {
    -- Portraits
    ["ferdinandoctaviuspratt"] = true,
    ["fatlady"] = true,
    ["marydunne"] = true,
    ["lethiaburbley"] = true,
    ["sircadogan"] = true,
    ["musicconductor"] = true,
    ["sylviapembroke"] = true,
    ["ogletheportrait"] = true,
    -- Ghosts (animation overrides rotation)
    ["cuthbertbinns"] = true,
}

-- Keywords in station owner name that indicate the NPC is sitting/static
local SITTING_KEYWORDS = {
    "Desk",
    "ProfessorDesk",
    "Sitting",
    "Chair",
    "DrinkingTable",
    "Stool",
    "LedgeSit",
    "EdgeSit",
    "Bench",
    "Table_GreatHall",
    "Couch",
    "Sofa"
}

-- ============================================
-- Initialization
-- ============================================

--- Initialize the module with a static cache getter function
--- @param staticCacheGetter function Returns static cache data (with refresh if needed)
function NPCLock.init(staticCacheGetter)
    getStaticCache = staticCacheGetter
    EnsureLockGazeLoop()
end

AbsorbLingeringLock = function(lockId, npcName, npcNameNorm)
    local data = _G.LockedNPCs[lockId]
    if not data or not data.lingering then
        return false
    end

    local resolvedNameNorm = npcNameNorm or _G.LockedNPCNames[lockId] or data.npcNameNorm
    local displayName = npcName or data.npcName or resolvedNameNorm or ("lock " .. tostring(lockId))
    print("[NPCLock] Absorbing linger lock for " .. tostring(displayName))

    NPCLock.StopLockGaze(lockId)

    if resolvedNameNorm and _G.LingerState.locks[resolvedNameNorm] == lockId then
        _G.LingerState.locks[resolvedNameNorm] = nil
    end

    if resolvedNameNorm and _G.LingerState.goodbyePending then
        local pending = _G.LingerState.goodbyePending
        if pending.locks and pending.locks[resolvedNameNorm] == lockId then
            pending.locks[resolvedNameNorm] = nil
        end
        if pending.selectedNames then
            pending.selectedNames[resolvedNameNorm] = nil
        end
    end

    _G.LockedNPCs[lockId] = nil
    _G.LockedNPCNames[lockId] = nil
    NPCLock.ResetLingerTimer()
    return true
end

function NPCLock.ClaimLingerGoodbye(generation, speakerIds)
    local pending = _G.LingerState.goodbyePending
    if not pending or pending.generation ~= generation then
        print("[NPCLock] Goodbye claim ignored - stale generation " .. tostring(generation))
        return false
    end

    pending.claimed = true
    pending.selectedNames = {}
    ClearGoodbyeTimer()

    local keepNames = {}
    for _, speakerId in ipairs(speakerIds or {}) do
        if type(speakerId) == "string" and speakerId ~= "" then
            keepNames[speakerId:gsub(" ", ""):lower()] = true
        end
    end

    if not next(keepNames) then
        ReleaseGoodbyePendingBatch(generation, "empty_claim")
        return false
    end

    for nameNorm, lockId in pairs(pending.locks or {}) do
        if keepNames[nameNorm] then
            pending.selectedNames[nameNorm] = true
        else
            if _G.LockedNPCs[lockId] then
                NPCLock.ReleaseNPC(lockId)
            end
            pending.locks[nameNorm] = nil
        end
    end

    if not next(pending.locks or {}) then
        ClearGoodbyePending()
        BumpLingerGeneration()
        return false
    end

    print("[NPCLock] Claimed linger goodbye batch generation " .. generation)
    return true
end

function NPCLock.AbortLingerGoodbye(generation, reason)
    local released = ReleaseGoodbyePendingBatch(generation, reason or "abort")
    return released > 0
end

-- ============================================
-- Internal Helpers
-- ============================================

--- Check if player is in a state where NPC locking should be disabled
--- @return boolean canLock, string|nil reason
local function CanLockNPCs()
    -- Check mount (broom, hippogriff, graphorn, etc.)
    if _G.MountState and _G.MountState.mounted then
        return false, "on mount"
    end

    -- Check combat
    local staticData = Cache.GetStaticData()
    local player = staticData and staticData.player
    if player then
        local inCombat = false
        pcall(function() inCombat = player.bInCombatMode or false end)
        if inCombat then
            return false, "in combat"
        end
    end

    return true, nil
end

-- Export for unified loop access
NPCLock.CanLockNPCs = CanLockNPCs

--- Check if the player is inside Hogwarts Castle (good nav mesh area).
--- Outside Hogwarts Castle, NPCs use snap rotation instead of MoveToLocation.
--- @return boolean true if inside Hogwarts Castle, defaults to true if location unavailable
local function IsInsideHogwartsCastle()
    if not _G.GetCurrentLocation then return true end
    local ok, loc, locationId = pcall(_G.GetCurrentLocation)
    if not ok or not loc then return true end

    -- Prefer the language-independent region ID. Display names are localized,
    -- and broad outdoor regions such as "Hogwarts Valley" also contain Hogwarts.
    if locationId then
        local normalizedId = locationId:gsub("[^%w]", ""):lower()
        return normalizedId:find("hogwartscastle", 1, true) ~= nil
    end

    -- Keep the safe inside-castle fallback when only the old generic value is
    -- available, but do not classify every Hogwarts-named outdoor area as inside.
    return loc == "Hogwarts" or loc == "Hogwarts Castle"
end

--- Check if currently in a "follow NPC" mission (cached, updates every 2s)
--- @return boolean isFollowMission
local function IsFollowMission()
    local cache = _G.FollowMissionCache
    local now = os.clock()

    -- Return cached value if still fresh
    if (now - cache.lastCheck) < cache.checkInterval then
        return cache.isFollowMission
    end

    -- Time to refresh
    cache.lastCheck = now
    cache.isFollowMission = false

    -- Get mission info (this uses widget cache internally)
    local mission = Utils.GetCurrentMission()
    if not mission or not mission.shortObjectives then
        return false
    end

    -- Check if any short objective has a "follow" word
    for _, objective in ipairs(mission.shortObjectives) do
        local objLower = objective:lower()

        -- Check prefix words (SVO languages - verb first)
        for _, followWord in ipairs(FOLLOW_WORDS_PREFIX) do
            if objLower:sub(1, #followWord) == followWord then
                cache.isFollowMission = true
                return true
            end
        end

        -- Check contains words (SOV/RTL languages - verb position varies)
        for _, followWord in ipairs(FOLLOW_WORDS_CONTAINS) do
            if objLower:find(followWord, 1, true) then
                cache.isFollowMission = true
                return true
            end
        end
    end

    return false
end

-- Export for external use
NPCLock.IsFollowMission = IsFollowMission

--- Check if an NPC or character ID matches the current companion
--- @param npcOrId NPC_Character|string Either an NPC UObject or a character ID string
--- @return boolean isCompanion
--- @return CompanionManager|nil companionMgr (only returned when called with NPC object)
local function IsCompanion(npcOrId)
    if not npcOrId then return false, nil end

    local isString = type(npcOrId) == "string"

    -- String ID path
    if isString then
        if npcOrId == "" then return false, nil end

        local success, result = pcall(function()
            local staticData = Cache.GetStaticData()
            local companionMgr = staticData and staticData.companionManager
            if not companionMgr then return false end

            local companionPawn = companionMgr:GetPrimaryCompanionPawn()
            if not companionPawn then return false end

            local fullName = companionPawn:GetFullName()
            if not fullName then return false end

            print(string.format("[IsCompanionId] Companion fullName: %s", fullName))

            local id = companionPawn.OverrideCharacterID
            print(string.format("[IsCompanionId] Companion OverrideCharacterID: %s", id))

            local companionId = fullName:match("([^/]+)$")
            if not companionId then return false end

            print(string.format("[IsCompanionId] Companion companionId: %s", companionId))

            return companionId == npcOrId
        end)

        return success and result or false, nil
    end

    -- NPC object path
    local success, isComp, mgr = pcall(function()
        local staticData = Cache.GetStaticData()
        local companionMgr = staticData and staticData.companionManager
        if not companionMgr then return false, nil end

        local companionPawn = companionMgr:GetPrimaryCompanionPawn()
        if not companionPawn then return false, companionMgr end

        -- Compare by full name (UObject == doesn't work reliably in Lua)
        local npcName = npcOrId:GetFullName()
        local compName = companionPawn:GetFullName()

        if npcName == compName then
            print("[NPCLock] Detected companion: " .. tostring(npcName):sub(1,60))
            return true, companionMgr
        end
        return false, companionMgr
    end)

    if success then return isComp, mgr end
    return false, nil
end

-- Export for combat tracking and dialogue recording
NPCLock.IsCompanion = IsCompanion

--- Check if an NPC (by normalized name) is a follower.
--- Followers need special handling: after conversation lock release,
--- scheduling must stay disabled so they don't walk back to their station.
--- @param nameNorm string|nil Normalized voice name (lowercase, no spaces)
--- @return boolean isFollower
local function IsFollower(nameNorm)
    if not nameNorm or not _G.NPCFollowers then return false end
    for voiceName, _ in pairs(_G.NPCFollowers) do
        if voiceName:gsub(" ", ""):lower() == nameNorm then
            return true
        end
    end
    return false
end

--- Find existing lock for an NPC (if already locked)
--- @param npc userdata The NPC actor to check
--- @return number|nil lockId if found, nil otherwise
local function FindExistingLock(npc)
    local npcKey = GetActorIdentityKey and GetActorIdentityKey(npc)
    if not npcKey then
        return nil
    end

    for lockId, data in pairs(_G.LockedNPCs) do
        local lockedNpcKey = data and data.npc and GetActorIdentityKey(data.npc)
        if lockedNpcKey and lockedNpcKey == npcKey then
            return lockId
        end
    end
    return nil
end

local function GetActorGazeRefs(actor)
    DevLog.Log("NPCLock", "GetActorGazeRefs ENTER", tostring(actor))
    if not actor or not Utils.SafeIsValid(actor) then return nil end

    local staticData = (getStaticCache and getStaticCache()) or Cache.GetStaticData()
    DevLog.Log("NPCLock", "GetActorGazeRefs static cache OK", tostring(actor))
    local npcId = Utils.GetActorVoiceId(actor, staticData)
    if not npcId or npcId == "" then
        DevLog.Log("NPCLock", "GetActorGazeRefs missing npcId", tostring(actor))
        DevPrint("[NPCLock] GetActorGazeRefs: missing npcId")
        return nil
    end

    DevLog.Log("NPCLock", "GetActorGazeRefs OK", tostring(npcId))
    DevPrint("[NPCLock] GetActorGazeRefs: OK " .. tostring(npcId))
    return {
        npcId = npcId,
    }
end

local function GetTargetGazeId(actor)
    if not actor or not Utils.SafeIsValid(actor) then return nil end

    local staticData = (getStaticCache and getStaticCache()) or Cache.GetStaticData()
    local actorFullName = nil
    pcall(function() actorFullName = actor:GetFullName() end)

    -- Normalize the player target to the special Blueprint resolver ID.
    local playerFullName = staticData and staticData.playerFullName
    if actorFullName and playerFullName and actorFullName == playerFullName then
        return "player"
    end

    local player = staticData and staticData.player
    if actorFullName and player and Utils.SafeIsValid(player) then
        local resolvedPlayerFullName = nil
        pcall(function() resolvedPlayerFullName = player:GetFullName() end)
        if resolvedPlayerFullName and actorFullName == resolvedPlayerFullName then
            return "player"
        end
    end

    local className = nil
    pcall(function()
        local class = actor:GetClass()
        if class then
            className = class:GetFullName()
        end
    end)
    if className and className:find("Biped_Player", 1, true) then
        return "player"
    end

    local targetId = Utils.GetActorVoiceId(actor, staticData)
    if targetId and targetId ~= "" then
        return targetId
    end
    return nil
end

local function ReleaseActorGaze(actor)
    DevLog.Log("NPCLock", "ReleaseActorGaze ENTER", tostring(actor))
    if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then return false end

    local refs = GetActorGazeRefs(actor)
    if not refs or not refs.npcId then
        DevLog.Log("NPCLock", "ReleaseActorGaze no refs", tostring(actor))
        return false
    end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        DevLog.Log("NPCLock", "ReleaseActorGaze no ModActor", tostring(refs.npcId))
        DevPrint("[NPCLock] ReleaseActorGaze: no ModActor")
        return false
    end

    local releaseNpcGazeById = mod.ReleaseNpcGazeById
    if not releaseNpcGazeById then
        error("ModActor missing callable ReleaseNpcGazeById")
    end

    local out = {}
    DevLog.Log("NPCLock", "ReleaseActorGaze CALL", tostring(refs.npcId))
    local ok, err = pcall(function()
        releaseNpcGazeById(mod, refs.npcId, out)
    end)
    if not ok then
        DevLog.Log("NPCLock", "ReleaseActorGaze ERROR", tostring(err))
        print("[NPCLock] ReleaseActorGaze failed: " .. tostring(err))
        return false
    end

    DevLog.Log("NPCLock", "ReleaseActorGaze RESULT", tostring(out.Success))
    return out.Success == true
end

GetActorIdentityKey = function(actor)
    if not actor or not Utils.SafeIsValid(actor) then
        return nil
    end

    local fullName = nil
    pcall(function() fullName = actor:GetFullName() end)
    if fullName and fullName ~= "" then
        return fullName
    end

    return tostring(actor)
end

local function ApplyGazeUpdate(entry, refs, targetActor)
    DevLog.Log("NPCLock", "ApplyGazeUpdate ENTER", tostring(refs and refs.npcId), tostring(targetActor))
    if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then
        DevLog.Log("NPCLock", "ApplyGazeUpdate blocked", tostring(_G.SonorusState and _G.SonorusState.playerLoaded), tostring(Utils.IsGamePaused()))
        print("[NPCLock] ApplyGazeUpdate FAIL: playerLoaded=" .. tostring(_G.SonorusState and _G.SonorusState.playerLoaded) .. " paused=" .. tostring(Utils.IsGamePaused()))
        return false
    end
    if not refs or not refs.npcId then
        DevLog.Log("NPCLock", "ApplyGazeUpdate no refs")
        print("[NPCLock] ApplyGazeUpdate FAIL: refs=" .. tostring(refs) .. " npcId=" .. tostring(refs and refs.npcId))
        return false
    end

    local targetId = GetTargetGazeId(targetActor)
    if not targetId then
        DevLog.Log("NPCLock", "ApplyGazeUpdate no targetId", tostring(refs.npcId), tostring(targetActor))
        print("[NPCLock] ApplyGazeUpdate FAIL: targetId nil, targetActor=" .. tostring(targetActor))
        return false
    end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        DevLog.Log("NPCLock", "ApplyGazeUpdate no ModActor", tostring(refs.npcId), tostring(targetId))
        print("[NPCLock] ApplyGazeUpdate FAIL: no ModActor")
        return false
    end
    local applyNpcGazeById = mod.ApplyNpcGazeById
    if not applyNpcGazeById then
        error("ModActor missing callable ApplyNpcGazeById")
    end

    local out = {}
    print(string.format("[NPCLock] applynpcgazebyid CALL: npc=%s target=%s",
        tostring(refs.npcId), tostring(targetId)))
    DevLog.Log("NPCLock", "ApplyGazeUpdate CALL", tostring(refs.npcId), tostring(targetId))
    local ok, err = pcall(function()
        applyNpcGazeById(mod, refs.npcId, targetId, out)
    end)
    if not ok then
        DevLog.Log("NPCLock", "ApplyGazeUpdate ERROR", tostring(err))
        print("[NPCLock] applynpcgazebyid failed: " .. tostring(err))
        return false
    end

    DevLog.Log("NPCLock", "ApplyGazeUpdate RESULT", tostring(out.Success))
    print("[NPCLock] applynpcgazebyid RESULT: " .. tostring(out.Success))
    return out.Success == true
end

local function ClearAmbientGazeState(preserveCurrentPose)
    local state = _G.NPCAmbientGazeState
    if not state then
        return false
    end

    local actor = state.npc
    local npcKey = state.npcKey or "?"
    -- DevPrint("[NPCLock] ClearAmbientGazeState: preserve=" .. tostring(preserveCurrentPose) .. " npc=" .. tostring(npcKey) .. " reason=" .. tostring(state.stopReason))

    local released = false
    if not preserveCurrentPose and actor and Utils.SafeIsValid(actor) then
        DevPrint("[NPCLock] ClearAmbientGazeState: releasing gaze")
        released = ReleaseActorGaze(actor)
        DevPrint("[NPCLock] ClearAmbientGazeState: release done=" .. tostring(released))
    end

    if state.loopHandle then
        TickScheduler.Unregister("npc_ambient_gaze")
        state.loopHandle = nil
    end

    state.npc = nil
    state.targetActor = nil
    state.refs = nil
    state.npcKey = nil
    state.targetKey = nil
    state.holdUntil = 0
    state.holdSeconds = 0
    state.stopReason = nil
    DevPrint("[NPCLock] ClearAmbientGazeState: DONE")
    return released
end

local function StopAmbientGazeInternal(reasonOverride)
    local state = _G.NPCAmbientGazeState
    if state and reasonOverride then
        state.stopReason = reasonOverride
    end
    return ClearAmbientGazeState(false)
end

local function StopLockGazeInternal(lockId)
    DevLog.Log("NPCLock", "StopLockGazeInternal ENTER", tostring(lockId))
    local state = _G.NPCLockGazeState
    local entry = state and state.active and state.active[lockId]
    if not entry then
        DevLog.Log("NPCLock", "StopLockGazeInternal no entry", tostring(lockId))
        return false
    end

    local released = false
    if entry.npc and Utils.SafeIsValid(entry.npc) then
        DevLog.Log("NPCLock", "StopLockGazeInternal ReleaseActorGaze PRE", tostring(lockId))
        released = ReleaseActorGaze(entry.npc)
        DevLog.Log("NPCLock", "StopLockGazeInternal ReleaseActorGaze POST", tostring(lockId), tostring(released))
    end

    state.active[lockId] = nil
    if state.loopHandle and not next(state.active) then
        TickScheduler.Unregister("npc_lock_gaze")
        state.loopHandle = nil
    end

    DevLog.Log("NPCLock", "StopLockGazeInternal EXIT", tostring(lockId), tostring(released))
    return released
end

EnsureLockGazeLoop = function()
    local state = _G.NPCLockGazeState
    if not state or state.loopHandle or not next(state.active) then
        return
    end

    DevLog.Log("NPCLock", "EnsureLockGazeLoop START")
    state.loopHandle = true
    TickScheduler.Register("npc_lock_gaze", GAZE_UPDATE_MS, function()
        DevLog.Log("NPCLock", "LockGazeLoop TICK ENTER")
        local loopState = _G.NPCLockGazeState
        if not loopState then return end

        local stopIds = {}
        for lockId, entry in pairs(loopState.active) do
            DevLog.Log("NPCLock", "LockGazeLoop entry ENTER", tostring(lockId), tostring(entry and entry.npc), tostring(entry and entry.targetActor))
            local lockData = _G.LockedNPCs[lockId]
            if not lockData or not entry
                or not entry.npc or not Utils.SafeIsValid(entry.npc)
                or not entry.targetActor or not Utils.SafeIsValid(entry.targetActor) then
                DevLog.Log("NPCLock", "LockGazeLoop entry invalid", tostring(lockId))
                table.insert(stopIds, lockId)
            else
                local refs = entry.refs
                if not refs or not refs.npcId then
                    DevLog.Log("NPCLock", "LockGazeLoop refresh refs PRE", tostring(lockId))
                    refs = GetActorGazeRefs(entry.npc)
                    entry.refs = refs
                    DevLog.Log("NPCLock", "LockGazeLoop refresh refs POST", tostring(lockId), tostring(refs and refs.npcId))
                end

                if not refs or not refs.npcId then
                    DevLog.Log("NPCLock", "LockGazeLoop no refs", tostring(lockId))
                    table.insert(stopIds, lockId)
                else
                    DevLog.Log("NPCLock", "LockGazeLoop ApplyGazeUpdate PRE", tostring(lockId), tostring(refs.npcId))
                    if not ApplyGazeUpdate(entry, refs, entry.targetActor) then
                        DevLog.Log("NPCLock", "LockGazeLoop ApplyGazeUpdate failed", tostring(lockId))
                        table.insert(stopIds, lockId)
                    else
                        DevLog.Log("NPCLock", "LockGazeLoop ApplyGazeUpdate OK", tostring(lockId))
                    end
                end
            end
        end

        for _, lockId in ipairs(stopIds) do
            DevLog.Log("NPCLock", "LockGazeLoop stop id", tostring(lockId))
            StopLockGazeInternal(lockId)
        end

        if loopState.loopHandle and not next(loopState.active) then
            DevLog.Log("NPCLock", "LockGazeLoop cancel empty")
            TickScheduler.Unregister("npc_lock_gaze")
            loopState.loopHandle = nil
        end
        DevLog.Log("NPCLock", "LockGazeLoop TICK EXIT")
    end)
end

EnsureAmbientGazeLoop = function()
    local state = _G.NPCAmbientGazeState
    if not state or state.loopHandle or not state.npc or not state.targetActor then
        return
    end

    state.loopHandle = true
    TickScheduler.Register("npc_ambient_gaze", GAZE_UPDATE_MS, function()
        local ambientState = _G.NPCAmbientGazeState
        if not ambientState then return end

        if not (_G.SonorusState and _G.SonorusState.playerLoaded) then
            ambientState.stopReason = "world invalid"
            StopAmbientGazeInternal()
            return
        end

        if _G.ChatPreviewLock
            or _G.STTPreviewLock
        then
            ambientState.stopReason = "conversation/input"
            StopAmbientGazeInternal()
            return
        end

        if (_G.MountState and _G.MountState.mounted)
            or (_G.CombatState and _G.CombatState.active)
            or (_G.CinematicState and _G.CinematicState.active)
            or (_G.StealthState and _G.StealthState.active)
            or _G.PlayerIdleState
        then
            ambientState.stopReason = "state change"
            StopAmbientGazeInternal()
            return
        end

        if Utils and Utils.IsGamePaused and Utils.IsGamePaused() then
            ambientState.stopReason = "paused"
            StopAmbientGazeInternal()
            return
        end

        if not ambientState.npc or not Utils.SafeIsValid(ambientState.npc)
            or not ambientState.targetActor or not Utils.SafeIsValid(ambientState.targetActor)
        then
            ambientState.stopReason = "actor invalid"
            StopAmbientGazeInternal()
            return
        end

        local activeLockId = FindExistingLock(ambientState.npc)
        if activeLockId then
            local lockData = _G.LockedNPCs[activeLockId]
            if not (lockData and lockData.commitmentLock) then
                ambientState.stopReason = "conversation lock"
                StopAmbientGazeInternal()
                return
            end
        end

        if ambientState.holdUntil > 0 and os.clock() >= ambientState.holdUntil then
            ambientState.stopReason = "hold elapsed"
            StopAmbientGazeInternal()
            return
        end

        local refs = ambientState.refs
        if not refs or not refs.npcId then
            DevPrint("[NPCLock] AmbientLoop: refs stale, refreshing")
            refs = GetActorGazeRefs(ambientState.npc)
            ambientState.refs = refs
        end

        if not refs or not refs.npcId then
            DevPrint("[NPCLock] AmbientLoop: no refs after refresh, stopping")
            ambientState.stopReason = "gaze update failed"
            StopAmbientGazeInternal()
            return
        end

        DevPrint("[NPCLock] AmbientLoop: ApplyGazeUpdate PRE " .. tostring(refs.npcId))
        local gazeOk = ApplyGazeUpdate(ambientState, refs, ambientState.targetActor)
        DevPrint("[NPCLock] AmbientLoop: ApplyGazeUpdate POST ok=" .. tostring(gazeOk))
        if not gazeOk then
            ambientState.stopReason = "gaze update failed"
            StopAmbientGazeInternal()
        end
    end)
end

function NPCLock.StartLockGaze(lockId, npc, targetActor)
    DevLog.Log("NPCLock", "StartLockGaze ENTER", tostring(lockId), tostring(npc), tostring(targetActor))
    if not _G.NPCAmbientGazeEnabled then
        DevLog.Log("NPCLock", "StartLockGaze disabled", tostring(lockId))
        return false
    end
    if lockId == nil or not npc or not targetActor then
        DevLog.Log("NPCLock", "StartLockGaze missing args", tostring(lockId), tostring(npc), tostring(targetActor))
        return false
    end
    if not Utils.SafeIsValid(npc) or not Utils.SafeIsValid(targetActor) then
        DevLog.Log("NPCLock", "StartLockGaze invalid actor", tostring(lockId))
        return false
    end

    DevLog.Log("NPCLock", "StartLockGaze StopLockGazeInternal PRE", tostring(lockId))
    StopLockGazeInternal(lockId)
    DevLog.Log("NPCLock", "StartLockGaze StopLockGazeInternal POST", tostring(lockId))

    local refs = GetActorGazeRefs(npc)
    if not refs then
        DevLog.Log("NPCLock", "StartLockGaze no refs", tostring(lockId))
        return false
    end

    DevLog.Log("NPCLock", "StartLockGaze set active PRE", tostring(lockId), tostring(refs.npcId))
    _G.NPCLockGazeState.active[lockId] = {
        npc = npc,
        targetActor = targetActor,
        refs = refs,
    }
    DevLog.Log("NPCLock", "StartLockGaze EnsureLockGazeLoop PRE", tostring(lockId))
    EnsureLockGazeLoop()
    DevLog.Log("NPCLock", "StartLockGaze EXIT", tostring(lockId))
    return true
end

function NPCLock.StopLockGaze(lockId)
    return StopLockGazeInternal(lockId)
end

function NPCLock.StopAllLockGaze()
    if not _G.NPCAmbientGazeEnabled then return false end
    local stopped = false
    local lockIds = {}
    for lockId, _ in pairs(_G.NPCLockGazeState.active) do
        table.insert(lockIds, lockId)
    end
    for _, lockId in ipairs(lockIds) do
        if StopLockGazeInternal(lockId) then
            stopped = true
        end
    end

    if _G.NPCLockGazeState.loopHandle then
        TickScheduler.Unregister("npc_lock_gaze")
        _G.NPCLockGazeState.loopHandle = nil
    end

    if StopAmbientGazeInternal("stop all lock gaze") then
        stopped = true
    end

    _G.NPCLockGazeState.active = {}
    return stopped
end

function NPCLock.StartAmbientGaze(npc, targetActor)
    if not _G.NPCAmbientGazeEnabled then return false end
    if not npc or not targetActor then
        return false
    end
    if not Utils.SafeIsValid(npc) or not Utils.SafeIsValid(targetActor) then
        return false
    end

    local state = _G.NPCAmbientGazeState
    if not _G._NPCAmbientGazeRandomSeeded then
        _G._NPCAmbientGazeRandomSeeded = true
        math.randomseed(os.time())
        math.random()
        math.random()
        math.random()
    end

    local holdSeconds = AMBIENT_GAZE_MIN_HOLD_S + math.random() * (AMBIENT_GAZE_MAX_HOLD_S - AMBIENT_GAZE_MIN_HOLD_S)
    local now = os.clock()
    local npcKey = GetActorIdentityKey(npc)
    local targetKey = GetActorIdentityKey(targetActor)

    if state and state.npcKey and state.targetKey
        and npcKey and targetKey
        and state.npcKey == npcKey
        and state.targetKey == targetKey
    then
        -- Same NPC+target, extend hold
        state.holdSeconds = holdSeconds
        state.holdUntil = now + holdSeconds
        state.stopReason = nil
        state.npc = npc
        state.targetActor = targetActor
        if not state.refs or not state.refs.npcId then
            DevPrint("[NPCLock] StartAmbientGaze: refreshing stale refs for " .. tostring(npcKey))
            state.refs = GetActorGazeRefs(npc)
        end
        EnsureAmbientGazeLoop()
        return true
    end

    -- Different NPC or first start
    DevPrint("[NPCLock] StartAmbientGaze: NEW npc=" .. tostring(npcKey) .. " hold=" .. string.format("%.1f", holdSeconds) .. "s")
    state.stopReason = "retarget"
    StopAmbientGazeInternal("retarget")

    local refs = GetActorGazeRefs(npc)
    if not refs then
        DevPrint("[NPCLock] StartAmbientGaze: GetActorGazeRefs returned nil, aborting")
        return false
    end

    state.npc = npc
    state.targetActor = targetActor
    state.refs = refs
    state.npcKey = npcKey
    state.targetKey = targetKey
    state.holdSeconds = holdSeconds
    state.holdUntil = now + holdSeconds
    state.stopReason = nil
    DevPrint("[NPCLock] StartAmbientGaze: loop starting")
    EnsureAmbientGazeLoop()
    return true
end

function NPCLock.IsAmbientGazeActor(actor)
    local state = _G.NPCAmbientGazeState
    local actorKey = GetActorIdentityKey(actor)
    return state ~= nil and state.npcKey ~= nil and actorKey ~= nil and state.npcKey == actorKey
end

function NPCLock.StopAmbientGaze(reason)
    return StopAmbientGazeInternal(reason or "external stop")
end

-- ============================================
-- Public API
-- ============================================

--- Check if an NPC (by name) is currently in an AI conversation (locked)
--- Uses cached names for thread-safe lookup (no UObject access)
--- @param name string The NPC name (voiceName or speakerName) to check
--- @return boolean isInConversation true if NPC is locked in a conversation
function NPCLock.IsNPCInConversation(name)
    if not name or name == "" or name == "Unknown" then
        return false
    end

    -- Normalize for comparison (remove spaces, lowercase)
    local nameNormalized = name:gsub(" ", ""):lower()

    -- Check against cached names (thread-safe, no UObject access)
    for _, cachedName in pairs(_G.LockedNPCNames) do
        if cachedName == nameNormalized then
            return true
        end
    end

    return false
end

--- Release a locked NPC
--- @param lockId number The ID returned by LockNPCToTarget
function NPCLock.ReleaseNPC(lockId)
    DevLog.Log("NPCLock", "ReleaseNPC ENTER", tostring(lockId))
    local data = _G.LockedNPCs[lockId]
    if not data then
        DevLog.Log("NPCLock", "ReleaseNPC no data", tostring(lockId))
        print("[NPCLock] No lock found for id=" .. tostring(lockId))
        return
    end

    if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then
        DevLog.Log("NPCLock", "ReleaseNPC loading cleanup", tostring(lockId))
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Lock " .. lockId .. " cleaned up (loading screen)")
        return
    end

    DevLog.Log("NPCLock", "ReleaseNPC StopLockGaze PRE", tostring(lockId))
    NPCLock.StopLockGaze(lockId)
    DevLog.Log("NPCLock", "ReleaseNPC StopLockGaze POST", tostring(lockId))

    -- Check if this NPC is a follower — if so, don't re-enable scheduling
    -- (follower system manages scheduling; re-enabling would send NPC back to station)
    local nameNorm = _G.LockedNPCNames[lockId] or (data.npcNameNorm)
    local isFollowerNPC = IsFollower(nameNorm)
    if nameNorm and _G.LingerState.goodbyePending and _G.LingerState.goodbyePending.locks then
        if _G.LingerState.goodbyePending.locks[nameNorm] == lockId then
            _G.LingerState.goodbyePending.locks[nameNorm] = nil
        end
    end

    -- Companion lock: pulse pattern already cleaned up, just clear state
    if data.isCompanionLock then
        DevLog.Log("NPCLock", "ReleaseNPC companion clear PRE", tostring(lockId))
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        DevLog.Log("NPCLock", "ReleaseNPC companion clear POST", tostring(lockId))
        print("[NPCLock] Companion released (id=" .. lockId .. ")")
        return
    end

    -- Static lock: nothing to restore (no-op lock for portraits, desk NPCs, etc.)
    if data.isStaticLock then
        DevLog.Log("NPCLock", "ReleaseNPC static clear PRE", tostring(lockId))
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        DevLog.Log("NPCLock", "ReleaseNPC static clear POST", tostring(lockId))
        print("[NPCLock] Static NPC released (id=" .. lockId .. ")")
        return
    end

    -- Snap lock: no move task was issued
    if data.isSnapLock then
        -- If this was a commitment lock being absorbed by a conversation lock,
        -- keep placed=true so it doesn't get re-teleported. Clear lockId since it's gone.
        if data.commitmentLock and nameNorm then
            for npcId, entry in pairs(_G.ActiveCommitments or {}) do
                if entry.lockId == lockId then
                    entry.placed = false
                    entry.lockId = nil
                    print("[NPCLock] Reset commitment placement for " .. npcId)
                    break
                end
            end
        end
        if not isFollowerNPC then
            local npcId = data.npcName or BlueprintHelpers.ToVoiceId(data.npc) or nameNorm
            if not BlueprintHelpers.ReleaseNpcTurnLockById(npcId) then
                print("[NPCLock] ReleaseNpcTurnLockById failed for snap lock: " .. tostring(npcId))
            end
        end
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Snap-locked NPC released (id=" .. lockId .. (isFollowerNPC and ", follower)" or ")"))
        return
    end

    if isFollowerNPC then
        -- Follower: keep scheduling disabled so they stay put
        -- Reset follower locked flag so followerTick picks them up again
        if data.scheduledEntity and Utils.SafeIsValid(data.scheduledEntity) then
            pcall(function()
                data.scheduledEntity:EnableScheduling(false, true, true)
            end)
        end
        for _, fData in pairs(_G.NPCFollowers) do
            if fData.npcName and fData.npcName:gsub(" ", ""):lower() == nameNorm then
                fData.locked = true  -- locked in place, followerTick will unlock when player moves away
                break
            end
        end
    else
        local npcId = data.npcName or BlueprintHelpers.ToVoiceId(data.npc) or nameNorm
        if not BlueprintHelpers.ReleaseNpcTurnLockById(npcId) then
            print("[NPCLock] ReleaseNpcTurnLockById failed for NPC: " .. tostring(npcId))
        end
    end

    _G.LockedNPCs[lockId] = nil
    _G.LockedNPCNames[lockId] = nil
    print("[NPCLock] NPC released (id=" .. lockId .. (isFollowerNPC and ", follower)" or ")"))
end

--- Release all currently locked NPCs
function NPCLock.ReleaseAllNPCs()
    StopAmbientGazeInternal("release all npcs")

    -- Cancel any active linger state (forced release cleans up everything)
    NPCLock.CancelLinger()

    -- Check for interrupt lock FIRST before releasing NPCs
    local preservedLockId = nil
    if _G.ChatPreviewLock and _G.ChatPreviewLock.interruptLock then
        preservedLockId = _G.ChatPreviewLock.lockId
        print("[NPCLock] Preserving interruptLock for: " .. tostring(_G.ChatPreviewLock.npcName))
        -- Clear the flag so subsequent resets will clear it
        _G.ChatPreviewLock.interruptLock = nil
    end

    local count = 0
    for lockId, _ in pairs(_G.LockedNPCs) do
        -- Skip the interrupt lock's NPC
        if lockId ~= preservedLockId then
            NPCLock.ReleaseNPC(lockId)
            count = count + 1
        end
    end
    -- Safety clear of name cache (but preserve the interrupt lock's name)
    if preservedLockId then
        local preservedName = _G.LockedNPCNames[preservedLockId]
        _G.LockedNPCNames = {}
        if preservedName then
            _G.LockedNPCNames[preservedLockId] = preservedName
        end
    else
        _G.LockedNPCNames = {}
    end
    -- Clear preview lock states if they exist (combat/broom/pause cleanup)
    -- Exception: interruptLock was already handled above
    if _G.ChatPreviewLock and not preservedLockId then
        print("[NPCLock] Clearing ChatPreviewLock: " .. tostring(_G.ChatPreviewLock.npcName) ..
            " (state=" .. tostring(_G.ChatPreviewLock.state) .. ")")
        _G.ChatPreviewLock = nil
    end
    if _G.STTPreviewLock then
        print("[NPCLock] Clearing STTPreviewLock: " .. tostring(_G.STTPreviewLock.npcName) ..
            " (state=" .. tostring(_G.STTPreviewLock.state) .. ")")
        _G.STTPreviewLock = nil
    end
    if count > 0 then
        print("[NPCLock] Released all " .. count .. " locked NPCs")
    end
end

--- Create a commitment lock: freeze NPC at current position without rotation/movement.
--- Used by CommitmentManager for teleport-placed NPCs.
--- @param npc userdata The NPC actor (already positioned)
--- @param scheduledEntity userdata The NPC's ScheduledEntity
--- @param npcName string The NPC's voice ID
--- @return number|nil lockId ID to use with ReleaseNPC, or nil on failure
function NPCLock.CreateCommitmentLock(npc, scheduledEntity, npcName)
    if not npc or not scheduledEntity then
        print("[NPCLock] CreateCommitmentLock: missing npc or scheduledEntity")
        return nil
    end

    -- AbandonStations + freeze (same as snap lock but no rotation)
    pcall(function() scheduledEntity:AbandonStations(0) end)
    pcall(function() scheduledEntity:PerformTask_RemoveActivePerformTask() end)
    pcall(function()
        local movement = npc.CharacterMovement
        if movement and Utils.SafeIsValid(movement) then
            movement:StopMovementImmediately()
        end
    end)
    pcall(function() scheduledEntity:EnableScheduling(false, true, true) end)

    lockIdCounter = lockIdCounter + 1
    local lockId = lockIdCounter

    local nameNorm = npcName and npcName:gsub(" ", ""):lower() or nil
    if nameNorm then
        _G.LockedNPCNames[lockId] = nameNorm
    end

    _G.LockedNPCs[lockId] = {
        npc = npc,
        targetActor = nil,
        scheduledEntity = scheduledEntity,
        locked = true,
        isSnapLock = true,
        commitmentLock = true,
        npcName = npcName,
        npcNameNorm = nameNorm,
    }

    print("[NPCLock] Commitment lock created (id=" .. lockId .. ") for " .. tostring(npcName))
    return lockId
end

--- Lock an NPC to face a target actor
--- If NPC is already locked, updates their target and re-locks
--- @param npc userdata The NPC actor to lock
--- @param targetActor userdata The actor to face (usually player)
--- @param onLocked function|nil Optional callback when NPC is locked in place
--- @return number|nil lockId ID to use with ReleaseNPC, or nil on failure
function NPCLock.LockNPCToTarget(npc, targetActor, onLocked)
    if not npc or not targetActor then
        print("[NPCLock] Missing npc or targetActor")
        return nil
    end

    -- Check if locking is allowed
    local canLock, reason = CanLockNPCs()
    if not canLock then
        print("[NPCLock] Cannot lock NPC: " .. tostring(reason))
        return nil
    end

    if NPCLock.IsAmbientGazeActor(npc) then
        ClearAmbientGazeState(true)
    else
        StopAmbientGazeInternal("lock npc to target")
    end

    -- Check if NPC is already locked - if so, absorb or release
    local existingLock = FindExistingLock(npc)
    local isRelock = false
    if existingLock then
        DevLog.Log("NPCLock", "Existing lock found", tostring(existingLock), tostring(npc), tostring(targetActor))
        isRelock = true
        local existingData = _G.LockedNPCs[existingLock]
        if existingData and existingData.commitmentLock then
            -- Commitment lock: silently clear it — conversation lock takes over seamlessly
            -- Don't call ReleaseNPC which would re-enable scheduling and cause NPC to walk off
            for npcId, entry in pairs(_G.ActiveCommitments or {}) do
                if entry.lockId == existingLock then
                    entry.lockId = nil
                    print("[NPCLock] Commitment lock absorbed for " .. npcId)
                    break
                end
            end
            _G.LockedNPCs[existingLock] = nil
            _G.LockedNPCNames[existingLock] = nil
        elseif not AbsorbLingeringLock(
            existingLock,
            existingData and existingData.npcName,
            existingData and existingData.npcNameNorm
        ) then
            print("[NPCLock] NPC already locked (id=" .. existingLock .. "), updating target")
            DevLog.Log("NPCLock", "Existing lock release PRE", tostring(existingLock))
            NPCLock.ReleaseNPC(existingLock)
            DevLog.Log("NPCLock", "Existing lock release POST", tostring(existingLock))
        end
    end

    -- Check if this NPC has a lingering lock (name-based, since UObject wrappers change)
    if not isRelock and _G.LingerState.active then
        -- We need the voice ID to check — get it early via a quick static cache access
        local quickStaticData = getStaticCache and getStaticCache() or Cache.GetStaticData()
        local quickName = quickStaticData and Utils.GetActorVoiceId(npc, quickStaticData)
        if quickName then
            local nameNorm = quickName:gsub(" ", ""):lower()
            local lingerLockId = _G.LingerState.locks[nameNorm]
            if lingerLockId and _G.LockedNPCs[lingerLockId] then
                if AbsorbLingeringLock(lingerLockId, quickName, nameNorm) then
                    isRelock = true  -- skip station check — NPC is already out
                end
            end
        end
    end

    -- Get static cache (with refresh if needed)
    local staticData = getStaticCache and getStaticCache() or Cache.GetStaticData()
    local popManager = staticData and staticData.populationManager
    if not popManager then
        print("[NPCLock] PopulationManager not found")
        return nil
    end

    -- Get ScheduledEntity
    local scheduledEntity = nil
    pcall(function()
        scheduledEntity = popManager:GetScheduledEntityFromActor(npc, false)
    end)
    if not scheduledEntity then
        print("[NPCLock] No ScheduledEntity for this NPC")
        return nil
    end

    -- Generate lock ID
    lockIdCounter = lockIdCounter + 1
    local lockId = lockIdCounter
    local function activateLockGaze()
        DevLog.Log("NPCLock", "activateLockGaze ENTER", tostring(lockId), tostring(npc), tostring(targetActor))
        pcall(function()
            NPCLock.StartLockGaze(lockId, npc, targetActor)
        end)
        DevLog.Log("NPCLock", "activateLockGaze EXIT", tostring(lockId))
    end

    -- Get NPC voice ID for checks and caching
    local npcName = Utils.GetActorVoiceId(npc, staticData)

    -- Check if NPC should be static (no-op lock) - by name or station type
    local isStatic = false
    local staticReason = nil

    -- Check by NPC name (portraits, ghosts, etc.)
    if npcName and STATIC_NPCS[npcName:lower()] then
        isStatic = true
        staticReason = "static NPC"
    end

    -- Check by station type (desks, etc.)
    if not isStatic then
        pcall(function()
            local station = scheduledEntity:GetActiveStation()
            if station then
                local owner = station:GetOwner()
                if owner then
                    local ownerName = nil
                    pcall(function() ownerName = owner:GetFullName() end)
                    _G.DevPrint("[DEBUG] GetActiveStation ownerName",ownerName)
                    if ownerName then
                        for _, keyword in ipairs(SITTING_KEYWORDS) do
                            if ownerName:find(keyword) then
                                isStatic = true
                                staticReason = "sitting"
                                break
                            end
                        end
                    end
                end
            end
        end)
    end

    -- Check if in a follow mission (NPC is leading player, don't interrupt their movement)
    if not isStatic then
        if IsFollowMission() then
            isStatic = true
            staticReason = "follow mission"
        end
    end

    -- Create no-op lock for static NPCs
    if isStatic then
        if npcName then
            _G.LockedNPCNames[lockId] = npcName:gsub(" ", ""):lower()
        end
        _G.LockedNPCs[lockId] = {
            npc = npc,
            targetActor = targetActor,
            scheduledEntity = nil,
            locked = true,
            isStaticLock = true
        }
        activateLockGaze()
        print("[NPCLock] Static lock (" .. staticReason .. "): " .. tostring(npcName))
        return lockId
    end

    -- Cache NPC name for thread-safe lookup (used by IsNPCInConversation)
    if npcName and npcName ~= "" then
        _G.LockedNPCNames[lockId] = npcName:gsub(" ", ""):lower()
    end

    -- Companion lock: pulse pattern using SetCompanionForcedWaitLocation
    -- 1. SetCompanionForcedWaitLocation(200 units towards target) triggers pathfinding + turn
    -- 2. After turn delay: StopMovement(true/false) cancels walk, StopCompanionForcedWaiting
    --    restores follow. Companion ends up facing the target with follow behavior intact.
    local isComp, companionMgr = IsCompanion(npc)
    if isComp and companionMgr then
        DevLog.Log("NPCLock", "Companion branch ENTER", tostring(lockId), tostring(npc), tostring(targetActor))
        -- Guard: if the companion is already in a forced wait (quest/puzzle), don't override it.
        -- Use a no-op lock instead so IsNPCInConversation still works.
        if Utils.IsCompanionForcedWaiting(npc, companionMgr) then
            DevLog.Log("NPCLock", "Companion forced-wait static path", tostring(lockId))
            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isStaticLock = true  -- no-op release, won't call StopCompanionForcedWaiting
            }
            activateLockGaze()
            print("[NPCLock] Companion already in forced wait (quest?) - static lock (id=" .. lockId .. ")")
            if onLocked then pcall(onLocked) end
            return lockId
        end

        -- Pulse pattern: trigger turn via SetCompanionForcedWaitLocation, then cancel
        -- the walk and forced wait after the turn animation. Companion ends up facing
        -- the target with normal follow behavior restored.
        local turnAngle = 0
        local needsTurn = false

        pcall(function()
            DevLog.Log("NPCLock", "Companion angle calc ENTER", tostring(lockId))
            local tgtLoc = targetActor:K2_GetActorLocation()
            local npcLoc = npc:K2_GetActorLocation()
            local npcRot = npc:K2_GetActorRotation()

            local dx = tgtLoc.X - npcLoc.X
            local dy = tgtLoc.Y - npcLoc.Y
            local dist = math.sqrt(dx * dx + dy * dy)

            -- Normalize direction
            local dirX, dirY = 0, 1
            if dist > 1 then
                dirX = dx / dist
                dirY = dy / dist
            end

            -- Calculate turn angle
            local angleToTarget = math.atan(dirY, dirX) * 180 / math.pi
            local npcYaw = npcRot.Yaw or 0
            local diff = angleToTarget - npcYaw
            while diff > 180 do diff = diff - 360 end
            while diff < -180 do diff = diff + 360 end
            turnAngle = math.abs(diff)
            needsTurn = turnAngle > 50

            -- Lua only decides which path to use. BP owns the inside-castle
            -- forced-wait pulse so we do not keep companion manager refs here.
            DevLog.Log("NPCLock", "Companion angle calc EXIT", tostring(lockId), tostring(turnAngle), tostring(needsTurn))
        end)

        if needsTurn and not IsInsideHogwartsCastle() then
            DevLog.Log("NPCLock", "Companion outside non-BP turn path", tostring(lockId), tostring(turnAngle))
            -- Outside Hogwarts Castle: snap rotate only if player is stationary
            local playerSpeed = 0
            pcall(function()
                local staticData = Cache.GetStaticData()
                local player = staticData and staticData.player
                if player then
                    pcall(function()
                        local vel = player:GetVelocity()
                        if vel then
                            playerSpeed = math.sqrt(vel.X*vel.X + vel.Y*vel.Y + vel.Z*vel.Z)
                        end
                    end)
                end
            end)

            if playerSpeed < 10 then
                -- Player stationary: pulse stop → snap rotate → release
                pcall(function() companionMgr:StopMovement(true) end)
                pcall(function() companionMgr:StopMovement(false) end)

                local snapNpcId = npcName or BlueprintHelpers.ToVoiceId(npc)
                local snapTargetId = BlueprintHelpers.ToVoiceId(targetActor)
                local snapped = BlueprintHelpers.SnapNpcFaceTargetById(snapNpcId, snapTargetId)

                -- Release after 50ms so the rotation sticks for at least a frame
                _G._PendingCompanionSnapMgr = companionMgr
                ExecuteInGameThreadWithDelay(50, function()
                    BlueprintHelpers.SnapNpcFaceTargetById(snapNpcId, snapTargetId)
                    local mgr = _G._PendingCompanionSnapMgr
                    _G._PendingCompanionSnapMgr = nil
                    if mgr then
                        pcall(function() mgr:StopCompanionForcedWaiting() end)
                    end
                end)

                if snapped then
                    print("[NPCLock] Companion snap-rotated via BP (id=" .. lockId .. ", angle=" .. math.floor(turnAngle) .. ")")
                else
                    print("[NPCLock] Companion BP snap failed (id=" .. lockId .. ")")
                end
            else
                print("[NPCLock] Companion snap skipped - player moving (speed=" .. math.floor(playerSpeed) .. ")")
            end

            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isCompanionLock = true
            }
            activateLockGaze()
            DevLog.Log("NPCLock", "Companion outside path after activate", tostring(lockId))
            if onLocked then pcall(onLocked) end
        elseif needsTurn then
            DevLog.Log("NPCLock", "Companion BP turn path ENTER", tostring(lockId), tostring(turnAngle))
            -- Inside Hogwarts Castle: BP owns the animated companion turn start.
            local targetId = BlueprintHelpers.ToVoiceId(targetActor)
            if not targetId then
                _G.LockedNPCNames[lockId] = nil
                print("[NPCLock] Companion lock failed: missing target id")
                return nil
            end

            DevLog.Log("NPCLock", "Companion BP Start PRE", tostring(lockId), tostring(targetId))
            local bpTurn = BlueprintHelpers.StartCompanionTurnLockById(targetId)
            DevLog.Log("NPCLock", "Companion BP Start POST", tostring(lockId), tostring(bpTurn and bpTurn.success), tostring(bpTurn and bpTurn.needsDelayedFinish), tostring(bpTurn and bpTurn.turnAngle))
            if not bpTurn or not bpTurn.success then
                _G.LockedNPCNames[lockId] = nil
                print("[NPCLock] Companion lock failed: BP turn start failed")
                return nil
            end

            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isCompanionLock = true
            }

            local bpTurnAngle = bpTurn.turnAngle or turnAngle
            if bpTurn.needsDelayedFinish then
                local delay = bpTurnAngle > 120 and 700 or 500
                local capturedLockId = lockId
                local capturedOnLocked = onLocked
                ExecuteInGameThreadWithDelay(delay, function()
                    DevLog.Log("NPCLock", "Companion BP finish callback ENTER", tostring(capturedLockId))
                    local data = _G.LockedNPCs[capturedLockId]
                    if not data then return end

                    DevLog.Log("NPCLock", "Companion BP Finish PRE", tostring(capturedLockId))
                    if not BlueprintHelpers.FinishCompanionTurnLock() then
                        _G.LockedNPCs[capturedLockId] = nil
                        _G.LockedNPCNames[capturedLockId] = nil
                        print("[NPCLock] Companion lock failed: BP turn finish failed")
                        return
                    end

                    DevLog.Log("NPCLock", "Companion BP Finish POST", tostring(capturedLockId))
                    DevLog.Log("NPCLock", "Companion BP deferred gaze activate PRE", tostring(capturedLockId))
                    activateLockGaze()
                    DevLog.Log("NPCLock", "Companion BP deferred gaze activate POST", tostring(capturedLockId))
                    print("[NPCLock] Companion turn pulse complete (id=" .. capturedLockId .. ", angle=" .. math.floor(bpTurnAngle) .. ")")
                    if capturedOnLocked then pcall(capturedOnLocked) end
                end)

                print("[NPCLock] Companion lock (id=" .. lockId .. ", angle=" .. math.floor(bpTurnAngle) .. ", delay=" .. delay .. "ms)")
            else
                activateLockGaze()
                print("[NPCLock] Companion lock (id=" .. lockId .. ", angle=" .. math.floor(bpTurnAngle) .. ", no delay)")
                if onLocked then pcall(onLocked) end
            end
        else
            DevLog.Log("NPCLock", "Companion already-facing path ENTER", tostring(lockId), tostring(turnAngle))
            -- Already facing target, no-op static lock
            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isStaticLock = true
            }
            activateLockGaze()
            DevLog.Log("NPCLock", "Companion already-facing after activate", tostring(lockId))
            print("[NPCLock] Companion lock (id=" .. lockId .. ", angle=" .. math.floor(turnAngle) .. ", already facing)")
            if onLocked then pcall(onLocked) end
        end

        DevLog.Log("NPCLock", "Companion branch EXIT", tostring(lockId))
        return lockId
    end

    -- Normal NPC lock path (non-companions)
    -- Store state (including target for angle checking)
    _G.LockedNPCs[lockId] = {
        npc = npc,
        targetActor = targetActor,
        targetId = BlueprintHelpers.ToVoiceId(targetActor),
        scheduledEntity = scheduledEntity,
        locked = false,
        npcName = npcName,
        npcNameNorm = npcName and npcName:gsub(" ", ""):lower() or nil,
    }
    if npcName and _G.LingerState.goodbyePending and _G.LingerState.goodbyePending.selectedNames then
        local selectedNorm = npcName:gsub(" ", ""):lower()
        if _G.LingerState.goodbyePending.selectedNames[selectedNorm] then
            _G.LingerState.goodbyePending.locks[selectedNorm] = lockId
        end
    end
    activateLockGaze()

    local function cleanupDeadLock(reason)
        local data = _G.LockedNPCs[lockId]
        local nameNorm = _G.LockedNPCNames[lockId] or (data and data.npcNameNorm)

        NPCLock.StopLockGaze(lockId)

        if nameNorm and _G.LingerState.locks[nameNorm] == lockId then
            _G.LingerState.locks[nameNorm] = nil
        end

        if nameNorm and _G.LingerState.goodbyePending then
            local pending = _G.LingerState.goodbyePending
            if pending.locks and pending.locks[nameNorm] == lockId then
                pending.locks[nameNorm] = nil
            end
            if pending.selectedNames and pending.selectedNames[nameNorm] then
                pending.selectedNames[nameNorm] = nil
            end
        end

        if _G.ChatPreviewLock and _G.ChatPreviewLock.lockId == lockId then
            _G.ChatPreviewLock = nil
        end
        if _G.STTPreviewLock and _G.STTPreviewLock.lockId == lockId then
            _G.STTPreviewLock = nil
        end

        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Lock " .. tostring(lockId) .. " cleaned up: " .. tostring(reason))
    end

    local function markLockLocked(markAsSnapLock)
        local data = _G.LockedNPCs[lockId]
        if not data then
            print("[NPCLock] Lock " .. tostring(lockId) .. " missing during lock completion")
            return false
        end

        data.locked = true
        if markAsSnapLock then
            data.isSnapLock = true
        end
        return true
    end

    local function getCurrentLockData()
        local data = _G.LockedNPCs[lockId]
        if not data then
            return nil, "released"
        end
        if not data.npc or not Utils.SafeIsValid(data.npc) then
            return nil, "npc invalid"
        end
        if not data.targetActor or not Utils.SafeIsValid(data.targetActor) then
            return nil, "target invalid"
        end
        if not data.scheduledEntity or not Utils.SafeIsValid(data.scheduledEntity) then
            return nil, "scheduled entity invalid"
        end
        return data
    end

    -- The core lock sequence: AbandonStations → calculate angle → move → freeze
    -- Outside Hogwarts Castle, uses snap rotation (no nav mesh dependency)
    local function executeLockSequence()
        local currentData, invalidReason = getCurrentLockData()
        if not currentData then
            if invalidReason ~= "released" then
                cleanupDeadLock(invalidReason)
            end
            return
        end

        local currentNpc = currentData.npc
        local currentTargetActor = currentData.targetActor
        local currentScheduledEntity = currentData.scheduledEntity

        -- Outside Hogwarts Castle: snap rotation path (no nav mesh dependency)
        -- AbandonStations → disable scheduling → snap rotate. No MoveToLocation needed.
        if not IsInsideHogwartsCastle() then
            pcall(function() currentScheduledEntity:AbandonStations(0) end)
            pcall(function() currentScheduledEntity:PerformTask_RemoveActivePerformTask() end)
            pcall(function()
                local movement = currentNpc.CharacterMovement
                if movement and Utils.SafeIsValid(movement) then
                    movement:StopMovementImmediately()
                end
            end)
            pcall(function() currentScheduledEntity:EnableScheduling(false, true, true) end)

            -- Blueprint resolves both actors by stable ID and owns the rotation.
            local snapNpcId = currentData.npcName
            local snapTargetId = currentData.targetId
            if not BlueprintHelpers.SnapNpcFaceTargetById(snapNpcId, snapTargetId) then
                cleanupDeadLock("BP snap turn failed")
                return
            end
            ExecuteInGameThreadWithDelay(50, function()
                BlueprintHelpers.SnapNpcFaceTargetById(snapNpcId, snapTargetId)
            end)

            if not markLockLocked(true) then
                return
            end
            print("[NPCLock] NPC snap-locked (id=" .. lockId .. ")")
            if onLocked then pcall(onLocked) end
            return
        end

        -- Inside Hogwarts Castle: BP owns the animated turn start. If this fails,
        -- fail the lock instead of falling back to the old Lua UObject path.
        local npcId = BlueprintHelpers.ToVoiceId(currentNpc)
        local targetId = BlueprintHelpers.ToVoiceId(currentTargetActor)
        if not npcId or not targetId then
            cleanupDeadLock("missing npc/target id for BP turn lock")
            return
        end

        local bpTurn = BlueprintHelpers.StartNpcTurnLockById(npcId, targetId)
        if not bpTurn or not bpTurn.success then
            cleanupDeadLock("BP turn lock failed")
            return
        end

        if bpTurn.needsDelayedFinish then
            local delay = bpTurn.turnAngle > 120 and 700 or 500
            local capturedLockId = lockId
            local capturedOnLocked = onLocked
            local capturedNpcId = npcId
            ExecuteInGameThreadWithDelay(delay, function()
                if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC BP delay callback START") end
                local ok, err = pcall(function()
                    local data = _G.LockedNPCs[capturedLockId]
                    if not data then return end

                    if not BlueprintHelpers.FinishNpcTurnLockById(capturedNpcId) then
                        cleanupDeadLock("BP finish turn lock failed")
                        return
                    end
                    data.locked = true
                    print("[NPCLock] NPC locked after BP turn (id=" .. capturedLockId .. ")")

                    if capturedOnLocked then
                        pcall(capturedOnLocked)
                    end
                end)
                if not ok and _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC BP error: " .. tostring(err)) end
                if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC BP delay callback END") end
            end)
        else
            if not markLockLocked(false) then
                return
            end
            print("[NPCLock] NPC locked immediately via BP (id=" .. lockId .. ")")

            if onLocked then
                pcall(onLocked)
            end
        end
    end

    -- On re-locks or followers, skip station check entirely.
    -- Re-locks: NPC was already pulled from station on first lock.
    -- Followers: already off-station, managed by follower system.
    local isFollowerLock = npcName and IsFollower(npcName:gsub(" ", ""):lower())
    if isRelock or isFollowerLock then
        print("[NPCLock] " .. (isRelock and "Re-lock" or "Follower") .. " - skipping station check")
        executeLockSequence()
    else
        -- Check if NPC is at a station - if so, graceful exit first
        -- Skip station exit if NPC is in transit (walking between locations) —
        -- GetActiveStation() returns stale data for in-transit NPCs
        local stationComp = nil
        local inTransit = false
        pcall(function() inTransit = scheduledEntity:IsInTransit() end)
        if not inTransit then
            pcall(function() stationComp = scheduledEntity:GetActiveStation() end)
        end
        print("[NPCLock] Station check: " .. tostring(stationComp ~= nil) .. ", inTransit: " .. tostring(inTransit))

        if stationComp and not inTransit then
            -- Graceful exit: RequestStationExit starts natural exit animation,
            -- then after 2s we AbandonStations to take control
            print("[NPCLock] NPC at station - graceful exit (" .. STATION_EXIT_DELAY_MS .. "ms)")
            pcall(function() scheduledEntity:RequestStationExit(stationComp) end)
            ExecuteInGameThreadWithDelay(STATION_EXIT_DELAY_MS, function()
                -- Verify lock still exists (wasn't released during the delay)
                local delayedData = _G.LockedNPCs[lockId]
                if not delayedData then
                    print("[NPCLock] Lock " .. lockId .. " was released during graceful exit delay")
                    return
                end
                if not delayedData.npc or not Utils.SafeIsValid(delayedData.npc) then
                    cleanupDeadLock("npc invalid during station exit delay")
                    return
                end
                if not delayedData.targetActor or not Utils.SafeIsValid(delayedData.targetActor) then
                    cleanupDeadLock("target invalid during station exit delay")
                    return
                end
                if not delayedData.scheduledEntity or not Utils.SafeIsValid(delayedData.scheduledEntity) then
                    cleanupDeadLock("scheduled entity invalid during station exit delay")
                    return
                end
                if _G.DevPrint then _G.DevPrint("[DEBUG] NPCLock: executeLockSequence after station exit delay (id=" .. lockId .. ")") end
                executeLockSequence()
            end)
        else
            -- Not at a station, execute lock immediately
            executeLockSequence()
        end
    end

    print("[NPCLock] Started lock sequence (id=" .. lockId .. ")")
    return lockId
end

-- ============================================
-- Graceful Station Exit Helper
-- ============================================
-- Requests a natural station exit and fires callback when the NPC has fully left.
-- If NPC is not at a station, callback fires immediately.
-- Falls back to AbandonStations after timeout.
--
-- Usage:
--   NPCLock.GracefulStationExit(scheduledEntity, function()
--       -- NPC has finished exit animation, do something
--   end)
--
function NPCLock.GracefulStationExit(scheduledEntity, npcActor, callback, timeoutMs)
    timeoutMs = timeoutMs or 15000

    -- Check if NPC is at a station
    local stationComp = nil
    pcall(function() stationComp = scheduledEntity:GetActiveStation() end)

    if not stationComp then
        print("[NPCLock] GracefulStationExit: no active station, firing callback immediately")
        if ShowHint then ShowHint("Not at station", 2) end
        if callback then pcall(callback) end
        return
    end

    local stationName = "?"
    pcall(function() stationName = stationComp:GetFullName():match("([^%.]+)$") or stationComp:GetFullName() end)

    print(string.format("[NPCLock] GracefulStationExit: leaving %s", stationName))

    -- Request graceful exit
    print("[NPCLock] GracefulStationExit: requesting exit...")
    pcall(function() scheduledEntity:RequestStationExit(stationComp) end)

    -- Use fixed delay - animation detection APIs are unreliable from UE4SS
    local exitDelayMs = 2000
    if ShowHint then ShowHint(stationName .. ": exiting...", 3) end

    ExecuteInGameThreadWithDelay(exitDelayMs, function()
        print(string.format("[NPCLock] GracefulStationExit: DONE (%.1fs fixed delay)", exitDelayMs / 1000))
        if ShowHint then ShowHint("EXIT DONE", 2) end
        if callback then pcall(callback) end
    end)
end

-- ============================================
-- Snap Re-face Helper
-- ============================================

--- Re-face a snap-locked NPC to their target through the Blueprint bridge.
--- Used by the re-face loop to avoid the full release/re-lock cycle.
--- @param data table The lock data from _G.LockedNPCs
function NPCLock.SnapRefaceNPC(data)
    if not data then return false end
    return BlueprintHelpers.SnapNpcFaceTargetById(data.npcName, data.targetId)
end

-- ============================================
-- Post-Conversation Linger System
-- ============================================
-- After a conversation ends naturally, NPCs stay frozen in place for
-- LINGER_TIMEOUT_MS instead of immediately returning to their schedules.
-- If any lingering NPC is re-addressed, its linger lock is absorbed
-- (no station exit delay) and the timer resets for remaining lingerers.

BumpLingerGeneration = function()
    local state = _G.LingerState
    state.generation = (state.generation or 0) + 1
    return state.generation
end

ClearGoodbyeTimer = function()
    if _G.LingerState.goodbyeTimerHandle then
        pcall(function() CancelDelayedAction(_G.LingerState.goodbyeTimerHandle) end)
        _G.LingerState.goodbyeTimerHandle = nil
    end
end

ClearGoodbyePending = function()
    ClearGoodbyeTimer()
    _G.LingerState.goodbyePending = nil
end

ReleaseGoodbyePendingBatch = function(expectedGeneration, reason)
    local pending = _G.LingerState.goodbyePending
    if not pending or pending.generation ~= expectedGeneration then
        return 0
    end

    local count = 0
    for nameNorm, lockId in pairs(pending.locks or {}) do
        if _G.LockedNPCs[lockId] then
            NPCLock.ReleaseNPC(lockId)
            count = count + 1
        end
        pending.locks[nameNorm] = nil
    end

    ClearGoodbyePending()
    BumpLingerGeneration()
    if count > 0 then
        print("[NPCLock] Released pending linger goodbye batch (" .. tostring(reason or "unknown") .. ", " .. count .. " NPCs)")
    end
    return count
end

QueueLingerGoodbyeBatch = function()
    local state = _G.LingerState
    if not state.active or not next(state.locks) then
        state.active = false
        state.timerHandle = nil
        state.locks = {}
        return false
    end

    local generation = state.generation or 0
    local pendingLocks = {}
    local speakerIds = {}
    for nameNorm, lockId in pairs(state.locks) do
        if _G.LockedNPCs[lockId] then
            pendingLocks[nameNorm] = lockId
            local data = _G.LockedNPCs[lockId]
            local speakerId = data and data.npcName
            if speakerId and speakerId ~= "" then
                table.insert(speakerIds, speakerId)
            end
        end
    end

    state.active = false
    state.timerHandle = nil
    state.locks = {}

    if not next(pendingLocks) then
        ClearGoodbyePending()
        return false
    end

    state.goodbyePending = {
        generation = generation,
        locks = pendingLocks,
        claimed = false,
        selectedNames = {},
    }

    if not (_G.SocketClient and _G.SocketClient.isConnected and _G.SocketClient.isConnected() and _G.SocketClient.send) then
        ReleaseGoodbyePendingBatch(generation, "socket_unavailable")
        return false
    end

    local sent = _G.SocketClient.send({
        type = "game_event",
        event = "linger:goodbye_due",
        data = {
            generation = generation,
            speaker_ids = speakerIds,
            speaker_count = #speakerIds,
        }
    })
    if not sent then
        ReleaseGoodbyePendingBatch(generation, "event_send_failed")
        return false
    end

    ClearGoodbyeTimer()
    state.goodbyeTimerHandle = ExecuteInGameThreadWithDelay(GOODBYE_CLAIM_TIMEOUT_MS, function()
        local currentPending = _G.LingerState.goodbyePending
        if not currentPending or currentPending.generation ~= generation or currentPending.claimed then
            return
        end
        ReleaseGoodbyePendingBatch(generation, "claim_timeout")
    end)
    print("[NPCLock] Linger expired - awaiting goodbye claim for generation " .. generation)
    return true
end

--- Cancel any active linger timer and clear linger state.
--- Called by ReleaseAllNPCs() for forced releases (F8, combat, broom, loading).
function NPCLock.CancelLinger()
    for _, lockId in pairs(_G.LingerState.locks) do
        NPCLock.StopLockGaze(lockId)
    end
    if _G.LingerState.timerHandle then
        pcall(function() CancelDelayedAction(_G.LingerState.timerHandle) end)
    end
    _G.LingerState.active = false
    _G.LingerState.timerHandle = nil
    _G.LingerState.locks = {}
    ClearGoodbyePending()
    BumpLingerGeneration()
end

--- Release only lingering NPCs (timer callback).
function NPCLock.ReleaseLingeringNPCs()
    if QueueLingerGoodbyeBatch() then
        return
    end
    local count = 0
    for nameNorm, lockId in pairs(_G.LingerState.locks) do
        if _G.LockedNPCs[lockId] then
            NPCLock.ReleaseNPC(lockId)
            count = count + 1
        end
    end
    _G.LingerState.active = false
    _G.LingerState.timerHandle = nil
    _G.LingerState.locks = {}
    if count > 0 then
        print("[NPCLock] Linger expired — released " .. count .. " NPCs")
    end
end

--- Reset the linger timer for remaining lingerers.
--- If no lingerers remain, clears linger state entirely.
function NPCLock.ResetLingerTimer()
    -- Cancel existing timer
    if _G.LingerState.timerHandle then
        pcall(function() CancelDelayedAction(_G.LingerState.timerHandle) end)
        _G.LingerState.timerHandle = nil
    end

    -- Check if any lingerers remain
    local hasLingerers = false
    for _ in pairs(_G.LingerState.locks) do
        hasLingerers = true
        break
    end

    if not hasLingerers then
        _G.LingerState.active = false
        _G.LingerState.locks = {}
        print("[NPCLock] No lingerers remain — linger state cleared")
        return
    end

    -- Restart timer
    _G.LingerState.timerHandle = ExecuteInGameThreadWithDelay(LINGER_TIMEOUT_MS, function()
        NPCLock.ReleaseLingeringNPCs()
    end)
    print("[NPCLock] Linger timer reset (" .. LINGER_TIMEOUT_MS .. "ms)")
end

--- Transition all current normal locks to lingering state.
--- Called when a conversation ends naturally (not F8/combat/broom).
--- Companion and static locks are released immediately (they don't need to linger).
function NPCLock.LingerAllNPCs()
    -- Cancel any existing linger first (fresh start)
    NPCLock.CancelLinger()

    local lingerCount = 0
    local releaseIds = {}

    for lockId, data in pairs(_G.LockedNPCs) do
        NPCLock.StopLockGaze(lockId)
        -- Companion, static, and follower locks: release immediately (no lingering)
        -- Followers: ReleaseNPC keeps scheduling disabled, followerTick takes over
        if data.isCompanionLock or data.isStaticLock or IsFollower(data.npcNameNorm) then
            table.insert(releaseIds, lockId)
        elseif data.commitmentLock then
            -- Skip: commitment locks are managed by CommitmentManager, not conversation lifecycle
        else
            -- Normal lock: transition to lingering
            data.lingering = true
            local nameNorm = data.npcNameNorm
            if nameNorm then
                _G.LingerState.locks[nameNorm] = lockId
                lingerCount = lingerCount + 1
            else
                -- No name — can't track for absorption, release it
                table.insert(releaseIds, lockId)
            end
        end
    end

    -- Release non-lingering locks
    for _, lockId in ipairs(releaseIds) do
        NPCLock.ReleaseNPC(lockId)
    end

    -- Clear name cache — lingering NPCs are not "in conversation" for ambient dialogue
    _G.LockedNPCNames = {}

    -- Clear preview lock states (conversation is over)
    if _G.ChatPreviewLock then
        print("[NPCLock] Clearing ChatPreviewLock on linger: " .. tostring(_G.ChatPreviewLock.npcName))
        _G.ChatPreviewLock = nil
    end
    if _G.STTPreviewLock then
        print("[NPCLock] Clearing STTPreviewLock on linger: " .. tostring(_G.STTPreviewLock.npcName))
        _G.STTPreviewLock = nil
    end

    if lingerCount > 0 then
        _G.LingerState.active = true
        _G.LingerState.timerHandle = ExecuteInGameThreadWithDelay(LINGER_TIMEOUT_MS, function()
            NPCLock.ReleaseLingeringNPCs()
        end)
        print("[NPCLock] " .. lingerCount .. " NPCs lingering (" .. LINGER_TIMEOUT_MS .. "ms)")
    else
        print("[NPCLock] No NPCs to linger — all released")
    end
end

-- ============================================
-- Companion Soft Reorientation
-- ============================================
-- One-shot pulse to make the companion face the player when another NPC
-- gets locked. Not a real lock — just a turn pulse, then follow resumes.
-- Debounced: won't re-pulse within 3 seconds of the last one.

_G._CompanionOrientLastPulse = _G._CompanionOrientLastPulse or 0
_G.CompanionOrientEnabled = true

--- Orient the companion to face the player (soft, one-shot pulse).
--- Skips if: no companion, companion IS the locked NPC, companion in quest wait,
--- companion already facing player (<50°), or pulsed within last 3s.
--- @param lockedNpc userdata|nil The NPC being locked (to avoid orienting the companion toward player if they ARE the conversation participant)
function NPCLock.OrientCompanionToPlayer(lockedNpc)
    if not _G.CompanionOrientEnabled then return end
    pcall(function()
        -- Debounce: skip if pulsed recently
        local now = os.clock()
        if now - _G._CompanionOrientLastPulse < 3 then return end

        local staticData = Cache.GetStaticData()
        if not staticData then return end

        local companionMgr = staticData.companionManager
        if not companionMgr then return end

        local companionPawn = companionMgr:GetPrimaryCompanionPawn()
        if not companionPawn then return end
        if not Utils.SafeIsValid(companionPawn) then return end

        -- Skip if the companion IS the NPC being locked (or already locked as conversation participant)
        local compName = companionPawn:GetFullName()
        if lockedNpc then
            local lockedName = lockedNpc:GetFullName()
            if compName == lockedName then return end
        end
        -- Also check if companion is already in _G.LockedNPCs (e.g. as speaker or target)
        for _, data in pairs(_G.LockedNPCs) do
            if data.npc then
                local match = false
                pcall(function()
                    if data.npc:GetFullName() == compName then match = true end
                end)
                if match then return end
            end
        end

        -- Skip if companion is already in a forced wait (quest/puzzle)
        if Utils.IsCompanionForcedWaiting(companionPawn, companionMgr) then return end

        -- Get player
        local player = staticData.player
        if not player or not Utils.SafeIsValid(player) then return end

        -- Check angle to player — skip if already facing within 50°
        local tgtLoc, npcLoc, npcRot = nil, nil, nil
        pcall(function()
            tgtLoc = player:K2_GetActorLocation()
            npcLoc = companionPawn:K2_GetActorLocation()
            npcRot = companionPawn:K2_GetActorRotation()
        end)
        if not tgtLoc or not npcLoc or not npcRot then return end

        local dx = tgtLoc.X - npcLoc.X
        local dy = tgtLoc.Y - npcLoc.Y
        local dist = math.sqrt(dx * dx + dy * dy)
        if dist < 1 then return end

        local dirX = dx / dist
        local dirY = dy / dist

        local angleToTarget = math.atan(dirY, dirX) * 180 / math.pi
        local npcYaw = npcRot.Yaw or 0
        local diff = angleToTarget - npcYaw
        while diff > 180 do diff = diff - 360 end
        while diff < -180 do diff = diff + 360 end
        local turnAngle = math.abs(diff)

        if turnAngle <= 50 then return end

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
        pcall(function() companionMgr:StopMovement(true) end)
        pcall(function() companionMgr:StopMovement(false) end)

        -- Fire the pulse-turn: SetCompanionForcedWaitLocation 200 units toward player
        local waitPos = {
            X = npcLoc.X + dirX * 200,
            Y = npcLoc.Y + dirY * 200,
            Z = npcLoc.Z
        }
        local waitDir = { X = dirX, Y = dirY, Z = 0 }
        pcall(function() companionMgr:SetCompanionForcedWaitLocation(waitPos, waitDir) end)

        _G._CompanionOrientLastPulse = now

        -- Cancel walk + forced wait after turn animation completes
        local delay = turnAngle > 120 and 700 or 500
        if speed > 1 then
            delay = math.floor(delay / 2)
        end
        ExecuteInGameThreadWithDelay(delay, function()
            pcall(function() companionMgr:StopMovement(true) end)
            pcall(function() companionMgr:StopMovement(false) end)
            pcall(function() companionMgr:StopCompanionForcedWaiting() end)
        end)

        print("[NPCLock] Companion orient pulse (angle=" .. math.floor(turnAngle) .. ", delay=" .. delay .. "ms)")
    end)
end

return NPCLock
