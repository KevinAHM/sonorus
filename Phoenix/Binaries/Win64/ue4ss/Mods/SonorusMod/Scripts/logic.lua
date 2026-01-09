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

-- JSON library (rxi/json)
local json = require "json"

-- Unified caching utility (persists across F11 reloads)
local Cache = require "Utils.Cache"

-- Utils module
local Utils = require "Utils.Utils"

-- Socket client for Python server communication (lipsync, visemes)
-- NOTE: socket_client.lua sets _G.SocketClient, use that directly (no local shadow)
require "socket_client"

-- On reload: immediately try to reconnect (don't wait for unified loop tick)
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

--- Get FacialComponent from an NPC actor
--- @param npc userdata The NPC actor
--- @return userdata|nil The FacialComponent, or nil if not found
function GetNPCFacialComponent(npc)
    if not npc then return nil end

    local staticData = Cache.GetStaticData()
    local facialClass = staticData and staticData.facialComponentClass
    if not facialClass then return nil end

    local facialComp = nil
    local ok = pcall(function()
        facialComp = npc:GetComponentByClass(facialClass)
    end)

    return ok and facialComp or nil
end

--- Stop ambient dialogue lip sync on an NPC
--- @param npc userdata The NPC actor
--- @return boolean success Whether the cancel succeeded
function StopNPCDialogueLipSync(npc)
    local facialComp = GetNPCFacialComponent(npc)
    if not facialComp then return false end

    local result = false
    local ok = pcall(function()
        result = facialComp:EditorCancelPlayingCurrentDialogueLine()
    end)

    return ok and result
end

--- Check if NPC is currently playing dialogue lip sync
--- @param npc userdata The NPC actor
--- @return boolean isPlaying
function IsNPCPlayingDialogueLipSync(npc)
    local facialComp = GetNPCFacialComponent(npc)
    if not facialComp then return false end

    local isPlaying = false
    pcall(function()
        isPlaying = facialComp:IsPlayingDialogueLine()
    end)

    return isPlaying
end

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
-- NPC Attention Lock System
-- ============================================
-- Makes NPCs walk towards target and lock in place for conversations

-- Persisted state for locked NPCs
_G.LockedNPCs = _G.LockedNPCs or {}
_G.LockedNPCNames = _G.LockedNPCNames or {}  -- lockId -> normalized name (for thread-safe lookup)
local lockIdCounter = 0

-- Static NPCs - can't or shouldn't move to face you, use no-op lock
-- Check is by NPC name. Station-based checks (e.g., desks) are handled separately.
local STATIC_NPCS = {
    -- Portraits
    ["FerdinandOctaviusPratt"] = true,
    ["FatLady"] = true,
    ["MaryDunne"] = true,
    ["LethiaBurbley"] = true,
    ["SirCadogan"] = true,
    ["MusicConductor"] = true,
    ["SylviaPembroke"] = true,
    ["OgleThePortrait"] = true,
    -- Ghosts (animation overrides rotation)
    ["CuthbertBinns"] = true,
}

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

--- Check if an NPC is the player's current companion
--- @param npc userdata The NPC actor to check
--- @return boolean isCompanion, userdata|nil companionManager
local function IsCompanion(npc)
    if not npc then return false, nil end

    local success, isComp, mgr = pcall(function()
        local staticData = Cache.GetStaticData()
        local companionMgr = staticData and staticData.companionManager
        if not companionMgr then return false, nil end

        local companionPawn = companionMgr:GetPrimaryCompanionPawn()
        if not companionPawn then return false, companionMgr end

        -- Compare by full name (UObject == doesn't work reliably in Lua)
        local npcName = npc:GetFullName()
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

    -- Get PopulationManager from cache
    local staticData = Cache.GetStatic(RefreshStaticData, 30)
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

    -- Get NPC name for checks and caching
    local npcName = nil
    pcall(function()
        local lib = staticData and staticData.bpLibrary
        if lib then
            local nameResult = lib:GetActorName(npc)
            if nameResult then
                npcName = nameResult:ToString()
            end
        end
    end)

    -- Check if NPC should be static (no-op lock) - by name or station type
    local isStatic = false
    local staticReason = nil

    -- Check by NPC name (portraits, ghosts, etc.)
    if npcName and STATIC_NPCS[npcName:gsub(" ", "")] then
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
                    if ownerName and ownerName:find("Desk") then
                        isStatic = true
                        staticReason = "at desk"
                    end
                end
            end
        end)
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
        ExecuteInGameThreadWithDelay(500, function()
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
            local staticData = Cache.GetStaticData()
            local companionMgr = staticData and staticData.companionManager
            if companionMgr then
                companionMgr:StopMovement(false)
            end
        end)
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

-- Gear Slot ID Enum
local EGearSlotID = {
    HEAD = 0,
    OUTFIT = 1,
    BACK = 2,
    NECK = 3,
    HAND = 4,
    FACE = 5,
}

local GearSlotNames = {
    [0] = "HEAD",
    [1] = "OUTFIT",
    [2] = "BACK",
    [3] = "NECK",
    [4] = "HAND",
    [5] = "FACE",
}

-- ============================================
-- Get Player Gear (structured table)
-- ============================================
-- Returns table with all equipped gear info:
-- {
--   HEAD = { name = "Display Name", id = "Head_068_Legendary", transmogged = true, appearance = "Other Item Name" },
--   OUTFIT = { name = "Display Name", id = "Outfit_089_Legendary", transmogged = false },
--   ...
--   WAND = { equipped = true },
--   HOOD = { up = false }
-- }
function GetPlayerGear()
    local gear = {}

    -- Slot names (defined locally to avoid scope issues)
    local slotNames = {
        [0] = "HEAD",
        [1] = "OUTFIT",
        [2] = "BACK",
        [3] = "NECK",
        [4] = "HAND",
        [5] = "FACE",
    }

    -- Get player and GearManager from cache
    local staticData = Cache.GetStaticData()
    local player = staticData and staticData.player
    if not player then return nil end

    local gearManager = staticData.gearManager
    if not gearManager then return nil end

    -- Get player's ActorId for appearance lookups
    local playerActorId = nil
    pcall(function()
        local staticData = Cache.GetStaticData()
        local bpLib = staticData and staticData.bpLibrary
        if bpLib then
            local outTable = {}
            bpLib:GetActorId(player, outTable)
            if outTable.OutActorId then
                playerActorId = outTable.OutActorId:ToString()
            end
        end
    end)

    -- Get each gear slot
    for slotId = 0, 5 do
        local slotName = slotNames[slotId]
        local slotData = { equipped = false }

        pcall(function()
            local gearItemId = gearManager:GetActorEquippedGearItemID(player, slotId)
            if gearItemId and gearItemId.IsEquipped then
                slotData.equipped = true

                -- Get GearID (stats item)
                local gearId = nil
                pcall(function()
                    local fname = gearItemId.GearID
                    if fname then pcall(function() gearId = fname:ToString() end) end
                end)
                slotData.id = gearId
                slotData.name = GetDisplayName(gearId)

                -- Check for transmog
                local hasOverride = false
                pcall(function()
                    hasOverride = gearManager:DoesGearHaveAppearanceOverride(gearItemId)
                end)

                if hasOverride and playerActorId then
                    slotData.transmogged = true
                    -- Get the appearance override
                    pcall(function()
                        local result = gearManager:GetEquippedGearAppearanceOverrideID(playerActorId, slotId)
                        if result then
                            local appearanceId = nil
                            pcall(function() appearanceId = result:ToString() end)
                            if appearanceId and appearanceId ~= "" and appearanceId ~= "None" then
                                slotData.appearanceId = appearanceId
                                slotData.appearance = GetDisplayName(appearanceId)
                            end
                        end
                    end)
                else
                    slotData.transmogged = false
                end
            end
        end)

        gear[slotName] = slotData
    end

    -- Hood status
    pcall(function()
        gear.HOOD = { up = gearManager:IsHoodUp(player) }
    end)

    -- Wand status
    pcall(function()
        gear.WAND = { equipped = player:IsWandEquipped() }
    end)

    return gear
end

-- Extract rarity from GearID (e.g., "Head_068_Legendary" -> "Legendary")
local function GetRarityFromId(gearId)
    if not gearId then return nil end
    local rarity = gearId:match("_(%a+)$")
    if rarity and (rarity == "Common" or rarity == "Uncommon" or rarity == "Rare"
                   or rarity == "Epic" or rarity == "Legendary") then
        return rarity
    end
    return nil
end

-- Get description from localization (item_desc key)
local function GetItemDescription(itemId)
    if not itemId or itemId == "" then return nil end
    if not _G.LocalizationLoaded then LoadLocalization() end
    if not _G.Localization then return nil end
    return _G.Localization[itemId .. "_desc"]
end

-- Format gear for LLM context (human-readable string)
-- Pass existing gear table to avoid redundant GetPlayerGear() call
function FormatPlayerGearForContext(gear)
    gear = gear or GetPlayerGear()
    if not gear then return "Unable to get player gear." end

    local lines = {}
    local slotOrder = {"HEAD", "FACE", "NECK", "OUTFIT", "BACK", "HAND"}

    for _, slot in ipairs(slotOrder) do
        local data = gear[slot]
        if data and data.equipped and data.name then
            local rarity = GetRarityFromId(data.id)
            local rarityStr = rarity and (" [" .. rarity .. "]") or ""

            -- Get description: prefer appearance description if transmogged, else base item
            local description = nil
            if data.transmogged and data.appearanceId then
                description = GetItemDescription(data.appearanceId)
            end
            if not description then
                description = GetItemDescription(data.id)
            end

            if data.transmogged and data.appearance then
                -- Transmogged: show what it looks like, note the stats source with rarity
                table.insert(lines, string.format("%s: %s (transmogged, stats from %s%s)",
                    slot, data.appearance, data.name, rarityStr))
            else
                table.insert(lines, string.format("%s: %s%s", slot, data.name, rarityStr))
            end

            -- Add description on next line
            if description then
                table.insert(lines, string.format("  - %s", description))
            end
        end
    end

    -- Accessories
    if gear.HOOD and gear.HOOD.up then
        table.insert(lines, "HOOD: Up")
    end
    if gear.WAND and gear.WAND.equipped then
        table.insert(lines, "WAND: Equipped")
    end

    return table.concat(lines, "\n")
end

-- F7 Debug Function - Toggle lock/unlock on NPC player is looking at
function DebugF7()
    ExecuteInGameThread(function()
        print("[DebugF7] === Toggle NPC Lock ===")

        -- Get NPC player is looking at
        local npc, npcName, npcDist = GetLookedAtNPC(0.7, 2000)
        if not npc then
            print("[DebugF7] No NPC in line of sight")
            return
        end

        print("[DebugF7] NPC: " .. tostring(npcName) .. " (dist: " .. math.floor(npcDist) .. ")")

        -- Check if NPC is already locked
        local existingLock = FindExistingLock(npc)
        if existingLock then
            -- NPC is locked, release them
            ReleaseNPC(existingLock)
            print("[DebugF7] UNLOCKED " .. tostring(npcName))
        else
            -- NPC is not locked, lock them to face the player
            local staticData = Cache.GetStatic(RefreshStaticData, 30)
            local player = staticData and staticData.player
            if not player then
                print("[DebugF7] Player not found")
                return
            end

            local lockId = LockNPCToTarget(npc, player, nil)
            if lockId then
                print("[DebugF7] LOCKED " .. tostring(npcName) .. " (id=" .. lockId .. ")")
            else
                print("[DebugF7] Failed to lock " .. tostring(npcName))
            end
        end

        print("[DebugF7] === END ===")
    end)
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

-- ============================================
-- SetBlendshape - set morph target on NPC
-- ============================================
function CallSetBlendshape(actor, curveName, value, modActor)
    -- Use passed modActor or fetch (caller should cache for multiple calls)
    local mod = modActor or GetSonorusModActor()
    if not mod then
        print("[Blueprint] SetBlendshape error: ModActor is nil")
        return false
    end
    if not actor then
        print("[Blueprint] SetBlendshape error: Actor is nil")
        return false
    end

    local ok, err = pcall(function()
        mod:setblendshape(actor, curveName, value)
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
    local mod = GetSonorusModActor()
    if not mod then
        print("[Blueprint] ActionExecute error: ModActor is nil")
        return false
    end
    if not actor then
        print("[Blueprint] ActionExecute error: Actor is nil")
        return false
    end

    local ok, err = pcall(function()
        mod:actionexecute(actor, actionName)
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
        for _, comp in ipairs(mutedComps) do
            UnmuteNPCAudio(comp)
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

-- Send dialogue entry to Python for persistence
-- Python is the sole writer for dialogue_history.json to avoid race conditions
local function sendDialogueEntry(entry)
    if not entry then return end

    -- Send to Python via socket
    pcall(function()
        SocketClient.send({
            type = "record_dialogue",
            entry = entry
        })
    end)
end

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

local function GetEarshotWitnesses(speakerVoiceName)
    -- Get list of named NPC IDs within earshot, excluding speaker and player
    -- Uses player-relative nearbyNPCs as a proxy for speaker-relative earshot
    local witnesses = {}

    -- Get nearby NPCs (use cached result if available from recent WriteGameContext)
    local npcResult = nil
    pcall(function()
        npcResult = GetNearbyNPCs(2000, 0.9)
    end)

    if not npcResult or not npcResult.nearbyList then
        return witnesses
    end

    -- Reduce earshot distance when player is invisible (Disillusionment)
    local earshotDistance = 1000  -- ~10m normal
    if npcResult.playerInStealth then
        earshotDistance = 300  -- ~3m when invisible (30%)
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

-- GetPlayerGear is defined earlier in the file (around line 541)

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
                for _, entry in ipairs(npcResult.nearbyList) do
                    table.insert(nearbyNpcsForContext, {
                        name = entry.name,
                        distance = math.floor(entry.distance),
                        isLookedAt = entry.isLookedAt,
                        onScreen = entry.onScreen
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
                pcall(function()
                    local companionMgr = staticData and staticData.companionManager
                    if companionMgr then
                        local companionPawn = companionMgr:GetPrimaryCompanionPawn()
                        if companionPawn then
                            context.hasCompanion = true
                            context.companionInStealth = context.inStealth  -- Shares player's state
                            -- Companion swimming (via cached NPC_Component class)
                            pcall(function()
                                local npcCompClass = staticData.npcComponentClass
                                if npcCompClass then
                                    local npcComp = companionPawn:GetComponentByClass(npcCompClass)
                                    if npcComp then
                                        context.companionIsSwimming = npcComp:IsSwimming() or false
                                    end
                                end
                            end)
                            -- Get companion name using PhoenixBPLibrary (same as NPCs)
                            pcall(function()
                                local lib = staticData.bpLibrary
                                if lib then
                                    local nameResult = lib:GetActorName(companionPawn)
                                    if nameResult then
                                        local name = nil
                                        pcall(function() name = nameResult:ToString() end)
                                        if name and name ~= "" then
                                            context.companionId = name  -- Internal ID, not display name
                                        end
                                    end
                                end
                            end)
                        end
                    end
                end)
            end
        end)

        -- Check if player is on broom (now tracked via hooks in main.lua)
        -- Fallback to GearScreen if hooks haven't fired yet
        if _G.BroomState then
            context.isOnBroom = _G.BroomState.mounted or false
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
--   companion: hasCompanion, companionId, companionInStealth, companionIsSwimming

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
            context.timePeriod = time.period
            context.isDay = time.isDay
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
                for _, entry in ipairs(npcResult.nearbyList) do
                    table.insert(nearbyNpcsForContext, {
                        name = entry.name,
                        distance = math.floor(entry.distance),
                        isLookedAt = entry.isLookedAt,
                        onScreen = entry.onScreen
                    })
                end
                context.nearbyNpcs = nearbyNpcsForContext
                if npcResult.lookedAtNpc then
                    context.lookedAtNpcName = npcResult.lookedAtNpc.name
                end
            end
        end)
    end

    -- GROUP: vision (for vision LLM - line trace visibility checks on on-screen NPCs)
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
        pcall(function()
            local staticData = Cache.GetStaticData()
            local companionMgr = staticData and staticData.companionManager
            if companionMgr then
                local companionPawn = companionMgr:GetPrimaryCompanionPawn()
                if companionPawn then
                    context.hasCompanion = true
                    -- Companion shares player's stealth state
                    if player then
                        pcall(function() context.companionInStealth = player.InStealthMode or false end)
                    end
                    -- Companion swimming (via cached NPC_Component class)
                    pcall(function()
                        local npcCompClass = staticData.npcComponentClass
                        if npcCompClass then
                            local npcComp = companionPawn:GetComponentByClass(npcCompClass)
                            if npcComp then
                                context.companionIsSwimming = npcComp:IsSwimming() or false
                            end
                        end
                    end)
                    -- Get companion name
                    pcall(function()
                        local lib = staticData.bpLibrary
                        if lib then
                            local nameResult = lib:GetActorName(companionPawn)
                            if nameResult then
                                local name = nil
                                pcall(function() name = nameResult:ToString() end)
                                if name and name ~= "" then
                                    context.companionId = name  -- Internal ID, not display name
                                end
                            end
                        end
                    end)
                end
            end
        end)
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
        -- Check if text changed (for updates vs fresh add)
        local textChanged = (_G.ChatInputLastText ~= text)
        _G.ChatInputLastText = text

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

-- Show notification toast (top-left notification panel)
function ShowNotification(text)
    if not text or text == "" then return end

    -- Use cached HUD lookup
    local hud = Cache.Get("PhoenixHUD", function()
        return FindFirstOf("PhoenixHUD")
    end)

    if not hud then return end

    pcall(function()
        hud.HUDWidgetRef.TextNotificationPanel:AddNotification(text)
    end)
end

-- ============================================
-- NPC Audio Muting Functions
-- ============================================
function MuteNPCAudio(actor)
    if not actor or not SafeIsValid(actor) then
        print("[Sonorus] MuteNPCAudio: Invalid actor")
        return nil
    end

    local staticData = Cache.GetStaticData()
    local akClass = staticData and staticData.akComponentClass
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
-- Store last speaker actor for closing (survives cache clears)
_G.LastSpeakerActorForClosing = nil

function CloseLips()
    -- Timeout protection: force complete after ~1.5 seconds (30 ticks at 50ms)
    _G.CloseLipsIterations = (_G.CloseLipsIterations or 0) + 1
    if _G.CloseLipsIterations >= 30 then
        print("[Sonorus] CloseLips: Timeout - forcing completion")
        local actor = _G.LastSpeakerActorForClosing
        if actor then
            ForceResetBlendshapes(actor)
        end
        ResetNearbyNPCLips()
        _G.CloseLipsComplete = true
        _G.CloseLipsIterations = 0
        _G.LastSpeakerActorForClosing = nil
        -- Reset viseme data
        local data = _G.VisemeData
        data.currentJaw = 0
        data.currentSmile = 0
        data.currentFunnel = 0
        data.loaded = false
        data.frames = {}
        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({ type = "turn_complete" })
        end
        return
    end

    -- Use stored actor from AnimateLips - DO NOT call GetCurrentSpeakerActor()
    -- because currentTurnId may have already changed to the next speaker (pre-buffering)
    local actor = _G.LastSpeakerActorForClosing
    if actor then
        local valid = false
        pcall(function() valid = actor:IsValid() end)
        if not valid then
            actor = nil
            _G.LastSpeakerActorForClosing = nil
        end
    end

    if not actor then
        print("[Sonorus] CloseLips: No actor found - resetting all nearby NPC lips")
        -- Force reset all nearby NPCs as fallback
        ResetNearbyNPCLips()
        _G.CloseLipsComplete = true
        _G.LastSpeakerActorForClosing = nil
        -- Still signal Python even if no actor (so it doesn't wait for timeout)
        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({ type = "turn_complete" })
        end
        return  -- Done
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
        _G.LastSpeakerActorForClosing = nil  -- Clear stored actor

        -- Signal Python that this turn's mouth animation is complete
        -- This allows Python to safely start the next turn
        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({ type = "turn_complete" })
        end
        print("[Sonorus] CloseLips: Complete")
    end
    -- Still closing - flag remains false
end

-- Force reset all lip blendshapes on an actor (instant, no smooth transition)
-- Used by ResetState to fix any NPCs with stuck lip sync
function ForceResetBlendshapes(actor)
    if not actor then return end
    local valid = false
    pcall(function() valid = actor:IsValid() end)
    if not valid then return end

    local blendshapes = {
        "lwr_lip_funl_l", "lwr_lip_funl_r", "upr_lip_funl_r", "upr_lip_funl_l",
        "jaw_drop", "lips_up_l", "lwr_lip_dn_l", "lwr_lip_dn_r",
        "dimple_l", "dimple_r", "smile_l", "smile_r",
        "mouth_mov_r", "mouth_mov_l", "lips_up_r"
    }
    for _, name in ipairs(blendshapes) do
        CallSetBlendshape(actor, name, 0)
    end
end

-- Reset lips on all nearby NPCs (used by F8 reset to fix stuck blendshapes)
function ResetNearbyNPCLips()
    print("[Sonorus] Resetting lips on nearby NPCs...")
    local npcResult = GetNearbyNPCs(2000, 0.9)

    if not npcResult or not npcResult.nearbyList then
        print("[Sonorus] No nearby NPCs found")
        return
    end

    -- Collect valid actors
    local actors = {}
    for _, entry in ipairs(npcResult.nearbyList) do
        if entry.actor then
            table.insert(actors, entry.actor)
        end
    end

    if #actors == 0 then
        print("[Sonorus] No valid NPC actors found")
        return
    end

    -- Loop reset over multiple frames to overcome Blueprint lerping
    local iterations = 0
    local maxIterations = 20
    local resetHandle
    resetHandle = LoopInGameThreadAfterFrames(1, function()
        iterations = iterations + 1
        for _, actor in ipairs(actors) do
            ForceResetBlendshapes(actor)
        end
        if iterations >= maxIterations then
            CancelDelayedAction(resetHandle)
            print("[Sonorus] Reset blendshapes on " .. #actors .. " nearby NPCs (complete)")
        end
    end)
    print("[Sonorus] Started lip reset loop for " .. #actors .. " NPCs")
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

    -- Store actor for CloseLips (survives cache clears)
    _G.LastSpeakerActorForClosing = actor

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
    data.gearScreen = StaticFindObject("/Script/Phoenix.Default__GearScreen")
    data.npcComponentClass = StaticFindObject("/Script/Phoenix.NPC_Component")
    data.audioStatics = StaticFindObject("/Script/Phoenix.Default__AvaAudioGameplayStatics")
    data.akComponentClass = StaticFindObject("/Script/AkAudio.AkComponent")
    data.facialComponentClass = StaticFindObject("/Script/AvaAnimation.FacialComponent")
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
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = false }
    end

    local cam = staticData.cameraManager
    local camValid = false
    if cam then pcall(function() camValid = cam:IsValid() end) end
    if not camValid then
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = false }
    end

    local camLoc, camRot, camFOV
    pcall(function()
        camLoc = cam:GetCameraLocation()
        camRot = cam:GetCameraRotation()
        camFOV = cam:GetFOVAngle()  -- Get camera field of view
    end)
    if not camLoc or not camRot then
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
    if player then
        pcall(function() playerInStealth = player.InStealthMode or false end)
    end

    -- Use reactive NPC cache (no FindAllOf after first load)
    local npcs = GetCachedNPCs()
    if not npcs or #npcs == 0 then
        return { nearbyList = {}, lookedAtNpc = nil, playerInStealth = playerInStealth }
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
        -- Wrap validity check in pcall - corrupted references crash on :IsValid() call
        local isValid = false
        pcall(function() isValid = npc:IsValid() end)
        if isValid then
            local fullName = nil
            pcall(function() fullName = npc:GetFullName() end)
            if fullName and fullName ~= playerFullName then
                local npcLoc = nil
                pcall(function() npcLoc = npc:K2_GetActorLocation() end)
                if not npcLoc then goto continue end

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

                    -- Check if NPC is within camera FOV (on screen)
                    local onScreen = dot > onScreenThreshold

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
                        isLookedAt = isLookedAt,
                        onScreen = onScreen
                    })
                end
            end
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

    return { nearbyList = nearbyList, lookedAtNpc = lookedAtNpc, playerInStealth = playerInStealth }
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
-- Line Trace Visibility Check (for Vision LLM)
-- Returns list of NPC names that are actually visible (not occluded)
-- ============================================
function CheckNPCVisibility(npcList)
    if not npcList or #npcList == 0 then
        return {}
    end

    local UEHelpers = require("UEHelpers")
    local KismetSystem = nil
    local KismetMath = nil

    pcall(function()
        KismetSystem = UEHelpers.GetKismetSystemLibrary()
        KismetMath = UEHelpers.GetKismetMathLibrary()
    end)

    if not KismetSystem or not KismetMath then
        print("[Sonorus] CheckNPCVisibility: Kismet libraries not available")
        -- Return all as visible if we can't do traces
        local result = {}
        for _, npc in ipairs(npcList) do
            result[npc.name] = true
        end
        return result
    end

    -- Get camera position
    local staticData = GetStaticCache()
    local cam = staticData and staticData.cameraManager
    if not cam then
        print("[Sonorus] CheckNPCVisibility: No camera manager")
        return {}
    end

    local camLoc = nil
    pcall(function() camLoc = cam:GetCameraLocation() end)
    if not camLoc then
        print("[Sonorus] CheckNPCVisibility: Could not get camera location")
        return {}
    end

    local player = staticData.player
    local playerPawn = nil
    if player then
        pcall(function() playerPawn = player end)
    end

    -- Trace settings
    local ETraceTypeQuery_Visibility = 0  -- Visibility channel
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

        -- Get NPC location (add Z offset for torso/head height ~100 units)
        local npcLoc = nil
        pcall(function() npcLoc = npcActor:K2_GetActorLocation() end)
        if not npcLoc then
            visibilityResults[npcName] = false
            goto continue
        end

        -- Offset to aim at torso/head rather than feet
        local targetLoc = {
            X = npcLoc.X,
            Y = npcLoc.Y,
            Z = npcLoc.Z + 100  -- ~1 meter up from origin (torso height)
        }

        -- Build end vector
        local EndVector = {}
        pcall(function()
            EndVector = KismetMath:MakeVector(targetLoc.X, targetLoc.Y, targetLoc.Z)
        end)

        -- Actors to ignore (player pawn)
        local ActorsToIgnore = {}
        if playerPawn then
            table.insert(ActorsToIgnore, playerPawn)
        end

        -- Do line trace
        local HitResult = {}
        local WasHit = false

        pcall(function()
            WasHit = KismetSystem:LineTraceSingle(
                playerPawn or npcActor,  -- WorldContextObject
                camLoc,                   -- Start
                EndVector,                -- End
                ETraceTypeQuery_Visibility,
                false,                    -- bTraceComplex
                ActorsToIgnore,
                EDrawDebugTrace_None,
                HitResult,
                true,                     -- bIgnoreSelf
                TraceColor,
                TraceColor,
                0.0                       -- DrawTime
            )
        end)

        if WasHit then
            -- Check if we hit the NPC or something else
            local HitActor = nil
            pcall(function()
                -- Handle different UE versions
                if UnrealVersion:IsBelow(5, 0) then
                    HitActor = HitResult.Actor:Get()
                elseif UnrealVersion:IsBelow(5, 4) then
                    HitActor = HitResult.HitObjectHandle.Actor:Get()
                else
                    HitActor = HitResult.HitObjectHandle.ReferenceObject:Get()
                end
            end)

            if HitActor == npcActor then
                -- We hit the NPC directly - they're visible
                visibilityResults[npcName] = true
            else
                -- We hit something else first - NPC is occluded
                visibilityResults[npcName] = false
            end
        else
            -- No hit means clear line of sight (shouldn't happen if NPC is there, but treat as visible)
            visibilityResults[npcName] = true
        end

        ::continue::
    end

    return visibilityResults
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
                    HideMessage()
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

    -- Unmute all speakers
    UnmuteAllSpeakers()

    -- Release all locked NPCs
    ReleaseAllNPCs()

    -- Close lips
    CloseLips()

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
    end

    local timestamp = os.time()

    local speakerName = "Unknown"
    if speakingActor then
        pcall(function()
            if speakingActor:IsValid() then
                local staticData = Cache.GetStaticData()
                local lib = staticData and staticData.bpLibrary
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

    -- Get earshot witnesses (nearby named NPCs)
    local earshot = GetEarshotWitnesses("Player")

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
    }

    -- Send to Python for persistence
    sendDialogueEntry(entry)

    -- Log for debugging
    print(string.format("[Sonorus] Location: %s entered %s", playerName, newLocation))
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
        -- Skip companions and static locks (portraits, desk NPCs, etc.)
        local needsReface = {}
        for lockId, data in pairs(_G.LockedNPCs) do
            if data.locked and data.npc and data.targetActor
               and not data.isCompanionLock and not data.isStaticLock then
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
_G.UnifiedLoop.interval = _G.UnifiedLoop.interval or 100  -- Default 100ms, configurable via config page

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
        -- This is CRITICAL - socket must update frequently for responsive chat input
        -- NOTE: Pure LuaSocket, no UObjects
        if _G.SocketClient then
            pcall(_G.SocketClient.update)
        else
            print("No socket client!")
        end
        if devMode then t2 = os.clock() end

        -- Process chat input display (already on game thread, call directly)
        if _G.ChatInputState and _G.ChatInputState.dirty then
            _ProcessChatInputWrapper()
        end
        if devMode then t3 = os.clock() end

        -- REMOVED: Periodic context writing - now on-demand via handshake
        -- Python requests context when needed via request_context message
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

        -- Cinematic and combat state polling every 1 second
        _G.UnifiedLoop.lastStateCheck = _G.UnifiedLoop.lastStateCheck or 0
        if (now - _G.UnifiedLoop.lastStateCheck) >= 1.0 then
            _G.UnifiedLoop.lastStateCheck = now

            local inCinematic = false
            local inCombat = false
            pcall(function()
                local staticData = GetStaticCache()
                local player = staticData.player
                if player then
                    inCinematic = player.InCinematic or false
                    inCombat = player.bInCombatMode or false
                end
            end)

            -- Cinematic state change - stop conversations
            local prevCinematic = _G.CinematicState and _G.CinematicState.active or false
            if inCinematic ~= prevCinematic then
                _G.CinematicState = _G.CinematicState or {}
                _G.CinematicState.active = inCinematic
                if inCinematic then
                    print("[Sonorus] Cinematic started - stopping conversation")
                    if ResetState then pcall(ResetState) end
                else
                    print("[Sonorus] Cinematic ended")
                end
                -- Send state-only context update (cheap)
                pcall(function() WriteSelectiveContext({"state"}) end)
            end

            -- Combat state change - block new conversations
            local prevCombat = _G.CombatState and _G.CombatState.active or false
            if inCombat ~= prevCombat then
                _G.CombatState = _G.CombatState or {}
                _G.CombatState.active = inCombat
                if inCombat then
                    print("[Sonorus] Combat started")
                else
                    print("[Sonorus] Combat ended")
                end
                -- Send state-only context update (cheap)
                pcall(function() WriteSelectiveContext({"state"}) end)
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
        if devMode then t5 = os.clock() end

        -- Log timing when devMode enabled (times in ms)
        if true and devMode and t0 then
            local total = (t5 - t0) * 1000
            if total >= 20 then  -- Only log if tick took > 1ms
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

    -- Cast the spell: Cancel -> Activate -> Cast
    local castOk, castErr = pcall(function()
        wandTool:CancelCurrentSpell()
        wandTool:ActivateSpellTool(spellToolRecord, false)
        wandTool:CastActiveSpell()
    end)

    if not castOk then
        print("[VoiceSpell] Cast error: " .. tostring(castErr))
        return false
    end

    print("[VoiceSpell] Cast successful: " .. internalSpellName)

    return true
end

-- ============================================
-- Mod Initialization
-- ============================================

print("[Sonorus] logic.lua ready!")
