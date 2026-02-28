-- Utils/NPCLock.lua
-- NPC Attention Lock System - makes NPCs walk towards and face conversation targets
-- Extracted from logic.lua for modularity

local NPCLock = {}

-- Module requires
local Cache = require("Utils.Cache")
local Utils = require("Utils.Utils")

-- ============================================
-- State (persisted in _G for hot reload)
-- ============================================
_G.LockedNPCs = _G.LockedNPCs or {}
_G.LockedNPCNames = _G.LockedNPCNames or {}  -- lockId -> normalized name (for thread-safe lookup)

-- Linger timeout: how long NPCs stay frozen after conversation ends (ms)
local LINGER_TIMEOUT_MS = 20000

-- Linger state (persisted in _G for hot reload)
_G.LingerState = _G.LingerState or {
    active = false,
    timerHandle = nil,
    locks = {},  -- normalized npcName -> lockId
}

-- Local state (resets on hot reload, which is fine for counter)
local lockIdCounter = 0

-- Static cache getter - set via init()
local getStaticCache = nil

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
end

-- ============================================
-- Internal Helpers
-- ============================================

--- Check if player is in a state where NPC locking should be disabled
--- @return boolean canLock, string|nil reason
local function CanLockNPCs()
    -- Check broom
    if _G.BroomState and _G.BroomState.mounted then
        return false, "on broom"
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
    local ok, loc = pcall(_G.GetCurrentLocation)
    if not ok or not loc then return true end
    return loc:find("Hogwarts") ~= nil
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
    local data = _G.LockedNPCs[lockId]
    if not data then
        print("[NPCLock] No lock found for id=" .. tostring(lockId))
        return
    end

    -- Companion lock: pulse pattern already cleaned up, just clear state
    if data.isCompanionLock then
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Companion released (id=" .. lockId .. ")")
        return
    end

    -- Static lock: nothing to restore (no-op lock for portraits, desk NPCs, etc.)
    if data.isStaticLock then
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Static NPC released (id=" .. lockId .. ")")
        return
    end

    -- Snap lock: no move task was issued, just re-enable scheduling
    if data.isSnapLock then
        pcall(function()
            data.scheduledEntity:EnableScheduling(true, false, true)
        end)
        _G.LockedNPCs[lockId] = nil
        _G.LockedNPCNames[lockId] = nil
        print("[NPCLock] Snap-locked NPC released (id=" .. lockId .. ")")
        return
    end

    -- Normal NPC (animated turn): clear task and re-enable scheduling
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

