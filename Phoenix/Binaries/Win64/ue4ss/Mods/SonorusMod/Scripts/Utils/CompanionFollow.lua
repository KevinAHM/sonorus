-- Utils/CompanionFollow.lua
-- Ensures companion doesn't stall behind the player after idle periods.
-- The game's companion AI sometimes stops following after the player is idle for ~5-10s.
-- This polls every 2s and nudges the companion via CompanionManager:MoveToLocation
-- to a point 200uu from the player when it detects the companion has stalled.

local CompanionFollow = {}

local Cache = require("Utils.Cache")
local Utils = require("Utils.Utils")
local TickScheduler = require("Utils.TickScheduler")

-- Static cache getter - set via init()
local getStaticCache = nil

-- Timer handles (persisted in _G for hot reload)
_G.CompanionFollowHandle = _G.CompanionFollowHandle or nil
_G.FollowerTickHandle = _G.FollowerTickHandle or nil
_G.FollowerTickDelayHandle = _G.FollowerTickDelayHandle or nil

-- Feature toggle (persisted in _G for hot reload, default OFF)
_G.CompanionFollowEnabled = false

-- Config
local POLL_INTERVAL_MS = 2091
local DEFAULT_FOLLOW_DISTANCE = 200  -- uu (2.0m default)
local TELEPORT_DISTANCE = 2000       -- uu: teleport NPC if farther than this
local TELEPORT_BEHIND_DIST = 300     -- uu: how far behind the player to place them

-- Stuck detection: companion is moving (speed > 0) but barely (speed < threshold)
-- e.g. running animation playing but caught on geometry
local STUCK_SPEED_THRESHOLD = 50     -- uu/s: below this while "moving" = likely stuck
local STUCK_TICKS_TO_TELEPORT = 3    -- consecutive stuck ticks before teleporting (3 * 2s = 6s)

-- Persistent stuck counter (survives hot reload)
_G.CompanionStuckTicks = _G.CompanionStuckTicks or 0

--- Try to find a valid position behind the player for NPC teleport.
--- Calculates a point behind the player, checks LOS from player head,
--- then does a floor trace to snap to ground. Returns position or nil.
local function findTeleportPosition(staticData, player, playerLoc, playerRot, npcActor)
    local KismetSystem = staticData.kismetSystem
    local KismetMath = staticData.kismetMath
    if not KismetSystem or not KismetMath then return nil end

    local yawRad = math.rad(playerRot.Yaw or 0)
    local cosYaw = math.cos(yawRad)
    local sinYaw = math.sin(yawRad)

    -- Player head position for LOS trace origin
    local playerHH = 88
    pcall(function()
        local cap = player.CapsuleComponent
        if cap and cap.CapsuleHalfHeight then playerHH = cap.CapsuleHalfHeight end
    end)
    local headZ = playerLoc.Z + playerHH * 2

    -- Try several angles behind the player: directly behind, then left/right offsets
    local angles = { 180, 200, 160, 220, 140 }  -- degrees offset from forward
    for _, angleOff in ipairs(angles) do
        local rad = yawRad + math.rad(angleOff)
        local candidateX = playerLoc.X + math.cos(rad) * TELEPORT_BEHIND_DIST
        local candidateY = playerLoc.Y + math.sin(rad) * TELEPORT_BEHIND_DIST
        local candidateZ = playerLoc.Z

        -- 1) LOS check: trace from player head to candidate (at head height)
        local losOk = false
        pcall(function()
            local traceStart = KismetMath:MakeVector(playerLoc.X, playerLoc.Y, headZ)
            local traceEnd = KismetMath:MakeVector(candidateX, candidateY, headZ)
            local HitResult = {}
            local TraceColor = { R = 0, G = 0, B = 0, A = 0 }
            local WasHit = KismetSystem:LineTraceSingle(
                player, traceStart, traceEnd,
                0, false, { player, npcActor },
                0, HitResult, true,
                TraceColor, TraceColor, 0.0
            )
            losOk = not WasHit  -- no hit = clear LOS
        end)

        if not losOk then
            print(string.format("[NPCFollow] Teleport angle %d blocked by wall", angleOff))
            goto nextAngle
        end

        -- 2) Floor trace: find ground at candidate position
        local floorPos = nil
        pcall(function()
            local floorStart = KismetMath:MakeVector(candidateX, candidateY, candidateZ + 200)
            local floorEnd = KismetMath:MakeVector(candidateX, candidateY, candidateZ - 200)
            local FloorHit = {}
            local TraceColor = { R = 0, G = 0, B = 0, A = 0 }
            local FloorWasHit = KismetSystem:LineTraceSingle(
                player, floorStart, floorEnd,
                0, false, { player, npcActor },
                0, FloorHit, true,
                TraceColor, TraceColor, 0.0
            )
            if FloorWasHit then
                floorPos = {
                    X = FloorHit.ImpactPoint_X or FloorHit.ImpactPoint.X,
                    Y = FloorHit.ImpactPoint_Y or FloorHit.ImpactPoint.Y,
                    Z = FloorHit.ImpactPoint_Z or FloorHit.ImpactPoint.Z
                }
            end
        end)

        if floorPos then
            print(string.format("[NPCFollow] Teleport found valid spot at angle %d: (%.0f, %.0f, %.0f)",
                angleOff, floorPos.X, floorPos.Y, floorPos.Z))
            return floorPos
        end

        ::nextAngle::
    end

    print("[NPCFollow] Teleport: no valid position found behind player")
    return nil
