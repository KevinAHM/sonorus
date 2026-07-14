-- BlueprintHelpers.lua - Blueprint mod actor interface
-- Handles all Lua <-> Blueprint communication for Sonorus

local UEHelpers = require("UEHelpers")
local Cache = require("Utils.Cache")
local Utils = require("Utils.Utils")

---@class BlueprintHelpers
local BlueprintHelpers = {}

local AssetRegistryHelpers = nil
local AssetRegistry = nil
local loaderHooksRegistered = false

local SONORUS_MOD_ACTOR = {
    assetPath = "/SonorusMod/ModActor",
    assetName = "ModActor_C",
    notifyPath = "/SonorusMod/ModActor.ModActor_C",
    classPath = "BlueprintGeneratedClass /SonorusMod/ModActor.ModActor_C",
}

local function Log(message)
    print("[Blueprint] " .. tostring(message))
end

local function GetState()
    return _G.SonorusState
end

local function SafeGetFullName(actor)
    if not actor then return nil end
    local fullName = nil
    pcall(function()
        fullName = actor:GetFullName()
    end)
    return fullName
end

local function IsUsableActor(actor)
    return actor ~= nil
end

local function GetActorVoiceId(actor, staticData)
    if not actor then return nil end
    staticData = staticData or Cache.GetStaticData()
    local lib = staticData and staticData.bpLibrary
    if not lib then return nil end

    local name = nil
    pcall(function()
        local nameResult = lib:GetActorName(actor)
        if nameResult then
            pcall(function() name = nameResult:ToString() end)
        end
    end)
    return (name and name ~= "") and name or nil
end

local function RememberSonorusModActor(actor, source)
    if not IsUsableActor(actor) then return nil end

    local state = GetState()
    if not state then return actor end

    local fullName = SafeGetFullName(actor)
    if fullName and fullName:lower():find("/temp/", 1, true) then
        Log(string.format("Ignoring temporary Sonorus ModActor from %s: %s", tostring(source), fullName))
        return nil
    end

    state.sonorusModActor = actor
    state.sonorusModActorName = fullName
    if fullName then
        Log(string.format("Sonorus ModActor cached from %s: %s", tostring(source), fullName))
    else
        Log(string.format("Sonorus ModActor cached from %s", tostring(source)))
    end
    return actor
end

local function CacheAssetRegistry()
    if AssetRegistryHelpers and AssetRegistry then return true end

    AssetRegistryHelpers = StaticFindObject("/Script/AssetRegistry.Default__AssetRegistryHelpers")
    if AssetRegistryHelpers and BlueprintHelpers.SafeIsValid(AssetRegistryHelpers) then
        local ok = pcall(function()
            AssetRegistry = AssetRegistryHelpers:GetAssetRegistry()
        end)
        if ok and AssetRegistry and BlueprintHelpers.SafeIsValid(AssetRegistry) then
            return true
        end
    end

    AssetRegistry = StaticFindObject("/Script/AssetRegistry.Default__AssetRegistryImpl")
    if AssetRegistry and BlueprintHelpers.SafeIsValid(AssetRegistry) then
        Log("AssetRegistryHelpers unavailable; falling back to direct class lookup only")
    end
    return false
end

local function ResolveModClass()
    local modClass = StaticFindObject(SONORUS_MOD_ACTOR.classPath)
    if modClass then
        return modClass
    end

    if not CacheAssetRegistry() then
        Log("AssetRegistry is not available for Sonorus ModActor lookup")
        return nil
    end

    local assetData
    if UnrealVersion.IsBelow(5, 1) then
        assetData = {
            ObjectPath = FName(string.format("%s.%s", SONORUS_MOD_ACTOR.assetPath, SONORUS_MOD_ACTOR.assetName)),
        }
    else
        assetData = {
            PackageName = FName(SONORUS_MOD_ACTOR.assetPath),
            AssetName = FName(SONORUS_MOD_ACTOR.assetName),
        }
    end

    local ok, result = pcall(function()
        return AssetRegistryHelpers:GetAsset(assetData)
    end)
    if not ok then
        Log("GetAsset failed for Sonorus ModActor: " .. tostring(result))
        return nil
    end
    if not result then
        Log("Resolved Sonorus ModActor class is not valid")
        return nil
    end
    return result