--- Release all currently locked NPCs
function NPCLock.ReleaseAllNPCs()
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

    -- Check if NPC is already locked - if so, release first (new target)
    local existingLock = FindExistingLock(npc)
    local isRelock = false
    if existingLock then
        print("[NPCLock] NPC already locked (id=" .. existingLock .. "), updating target")
        isRelock = true
        NPCLock.ReleaseNPC(existingLock)
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
                print("[NPCLock] Absorbing linger lock for " .. quickName)
                NPCLock.ReleaseNPC(lingerLockId)
                _G.LingerState.locks[nameNorm] = nil
                isRelock = true  -- skip station check — NPC is already out
                NPCLock.ResetLingerTimer()
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

    -- Get NPC voice ID for checks and caching
    local npcName = Utils.GetActorVoiceId(npc, staticData)

    -- Check if NPC should be static (no-op lock) - by name or station type
    local isStatic = false
    local staticReason = nil

    -- Check by NPC name (portraits, ghosts, etc.)
    if npcName and STATIC_NPCS[npcName] then
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
        -- Guard: if the companion is already in a forced wait (quest/puzzle), don't override it.
        -- Use a no-op lock instead so IsNPCInConversation still works.
        if Utils.IsCompanionForcedWaiting(npc, companionMgr) then
            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isStaticLock = true  -- no-op release, won't call StopCompanionForcedWaiting
            }
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
            needsTurn = turnAngle > 35

            -- Only trigger pathfinding if we need to turn AND inside Hogwarts Castle
            -- (outside Hogwarts, snap rotation is used instead — no pathfinding)
            if needsTurn and IsInsideHogwartsCastle() then
                local waitPos = {
                    X = npcLoc.X + dirX * 200,
                    Y = npcLoc.Y + dirY * 200,
                    Z = npcLoc.Z
                }
                local waitDir = {
                    X = dirX,
                    Y = dirY,
                    Z = 0
                }
                companionMgr:SetCompanionForcedWaitLocation(waitPos, waitDir)
            end
        end)

        if needsTurn and not IsInsideHogwartsCastle() then
            -- Outside Hogwarts Castle: snap rotate only if player is stationary
            local playerSpeed = 0
            pcall(function()
                local staticData = Cache.GetStaticData()
                local player = staticData and staticData.player
                if player then
                    local vel = player:GetVelocity()
                    if vel then
                        playerSpeed = math.sqrt(vel.X*vel.X + vel.Y*vel.Y + vel.Z*vel.Z)
                    end
                end
            end)

            if playerSpeed < 10 then
                -- Player stationary: pulse stop → snap rotate → release
                pcall(function() companionMgr:StopMovement(true) end)
                pcall(function() companionMgr:StopMovement(false) end)

                pcall(function()
                    local npcLoc = npc:K2_GetActorLocation()
                    local tgtLoc = targetActor:K2_GetActorLocation()
                    local dx = tgtLoc.X - npcLoc.X
                    local dy = tgtLoc.Y - npcLoc.Y
                    local dist = math.sqrt(dx * dx + dy * dy)
                    if dist > 1 then
                        local yaw = math.atan(dy, dx) * 180 / math.pi
                        npc:K2_SetActorRotation({Pitch = 0, Yaw = yaw, Roll = 0}, false)
                    end
                end)

                -- Release after 50ms so the rotation sticks for at least a frame
                _G._PendingCompanionSnapMgr = companionMgr
                ExecuteInGameThreadWithDelay(50, function()
                    local mgr = _G._PendingCompanionSnapMgr
                    _G._PendingCompanionSnapMgr = nil
                    if mgr then
                        pcall(function() mgr:StopCompanionForcedWaiting() end)
                    end
                end)

                print("[NPCLock] Companion snap-rotated (id=" .. lockId .. ", angle=" .. math.floor(turnAngle) .. ")")
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
            if onLocked then pcall(onLocked) end
        elseif needsTurn then
            -- Inside Hogwarts Castle: pulse pattern (animated turn via pathfinding)
            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isCompanionLock = true
            }
            local delay = turnAngle > 120 and 700 or 500

            _G._PendingCompanionLockId = lockId
            _G._PendingCompanionMgr = companionMgr
            _G._PendingCompanionOnLocked = onLocked
            ExecuteInGameThreadWithDelay(delay, function()
                local capLockId = _G._PendingCompanionLockId
                local capMgr = _G._PendingCompanionMgr
                local capOnLocked = _G._PendingCompanionOnLocked
                _G._PendingCompanionLockId = nil
                _G._PendingCompanionMgr = nil
                _G._PendingCompanionOnLocked = nil

                if capMgr then
                    pcall(function() capMgr:StopMovement(true) end)
                    pcall(function() capMgr:StopMovement(false) end)
                    pcall(function() capMgr:StopCompanionForcedWaiting() end)
                end
                print("[NPCLock] Companion turn pulse complete (id=" .. capLockId .. ", angle=" .. math.floor(turnAngle) .. ")")
                if capOnLocked then pcall(capOnLocked) end
            end)
            print("[NPCLock] Companion lock (id=" .. lockId .. ", angle=" .. math.floor(turnAngle) .. ", delay=" .. delay .. "ms)")
        else
            -- Already facing target, no-op static lock
            _G.LockedNPCs[lockId] = {
                npc = npc,
                targetActor = targetActor,
                scheduledEntity = nil,
                locked = true,
                isStaticLock = true
            }
            print("[NPCLock] Companion lock (id=" .. lockId .. ", angle=" .. math.floor(turnAngle) .. ", already facing)")
            if onLocked then pcall(onLocked) end
        end

        return lockId
    end

    -- Normal NPC lock path (non-companions)
    -- Store state (including target for angle checking)
    _G.LockedNPCs[lockId] = {
        npc = npc,
        targetActor = targetActor,
        scheduledEntity = scheduledEntity,
        locked = false,
        npcNameNorm = npcName and npcName:gsub(" ", ""):lower() or nil,
    }

    -- The core lock sequence: AbandonStations → calculate angle → move → freeze
    -- Outside Hogwarts Castle, uses snap rotation (no nav mesh dependency)
    local function executeLockSequence()
        -- Outside Hogwarts Castle: snap rotation path (no nav mesh dependency)
        -- AbandonStations → disable scheduling → snap rotate. No MoveToLocation needed.
        if not IsInsideHogwartsCastle() then
            pcall(function() scheduledEntity:AbandonStations(0) end)
            pcall(function() scheduledEntity:PerformTask_RemoveActivePerformTask() end)
            pcall(function() npc.CharacterMovement:StopMovementImmediately() end)
            pcall(function() scheduledEntity:EnableScheduling(false, true, true) end)

            -- Snap rotate to face target (immediate + 50ms reinforcement)
            local function applySnapRotation()
                pcall(function()
                    local npcLoc = npc:K2_GetActorLocation()
                    local tgtLoc = targetActor:K2_GetActorLocation()
                    local dx = tgtLoc.X - npcLoc.X
                    local dy = tgtLoc.Y - npcLoc.Y
                    local dist = math.sqrt(dx * dx + dy * dy)
                    if dist > 1 then
                        local yaw = math.atan(dy, dx) * 180 / math.pi
                        npc:K2_SetActorRotation({Pitch = 0, Yaw = yaw, Roll = 0}, false)
                    end
                end)
            end
            applySnapRotation()
            ExecuteInGameThreadWithDelay(50, applySnapRotation)

            _G.LockedNPCs[lockId].locked = true
            _G.LockedNPCs[lockId].isSnapLock = true
            print("[NPCLock] NPC snap-locked (id=" .. lockId .. ")")
            if onLocked then pcall(onLocked) end
            return
        end

        -- Inside Hogwarts Castle: animated turn path (good nav mesh)
        -- Abandon stations to clear any station control
        pcall(function() scheduledEntity:AbandonStations(0) end)

        -- Calculate angle to target to decide if we need to turn
        local needsTurn = false
        local turnAngle = 0
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

                -- Store angle for delay calculation
                turnAngle = math.abs(diff)

                -- If angle > 45 degrees, need animated turn
                needsTurn = turnAngle > 45

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
            return
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
            -- Angle > 45: Wait for turn animation, then disable
            -- Large turns (>120°) need 1000ms, smaller turns need 500ms
            local delay = turnAngle > 120 and 700 or 500
            -- Store lockId in global for delayed callback (avoids closure capture issues)
            _G._PendingLockId = lockId
            _G._PendingOnLocked = onLocked
            ExecuteInGameThreadWithDelay(delay, function()
                if _G.DevPrint then _G.DevPrint("[DEBUG] LockNPC delay callback START") end
                local capturedLockId = _G._PendingLockId
                local capturedOnLocked = _G._PendingOnLocked
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
    end

    -- On re-locks (same NPC, new target), skip station check entirely.
    -- The NPC was already pulled from their station on the first lock.
    -- Re-checking causes a 2.2s unlock gap where the NPC walks freely.
    if isRelock then
        print("[NPCLock] Re-lock - skipping station check")
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
                if not _G.LockedNPCs[lockId] then
                    print("[NPCLock] Lock " .. lockId .. " was released during graceful exit delay")
                    return
                end
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
    -- TODO: replace with proper detection once we find a safe method
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

--- Re-face a snap-locked NPC to their target via K2_SetActorRotation.
--- Used by the re-face loop to avoid the full release/re-lock cycle.
--- @param data table The lock data from _G.LockedNPCs
function NPCLock.SnapRefaceNPC(data)
    pcall(function()
        local npcLoc = data.npc:K2_GetActorLocation()
        local tgtLoc = data.targetActor:K2_GetActorLocation()
        local dx = tgtLoc.X - npcLoc.X
        local dy = tgtLoc.Y - npcLoc.Y
        local dist = math.sqrt(dx * dx + dy * dy)
        if dist > 1 then
            local yaw = math.atan(dy, dx) * 180 / math.pi
            data.npc:K2_SetActorRotation({Pitch = 0, Yaw = yaw, Roll = 0}, false)
        end
    end)
end

-- ============================================
-- Post-Conversation Linger System
-- ============================================
-- After a conversation ends naturally, NPCs stay frozen in place for
-- LINGER_TIMEOUT_MS instead of immediately returning to their schedules.
-- If any lingering NPC is re-addressed, its linger lock is absorbed
-- (no station exit delay) and the timer resets for remaining lingerers.

--- Cancel any active linger timer and clear linger state.
--- Called by ReleaseAllNPCs() for forced releases (F8, combat, broom, loading).
function NPCLock.CancelLinger()
    if _G.LingerState.timerHandle then
        pcall(function() CancelDelayedAction(_G.LingerState.timerHandle) end)
    end
    _G.LingerState.active = false
    _G.LingerState.timerHandle = nil
    _G.LingerState.locks = {}
end

--- Release only lingering NPCs (timer callback).
function NPCLock.ReleaseLingeringNPCs()
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
        -- Companion and static locks: release immediately
        if data.isCompanionLock or data.isStaticLock then
            table.insert(releaseIds, lockId)
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
--- companion already facing player (<35°), or pulsed within last 3s.
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

        -- Check angle to player — skip if already facing within 35°
        local tgtLoc = player:K2_GetActorLocation()
        local npcLoc = companionPawn:K2_GetActorLocation()
        local npcRot = companionPawn:K2_GetActorRotation()

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

        if turnAngle <= 35 then return end

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
        companionMgr:SetCompanionForcedWaitLocation(waitPos, waitDir)

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