end

function CompanionFollow.init(staticCacheGetter)
    getStaticCache = staticCacheGetter
end

-- Check if companion pawn is locked by NPCLock
local function isCompanionLocked(companionPawn)
    if not _G.LockedNPCs or not companionPawn then return false end
    local compName = companionPawn:GetFullName()
    for _, data in pairs(_G.LockedNPCs) do
        if data.npc and SafeIsValid(data.npc) and data.npc:GetFullName() == compName then
            return true
        end
    end
    return false
end

-- Check if companion is in a forced wait state (delegate to Utils helper)
local function isCompanionForceWaiting(cm, companionPawn)
    return Utils.IsCompanionForcedWaiting(companionPawn, cm)
end

-- Single tick of companion follow check
function CompanionFollow.tick()
    if not _G.CompanionFollowEnabled then _G.CompanionStuckTicks = 0 return end
    -- Skip during combat/broom - let companion AI handle its own positioning
    if _G.CombatState and _G.CombatState.active then _G.CompanionStuckTicks = 0 return end
    if _G.MountState and _G.MountState.mounted then _G.CompanionStuckTicks = 0 return end
    if not getStaticCache then _G.CompanionStuckTicks = 0 return end
    local staticData = getStaticCache()
    if not staticData then _G.CompanionStuckTicks = 0 return end

    local cm = staticData.companionManager
    local player = staticData.player
    if not cm or not SafeIsValid(cm) then _G.CompanionStuckTicks = 0 return end
    if not player or not SafeIsValid(player) then _G.CompanionStuckTicks = 0 return end

    local following = false
    pcall(function() following = cm:HasPrimaryFollowingCompanion() end)
    if not following then _G.CompanionStuckTicks = 0 return end

    local companionPawn = nil
    pcall(function() companionPawn = cm:GetPrimaryCompanionPawn() end)
    if not companionPawn or not SafeIsValid(companionPawn) then _G.CompanionStuckTicks = 0 return end

    -- Distance
    local dist = 0
    pcall(function()
        local pLoc = player:K2_GetActorLocation()
        local cLoc = companionPawn:K2_GetActorLocation()
        local dx = pLoc.X - cLoc.X
        local dy = pLoc.Y - cLoc.Y
        local dz = pLoc.Z - cLoc.Z
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
    end)

    local followDist = _G.CompanionFollowDistanceUU or DEFAULT_FOLLOW_DISTANCE
    local stallDist = followDist * 2  -- nudge when 2x the follow distance away
    if dist <= stallDist then
        _G.CompanionStuckTicks = 0
        return
    end

    -- Check velocity
    local speed = 0
    pcall(function()
        local vel = companionPawn:GetVelocity()
        if vel then
            speed = math.sqrt(vel.X*vel.X + vel.Y*vel.Y + vel.Z*vel.Z)
        end
    end)

    -- Skip if force waiting or locked
    if isCompanionForceWaiting(cm, companionPawn) then
        _G.CompanionStuckTicks = 0
        return
    end
    if isCompanionLocked(companionPawn) then
        _G.CompanionStuckTicks = 0
        return
    end

    -- Stuck detection: companion is moving but barely (caught on geometry)
    -- speed > 0 but < threshold while far from player = stuck on obstacle
    if speed > 0 and speed < STUCK_SPEED_THRESHOLD then
        _G.CompanionStuckTicks = _G.CompanionStuckTicks + 1
        print(string.format("[CompanionFollow] Stuck tick %d/%d (dist=%.0f speed=%.0f)",
            _G.CompanionStuckTicks, STUCK_TICKS_TO_TELEPORT, dist, speed))
        if _G.CompanionStuckTicks >= STUCK_TICKS_TO_TELEPORT then
            local teleOk = false
            pcall(function() teleOk = cm:TryCompanionTeleportBehindPlayer() end)
            print(string.format("[CompanionFollow] Stuck teleport -> %s", tostring(teleOk)))
            _G.CompanionStuckTicks = 0
        end
        return
    end

    -- Companion is actually moving at normal speed — not stuck
    if speed > 1 then
        _G.CompanionStuckTicks = 0
        return
    end

    -- Fully stalled (speed ~0): nudge via MoveToLocation
    _G.CompanionStuckTicks = 0
    pcall(function()
        local pLoc = player:K2_GetActorLocation()
        local cLoc = companionPawn:K2_GetActorLocation()
        local dx = cLoc.X - pLoc.X
        local dy = cLoc.Y - pLoc.Y
        local len = math.sqrt(dx*dx + dy*dy)
        if len > 0 then
            local tgt = {
                X = pLoc.X + (dx/len) * followDist,
                Y = pLoc.Y + (dy/len) * followDist,
                Z = pLoc.Z
            }
            cm:MoveToLocation(tgt, companionPawn)
        end
    end)