end

local function ResolveWorld(optionalWorld)
    if BlueprintHelpers.SafeIsValid(optionalWorld) then
        return optionalWorld
    end

    local world = nil
    pcall(function()
        world = UEHelpers.GetWorld()
    end)
    if BlueprintHelpers.SafeIsValid(world) then
        return world
    end

    local pc = nil
    pcall(function()
        pc = FindFirstOf("PlayerController")
    end)
    if BlueprintHelpers.SafeIsValid(pc) then
        pcall(function()
            world = pc:GetWorld()
        end)
    end
    if BlueprintHelpers.SafeIsValid(world) then
        return world
    end

    return nil
end

local function CallActorHook(actor, hookName)
    if not IsUsableActor(actor) then return false end

    local hook = nil
    local ok = pcall(function()
        hook = actor[hookName]
    end)
    if not ok or not hook or not BlueprintHelpers.SafeIsValid(hook) then
        return false
    end

    local success, err = pcall(function()
        hook()
    end)
    if not success then
        Log(string.format("%s failed: %s", hookName, tostring(err)))
    end
    return success
end

local function GetCachedSonorusModActor()
    local state = GetState()
    if not state then return nil end

    local actor = state.sonorusModActor
    if not actor then return nil end
    if IsUsableActor(actor) and BlueprintHelpers.SafeIsValid(actor) then
        return actor
    end

    state.sonorusModActor = nil
    state.sonorusModActorName = nil
    Log("Cleared stale cached Sonorus ModActor")
    return nil
end

local function IsSonorusModActor(actor)
    if not IsUsableActor(actor) or not BlueprintHelpers.SafeIsValid(actor) then
        return false
    end

    local fullName = SafeGetFullName(actor)
    if fullName and fullName:lower():find("/temp/", 1, true) then
        return false
    end

    local classFullName = nil
    pcall(function()
        local class = actor:GetClass()
        if class then
            classFullName = class:GetFullName()
        end
    end)
    return classFullName == SONORUS_MOD_ACTOR.classPath
        or (fullName and fullName:find(SONORUS_MOD_ACTOR.assetName, 1, true) ~= nil)
end

local function FindLiveSonorusModActor()
    local actor = nil
    pcall(function()
        actor = FindFirstOf(SONORUS_MOD_ACTOR.assetName)
    end)
    if IsSonorusModActor(actor) then
        return RememberSonorusModActor(actor, "live lookup")
    end

    local allActors = nil
    pcall(function()
        allActors = FindAllOf(SONORUS_MOD_ACTOR.assetName)
    end)
    if type(allActors) == "table" then
        for _, candidate in ipairs(allActors) do
            if IsSonorusModActor(candidate) then
                return RememberSonorusModActor(candidate, "live lookup")
            end
        end
    end

    return nil
end

---Batched blendshape setter - sets all lip sync morph targets via mesh:SetMorphTarget
---@param actor userdata The NPC actor
---@param jaw number Jaw open value
---@param smileL number Left smile value
---@param smileR number Right smile value
---@param lwrFunnelL number Lower funnel left
---@param lwrFunnelR number Lower funnel right
---@param uprFunnelL number Upper funnel left
---@param uprFunnelR number Upper funnel right
---@param lwrDnL number Lower down left
---@param lwrDnR number Lower down right
---@param press number|nil Mouth press (default 0)
---@param lipUp number|nil Upper lip up L+R (default 0)
---@param ee number|nil EE shape (default 0)
---@param o number|nil O shape (default 0)
---@param shh number|nil Shh shape (default 0)
---@return boolean success True if call succeeded
function BlueprintHelpers.CallSetBlendshapes(actor, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR, press, lipUp, ee, o, shh)
    return BlueprintHelpers.CallSetMorphTargets(actor, {
        jaw_drop       = jaw,
        smile_l        = smileL,
        smile_r        = smileR,
        lwr_lip_funl_l = lwrFunnelL,
        lwr_lip_funl_r = lwrFunnelR,
        upr_lip_funl_l = uprFunnelL,
        upr_lip_funl_r = uprFunnelR,
        lwr_lip_dn_l   = lwrDnL,
        lwr_lip_dn_r   = lwrDnR,
        mouth_press    = press or 0,
        upr_lip_up_l   = lipUp or 0,
        upr_lip_up_r   = lipUp or 0,
        ee             = ee or 0,
        o              = o or 0,
        shh            = shh or 0,
    })
