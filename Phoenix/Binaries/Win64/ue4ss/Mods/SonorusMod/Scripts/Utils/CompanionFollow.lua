-- Utils/CompanionFollow.lua
-- Ensures companion doesn't stall behind the player after idle periods.
-- The game's companion AI sometimes stops following after the player is idle for ~5-10s.
-- This polls every 2s and nudges the companion via CompanionManager:MoveToLocation
-- to a point 200uu from the player when it detects the companion has stalled.

local CompanionFollow = {}

local Cache = require("Utils.Cache")
local Utils = require("Utils.Utils")

-- Static cache getter - set via init()
local getStaticCache = nil

-- Timer handle (persisted in _G for hot reload)
_G.CompanionFollowHandle = _G.CompanionFollowHandle or nil

-- Feature toggle (persisted in _G for hot reload, default OFF)
_G.CompanionFollowEnabled = true

-- Config
local POLL_INTERVAL_MS = 2000
local DEFAULT_FOLLOW_DISTANCE = 200  -- uu (2.0m default)

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
    if not _G.CompanionFollowEnabled then return end
    -- Skip during combat/broom - let companion AI handle its own positioning
    if _G.CombatState and _G.CombatState.active then return end
    if _G.BroomState and _G.BroomState.mounted then return end
    if not getStaticCache then return end
    local staticData = getStaticCache()
    if not staticData then return end

    local cm = staticData.companionManager
    local player = staticData.player
    if not cm or not SafeIsValid(cm) then return end
    if not player or not SafeIsValid(player) then return end

    local following = false
    pcall(function() following = cm:HasPrimaryFollowingCompanion() end)
    if not following then return end

    local companionPawn = nil
    pcall(function() companionPawn = cm:GetPrimaryCompanionPawn() end)
    if not companionPawn or not SafeIsValid(companionPawn) then return end

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
    if dist <= stallDist then return end

    -- Check velocity
    local speed = 0
    pcall(function()
        local vel = companionPawn:GetVelocity()
        if vel then
            speed = math.sqrt(vel.X*vel.X + vel.Y*vel.Y + vel.Z*vel.Z)
        end
    end)

    if speed > 1 then return end -- companion is already moving

    -- Skip if force waiting or locked
    if isCompanionForceWaiting(cm, companionPawn) then return end
    if isCompanionLocked(companionPawn) then return end

    -- Nudge: move to followDist uu from player, in direction of companion
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
    _G.CompanionFollowHandle = LoopInGameThreadWithDelay(POLL_INTERVAL_MS, function()
        CompanionFollow.tick()
    end)
    print("[CompanionFollow] Started")
end

function CompanionFollow.stop()
    if _G.CompanionFollowHandle then
        pcall(function() CancelDelayedAction(_G.CompanionFollowHandle) end)
        _G.CompanionFollowHandle = nil
        print("[CompanionFollow] Stopped")
    end
end

return CompanionFollow