end

-- Apply follow distance setting to game's CompanionManager config
-- Call after load/fast travel since the config object gets recreated
function CompanionFollow.applySettings()
    if not getStaticCache then return end
    local staticData = getStaticCache()
    if not staticData then return end
    local cm = staticData.companionManager
    if not cm or not SafeIsValid(cm) then return end
    local distUU = _G.CompanionFollowDistanceUU or DEFAULT_FOLLOW_DISTANCE
    pcall(function()
        if cm.Config and cm.Config:IsValid() then
            local sd = cm.Config.CompanionSettingData
            if sd then
                sd.CompanionIdealFollowDistance = distUU
                sd.CompanionIdealFollowBufferDistance = 0.0
            end
        end
    end)
    -- Push config to the live companion via SetCompanionSettingDataToConfigBP
    -- NOTE: GetPrimaryCompanionNameBP returns FName which can native crash - use GetCompanionId instead
    pcall(function()
        local companionPawn = cm:GetPrimaryCompanionPawn()
        if companionPawn and companionPawn:IsValid() then
            local companionId = Utils.GetCompanionId(companionPawn)
            if companionId then
                cm:SetCompanionSettingDataToConfigBP(companionId)
            end
        end
    end)
end

function CompanionFollow.start()
    CompanionFollow.stop()
    TickScheduler.Register("companion_follow", POLL_INTERVAL_MS, function()
        CompanionFollow.tick()
    end)
    -- Follower tick on its own timer, offset by 1s to avoid stacking with companion tick
    TickScheduler.Register("npc_follower_tick", POLL_INTERVAL_MS + 432, function()
        CompanionFollow.followerTick()
    end)
    _G.CompanionFollowHandle = nil
    _G.FollowerTickHandle = nil
    print("[CompanionFollow] Started")