end

---Safe IsValid check - Blueprint output param actors may not support IsValid() properly
---@param actor userdata|nil The actor to check
---@return boolean valid True if actor is valid
function BlueprintHelpers.SafeIsValid(actor)
    if not actor then return false end
    local valid = false
    pcall(function()
        valid = actor:IsValid()
    end)
    return valid
end

---Clears the cached Sonorus Blueprint actor reference
---@param reason string|nil Optional reason for logging
function BlueprintHelpers.InvalidateSonorusModActor(reason)
    local state = GetState()
    if not state then return end

    state.sonorusModActor = nil
    state.sonorusModActorName = nil
    if reason then
        Log("Sonorus ModActor invalidated: " .. tostring(reason))
    end
end

---Gets the active Sonorus Blueprint actor, if one has already been loaded
---@param optionalWorld userdata|nil Optional world to spawn into if no actor exists
---@return userdata|nil actor
function BlueprintHelpers.GetSonorusModActor(optionalWorld)
    local actor = GetCachedSonorusModActor()
    if actor then
        return actor
    end

    local state = GetState()
    if state and state.playerLoaded == false and not optionalWorld then
        return nil
    end

    return FindLiveSonorusModActor()
end

function BlueprintHelpers.ToVoiceId(actorOrId)
    if type(actorOrId) == "string" then
        return (actorOrId ~= "") and actorOrId or nil
    end
    if not actorOrId or not BlueprintHelpers.SafeIsValid(actorOrId) then
        return nil
    end

    local staticData = Cache.GetStaticData()
    local actorFullName = SafeGetFullName(actorOrId)

    local playerFullName = staticData and staticData.playerFullName
    if actorFullName and playerFullName and actorFullName == playerFullName then
        return "player"
    end

    local player = staticData and staticData.player
    if actorFullName and player and BlueprintHelpers.SafeIsValid(player) then
        local resolvedPlayerFullName = SafeGetFullName(player)
        if resolvedPlayerFullName and actorFullName == resolvedPlayerFullName then
            return "player"
        end
    end

    local className = nil
    pcall(function()
        local class = actorOrId:GetClass()
        if class then
            className = class:GetFullName()
        end
    end)
    if className and className:find("Biped_Player", 1, true) then
        return "player"
    end

    local voiceId = GetActorVoiceId(actorOrId, staticData)
    if voiceId and voiceId ~= "" then
        return voiceId
    end
    return nil
end

function BlueprintHelpers.CallSetMorphTargets(actorOrId, targets)
    if type(targets) ~= "table" then
        return false
    end

    local voiceId = BlueprintHelpers.ToVoiceId(actorOrId)
    if not voiceId then
        return false
    end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return false
    end

    local method = mod.ApplyNpcBlendShapesById
    if not method then
        error("ModActor missing callable morph-target setter")
    end

    local out = {}
    local ok = pcall(function()
        method(mod, voiceId, targets, out)
    end)
    if not ok then
        Log(string.format("ApplyNpcBlendShapesById failed for %s", tostring(voiceId)))
        return false
    end
    if out.Success ~= nil then
        return out.Success == true
    end
    return true
end

function BlueprintHelpers.CallClearMorphTargets(actorOrId, names)
    if type(names) ~= "table" then
        return false
    end

    local targets = {}
    for _, name in ipairs(names) do
        if type(name) == "string" and name ~= "" then
            targets[name] = 0
        end
    end
    return BlueprintHelpers.CallSetMorphTargets(actorOrId, targets)
end

local function ReadOutValue(primaryOut, key, fallbackOut)
    if type(primaryOut) == "table" then
        if primaryOut[key] ~= nil then
            return primaryOut[key]
        end
        if primaryOut.ReturnValue ~= nil then
            return primaryOut.ReturnValue
        end
    end
    if type(fallbackOut) == "table" then
        if fallbackOut[key] ~= nil then
            return fallbackOut[key]
        end
        if fallbackOut.ReturnValue ~= nil then
            return fallbackOut.ReturnValue
        end
    end
    return nil
