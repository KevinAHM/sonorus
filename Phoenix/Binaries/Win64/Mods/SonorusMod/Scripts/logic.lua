-- logic.lua - Reloadable logic (press F11 to reload)
print("[Sonorus] logic.lua starting...")

-- DevPrint is defined in main.lua as _G.DevPrint (survives hot reload)
-- _G.SonorusDevMode is also in main.lua - set to true to enable debug output

-- Clear module caches so they reload with logic.lua (F11)
-- Note: Cache.lua uses _G.CacheStore for data persistence, so clearing
-- the module only reloads code, not cached data
package.loaded["Utils.Utils"] = nil
package.loaded["Utils.Cache"] = nil

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

-- Clear require cache for socket_client so it can be hot-reloaded
package.loaded["socket_client"] = nil
local BipedPlayer = FindFirstOf("Biped_Player")
if BipedPlayer and BipedPlayer:IsValid() then
    print(string.format("[Sonorus] BipedPlayer: %s", BipedPlayer:GetFullName()))
else
    print("[Sonorus] No BipedPlayer found yet (normal at startup)")
end

-- JSON library (rxi/json)
local json = require "json"

-- Unified caching utility (persists across F11 reloads)
local Cache = require "Utils.Cache"

-- Utils module
local Utils = require "Utils.Utils"

-- Socket client for Python server communication (lipsync, visemes)
-- NOTE: socket_client.lua sets _G.SocketClient, use that directly (no local shadow)
require "socket_client"