end

function CompanionFollow.stop()
    if _G.CompanionFollowHandle then
        pcall(function() CancelDelayedAction(_G.CompanionFollowHandle) end)
        _G.CompanionFollowHandle = nil
    end
    if _G.FollowerTickHandle then
        pcall(function() CancelDelayedAction(_G.FollowerTickHandle) end)
        _G.FollowerTickHandle = nil
    end
    TickScheduler.Unregister("companion_follow")
    TickScheduler.Unregister("npc_follower_tick")
    print("[CompanionFollow] Stopped")
end

-- ============================================
-- NPC Follower System
-- Regular NPCs that follow the player like a companion.
-- Scheduling disabled, movement driven by PerformTask_MoveToLocation.
-- ============================================

-- Persistent state across hot reloads
_G.NPCFollowers = _G.NPCFollowers or {}  -- voiceName -> { scheduledEntity, flesh, offset, npcName }
if _G.FollowersEnabled == nil then _G.FollowersEnabled = true end

-- Formation offsets: fan pattern behind player
-- Each new follower gets the next offset in the list
-- Companion occupies ~200uu directly behind player.
-- Followers go further back and to the sides to avoid overlap.
local FORMATION_OFFSETS = {
    { X = -350, Y = -250 },
    { X = -350, Y = 250 },
    { X = -500, Y = 0 },
    { X = -500, Y = -300 },
    { X = -500, Y = 300 },
    { X = -650, Y = -150 },
    { X = -650, Y = 150 },
}

--- Get the number of current followers
function CompanionFollow.getFollowerCount()
    local count = 0
    for _ in pairs(_G.NPCFollowers) do count = count + 1 end
    return count
end

-- Get fresh UObject refs for schedule override (same pattern as CommitmentManager)
local function GetFollowerRefs(voiceName)
    local staticData = getStaticCache and getStaticCache()
    if not staticData then return nil end

    local refs = {}
    refs.popManager = staticData.populationManager
    if not refs.popManager or not SafeIsValid(refs.popManager) then return nil end

    pcall(function() refs.se = refs.popManager:GetScheduledEntityFromName(voiceName) end)
    if not refs.se then return nil end

    local seValid = false
    pcall(function() seValid = refs.se:IsValid() end)
    if not seValid then return nil end

    -- Provider: mod actor or player controller
    pcall(function() refs.provider = _G.SonorusState and _G.SonorusState.sonorusModActor end)
    if not refs.provider or not SafeIsValid(refs.provider) then
        refs.provider = staticData.playerController
    end

    -- WorldEventActor (always fresh)
    pcall(function() refs.weActor = FindFirstOf("WorldEventActor") end)

    return refs
end