end

local function ReadVectorOut(primaryOut, fallbackOut)
    local function fromTable(tbl)
        if type(tbl) ~= "table" then
            return nil
        end
        if type(tbl.Location) == "table" then
            return tbl.Location
        end
        if type(tbl.ReturnValue) == "table" then
            return tbl.ReturnValue
        end
        if tbl.X ~= nil or tbl.Y ~= nil or tbl.Z ~= nil then
            return tbl
        end
        return nil
    end

    local vec = fromTable(primaryOut) or fromTable(fallbackOut)
    if not vec then
        return { X = 0, Y = 0, Z = 0 }
    end
    return {
        X = tonumber(vec.X) or 0,
        Y = tonumber(vec.Y) or 0,
        Z = tonumber(vec.Z) or 0,
    }
end

local function ReadStringOut(primaryOut, key, fallbackOut)
    local value = ReadOutValue(primaryOut, key, fallbackOut)
    if type(value) == "userdata" then
        return Utils.SafeFStringToString(value)
    end
    return value
end

function BlueprintHelpers.GetPlayerState()
    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return nil
    end

    local method = mod.GetPlayerState
    if not method then
        return nil
    end

    local successOut = {}
    local locationOut = {}
    local inCombatOut = {}
    local inStealthOut = {}
    local isSwimmingOut = {}
    local inCinematicOut = {}
    local ok = pcall(function()
        method(mod, successOut, locationOut, inCombatOut, inStealthOut, isSwimmingOut, inCinematicOut)
    end)
    local successValue = successOut.Success
    if successValue == nil then
        successValue = successOut.ReturnValue
    end
    if not ok or successValue ~= true then
        return nil
    end

    local location = ReadVectorOut(locationOut, successOut)
    return {
        x = location.X,
        y = location.Y,
        z = location.Z,
        inCombat = ReadOutValue(inCombatOut, "InCombat", locationOut),
        inStealth = ReadOutValue(inStealthOut, "InStealth", locationOut),
        isSwimming = ReadOutValue(isSwimmingOut, "IsSwimming", locationOut),
        inCinematic = ReadOutValue(inCinematicOut, "InCinematic", locationOut),
    }
end

function BlueprintHelpers.GetCompanionInfo()
    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return nil
    end

    local method = mod.GetCompanionInfo
    if not method then
        return nil
    end

    local successOut = {}
    local hasCompanionOut = {}
    local companionForcedWaitingOut = {}
    local companionIsSwimmingOut = {}
    local companionIdOut = {}
    local ok = pcall(function()
        method(mod, successOut, hasCompanionOut, companionForcedWaitingOut, companionIsSwimmingOut, companionIdOut)
    end)
    local successValue = successOut.Success
    if successValue == nil then
        successValue = successOut.ReturnValue
    end
    if not ok or successValue ~= true then
        return nil
    end

    return {
        hasCompanion = ReadOutValue(hasCompanionOut, "HasCompanion", successOut),
        companionForcedWaiting = ReadOutValue(companionForcedWaitingOut, "CompanionForcedWaiting", successOut),
        companionIsSwimming = ReadOutValue(companionIsSwimmingOut, "CompanionIsSwimming", successOut),
        companionId = ReadStringOut(companionIdOut, "CompanionId", successOut),
    }
end

function BlueprintHelpers.IsInCinematic()
    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return nil
    end

    local method = mod.IsInCinematic
    if not method then
        return nil
    end

    local out = {}
    local ok, result = pcall(function()
        return method(mod, out)
    end)
    if not ok then
        Log("IsInCinematic failed: " .. tostring(result))
        return nil
    end

    if type(result) == "boolean" then
        return result
    end

    local inCinematic = ReadOutValue(out, "InCinematic", nil)
    if inCinematic == nil then
        inCinematic = ReadOutValue(out, "inCinematic", nil)
    end
    if inCinematic == nil then
        inCinematic = ReadOutValue(out, "ReturnValue", nil)
    end

    if inCinematic ~= nil then
        return inCinematic == true
    end

    return nil