-- On reload: immediately try to reconnect (don't wait for LoopAsync tick)
-- This ensures chat works right away after F11
_G.SocketClient.connect()
print("[Sonorus] Socket reconnect triggered")

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

-- ============================================
-- File paths
-- ============================================
local FILES = {
    dialogueHistory = "sonorus\\data\\dialogue_history.json",
    subtitles = "sonorus\\data\\subtitles.json",
    locations = "sonorus\\data\\locations.json",
    localization = "sonorus\\data\\main_localization.json",
    spellMappings = "sonorus\\data\\spell_mappings.json",
}

-- ============================================
-- Debug: F7 Debug Function (hot-reloadable)
-- ============================================
-- ============================================
-- NPC Animation System (Blueprint-based)
-- ============================================
-- Animation via Lua crashes the game - use Blueprint instead
-- Call PlayNPCEmote(actor, emoteName) which delegates to Blueprint

-- Play emote on NPC via Blueprint ModActor
-- emoteName: "laugh", "shrug", "think", "greet", "wave", "nod"
function PlayNPCEmote(actor, emoteName)
    local mymod = GetSonorusModActor()
    if not mymod then
        print("[Anim] ModActor not found - can't play emote")
        return false
    end
    if not actor then
        print("[Anim] No actor provided")
        return false
    end

    -- Call Blueprint function: playemote(actor, emoteName)
    local ok, err = pcall(function()
        mymod:playemote(actor, emoteName)
    end)

    if not ok then
        print("[Anim] Blueprint playemote error: " .. tostring(err))
        return false
    end

    print("[Anim] Triggered emote '" .. emoteName .. "' via Blueprint")
    return true
end

-- ============================================
-- NPC Attention Lock System
-- ============================================
-- Makes NPCs walk towards target and lock in place for conversations

-- Persisted state for locked NPCs
_G.LockedNPCs = _G.LockedNPCs or {}
_G.LockedNPCNames = _G.LockedNPCNames or {}  -- lockId -> normalized name (for thread-safe lookup)
local lockIdCounter = 0

--- Check if player is in a state where NPC locking should be disabled
--- @return boolean canLock, string|nil reason
local function CanLockNPCs()
    -- Check broom
    if _G.BroomState and _G.BroomState.mounted then
        return false, "on broom"
    end

    -- Check combat
    local player = FindFirstOf("Biped_Player")
    if player then
        local inCombat = false
        pcall(function() inCombat = player.bInCombatMode or false end)
        if inCombat then
            return false, "in combat"
        end
    end

    return true, nil
end

--- Check if an NPC is the player's current companion
--- @param npc userdata The NPC actor to check
--- @return boolean isCompanion, userdata|nil companionManager
local function IsCompanion(npc)
    if not npc then return false, nil end
    local companionMgr = nil
    local companionPawn = nil
    pcall(function()
        companionMgr = FindFirstOf("CompanionManager")
        if companionMgr then
            companionPawn = companionMgr:GetPrimaryCompanionPawn()
        end
    end)
    -- Compare by full name (UObject == doesn't work reliably in Lua)
    local npcName = nil
    local compName = nil
    pcall(function() npcName = npc:GetFullName() end)
    pcall(function() if companionPawn then compName = companionPawn:GetFullName() end end)

    if npcName and compName and npcName == compName then
        print("[NPCLock] Detected companion: " .. tostring(npcName):sub(1,60))
        return true, companionMgr
    end
    return false, companionMgr
end

--- Release all currently locked NPCs
function ReleaseAllNPCs()
    local count = 0
    for lockId, _ in pairs(_G.LockedNPCs) do
        ReleaseNPC(lockId)
        count = count + 1
    end
    -- Safety clear of name cache
    _G.LockedNPCNames = {}
    if count > 0 then
        print("[NPCLock] Released all " .. count .. " locked NPCs")
    end
end

--- Find existing lock for an NPC (if already locked)
--- @param npc userdata The NPC actor to check
--- @return number|nil lockId if found, nil otherwise
local function FindExistingLock(npc)
    for lockId, data in pairs(_G.LockedNPCs) do
        if data.npc == npc then
            return lockId
        end
    end
    return nil
end

--- Check if an NPC (by name) is currently in an AI conversation (locked)
--- Uses cached names for thread-safe lookup (no UObject access)
--- @param name string The NPC name (voiceName or speakerName) to check
--- @return boolean isInConversation true if NPC is locked in a conversation
function IsNPCInConversation(name)
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

--- Lock an NPC to face a target actor
--- If NPC is already locked, updates their target and re-locks
--- @param npc userdata The NPC actor to lock
--- @param targetActor userdata The actor to face (usually player)
--- @param onLocked function|nil Optional callback when NPC is locked in place
--- @return number|nil lockId ID to use with ReleaseNPC, or nil on failure
function LockNPCToTarget(npc, targetActor, onLocked)
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

    -- Check if NPC is already locked - if so, release first (new target)
    local existingLock = FindExistingLock(npc)
    if existingLock then
        print("[NPCLock] NPC already locked (id=" .. existingLock .. "), updating target")
        ReleaseNPC(existingLock)
    end

    -- Get PopulationManager
    local popManager = nil
    pcall(function() popManager = FindFirstOf("PopulationManager") end)
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

    -- Cache NPC name for thread-safe lookup (used by IsNPCInConversation)
    local lib = StaticFindObject("/Script/Phoenix.Default__PhoenixBPLibrary")
    if lib then
        pcall(function()
            local nameResult = lib:GetActorName(npc)
            if nameResult then
                local npcName = nil
                pcall(function() npcName = nameResult:ToString() end)
                if npcName and npcName ~= "" then
                    _G.LockedNPCNames[lockId] = npcName:gsub(" ", ""):lower()
                end
            end
        end)
    end

    -- Check if this is the companion - use simpler lock (just StopMovement)
    local isCompanion, companionMgr = IsCompanion(npc)
    if isCompanion and companionMgr then
        -- For companions: ONLY use StopMovement, don't touch ScheduledEntity
        pcall(function() companionMgr:StopMovement(true) end)
        _G.LockedNPCs[lockId] = {
            npc = npc,
            targetActor = targetActor,
            scheduledEntity = nil,  -- Don't use ScheduledEntity for companions
            locked = true,
            isCompanionLock = true  -- Flag for special release
        }
        print("[NPCLock] Companion locked via StopMovement (id=" .. lockId .. ")")
        return lockId
    end

    -- Normal NPC lock path (non-companions)
    -- Store state (including target for angle checking)
    _G.LockedNPCs[lockId] = {
        npc = npc,
        targetActor = targetActor,
        scheduledEntity = scheduledEntity,
        locked = false
    }

    -- Step 1: AbandonStations
    pcall(function() scheduledEntity:AbandonStations(0) end)

    -- Step 2: Calculate angle to target to decide if we need to turn
    local needsTurn = false
    local targetPos = nil
    pcall(function()
        local tgtLoc = targetActor:K2_GetActorLocation()
        local npcLoc = npc:K2_GetActorLocation()
        local npcRot = npc:K2_GetActorRotation()

        local dirX = tgtLoc.X - npcLoc.X
        local dirY = tgtLoc.Y - npcLoc.Y
        local dist = math.sqrt(dirX * dirX + dirY * dirY)

        if dist > 1 then
            dirX = dirX / dist
            dirY = dirY / dist

            -- Calculate angle to target
            local angleToTarget = math.atan(dirY / dirX) * 180 / math.pi
            if dirX < 0 then
                angleToTarget = angleToTarget + 180
            end

            -- NPC's current yaw
            local npcYaw = npcRot.Yaw or 0

            -- Angle difference (normalize to -180 to 180)
            local diff = angleToTarget - npcYaw
            while diff > 180 do diff = diff - 360 end
            while diff < -180 do diff = diff + 360 end

            -- If angle > 45 degrees, need animated turn
            needsTurn = math.abs(diff) > 45

            -- Target position for move task
            targetPos = {
                X = npcLoc.X + dirX * 1,
                Y = npcLoc.Y + dirY * 1,
                Z = npcLoc.Z
            }
        else
            targetPos = {X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z}
        end
    end)

    if not targetPos then
        print("[NPCLock] Failed to calculate target position")
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        return nil
    end

    -- Always: Enable scheduling, issue move task
    -- NOTE: We MUST issue a task before disabling scheduling. Disabling scheduling
    -- alone doesn't stop the NPC - they need an active task assigned first, THEN
    -- disabling scheduling freezes them mid-task. Without a task, they just continue
    -- their normal station behavior.
    pcall(function() scheduledEntity:EnableScheduling(true, false, true) end)
    pcall(function()
        scheduledEntity:PerformTask_MoveToLocation(targetPos, 150, 30, false, 0, nil)
    end)

    if needsTurn then
        -- Angle > 45: Wait 500ms for turn animation, then disable
        -- Store lockId in global for delayed callback (avoids closure capture issues)
        _G._PendingLockId = lockId
        _G._PendingOnLocked = onLocked
        ExecuteWithDelay(500, function()
            if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC delay callback START") end
            local capturedLockId = _G._PendingLockId
            local capturedOnLocked = _G._PendingOnLocked
            ExecuteInGameThread(function()
                if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC game thread START") end
                local ok, err = pcall(function()
                    local data = _G.LockedNPCs[capturedLockId]
                    if not data then return end

                    pcall(function()
                        data.scheduledEntity:EnableScheduling(false, true, true)
                    end)
                    data.locked = true
                    print("[NPCLock] NPC locked after turn (id=" .. capturedLockId .. ")")

                    if capturedOnLocked then
                        pcall(capturedOnLocked)
                    end
                end)
                if not ok and _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC error: " .. tostring(err)) end
                if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC game thread END") end
            end)
            if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC delay callback END") end
        end)
    else
        -- Angle < 45: Immediately disable (no movement needed)
        pcall(function()
            scheduledEntity:EnableScheduling(false, true, true)
        end)
        _G.LockedNPCs[lockId].locked = true
        print("[NPCLock] NPC locked immediately (id=" .. lockId .. ")")

        if onLocked then
            pcall(onLocked)
        end
    end

    print("[NPCLock] Started lock sequence (id=" .. lockId .. ")")
    return lockId
end

--- Release a locked NPC
--- @param lockId number The ID returned by LockNPCToTarget
function ReleaseNPC(lockId)
    local data = _G.LockedNPCs[lockId]
    if not data then
        print("[NPCLock] No lock found for id=" .. tostring(lockId))
        return
    end

    -- Companion lock: just restore movement
    if data.isCompanionLock then
        pcall(function()
            local companionMgr = FindFirstOf("CompanionManager")
            if companionMgr then
                companionMgr:StopMovement(false)
            end
        end)
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Companion released (id=" .. lockId .. ")")
        return
    end

    -- Normal NPC: clear task and re-enable scheduling
    pcall(function()
        data.scheduledEntity:PerformTask_RemoveActivePerformTask()
    end)
    pcall(function()
        data.scheduledEntity:EnableScheduling(true, false, true)
    end)

    _G.LockedNPCs[lockId] = nil
    _G.LockedNPCNames[lockId] = nil
    print("[NPCLock] NPC released (id=" .. lockId .. ")")
end

-- Test lock ID for F7 toggle testing
_G.TestLockId = nil

-- ============================================
-- Player Voice ID Detection
-- ============================================
-- Returns "PlayerMale" or "PlayerFemale" based on character gender
-- Must be called from game thread (or inside ExecuteInGameThread)
function GetPlayerVoiceId()
    local voiceId = "PlayerMale"  -- Default fallback

    pcall(function()
        local audioStatics = StaticFindObject("/Script/Phoenix.Default__AvaAudioGameplayStatics")
        if audioStatics and audioStatics:IsValid() then
            local genderVoice = audioStatics:GetPlayerGenderVoice()
            -- 0 = Male, 1 = Female
            if genderVoice == 1 then
                voiceId = "PlayerFemale"
            end
        end
    end)

    return voiceId
end

-- F7 Debug Function
function DebugF7()
end

-- ============================================
-- Get Blueprint Mod Actors
-- ============================================
function GetSonorusModActor()
    -- Use unified Cache module with validity check
    return Cache.Get("SonorusModActor", function()
        -- Search for Sonorus mod actor
        local modactors = FindAllOf("ModActor_C")
        if modactors then
            for _, actor in ipairs(modactors) do
                local ok, valid = pcall(function() return actor:IsValid() end)
                if ok and valid then
                    local classOk, className = pcall(function()
                        return actor:GetClass():GetFullName()
                    end)
                    if classOk and className and className:find("sonorusblueprintmod") then
                        local nameOk, name = pcall(function() return actor:GetName() end)
                        print("[Cache] Found SonorusModActor: " .. (nameOk and name or "unknown"))
                        return actor
                    end
                end
            end
        end
        return nil
    end)
end

-- Get EmoteWithAnyNPC mod actor (optional dependency)
-- Returns nil if mod not installed - callers should handle gracefully
function GetEmoteModActor()
    -- Return cached if still valid
    if _G.SonorusState and _G.SonorusState.emoteModActor then
        local ok, valid = pcall(function() return _G.SonorusState.emoteModActor:IsValid() end)
        if ok and valid then
            return _G.SonorusState.emoteModActor
        end
    end

    -- Search for EmoteWithAnyNPC_C directly (not ModActor_C)
    local emoteActors = FindAllOf("EmoteWithAnyNPC_C")
    if emoteActors then
        for _, actor in ipairs(emoteActors) do
            local ok, valid = pcall(function() return actor:IsValid() end)
            if ok and valid then
                if _G.SonorusState then
                    _G.SonorusState.emoteModActor = actor
                end
                local nameOk, name = pcall(function() return actor:GetName() end)
                print("[GetEmoteModActor] Found EmoteWithAnyNPC: " .. (nameOk and name or "unknown"))
                return actor
            end
        end
    end

    -- Not found - this is expected if mod not installed
    return nil
end

-- Convenience function - returns Sonorus by default
function GetModActor()
    return GetSonorusModActor()
end

-- ============================================
-- Test NPC lookup via Blueprint
-- ============================================
-- Blueprint output parameter wrappers (intercept and log before calling)
function CallGetNpcByName(npcName)
    print("[Blueprint Wrapper] CallGetNpcByName input: " .. tostring(npcName))

    local mymod = GetSonorusModActor()
    if not mymod then
        print("[Blueprint Wrapper] CallGetNpcByName error: ModActor is nil")
        return nil
    end

    -- UE4SS requires output params to be passed as TABLE
    local outTable = {}
    print("[Blueprint Wrapper] Calling getnpcbyname with table output param...")

    local ok, err = pcall(function()
        mymod:getnpcbyname(npcName, outTable)
    end)

    print("[Blueprint Wrapper] Call ok=" .. tostring(ok))
    if not ok then
        print("[Blueprint Wrapper] Error: " .. tostring(err))
        return nil
    end

    -- Dump table contents to see what UE4SS populated
    print("[Blueprint Wrapper] Output table contents:")
    for k, v in pairs(outTable) do
        print("  [" .. tostring(k) .. "] = " .. tostring(v) .. " (type=" .. type(v) .. ")")
    end

    -- Try to get Actor from table
    local actor = outTable.Actor or outTable.actor or outTable[1] or outTable["Actor"]
    print("[Blueprint Wrapper] Final actor: " .. tostring(actor))
    return actor
end

-- ============================================
-- SetBlendshape - set morph target on NPC
-- ============================================
function CallSetBlendshape(actor, curveName, value, modActor)
    -- Use passed modActor or fetch (caller should cache for multiple calls)
    local mymod = modActor or GetSonorusModActor()
    if not mymod then
        print("[Blueprint] SetBlendshape error: ModActor is nil")
        return false
    end
    if not actor then
        print("[Blueprint] SetBlendshape error: Actor is nil")
        return false
    end

    local ok, err = pcall(function()
        mymod:setblendshape(actor, curveName, value)
    end)

    if not ok then
        print("[Blueprint] SetBlendshape error: " .. tostring(err))
        return false
    end
    return true
end

-- ============================================
-- ActionExecute - run action on NPC (LookAt, Wave, etc)
-- ============================================
function CallActionExecute(actor, actionName)
    local mymod = GetSonorusModActor()
    if not mymod then
        print("[Blueprint] ActionExecute error: ModActor is nil")
        return false
    end
    if not actor then
        print("[Blueprint] ActionExecute error: Actor is nil")
        return false
    end

    local ok, err = pcall(function()
        mymod:actionexecute(actor, actionName)
    end)

    if not ok then
        print("[Blueprint] ActionExecute error: " .. tostring(err))
        return false
    end
    return true
end

-- ============================================
-- Speaker Actor Cache (Multi-NPC support)
-- Maps speaker display names to actor references
-- ============================================
_G.SpeakerActorCache = _G.SpeakerActorCache or {}

-- Safe IsValid check - Blueprint output param actors may not support IsValid() properly
local function SafeIsValid(actor)
    if not actor then return false end
    local valid = false
    pcall(function()
        valid = actor:IsValid()
    end)
    return valid
end

-- Get actor for a speaker by name (cached)
function GetSpeakerActor(speakerName)
    if not speakerName or speakerName == "" then
        return nil
    end

    -- Special case: "player" means the player character
    if speakerName == "player" then
        local player = nil
        pcall(function() player = FindFirstOf("Biped_Player") end)
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
            print("[GetSpeakerActor] Cache hit but invalid: " .. speakerName)
        end
    else
        -- DEBUG: Show what keys ARE in cache
        local keys = {}
        for k, _ in pairs(_G.SpeakerActorCache or {}) do
            table.insert(keys, k)
        end
        print("[GetSpeakerActor] Cache miss: '" .. speakerName .. "', keys: " .. table.concat(keys, ", "))
    end

    -- Not in cache - return nil (don't call Blueprint, it returns invalid actors)
    return nil
end

-- Get actor for whoever is currently speaking (turn-based with fallbacks)
function GetCurrentSpeakerActor()
    local pState = _G.PlaybackState

    -- PRIMARY: Use currentTurnId from state (set by play_turn handler)
    -- This is the new atomic approach - avoids race conditions
    if _G.SonorusState and _G.SonorusState.currentTurnId then
        local actor = _G.TurnActorCache and _G.TurnActorCache[_G.SonorusState.currentTurnId]
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
            -- Fall back to speaker name lookup
            local lookupName = currentItem.speakerId or currentItem.speaker
            if lookupName then
                local actor = GetSpeakerActor(lookupName)
                if actor then return actor end
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

-- Clear speaker cache (on reset or conversation end)
function ClearSpeakerCache()
    _G.SpeakerActorCache = {}
    print("[SpeakerCache] Cache cleared")
end

-- Mute all speakers in the queue (call when queue is populated)
function MuteQueueSpeakers(queue)
    if not queue or #queue == 0 then return end
    if not _G.SonorusState then return end

    _G.SonorusState.mutedAkComponents = _G.SonorusState.mutedAkComponents or {}

    local mutedCount = 0
    for _, item in ipairs(queue) do
        local speakerName = item.speakerId or item.speaker
        if speakerName then
            local actor = GetSpeakerActor(speakerName)
            if actor then
                local comp = MuteNPCAudio(actor)
                if comp then
                    -- Check if already muted
                    local alreadyMuted = false
                    for _, existing in ipairs(_G.SonorusState.mutedAkComponents) do
                        if existing == comp then
                            alreadyMuted = true
                            break
                        end
                    end
                    if not alreadyMuted then
                        table.insert(_G.SonorusState.mutedAkComponents, comp)
                        mutedCount = mutedCount + 1
                    end
                end
            end
        end
    end

    if mutedCount > 0 then
        print("[Sonorus] Muted " .. mutedCount .. " queue speakers (total: " .. #_G.SonorusState.mutedAkComponents .. ")")
    end
end

-- Unmute all speakers (call at conversation end)
function UnmuteAllSpeakers()
    if not _G.SonorusState then return end
    local mutedComps = _G.SonorusState.mutedAkComponents or {}
    _G.SonorusState.mutedAkComponents = {}

    if #mutedComps > 0 then
        print("[Sonorus] Unmuting " .. #mutedComps .. " speakers")
        ExecuteInGameThread(function()
            for _, comp in ipairs(mutedComps) do
                UnmuteNPCAudio(comp)
            end
        end)
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
_G.DialogueHistory = _G.DialogueHistory or {}
_G.VoiceSamples = _G.VoiceSamples or {}
_G.PendingDialogue = _G.PendingDialogue or {}

-- Cleanup old dialogue entries (remove empty text, strip HTML tags)
-- This runs on hot reload to fix any existing bad data
local function cleanupDialogueHistory()
    if not _G.DialogueHistory or #_G.DialogueHistory == 0 then return false end

    local cleaned = {}
    local removedCount = 0
    for _, entry in ipairs(_G.DialogueHistory) do
        if entry.text and entry.text ~= "" then
            -- Strip HTML tags from old entries
            local cleanText = entry.text:gsub("<[^>]+>", "")
            if cleanText ~= "" then
                entry.text = cleanText
                table.insert(cleaned, entry)
            else
                removedCount = removedCount + 1
            end
        else
            removedCount = removedCount + 1
        end
    end

    if removedCount > 0 then
        _G.DialogueHistory = cleaned
        print(string.format("[Sonorus] Cleaned %d bad dialogue entries", removedCount))
        return true  -- Signal that we need to save
    end
    return false
end

_G.DialogueHistoryNeedsCleanup = cleanupDialogueHistory()
_G.Subtitles = _G.Subtitles or {}
_G.SubtitlesLoaded = _G.SubtitlesLoaded or false
_G.Locations = _G.Locations or {}
_G.LocationsLoaded = _G.LocationsLoaded or false
_G.Localization = _G.Localization or {}
_G.LocalizationLoaded = _G.LocalizationLoaded or false

-- ============================================
-- File I/O Helpers
-- ============================================
function ReadFile(path)
    -- Defensive: check io.open exists and is a function
    if type(io) ~= "table" or type(io.open) ~= "function" then
        print("[Sonorus] ERROR: io.open corrupted, skipping file read")
        return ""
    end

    local ok, f = pcall(function() return io.open(path, "r") end)
    if not ok or not f then
        return ""
    end

    -- Verify f is a file handle, not something weird
    if type(f) ~= "userdata" then
        print("[Sonorus] ERROR: io.open returned unexpected type: " .. type(f))
        return ""
    end

    local content = ""
    ok = pcall(function()
        content = f:read("*a") or ""
        f:close()
    end)

    return content
end

function WriteFile(path, content)
    local f = io.open(path, "w")
    if f then
        f:write(content)
        f:close()
        return true
    end
    return false
end

function ClearFile(path)
    WriteFile(path, "")
end

-- Parse JSON response
function ParseJsonResponse(jsonStr)
    if not jsonStr or jsonStr == "" then return {} end
    local ok, result = pcall(json.decode, jsonStr)
    if ok and result then return result end
    return {}
end

-- ============================================
-- Server Management
-- ============================================
function IsServerAlive()
    -- Check heartbeat file - if timestamp is recent, server is alive
    local content = ReadFile("sonorus\\server.heartbeat")
    if content == "" then return false end

    local timestamp = tonumber(content)
    if not timestamp then return false end

    -- Server is alive if heartbeat is within last 5 seconds
    local now = os.time()
    local alive = (now - timestamp) < 5

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

    -- Startup already in progress? Wait for it.
    if serverState.startupInProgress then
        local elapsed = os.time() - serverState.startupTime
        if elapsed < 30 then  -- 30 second timeout
            print("[Sonorus] Server startup in progress (" .. elapsed .. "s), waiting...")
            return true
        else
            -- Startup timed out, allow retry
            print("[Sonorus] Server startup timed out, retrying...")
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
    local handle = io.popen('start "SonorusServer" sonorus\\start_server.bat')
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

local function saveDialogueHistory()
    WriteFile(FILES.dialogueHistory, json.encode(_G.DialogueHistory))
end

local function loadDialogueHistory()
    local content = ReadFile(FILES.dialogueHistory)
    if content == "" or content == "[]" then return end

    local ok, entries = pcall(json.decode, content)
    if ok and entries and #entries > 0 then
        _G.DialogueHistory = entries
        print(string.format("[Sonorus] Loaded %d dialogue history entries", #entries))
    end
end

-- Load dialogue history at startup (wrapped in pcall to prevent crashes)
pcall(loadDialogueHistory)

-- Load subtitles.json (lazy load on first use)
function LoadSubtitles()
    if _G.SubtitlesLoaded then return true end

    print("[Sonorus] Loading subtitles.json...")
    local content = ReadFile(FILES.subtitles)

    if content == "" then
        print("[Sonorus] Warning: subtitles.json not found or empty")
        print("[Sonorus] Run extract_localization.py to generate it")
        return false
    end

    local ok, result = pcall(json.decode, content)
    if not ok or not result then
        print("[Sonorus] Error parsing subtitles.json")
        return false
    end

    _G.Subtitles = result
    _G.SubtitlesLoaded = true

    local count = 0
    for _ in pairs(_G.Subtitles) do count = count + 1 end
    print(string.format("[Sonorus] Loaded %d subtitle entries", count))

    return true
end

-- Load locations.json (lazy load on first use)
function LoadLocations()
    if _G.LocationsLoaded then return true end

    print("[Sonorus] Loading locations.json...")
    local content = ReadFile(FILES.locations)

    if content == "" then
        print("[Sonorus] Warning: locations.json not found or empty")
        print("[Sonorus] Run: python extract_localization.py --main")
        return false
    end

    local ok, result = pcall(json.decode, content)
    if not ok or not result then
        print("[Sonorus] Error parsing locations.json")
        return false
    end

    _G.Locations = result
    _G.LocationsLoaded = true

    local count = 0
    local withDesc = 0
    for _, v in pairs(_G.Locations) do
        count = count + 1
        if type(v) == "table" and v.desc then withDesc = withDesc + 1 end
    end
    print(string.format("[Sonorus] Loaded %d location entries (%d with descriptions)", count, withDesc))

    return true
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

    return nil
end

-- Get subtitle text for a lineID
function GetSubtitleText(lineID)
    if not _G.SubtitlesLoaded then
        LoadSubtitles()
    end
    -- Try original key first (NPCs use TitleCase)
    local text = _G.Subtitles[lineID]
    if text then return text end

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
function LoadLocalization()
    if _G.LocalizationLoaded then return true end

    print("[Sonorus] Loading main_localization.json...")
    local content = ReadFile(FILES.localization)

    if content == "" then
        print("[Sonorus] Warning: main_localization.json not found")
        print("[Sonorus] Run: python extract_localization.py --main")
        return false
    end

    local ok, result = pcall(json.decode, content)
    if not ok or not result then
        print("[Sonorus] Error parsing main_localization.json")
        return false
    end

    _G.Localization = result
    _G.LocalizationLoaded = true

    local count = 0
    for _ in pairs(_G.Localization) do count = count + 1 end
    print(string.format("[Sonorus] Loaded %d localization entries", count))

    return true
end

-- Get localized display name for internal ID
-- Falls back to prettified name if not found
function GetDisplayName(internalName)
    if not internalName or internalName == "" then return "Unknown" end

    -- Load localization if not loaded
    if not _G.LocalizationLoaded then
        LoadLocalization()
    end

    -- Try exact match
    local displayName = _G.Localization[internalName]
    if displayName and displayName ~= "" then
        return displayName
    end

    -- Fallback: prettify the internal name
    return string.gsub(internalName, "(%l)(%u)", "%1 %2")
end

-- ============================================
-- Spell Mappings (Blueprint name -> display name)
-- ============================================
_G.SpellMappings = _G.SpellMappings or {}
_G.SpellMappingsLoaded = _G.SpellMappingsLoaded or false

-- Load spell_mappings.json
function LoadSpellMappings()
    if _G.SpellMappingsLoaded then return true end

    print("[Sonorus] Loading spell_mappings.json...")
    local content = ReadFile(FILES.spellMappings)

    if content == "" then
        print("[Sonorus] Warning: spell_mappings.json not found")
        print("[Sonorus] Run: python extract_spell_mappings.py")
        return false
    end

    local ok, data = pcall(json.decode, content)
    if not ok or not data then
        print("[Sonorus] Error: Failed to parse spell_mappings.json")
        return false
    end

    _G.SpellMappings = data
    _G.SpellMappingsLoaded = true

    local count = 0
    for _ in pairs(_G.SpellMappings) do count = count + 1 end
    print(string.format("[Sonorus] Loaded %d spell mappings", count))

    return true
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

-- ============================================
-- Utility
-- ============================================
local function prettifyName(name)
    -- Use localization lookup (includes fallback to space-separated)
    return GetDisplayName(name)
end

local function calculateDistance(loc1, loc2)
    local dx = loc1.X - loc2.X
    local dy = loc1.Y - loc2.Y
    local dz = loc1.Z - loc2.Z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
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
        local isValid = false
        pcall(function() isValid = result:IsValid() end)
        if isValid then
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

    -- Get player name - nested pcall around ToString() is required
    pcall(function()
        local result = uiManager:GetPlayerName()
        if result then
            local str = nil
            pcall(function() str = result:ToString() end)
            if str and str ~= "" then info.name = str end
        end
    end)

    -- Get player house - nested pcall around ToString() is required
    pcall(function()
        local result = uiManager:GetPlayerHouse()
        if result then
            local str = nil
            pcall(function() str = result:ToString() end)
            if str and str ~= "" then info.house = str end
        end
    end)

    return info
end

-- Get current time using the Scheduler
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

    local scheduler = TryFindFirstOf("Scheduler")
    if not scheduler then
        return result
    end

    -- Each call in its own pcall to isolate failures
    -- Note: Some methods use : (instance) and some use . (may vary)
    pcall(function() result.hour = scheduler:GetHourOfTheDay() or 12 end)
    pcall(function()
        local minuteOfDay = scheduler:GetMinuteOfTheDay() or 0
        result.minute = minuteOfDay % 60
    end)
    pcall(function() result.dayOfWeek = scheduler:GetDayOfTheWeek() or 0 end)
    pcall(function() result.dayOfMonth = scheduler:GetDayOfTheMonth() or 1 end)
    pcall(function() result.month = scheduler:GetMonthOfTheYear() or 9 end)
    pcall(function() result.year = scheduler:GetCalendarYear() or 1890 end)

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
                local worldName = gameInstance:GetCurrentWorldName()
                if worldName then
                    pcall(function()
                        local name = worldName:ToString()
                        if name and name ~= "" then
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
                    end)
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
                local locName = mapHogwarts:GetMapLocationName()
                if locName then
                    pcall(function()
                        local name = locName:ToString()
                        if name and name ~= "" then
                            detailedLocation = name
                        end
                    end)
                end
            end
        end)
    end

    -- Method 5: Fallback to player's GetFullName path parsing
    if not detailedLocation then
        local player = TryFindFirstOf("Biped_Player")
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

-- Get equipped gear summary (uses cached player)
function GetPlayerGear()
    local gear = {
        wandEquipped = false,
    }

    -- Use cached player from static cache
    local staticData = Cache.GetStaticData()
    local player = staticData and staticData.player
    if player then
        local valid = false
        pcall(function() valid = player:IsValid() end)
        if valid then
            pcall(function()
                gear.wandEquipped = player:IsWandEquipped() or false
            end)
        end
    end

    return gear
end

-- Collect all game context and write to file
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
        -- Engine queries wrapped in ExecuteInGameThread (async - send must be inside)
        ExecuteInGameThread(function()
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

            -- Gear
            pcall(function()
                local gear = GetPlayerGear()
                context.wandEquipped = gear.wandEquipped
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
                    for _, entry in ipairs(npcResult.nearbyList) do
                        table.insert(nearbyNpcsForContext, {
                            name = entry.name,
                            distance = math.floor(entry.distance),
                            isLookedAt = entry.isLookedAt
                        })
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
                    end
                end
            end)

            -- Add pause state for Python-side detection
            context.isGamePaused = Utils.IsGamePaused()

            -- Add player position (x/y/z) for vision agent movement detection
            pcall(function()
                local player = TryFindFirstOf("Biped_Player")
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
                end
            end)

            -- Check if player is on broom (now tracked via hooks in main.lua)
            -- Fallback to GearScreen if hooks haven't fired yet
            if _G.BroomState then
                context.isOnBroom = _G.BroomState.mounted or false
            end
            --[[ Old polling approach - kept for reference
            pcall(function()
                local gearScreen = StaticFindObject("/Script/Phoenix.Default__GearScreen")
                if gearScreen then
                    context.isOnBroom = gearScreen:IsPlayerOnBroom() or false
                end
            end)
            --]]

            -- Send via socket (inside game thread so all data is ready)
            if _G.SocketClient and _G.SocketClient.isConnected() then
                _G.SocketClient.send({
                    type = "game_context",
                    data = context
                })
            end
        end)
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

-- Debug function to test GetLookedAtNPC (press F10)
function DumpGameState()
    print("=== LOOK-AT TEST ===")

    -- Wrap UObject access in ExecuteInGameThread for thread safety
    ExecuteInGameThread(function()
        local npc, name, dist = GetLookedAtNPC(0.9, 5000)

        if npc then
            print(string.format("[LookAt] Found: %s at %.0f units", name, dist))
            print("[LookAt] Actor: " .. npc:GetFullName())
        else
            print("[LookAt] No NPC in view")

            -- Fallback info
            local nearest = FindNearestNPC()
            if nearest then
                print("[LookAt] Nearest NPC: " .. nearest:GetFullName())
            end
        end

        print("=== END LOOK-AT TEST ===")
    end)
end

--[[ OLD DumpGameState - commented out
function DumpGameState_OLD()
    print("=== GAME STATE DUMP ===")

    -- Player
    local player = TryFindFirstOf("Biped_Player")
    if player then
        print("[FOUND] Biped_Player")
        local loc = player:K2_GetActorLocation()
        print("  World Location: " .. loc.X .. ", " .. loc.Y .. ", " .. loc.Z)
        pcall(function()
            print("  GetFullName: " .. player:GetFullName())
        end)
        pcall(function()
            print("  GetName: " .. player:GetName())
        end)
        pcall(function()
            local outer = player:GetOuter()
            if outer then
                print("  Outer: " .. outer:GetFullName())
            end
        end)
    end

    -- Try common classes
    local classesToTry = {
        "PersistentData", "Scheduler", "PhoenixGameInstance",
        "PhoenixGameMode", "GearManager", "InventoryManager",
        "WeatherComponent", "MapHogwarts", "UIManager", "PlayerController",
        "LevelStreamingManager", "WorldSettings", "GameState",
    }

    for _, className in ipairs(classesToTry) do
        local obj = TryFindFirstOf(className)
        if obj then
            print("[FOUND] " .. className)
            pcall(function()
                print("  FullName: " .. obj:GetFullName())
            end)
        end
    end

    -- Try to find location-specific objects
    print("=== LOCATION OBJECTS ===")
    local locationClasses = {
        "MapManager", "WorldMapManager", "LevelManager",
        "RegionManager", "AreaManager", "ZoneManager",
        "FastTravelManager", "NavigationManager",
    }
    for _, className in ipairs(locationClasses) do
        local obj = TryFindFirstOf(className)
        if obj then
            print("[FOUND] " .. className)
            pcall(function()
                print("  FullName: " .. obj:GetFullName())
            end)
        end
    end

    -- Try finding all loaded levels
    print("=== LEVELS ===")
    pcall(function()
        local world = FindFirstOf("World")
        if world then
            print("[FOUND] World: " .. world:GetFullName())
        end
    end)

    -- Write full context
    local context = WriteGameContext()
    print("=== CONTEXT ===")
    print("  Player: " .. (context.playerName or "?") .. " (" .. (context.playerHouse or "?") .. ")")
    print("  Time: " .. (context.timeFormatted or "?") .. " - " .. (context.timePeriod or "?"))
    print("  Date: " .. (context.dateFormatted or "?"))
    print("  Location: " .. (context.location or "?"))
    print("  Nearby: " .. #(context.nearbyCharacters or {}) .. " characters")
    print("=== END DUMP ===")
end
--]]

-- ============================================
-- UI
-- ============================================

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
    local displayText = "You: " .. text .. "|"

    -- Check if subtitle HUD exists (required for subtitles to display)
    local subtitleHUD = FindFirstOf("UI_BP_Subtitle_HUD_C")
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

    local subtitles = FindFirstOf("Subtitles")
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
        local ok, err = pcall(function()
            if _G.ChatInputSubtitleActive then
                subtitles:BPUpdateStandaloneSubtitle(displayText)
            else
                subtitles:BPAddStandaloneSubtitle(displayText)
                _G.ChatInputSubtitleActive = true
                print("[ChatInput] Subtitle added")
            end
        end)
        if not ok then
            print("[ChatInput] ERROR adding subtitle: " .. tostring(err))
        end
    else
        local ok, err = pcall(function()
            subtitles:BPRemoveStandaloneSubtitle()
        end)
        if not ok then
            print("[ChatInput] ERROR removing subtitle: " .. tostring(err))
        end
        _G.ChatInputSubtitleActive = false
    end
end

function ShowMessage(message)
    -- Mode 3 approach: Check Subtitle_HUD exists first for consistent display
    local subtitleHUD = FindFirstOf("UI_BP_Subtitle_HUD_C")
    if subtitleHUD and subtitleHUD:IsValid() then
        local subtitles = FindFirstOf("Subtitles")
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
    local UIManager = FindFirstOf("UIManager")
    if UIManager and UIManager:IsValid() then
        local layout = {
            Position = { X = 500, Y = 500 },
            Alignment = { X = 500, Y = 500 }
        }
        UIManager:SetAndShowHintMessage(message, layout, true, 3600)
    end
end

function HideMessage()
    local subtitles = FindFirstOf("Subtitles")
    if subtitles and subtitles:IsValid() then
        pcall(function()
            subtitles:BPRemoveStandaloneSubtitle()
        end)
    end
end

function UpdateMessage(message)
    local subtitles = FindFirstOf("Subtitles")
    if subtitles and subtitles:IsValid() then
        pcall(function()
            subtitles:BPUpdateStandaloneSubtitle(message)
        end)
    end
end

-- ============================================
-- NPC Audio Muting Functions
-- ============================================
function MuteNPCAudio(actor)
    if not actor or not SafeIsValid(actor) then
        print("[Sonorus] MuteNPCAudio: Invalid actor")
        return nil
    end

    local akClass = StaticFindObject("/Script/AkAudio.AkComponent")
    if not akClass then
        print("[Sonorus] Could not find AkComponent class")
        return nil
    end

    local comp = nil
    pcall(function()
        comp = actor:GetComponentByClass(akClass)
    end)

    if comp and comp:IsValid() then
        print("[Sonorus] Found AkComponent, muting...")
        pcall(function()
            comp:SetOutputBusVolume(0)
        end)
        return comp
    end

    return nil
end

function UnmuteNPCAudio(comp)
    if comp and comp:IsValid() then
        print("[Sonorus] Restoring audio volume...")
        pcall(function()
            comp:SetOutputBusVolume(1.0)
        end)
    end
end

-- ============================================
-- Lip Sync (Phoneme-based with visemes)
-- ============================================

-- Viseme data storage
_G.VisemeData = _G.VisemeData or {}
-- Ensure all fields exist (handles hot reload)
local vd = _G.VisemeData
vd.startTime = vd.startTime or 0
vd.localStartTime = vd.localStartTime or 0
vd.frames = vd.frames or {}
vd.loaded = vd.loaded or false
vd.lastContentLen = vd.lastContentLen or 0
vd.currentJaw = vd.currentJaw or 0
vd.currentSmile = vd.currentSmile or 0
vd.currentFunnel = vd.currentFunnel or 0
vd.lastReadTime = vd.lastReadTime or 0  -- Throttle file reads
vd.syncPrinted = vd.syncPrinted or false  -- Only print "sync started" once

-- State flag for async CloseLips completion (checked by OnTick)
_G.CloseLipsComplete = false

function CloseLips()
    -- Get actor from queue (no fallback)
    local actor = GetCurrentSpeakerActor()
    if not actor then
        _G.CloseLipsComplete = true
        -- Still signal Python even if no actor (so it doesn't wait for timeout)
        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({ type = "turn_complete" })
        end
        return  -- Done if no actor playing
    end

    -- Smoothly close mouth over several frames instead of snapping
    local data = _G.VisemeData
    local closeSpeed = 0.3

    -- Lerp current values toward 0
    data.currentJaw = data.currentJaw * (1 - closeSpeed)
    data.currentSmile = data.currentSmile * (1 - closeSpeed)
    data.currentFunnel = data.currentFunnel * (1 - closeSpeed)

    -- Apply per-character scale (same as AnimateLips)
    local scale = data.scale or 1.0
    local jaw = data.currentJaw * scale
    local smile = data.currentSmile * scale
    local funnel = data.currentFunnel * scale

    -- Apply smoothed + scaled values
    CallSetBlendshape(actor, "jaw_drop", jaw)
    CallSetBlendshape(actor, "smile_l", smile)
    CallSetBlendshape(actor, "smile_r", smile)
    CallSetBlendshape(actor, "lwr_lip_funl_l", funnel)
    CallSetBlendshape(actor, "lwr_lip_funl_r", funnel)
    CallSetBlendshape(actor, "upr_lip_funl_l", funnel * 0.7)
    CallSetBlendshape(actor, "upr_lip_funl_r", funnel * 0.7)
    CallSetBlendshape(actor, "lwr_lip_dn_l", jaw * 0.3)
    CallSetBlendshape(actor, "lwr_lip_dn_r", jaw * 0.3)

    -- If values are near zero, fully reset and signal done
    if data.currentJaw < 0.01 and data.currentSmile < 0.01 and data.currentFunnel < 0.01 then
        local blendshapes = {
            "lwr_lip_funl_l", "lwr_lip_funl_r", "upr_lip_funl_r", "upr_lip_funl_l",
            "jaw_drop", "lips_up_l", "lwr_lip_dn_l", "lwr_lip_dn_r",
            "dimple_l", "dimple_r", "smile_l", "smile_r",
            "mouth_mov_r", "mouth_mov_l", "lips_up_r"
        }
        for _, name in ipairs(blendshapes) do
            CallSetBlendshape(actor, name, 0)
        end
        -- Reset viseme data for next conversation
        data.loaded = false
        data.syncPrinted = false  -- Allow new sync on next conversation
        data.frames = {}
        data.localStartTime = 0
        data.lastContentLen = 0
        data.currentJaw = 0
        data.currentSmile = 0
        data.currentFunnel = 0
        _G.CloseLipsComplete = true  -- Signal done via flag (async-safe)

        -- Signal Python that this turn's mouth animation is complete
        -- This allows Python to safely start the next turn
        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({ type = "turn_complete" })
        end
    end
    -- Still closing - flag remains false
end

function LoadVisemes()
    local data = _G.VisemeData

    -- Throttle file reads to every 250ms (file I/O is expensive ~6-8ms)
    local now = os.clock()
    if data.lastReadTime and (now - data.lastReadTime) < 0.25 then
        return data.loaded  -- Use cached data
    end
    data.lastReadTime = now

    local content = ReadFile("sonorus\\visemes.txt")
    if content == "" then
        return false
    end

    -- Check if file changed (by length) - skip reparse if same
    local contentLen = #content
    if contentLen == data.lastContentLen and data.loaded then
        return true  -- No change, keep existing data
    end
    data.lastContentLen = contentLen

    -- Reparse the file
    data.frames = {}
    local foundStart = false

    for line in string.gmatch(content, "[^\r\n]+") do
        -- Parse START:timestamp
        local startTime = string.match(line, "^START:([%d%.]+)")
        if startTime then
            -- Only set localStartTime once per conversation
            if not data.syncPrinted then
                data.startTime = tonumber(startTime) or 0
                data.localStartTime = os.clock()
                print("[Sonorus] Viseme sync started")
                data.syncPrinted = true
            end
            foundStart = true
        elseif line ~= "END" then
            -- Parse time:jaw,smile,funnel
            local t, jaw, smile, funnel = string.match(line, "^([%d%.]+):([%d%.]+),([%d%.]+),([%d%.]+)")
            if t then
                table.insert(data.frames, {
                    t = tonumber(t) or 0,
                    jaw = tonumber(jaw) or 0,
                    smile = tonumber(smile) or 0,
                    funnel = tonumber(funnel) or 0,
                })
            end
        end
    end

    data.loaded = foundStart and #data.frames > 0
    return data.loaded
end

function GetVisemeAtTime(elapsed)
    local data = _G.VisemeData
    if not data.loaded or #data.frames == 0 then
        return {jaw = 0, smile = 0, funnel = 0}
    end

    local frames = data.frames

    -- Before first frame
    if elapsed <= frames[1].t then
        return frames[1]
    end

    -- After last frame
    if elapsed >= frames[#frames].t then
        return frames[#frames]
    end

    -- Find surrounding frames and interpolate
    for i = 1, #frames - 1 do
        if elapsed >= frames[i].t and elapsed < frames[i + 1].t then
            local f1 = frames[i]
            local f2 = frames[i + 1]
            local alpha = (elapsed - f1.t) / (f2.t - f1.t)

            return {
                jaw = f1.jaw + (f2.jaw - f1.jaw) * alpha,
                smile = f1.smile + (f2.smile - f1.smile) * alpha,
                funnel = f1.funnel + (f2.funnel - f1.funnel) * alpha,
            }
        end
    end

    return frames[#frames]
end

-- Debug: throttle lipsync debug logs
local _lastLipsyncDebugTime = 0
local _lastDetailedLipsyncLog = 0  -- For timing diagnosis

-- Get detailed frame info for diagnostic logging
function GetCurrentFrameInfo(elapsed)
    local data = _G.VisemeData
    if not data.loaded or #data.frames == 0 then
        return { index = 0, total = 0, t = 0, jaw = 0 }
    end

    local frames = data.frames

    -- Before first frame
    if elapsed <= frames[1].t then
        return { index = 1, total = #frames, t = frames[1].t, jaw = frames[1].jaw }
    end

    -- After last frame
    if elapsed >= frames[#frames].t then
        return { index = #frames, total = #frames, t = frames[#frames].t, jaw = frames[#frames].jaw }
    end

    -- Find current frame
    for i = 1, #frames - 1 do
        if elapsed >= frames[i].t and elapsed < frames[i + 1].t then
            return { index = i, total = #frames, t = frames[i].t, jaw = frames[i].jaw }
        end
    end

    return { index = #frames, total = #frames, t = frames[#frames].t, jaw = frames[#frames].jaw }
end

function AnimateLips()
    local perfStart = os.clock()
    local perfThreshold = 0.005  -- 5ms warning threshold
    local perfTimes = {}

    -- Get actor from queue (no fallback)
    local t0 = os.clock()
    local actor = GetCurrentSpeakerActor()
    perfTimes.getActor = os.clock() - t0
    if not actor then
        -- Debug: log if actor is nil (throttled)
        if (os.clock() - _lastLipsyncDebugTime) > 2 then
            _lastLipsyncDebugTime = os.clock()
            print("[AnimateLips] No actor from GetCurrentSpeakerActor")
        end
        return
    end

    -- Cache modActor once for all blendshape calls (avoid 8+ lookups)
    local modActor = GetSonorusModActor()
    if not modActor then
        print("[AnimateLips] No modActor from GetSonorusModActor")
        return
    end

    local data = _G.VisemeData
    -- Visemes already loaded outside game thread

    -- Target values
    local targetJaw, targetSmile, targetFunnel = 0, 0, 0

    -- If no visemes yet, use fallback animation
    if not data.loaded or #data.frames == 0 then
        -- Fallback: simple sine wave
        local t = os.clock()
        targetJaw = 0.4 * math.abs(math.sin(2 * math.pi * 0.8 * t))
    else
        -- Calculate elapsed time since audio start
        -- Apply audioOffset for drift correction (from audio_sync messages)
        local elapsed = os.clock() - data.localStartTime + (data.audioOffset or 0)

        -- Get interpolated viseme from timeline
        local v = GetVisemeAtTime(elapsed)

        -- Detailed timing diagnostic (every 500ms)
        if _G.SonorusDevMode and (os.clock() - _lastDetailedLipsyncLog) > 0.5 then
            _lastDetailedLipsyncLog = os.clock()
            local frameInfo = GetCurrentFrameInfo(elapsed)
            -- Log format: elapsed (Lua time), frame index, frame timestamp, frame jaw, applied jaw
            _G.DevPrint(string.format(
                "[LipsyncTiming] elapsed=%.3fs, frame=%d/%d, frameT=%.3fs, frameJaw=%.2f, sysTime=%.3f",
                elapsed,
                frameInfo.index,
                frameInfo.total,
                frameInfo.t,
                frameInfo.jaw,
                os.clock()
            ))
        end

        -- Simple scaling
        targetJaw = v.jaw * 2.5
        targetSmile = v.smile * 1.0
        targetFunnel = v.funnel * 1.0
    end

    -- Smooth lerp toward target (higher = snappier)
    local lerpSpeed = 0.6
    data.currentJaw = data.currentJaw + (targetJaw - data.currentJaw) * lerpSpeed
    data.currentSmile = data.currentSmile + (targetSmile - data.currentSmile) * lerpSpeed
    data.currentFunnel = data.currentFunnel + (targetFunnel - data.currentFunnel) * lerpSpeed

    -- Apply blendshapes with per-character scale
    local scale = data.scale or 1.0
    local jaw = data.currentJaw * scale
    local smile = data.currentSmile * scale
    local funnel = data.currentFunnel * scale

    -- Debug: log jaw value periodically
    if _G.SonorusDevMode and (os.clock() - _lastLipsyncDebugTime) > 1 then
        _lastLipsyncDebugTime = os.clock()
        local scaleStr = scale ~= 1.0 and string.format(" (scale=%.2f)", scale) or ""
        _G.DevPrint(string.format("[Lipsync] jaw=%.2f, frames=%d, loaded=%s%s", jaw, #data.frames, tostring(data.loaded), scaleStr))
    end

    t0 = os.clock()

    -- Jaw opening
    CallSetBlendshape(actor, "jaw_drop", jaw, modActor)

    -- Smile/wide mouth (E, I sounds)
    CallSetBlendshape(actor, "smile_l", smile, modActor)
    CallSetBlendshape(actor, "smile_r", smile, modActor)

    -- Lip rounding/funnel (O, U sounds)
    CallSetBlendshape(actor, "lwr_lip_funl_l", funnel, modActor)
    CallSetBlendshape(actor, "lwr_lip_funl_r", funnel, modActor)
    CallSetBlendshape(actor, "upr_lip_funl_l", funnel * 0.7, modActor)
    CallSetBlendshape(actor, "upr_lip_funl_r", funnel * 0.7, modActor)

    -- Lower lip follows jaw slightly
    CallSetBlendshape(actor, "lwr_lip_dn_l", jaw * 0.3, modActor)
    CallSetBlendshape(actor, "lwr_lip_dn_r", jaw * 0.3, modActor)

    perfTimes.blendshapes = os.clock() - t0

    -- Perf warning if any section is slow
    local totalTime = os.clock() - perfStart
    if totalTime > perfThreshold then
        print(string.format("[Perf] AnimateLips: %.1fms (actor:%.1f, blend:%.1f)",
            totalTime * 1000,
            perfTimes.getActor * 1000,
            perfTimes.blendshapes * 1000))
    end
end

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
            npcZ = npcPos.Z
        })
    end
end

-- ============================================
-- Get NPC Player Is Looking At (View Cone Raycast)
-- ============================================
-- Returns: npc actor, name, distance (or nil if none in view)
-- Uses dot product to find NPC closest to camera center
-- minDot: how centered NPC must be (0.9 = ~25°, 0.95 = ~18°, 0.99 = ~8°)
-- maxDistance: max range in UE units (100 = ~1 meter)
-- ============================================
-- Cache Setup (uses unified Cache module)
-- ============================================

-- NPC class paths for spawn hooks
local NPC_CLASS_PATHS = {
    "/Script/Phoenix.NPC_Character",
    "/Game/Characters/NPC_Character",
}

-- Register NPC spawn hooks (idempotent - only registers once)
Cache.RegisterSpawnHook("NPC", NPC_CLASS_PATHS)

-- Static cache refresh function
local function RefreshStaticData(data)
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

    data.bpLibrary = StaticFindObject("/Script/Phoenix.Default__PhoenixBPLibrary")

    -- Mark primary object for validity checks
    data._primary = data.playerController
end

-- Get cached NPCs (initializes on first call, cleans periodically)
local function GetCachedNPCs()
    -- Initialize if needed (one-time FindAllOf)
    if not Cache.IsEntityCacheReady("NPC") then
        Cache.InitEntities("NPC", "NPC_Character")
    end

    -- Cleanup invalid entries periodically (every 5s)
    Cache.CleanEntities("NPC", 5)

    return Cache.GetEntities("NPC")
end

-- Get static cache (refreshes every 30s or when invalid)
local function GetStaticCache()
    return Cache.GetStatic(RefreshStaticData, 30)
end

-- ============================================
-- Get Nearby NPCs (single iteration, returns list + looked-at)
-- Returns: { nearbyList = [{name, distance, actor, isLookedAt}], lookedAtNpc = {name, actor, distance} or nil }
-- ============================================
-- GetNearbyNPCs - MUST be called from game thread (inside ExecuteInGameThread or hook)
-- Returns: { nearbyList = [{name, distance, actor, isLookedAt}], lookedAtNpc = {name, actor, distance} or nil }
function GetNearbyNPCs(maxDistance, lookDotThreshold)
    maxDistance = maxDistance or 2000  -- ~20 meters default
    lookDotThreshold = lookDotThreshold or 0.9  -- How centered in view to count as "looked at"

    -- Use cached static objects
    local staticData = GetStaticCache()

    local pc = staticData.playerController
    if not pc or not pc:IsValid() then
        return { nearbyList = {}, lookedAtNpc = nil }
    end

    local cam = staticData.cameraManager
    if not cam or not cam:IsValid() then
        return { nearbyList = {}, lookedAtNpc = nil }
    end

    local camLoc = cam:GetCameraLocation()
    local camRot = cam:GetCameraRotation()
    if not camLoc or not camRot then
        return { nearbyList = {}, lookedAtNpc = nil }
    end

    local playerFullName = staticData.playerFullName
    local lib = staticData.bpLibrary

    -- Use reactive NPC cache (no FindAllOf after first load)
    local npcs = GetCachedNPCs()
    if not npcs or #npcs == 0 then
        return { nearbyList = {}, lookedAtNpc = nil }
    end

    -- Calculate forward vector from camera rotation
    local pitch = math.rad(camRot.Pitch)
    local yaw = math.rad(camRot.Yaw)
    local forward = {
        X = math.cos(pitch) * math.cos(yaw),
        Y = math.cos(pitch) * math.sin(yaw),
        Z = math.sin(pitch)
    }

    local nearbyList = {}
    local lookedAtNpc = nil
    local bestDot = lookDotThreshold

    -- Single iteration through all NPCs
    for _, npc in pairs(npcs) do
        if npc:IsValid() then
            local fullName = npc:GetFullName()
            if fullName ~= playerFullName then
                local npcLoc = npc:K2_GetActorLocation()

                -- Vector from camera to NPC
                local toNpc = {
                    X = npcLoc.X - camLoc.X,
                    Y = npcLoc.Y - camLoc.Y,
                    Z = npcLoc.Z - camLoc.Z
                }
                local dist = math.sqrt(toNpc.X * toNpc.X + toNpc.Y * toNpc.Y + toNpc.Z * toNpc.Z)

                if dist > 0 and dist <= maxDistance then
                    -- Get NPC name
                    local npcName = "Unknown"
                    if lib then
                        pcall(function()
                            local nameResult = lib:GetActorName(npc)
                            if nameResult then
                                pcall(function() npcName = nameResult:ToString() end)
                            end
                        end)
                    end

                    -- Normalize direction vector
                    toNpc.X = toNpc.X / dist
                    toNpc.Y = toNpc.Y / dist
                    toNpc.Z = toNpc.Z / dist

                    -- Dot product with forward (1.0 = perfectly aligned with camera)
                    local dot = forward.X * toNpc.X + forward.Y * toNpc.Y + forward.Z * toNpc.Z

                    -- Check if this is the best "looked at" candidate
                    local isLookedAt = false
                    if dot > bestDot then
                        bestDot = dot
                        lookedAtNpc = { name = npcName, actor = npc, distance = dist }
                        isLookedAt = true
                    end

                    -- Add to nearby list
                    table.insert(nearbyList, {
                        name = npcName,
                        distance = dist,
                        actor = npc,
                        isLookedAt = isLookedAt
                    })
                end
            end
        end
    end

    -- Sort by distance (closest first)
    table.sort(nearbyList, function(a, b) return a.distance < b.distance end)

    -- Mark the looked-at NPC in the list (update isLookedAt flags)
    if lookedAtNpc then
        for _, entry in ipairs(nearbyList) do
            entry.isLookedAt = (entry.actor == lookedAtNpc.actor)
        end
    end

    return { nearbyList = nearbyList, lookedAtNpc = lookedAtNpc }
end

-- Legacy wrapper for compatibility
function GetLookedAtNPC(minDot, maxDistance)
    local result = GetNearbyNPCs(maxDistance, minDot)
    if result.lookedAtNpc then
        return result.lookedAtNpc.actor, result.lookedAtNpc.name, result.lookedAtNpc.distance
    end
    return nil
end

-- ============================================
-- Find Nearest NPC
-- MUST be called from game thread (inside ExecuteInGameThread or hook callback)
-- ============================================
function FindNearestNPC()
    local player = FindFirstOf("Biped_Player")
    if not player or not player:IsValid() then return nil end

    local playerLoc = player:K2_GetActorLocation()
    local npcs = FindAllOf("NPC_Character")
    if not npcs then return nil end

    local nearest = nil
    local nearestDist = math.huge
    local playerFullName = player:GetFullName()

    for _, npc in pairs(npcs) do
        if npc:IsValid() and npc:GetFullName() ~= playerFullName then
            local npcLoc = npc:K2_GetActorLocation()
            local dist = calculateDistance(playerLoc, npcLoc)
            if dist < nearestDist then
                nearest = npc
                nearestDist = dist
            end
        end
    end

    return nearest
end

-- ============================================
-- Find NPC by Name (for multi-NPC conversations)
-- ============================================
function FindNPCByName(targetName)
    if not targetName or targetName == "" then return nil end

    local npcs, lib
    ExecuteInGameThread(function()
        npcs = FindAllOf("NPC_Character")
        lib = StaticFindObject("/Script/Phoenix.Default__PhoenixBPLibrary")
    end)

    if not npcs then return nil end

    local targetLower = targetName:lower()

    for _, npc in pairs(npcs) do
        if npc:IsValid() then
            local npcName = nil

            -- Try to get name via library
            if lib then
                pcall(function()
                    local nameResult = lib:GetActorName(npc)
                    if nameResult then
                        pcall(function() npcName = nameResult:ToString() end)
                    end
                end)
            end

            -- Fallback to parsing full name
            if not npcName or npcName == "" then
                local fullName = npc:GetFullName()
                npcName = fullName:match("([^_]+)_C_") or fullName:match("([^/]+)$") or ""
            end

            -- Check for match (case-insensitive, also check display name)
            if npcName and npcName:lower() == targetLower then
                return npc, npcName
            end

            -- Also check display name
            local displayName = GetDisplayName(npcName)
            if displayName and displayName:lower() == targetLower then
                return npc, npcName
            end
        end
    end

    return nil
end

-- ============================================
-- Speech Complete Handler (per queue item, NOT whole conversation)
-- NOTE: Currently unused - closing is triggered by socket lipsync_stop
-- ============================================
function OnSpeechComplete(responseText)
    State = _G.SonorusState or {}
    print("[Sonorus] Speech complete")

    -- Parse action from response if present
    local actionIdx = string.find(responseText, "Action:")
    local parsedAction = nil

    if actionIdx then
        parsedAction = string.sub(responseText, actionIdx + 7)
        parsedAction = string.gsub(parsedAction, "^%s+", "")
        parsedAction = string.gsub(parsedAction, "%s+$", "")
        parsedAction = string.gsub(parsedAction, "\n+", "")

        if parsedAction ~= "" then
            print("[Sonorus] Action: " .. parsedAction)
            ExecuteInGameThread(function()
                local actor = GetCurrentSpeakerActor()
                if actor then
                    CallActionExecute(actor, parsedAction)
                end
            end)
        end
    end
end

-- ============================================
-- Tick Handler
-- ============================================

-- Static wrapper functions to avoid creating new closures in hot loops
-- (creating closures at 20Hz corrupts UE4SS Lua registry -> PANIC crash)
local _animateLipsCallCount = 0
local function _AnimateLipsWrapper()
    _animateLipsCallCount = _animateLipsCallCount + 1
    -- Log every 100 calls (~5 seconds at 20Hz) to track if we're in the right place
    if _G.DevPrint and _animateLipsCallCount % 100 == 0 then
        _G.DevPrint("[DEBUG] AnimateLips call #" .. _animateLipsCallCount)
    end
    local ok, err = pcall(AnimateLips)
    if not ok then
        print("[Sonorus] AnimateLips error: " .. tostring(err))
    end
end

local function _CloseLipsWrapper()
    if _G.DevPrint then _G.DevPrint("[DEBUG] CloseLips wrapper called") end
    local ok, err = pcall(CloseLips)
    if not ok then
        print("[Sonorus] CloseLips error: " .. tostring(err))
    end
end

local function _HideMessageWrapper()
    if _G.DevPrint then _G.DevPrint("[DEBUG] HideMessage wrapper called") end
    local ok, err = pcall(HideMessage)
    if not ok then
        print("[Sonorus] HideMessage error: " .. tostring(err))
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
            if pState.playing and pState.currentIndex < #pState.queue then
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
                ReleaseAllNPCs()        -- Release locked NPCs when conversation ends
                ResetPlaybackState()
                -- Hide subtitles now that closing is complete
                if HideMessage then
                    ExecuteInGameThread(_HideMessageWrapper)
                end
                print("[Sonorus] Ready for next conversation")
            end
        else
            -- Still closing - dispatch CloseLips to game thread (async)
            -- Use static wrapper to avoid creating new closure each tick
            ExecuteInGameThread(_CloseLipsWrapper)
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
            local npcName = prettifyName(currentItem.speaker or "NPC")
            local text = currentItem.full_text

            -- Get text from current segment if available
            if currentItem.segments and currentItem.segments[pState.currentSegment] then
                text = currentItem.segments[pState.currentSegment].text or text
            end

            -- Strip [emotion] tags for display
            local displayText = string.gsub(text or "", "%[%w+%]%s*", "")
            displayMessage = npcName .. ": " .. displayText
        end

        if displayMessage then
            print("[Sonorus] Showing message: " .. displayMessage)
            ExecuteInGameThread(function()
                ShowMessage(displayMessage)
            end)
            _G.SonorusState.messageShown = true
        end
    end

    -- Animate lips while playing (viseme data populated by socket_client)
    -- Socket triggers phase="closing" on lipsync_stop, handled at top of OnTick
    if (phase == "playing" or _G.SonorusState.lipsyncStarted) and phase ~= "closing" and not _G.SonorusState.closing then
        -- Viseme data now comes via socket - no need for LoadVisemes()
        -- DISABLE LIPSYNC FOR TESTING: set _G.DisableLipsync = true
        if not _G.DisableLipsync then
            -- Use static wrapper to avoid creating new closure each tick (20Hz!)
            ExecuteInGameThread(_AnimateLipsWrapper)
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

    -- Unmute all speakers
    UnmuteAllSpeakers()

    -- Release all locked NPCs
    ReleaseAllNPCs()

    -- Close lips on game thread
    ExecuteInGameThread(CloseLips)

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

function ProcessInitDialogueData(Context, AudioDialogueLineData)
    local elem = nil
    pcall(function() elem = Context:get() end)

    if not elem then return end

    ExecuteWithDelay(50, function()
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
            -- Use _G lookup to survive F11 reload (closure captures stale ref)
            if _G.RecordDialogueLine then
                _G.RecordDialogueLine(voiceName, lineID, duration, "", nil, nil)
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

    local timestamp = os.time()

    local speakerName = "Unknown"
    if speakingActor then
        pcall(function()
            if speakingActor:IsValid() then
                local lib = StaticFindObject("/Script/Phoenix.Default__PhoenixBPLibrary")
                if lib then
                    local nameResult = lib:GetActorName(speakingActor)
                    if nameResult then
                        speakerName = nameResult:ToString()
                    end
                end
            end
        end)
    end

    -- Fallback to prettified voiceName if speaker is still Unknown
    if speakerName == "Unknown" and voiceName and voiceName ~= "" and voiceName ~= "Unknown" then
        speakerName = prettifyName(voiceName)
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

    table.insert(_G.DialogueHistory, {
        timestamp = timestamp,
        gameTime = gameTime.formatted,
        gameDate = gameTime.dateShort or gameTime.dateFormatted,
        speaker = speakerName,
        voiceName = isPlayer and "Player" or (voiceName or "Unknown"),
        lineID = lineID or "Unknown",
        text = text,
        duration = duration or 0,
        target = targetName or "Unknown",
        isAIResponse = false,
        isPlayer = isPlayer,
        type = "chatter",  -- Native game dialogue (random NPC world chatter)
    })

    -- No limit on dialogue history - let it grow for full conversation context

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
            -- print("[Sonorus] NEW SAMPLE: " .. voiceName .. " / " .. tostring(lineID) .. " (" .. tostring(duration) .. "s)")
        end
    end

    saveDialogueHistory()
end

-- ============================================
-- Spell Event Recording
-- ============================================

-- Record a spell cast event to DialogueHistory
-- Called from the SpellTool:Start hook in main.lua
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
    }

    table.insert(_G.DialogueHistory, entry)

    -- Log for debugging
    print(string.format("[Sonorus] Spell: %s cast %s (%s)",
        playerName, spellName, category))

    -- Save immediately
    saveDialogueHistory()
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
    }

    table.insert(_G.DialogueHistory, entry)

    -- Log for debugging
    print(string.format("[Sonorus] Broom: %s %s", playerName, actionText:lower()))

    -- Save immediately
    saveDialogueHistory()
end

-- ============================================
-- Location Transition Recording
-- ============================================
-- Track the last known zone location for change detection
_G.LastTrackedLocation = _G.LastTrackedLocation or nil

-- Record a location transition event to DialogueHistory
-- Called when the zone/location changes (detected in WriteGameContext)
function RecordLocationTransition(newLocation)
    if not newLocation or newLocation == "" then return end

    -- Check if the last entry is already a location entry for the same place
    -- This prevents duplicates when loading a save after server boot
    local history = _G.DialogueHistory or {}
    if #history > 0 then
        local lastEntry = history[#history]
        if lastEntry.type == "location" and lastEntry.location == newLocation then
            print(string.format("[Sonorus] Location: Skipping duplicate entry for %s", newLocation))
            return
        end
    end

    local timestamp = os.time()
    local gameTime = GetTimeOfDay()

    -- Get player name
    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
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
    }

    table.insert(_G.DialogueHistory, entry)

    -- Log for debugging
    print(string.format("[Sonorus] Location: %s entered %s", playerName, newLocation))

    -- Save immediately
    saveDialogueHistory()
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
    LoopAsync(5000, function()
        return MonitorServerHeartbeat()
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
    if _G.DevPrint then _G.DevPrint("[DEBUG] NPCLockCheck START") end
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
        -- Skip companions - they don't need re-facing and it causes spam
        local needsReface = {}
        for lockId, data in pairs(_G.LockedNPCs) do
            if data.locked and data.npc and data.targetActor and not data.isCompanionLock then
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
                        table.insert(needsReface, {
                            lockId = lockId,
                            npc = data.npc,
                            target = data.targetActor,
                            angle = math.floor(diff)
                        })
                    end
                end)
            end
        end
        -- Now process re-facing outside the iteration loop
        for _, item in ipairs(needsReface) do
            print("[NPCLock] Re-facing NPC (angle=" .. item.angle .. ")")
            ReleaseNPC(item.lockId)
            LockNPCToTarget(item.npc, item.target)
        end
    end)
    if not ok then
        print("[Sonorus] NPCLockCheck error: " .. tostring(err))
    end
    if _G.DevPrint then _G.DevPrint("[DEBUG] NPCLockCheck END") end
end

_G.UnifiedLoop = _G.UnifiedLoop or { version = 0, lastContextWrite = 0 }
_G.UnifiedLoop.version = (_G.UnifiedLoop.version or 0) + 1  -- Increment on each reload
local myLoopVersion = _G.UnifiedLoop.version
print("[Sonorus] Starting unified loop v" .. myLoopVersion)

LoopAsync(100, function()
    -- Exit if a newer version of the loop exists (from F11 reload)
    if _G.UnifiedLoop.version ~= myLoopVersion then
        print("[Sonorus] Unified loop v" .. myLoopVersion .. " exiting (superseded by v" .. _G.UnifiedLoop.version .. ")")
        return true  -- Stop this loop
    end

    local now = os.clock()

    -- Socket update EVERY tick (100ms) - handles reconnection and message processing
    -- This is CRITICAL - socket must update frequently for responsive chat input
    -- NOTE: Use _G.SocketClient (not local _G.SocketClient) so hot reload works!
    if _G.SocketClient then
        pcall(_G.SocketClient.update)
    end

    -- Process chat input display (must be on game thread for UObject access)
    -- Use static wrapper to avoid creating closures during active typing
    if _G.ChatInputState and _G.ChatInputState.dirty then
        ExecuteInGameThread(_ProcessChatInputWrapper)
    end

    -- Write game context every 5 seconds (throttled)
    -- Includes player position (x/y/z) for vision agent movement detection
    if (now - _G.UnifiedLoop.lastContextWrite) >= 5.0 then
        _G.UnifiedLoop.lastContextWrite = now
        pcall(WriteGameContext)
    end

    -- Broom state polling every 2 seconds (replaces ReceiveTick hooks)
    _G.UnifiedLoop.lastBroomCheck = _G.UnifiedLoop.lastBroomCheck or 0
    if (now - _G.UnifiedLoop.lastBroomCheck) >= 2.0 then
        _G.UnifiedLoop.lastBroomCheck = now
        local onBroom = false
        pcall(function()
            local gearScreen = StaticFindObject("/Script/Phoenix.Default__GearScreen")
            if gearScreen then
                onBroom = gearScreen:IsPlayerOnBroom() or false
            end
        end)
        -- Detect state change
        local prevMounted = _G.BroomState and _G.BroomState.mounted or false
        if onBroom ~= prevMounted then
            _G.BroomState = _G.BroomState or {}
            _G.BroomState.mounted = onBroom
            if onBroom then
                print("[Sonorus] Player mounted broom (polled)")
                if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
                if RecordBroomEvent then pcall(function() RecordBroomEvent("mounted") end) end
            else
                print("[Sonorus] Player dismounted broom (polled)")
                if RecordBroomEvent then pcall(function() RecordBroomEvent("dismounted") end) end
            end
        end
    end

    -- Check locked NPCs every 1 second: combat/broom release, angle refresh
    _G.UnifiedLoop.lastLockCheck = _G.UnifiedLoop.lastLockCheck or 0
    if next(_G.LockedNPCs) and (now - _G.UnifiedLoop.lastLockCheck) >= 1.0 then
        _G.UnifiedLoop.lastLockCheck = now

        -- All NPC lock operations must be on game thread
        -- Use static wrapper to avoid creating closures every second
        ExecuteInGameThread(_NPCLockCheckWrapper)
    end

    return false  -- Keep running
end)

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
    if not spellTool then return "unknown" end
    local isValid = false
    pcall(function() isValid = spellTool:IsValid() end)
    if not isValid then return "unknown" end

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
    local isValid = false
    pcall(function() isValid = actor:IsValid() end)
    if not isValid then return "invalid" end

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
-- Mod Initialization
-- ============================================

-- Save cleaned dialogue history if cleanup removed entries
if _G.DialogueHistoryNeedsCleanup then
    saveDialogueHistory()
    _G.DialogueHistoryNeedsCleanup = false
end

print("[Sonorus] logic.lua ready!")