--- Add an NPC as a follower
--- @param npcActor userdata The NPC actor
--- @param voiceName string The NPC's voice ID
--- @return boolean success
function CompanionFollow.addFollower(npcActor, voiceName)
    if _G.FollowersEnabled == false then
        print("[NPCFollow] Followers disabled")
        return false
    end

    if not npcActor or not voiceName or voiceName == "" then
        print("[NPCFollow] Missing actor or voiceName")
        return false
    end

    if not SafeIsValid(npcActor) then
        print("[NPCFollow] Actor not valid for " .. voiceName)
        return false
    end

    -- Already a follower?
    if _G.NPCFollowers[voiceName] then
        print("[NPCFollow] " .. voiceName .. " is already a follower")
        return false
    end

    local refs = GetFollowerRefs(voiceName)
    if not refs then
        print("[NPCFollow] Could not get refs for " .. voiceName)
        return false
    end

    -- Pick formation offset
    local count = CompanionFollow.getFollowerCount()
    local offsetIdx = (count % #FORMATION_OFFSETS) + 1
    local offset = FORMATION_OFFSETS[offsetIdx]

    -- Break from station
    pcall(function() refs.se:AbandonStations(0) end)
    print("[NPCFollow] AbandonStations done for " .. voiceName)

    -- -- Schedule override (disabled — doesn't prevent station returns)
    -- local overrideResult = nil
    -- local ok1, err1 = pcall(function()
    --     overrideResult = refs.se:StartSchedulingOverride(true, 4, refs.provider, true, true, true)
    -- end)
    -- print(string.format("[NPCFollow] StartSchedulingOverride: ok=%s result=%s err=%s",
    --     tostring(ok1), tostring(overrideResult), tostring(err1)))

    _G.NPCFollowers[voiceName] = {
        scheduledEntity = refs.se,
        flesh = npcActor,
        offset = offset,
        npcName = voiceName,
    }

    print("[NPCFollow] Added follower: " .. voiceName .. " (offset " .. offsetIdx .. ")")
    return true
end

--- Remove an NPC from followers
--- @param voiceName string The NPC's voice ID
--- @return boolean success
function CompanionFollow.removeFollower(voiceName)
    local follower = _G.NPCFollowers[voiceName]
    if not follower then
        print("[NPCFollow] " .. tostring(voiceName) .. " is not a follower")
        return false
    end

    local refs = GetFollowerRefs(voiceName)
    if refs and refs.se then
        follower.scheduledEntity = refs.se
    end

    -- Clear our move task, then re-enable scheduling so NPC returns to normal behavior
    -- Reinforcement at 500ms required; re-fetch then too so we never carry
    -- a stale ScheduledEntity UObject across the delay.
    local se = refs and refs.se or nil
    if se then
        local inFlesh = false
        pcall(function() inFlesh = se:CurrentlyInFlesh() end)
        DevPrint("[NPCFollow] removeFollower fresh refs: " .. tostring(voiceName) .. " inFlesh=" .. tostring(inFlesh))
        pcall(function() se:PerformTask_RemoveActivePerformTask() end)
        pcall(function() se:EnableScheduling(true, false, true) end)
        ExecuteInGameThreadWithDelay(500, function()
            local delayedRefs = GetFollowerRefs(voiceName)
            local delayedSe = delayedRefs and delayedRefs.se or nil
            if delayedSe then
                pcall(function() delayedSe:EnableScheduling(true, false, true) end)
                DevPrint("[NPCFollow] removeFollower delayed scheduling enable: " .. tostring(voiceName))
            else
                DevPrint("[NPCFollow] removeFollower delayed refs unavailable: " .. tostring(voiceName))
            end
        end)
        print("[NPCFollow] Released: " .. voiceName)
    else
        print("[NPCFollow] Release: no fresh SE for " .. voiceName)
    end

    _G.NPCFollowers[voiceName] = nil
    print("[NPCFollow] Removed follower: " .. voiceName)
    return true
end

--- Remove all followers
function CompanionFollow.removeAllFollowers()
    local names = {}
    for voiceName, _ in pairs(_G.NPCFollowers) do
        table.insert(names, voiceName)
    end
    for _, voiceName in ipairs(names) do
        CompanionFollow.removeFollower(voiceName)
    end
    if #names > 0 then
        print("[NPCFollow] Removed all " .. #names .. " followers")
    end
end

--- Check if a given NPC is a follower
--- @param voiceName string
--- @return boolean
function CompanionFollow.isFollower(voiceName)
    return _G.NPCFollowers[voiceName] ~= nil
end

--- Single tick for NPC followers: move stalled followers toward player
--- Call from a polling loop (not hooked in yet)
function CompanionFollow.followerTick()
    if not next(_G.NPCFollowers) then return end
    if _G.FollowersEnabled == false then return end
    if _G.CombatState and _G.CombatState.active then return end
    if _G.MountState and _G.MountState.mounted then return end
    if not getStaticCache then return end
    local staticData = getStaticCache()
    if not staticData then return end

    local player = staticData.player
    if not player or not SafeIsValid(player) then return end

    local playerLoc, playerRot
    pcall(function()
        playerLoc = player:K2_GetActorLocation()
        playerRot = player:K2_GetActorRotation()
    end)
    if not playerLoc or not playerRot then return end

    -- Player's forward direction (for formation offset rotation)
    local yawRad = math.rad(playerRot.Yaw or 0)
    local cosYaw = math.cos(yawRad)
    local sinYaw = math.sin(yawRad)

    local toRemove = {}       -- bare cleanup (SE gone)
    local toRelease = {}      -- proper release (SE still valid)

    local popManager = staticData.populationManager
    if not popManager or not SafeIsValid(popManager) then return end

    -- Detect if any follower became the companion (via quest, mod, or action)
    local companionId = nil
    pcall(function()
        local companionMgr = staticData.companionManager
        if companionMgr then
            local pawn = companionMgr:GetPrimaryCompanionPawn()
            if pawn and SafeIsValid(pawn) then
                companionId = Utils.GetCompanionId(pawn)
            end
        end
    end)

    for voiceName, follower in pairs(_G.NPCFollowers) do
        -- Auto-remove if this NPC became the companion (proper release — SE still valid)
        if companionId and voiceName:lower() == companionId:lower() then
            print("[NPCFollow] " .. voiceName .. " is now the companion, removing from followers")
            table.insert(toRelease, voiceName)
            goto continue
        end

        -- Always re-fetch SE and flesh by name (actor refs go stale on re-stream)
        local se = nil
        pcall(function() se = popManager:GetScheduledEntityFromName(voiceName) end)
        if not se then
            print("[NPCFollow] " .. voiceName .. " SE not found, removing")
            table.insert(toRemove, voiceName)
            goto continue
        end

        local seValid = false
        pcall(function() seValid = se:IsValid() end)
        if not seValid then
            print("[NPCFollow] " .. voiceName .. " SE invalid, removing")
            table.insert(toRemove, voiceName)
            goto continue
        end

        -- Check if NPC is currently in flesh (streamed in)
        local inFlesh = false
        pcall(function() inFlesh = se:CurrentlyInFlesh() end)
        if not inFlesh then
            -- Not streamed in — skip this tick, don't remove (they may come back)
            goto continue
        end

        local npc = nil
        pcall(function() npc = se:GetFlesh() end)
        if not npc or not SafeIsValid(npc) then
            goto continue
        end

        -- Update stored refs (so removeFollower/NPCLock have fresh ones)
        follower.scheduledEntity = se
        follower.flesh = npc

        -- Skip if NPC is currently locked in conversation (NPCLock manages them)
        local nameNorm = voiceName:gsub(" ", ""):lower()
        local isLocked = false
        for _, cachedName in pairs(_G.LockedNPCNames or {}) do
            if cachedName == nameNorm then
                isLocked = true
                break
            end
        end
        if isLocked then
            goto continue
        end

        -- Prevent scheduler from reclaiming NPC into a station
        pcall(function() se:AbandonStations(0) end)

        -- Calculate target position: rotate offset by player yaw
        local ox = follower.offset.X
        local oy = follower.offset.Y
        local rotX = ox * cosYaw - oy * sinYaw
        local rotY = ox * sinYaw + oy * cosYaw
        local targetPos = {
            X = playerLoc.X + rotX,
            Y = playerLoc.Y + rotY,
            Z = playerLoc.Z
        }

        -- Check distance from NPC to its target position
        local npcLoc
        pcall(function() npcLoc = npc:K2_GetActorLocation() end)
        if not npcLoc then
            print("[NPCFollow] " .. voiceName .. " no location, skipping")
            goto continue
        end

        local dx = targetPos.X - npcLoc.X
        local dy = targetPos.Y - npcLoc.Y
        local dist = math.sqrt(dx * dx + dy * dy)

        -- NPC speed
        local speed = 0
        pcall(function()
            local vel = npc:GetVelocity()
            if vel then
                speed = math.sqrt(vel.X * vel.X + vel.Y * vel.Y + vel.Z * vel.Z)
            end
        end)

        -- Scheduling state
        local schedEnabled = false
        pcall(function() schedEnabled = se:IsSchedulingEnabled() end)

        -- In transit?
        local inTransit = false
        pcall(function() inTransit = se:IsInTransit() end)

        print(string.format("[NPCFollow] %s: dist=%.0f speed=%.0f locked=%s schedEnabled=%s inTransit=%s",
            voiceName, dist, speed, tostring(follower.locked), tostring(schedEnabled), tostring(inTransit)))

        -- Far from target: unlock, move toward target
        -- Close to target: lock in place (disable scheduling)
        if dist > TELEPORT_DISTANCE then
            -- Too far — teleport behind player
            if follower.locked then
                pcall(function() se:EnableScheduling(true, false, true) end)
                follower.locked = false
            end
            pcall(function() se:PerformTask_RemoveActivePerformTask() end)
            local telePos = findTeleportPosition(staticData, player, playerLoc, playerRot, npc)
            if telePos then
                local teleOk = false
                pcall(function()
                    -- Face toward the player
                    local dx = playerLoc.X - telePos.X
                    local dy = playerLoc.Y - telePos.Y
                    local faceYaw = math.deg(math.atan(dy, dx))
                    local rot = { Pitch = 0, Yaw = faceYaw, Roll = 0 }
                    teleOk = npc:K2_TeleportTo(telePos, rot)
                end)
                print(string.format("[NPCFollow] %s TELEPORTED (dist=%.0f) -> %s",
                    voiceName, dist, tostring(teleOk)))
            else
                print(string.format("[NPCFollow] %s needs teleport (dist=%.0f) but no valid position found",
                    voiceName, dist))
            end
        elseif dist > 400 then
            -- Normal range — walk toward target
            if follower.locked then
                print("[NPCFollow] " .. voiceName .. " UNLOCKING for move")
                pcall(function() se:EnableScheduling(true, false, true) end)
                follower.locked = false
            end
            pcall(function() se:PerformTask_RemoveActivePerformTask() end)
            -- NOTE: speed params have no effect on NPC walk speed
            local moveOk, moveErr = pcall(function()
                se:PerformTask_MoveToLocation(targetPos, 150, 30, false, 0, nil)
            end)
            print(string.format("[NPCFollow] %s MoveToLocation(dist=%.0f) -> %s %s",
                voiceName, dist, tostring(moveOk), tostring(moveErr)))
        elseif not follower.locked then
            -- In range — freeze in place
            print("[NPCFollow] " .. voiceName .. " LOCKING in place (dist=" .. math.floor(dist) .. ")")
            pcall(function() se:PerformTask_RemoveActivePerformTask() end)
            pcall(function() npc.CharacterMovement:StopMovementImmediately() end)
            pcall(function() se:EnableScheduling(false, true, true) end)
            follower.locked = true
        end

        ::continue::
    end

    -- Proper release (SE still valid — re-enable scheduling)
    for _, voiceName in ipairs(toRelease) do
        pcall(function() CompanionFollow.removeFollower(voiceName) end)
    end
    -- Bare cleanup (SE gone — nothing to release)
    for _, voiceName in ipairs(toRemove) do
        _G.NPCFollowers[voiceName] = nil
    end
end

return CompanionFollow