end

local function ReadNamedOut(primaryOut, key, aggregateOut)
    if type(primaryOut) == "table" and primaryOut[key] ~= nil then
        return primaryOut[key]
    end
    if type(aggregateOut) == "table" and aggregateOut[key] ~= nil then
        return aggregateOut[key]
    end
    return nil
end

function BlueprintHelpers.StartNpcTurnLockById(npcId, targetId)
    if not npcId or npcId == "" or not targetId or targetId == "" then
        return nil
    end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return nil
    end

    local method = mod.StartNpcTurnLockById
    if not method then
        return nil
    end

    local successOut = {}
    local needsDelayedFinishOut = {}
    local turnAngleOut = {}
    local ok = pcall(function()
        method(mod, npcId, targetId, successOut, needsDelayedFinishOut, turnAngleOut)
    end)
    if not ok then
        Log(string.format("StartNpcTurnLockById failed for %s -> %s", tostring(npcId), tostring(targetId)))
        return nil
    end

    local successValue = successOut.Success
    if successValue == nil then
        successValue = successOut.ReturnValue
    end
    if successValue ~= true then
        return {
            success = false,
            needsDelayedFinish = false,
            turnAngle = 0,
        }
    end

    return {
        success = true,
        needsDelayedFinish = ReadNamedOut(needsDelayedFinishOut, "NeedsDelayedFinish", successOut) == true,
        turnAngle = tonumber(ReadNamedOut(turnAngleOut, "TurnAngle", successOut)) or 0,
    }
end

function BlueprintHelpers.StartCompanionTurnLockById(targetId)
    if not targetId or targetId == "" then
        return nil
    end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return nil
    end

    local method = mod.StartCompanionTurnLockById
    if not method then
        return nil
    end

    local successOut = {}
    local needsDelayedFinishOut = {}
    local turnAngleOut = {}
    local ok = pcall(function()
        method(mod, targetId, successOut, needsDelayedFinishOut, turnAngleOut)
    end)
    if not ok then
        Log(string.format("StartCompanionTurnLockById failed for target %s", tostring(targetId)))
        return nil
    end

    local successValue = successOut.Success
    if successValue == nil then
        successValue = successOut.ReturnValue
    end
    if successValue ~= true then
        return {
            success = false,
            needsDelayedFinish = false,
            turnAngle = 0,
        }
    end

    return {
        success = true,
        needsDelayedFinish = ReadNamedOut(needsDelayedFinishOut, "NeedsDelayedFinish", successOut) == true,
        turnAngle = tonumber(ReadNamedOut(turnAngleOut, "TurnAngle", successOut)) or 0,
    }
end

local function CallNpcTurnLockBool(methodName, npcId)
    if not npcId or npcId == "" then
        return false
    end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return false
    end

    local method = mod[methodName]
    if not method then
        return false
    end

    local successOut = {}
    local ok = pcall(function()
        method(mod, npcId, successOut)
    end)
    if not ok then
        Log(string.format("%s failed for %s", tostring(methodName), tostring(npcId)))
        return false
    end

    local successValue = successOut.Success
    if successValue == nil then
        successValue = successOut.ReturnValue
    end
    return successValue == true
end

local function CallCompanionTurnLockBool(methodName)
    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then
        return false
    end

    local method = mod[methodName]
    if not method then
        return false
    end

    local successOut = {}
    local ok = pcall(function()
        method(mod, successOut)
    end)
    if not ok then
        Log(string.format("%s failed", tostring(methodName)))
        return false
    end

    local successValue = successOut.Success
    if successValue == nil then
        successValue = successOut.ReturnValue
    end
    return successValue == true
end

function BlueprintHelpers.FinishNpcTurnLockById(npcId)
    return CallNpcTurnLockBool("FinishNpcTurnLockById", npcId)
end

function BlueprintHelpers.ReleaseNpcTurnLockById(npcId)
    return CallNpcTurnLockBool("ReleaseNpcTurnLockById", npcId)
end

function BlueprintHelpers.FinishCompanionTurnLock()
    return CallCompanionTurnLockBool("FinishCompanionTurnLock")
end

---Returns the active Sonorus Blueprint bridge actor if it is already active.
---BPModLoaderMod is responsible for loading/spawning the actor.
---@param optionalWorld userdata|nil Optional world to spawn into
---@return userdata|nil actor
function BlueprintHelpers.EnsureSonorusModActor(optionalWorld)
    return BlueprintHelpers.GetSonorusModActor(optionalWorld)
end

---Registers passive hooks that cache the Sonorus Blueprint actor when another loader spawns it.
function BlueprintHelpers.SetupSonorusModActorLoader()
    if loaderHooksRegistered then return end
    loaderHooksRegistered = true

    NotifyOnNewObject(SONORUS_MOD_ACTOR.notifyPath, function(context)
        RememberSonorusModActor(context, "notify")
    end)

    RegisterBeginPlayPostHook(function(contextParam)
        local context = nil
        local ok = pcall(function()
            context = contextParam:get()
        end)
        if not ok or not BlueprintHelpers.SafeIsValid(context) then return end

        if not IsSonorusModActor(context) then return end

        RememberSonorusModActor(context, "beginplay")
        CallActorHook(context, "PostBeginPlay")
    end)

    RegisterLoadMapPostHook(function(engine, world)
        Log("Map load observed; waiting for external ModActor spawn/cache")
    end)
end

---Make an NPC a companion
---@param voiceId string The NPC's voice ID (e.g., "SebastianSallow" or "sebastiansallow")
---@return boolean success True if companion was set
function BlueprintHelpers.MakeCompanion(voiceId)
    if not voiceId or voiceId == "" then
        print("[Blueprint] MakeCompanion error: No voice ID provided")
        return false
    end

    local staticData = Cache.GetStaticData()
    local companionMgr = staticData and staticData.companionManager
    if not companionMgr then
        print("[Blueprint] MakeCompanion error: CompanionManager not found")
        return false
    end

    -- Get display name for logging
    local displayName = voiceId
    if _G.GetDisplayName then
        displayName = _G.GetDisplayName(voiceId) or voiceId
    end

    print(string.format("[Blueprint] Making companion: %s (%s)", displayName, voiceId))

    -- Use SetSystemicCompanionBP (works with both native and Floo Flame Companions mod)
    local ok, err = pcall(function()
        companionMgr:SetSystemicCompanionBP(voiceId, true)
    end)

    if not ok then
        print("[Blueprint] MakeCompanion error: " .. tostring(err))
        return false
    end

    return true
end

---Clear the current companion
---@return boolean success True if companion was cleared
function BlueprintHelpers.ClearCompanion()
    local staticData = Cache.GetStaticData()
    local companionMgr = staticData and staticData.companionManager
    if not companionMgr then
        print("[Blueprint] ClearCompanion error: CompanionManager not found")
        return false
    end

    -- Get current companion
    local currentCompanion = nil
    pcall(function() currentCompanion = companionMgr:GetPrimaryCompanionPawn() end)

    if not currentCompanion or not BlueprintHelpers.SafeIsValid(currentCompanion) then
        print("[Blueprint] ClearCompanion: No active companion")
        return true  -- Not an error - just nothing to clear
    end

    -- Get companion's voice ID (raw from game, may be lowercase)
    local voiceId = GetActorVoiceId(currentCompanion, staticData)
    if not voiceId then
        print("[Blueprint] ClearCompanion error: Could not get companion voice ID")
        return false
    end

    -- Clear via both systems - companions may have been set via either method
    local ok1, err1 = pcall(function()
        companionMgr:SetSystemicCompanionBP(voiceId, false)
    end)
    if not ok1 then
        print("[Blueprint] ClearCompanion SetSystemicCompanionBP error: " .. tostring(err1))
    end

    local ok2, err2 = pcall(function()
        companionMgr:SetCompanionBP(voiceId, false)
    end)
    if not ok2 then
        print("[Blueprint] ClearCompanion SetCompanionBP error: " .. tostring(err2))
    end

    if not ok1 and not ok2 then
        return false
    end

    return true
end

return BlueprintHelpers
