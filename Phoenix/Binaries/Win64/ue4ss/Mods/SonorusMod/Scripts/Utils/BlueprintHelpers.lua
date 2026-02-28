-- BlueprintHelpers.lua - Blueprint mod actor interface
-- Handles all Lua <-> Blueprint communication for Sonorus

---@class BlueprintHelpers
local BlueprintHelpers = {}

-- Cache dependency (loaded lazily to avoid circular deps)
local Cache = nil

local function getCache()
    if not Cache then
        Cache = require("Utils.Cache")
    end
    return Cache
end

-- Blendshape method state (nil = untested, true = use direct, false = blueprint works)
local useDirect = nil
local lastVerifyTime = 0
local VERIFY_INTERVAL = 10 -- seconds between verification checks
local forceDirect = false -- User-controlled override from config

---Force direct blendshape mode (bypasses Blueprint detection)
---@param enabled boolean True to force direct, false to use auto-detection
function BlueprintHelpers.SetForceDirect(enabled)
    -- Only act if state is changing (idempotent)
    if enabled and not forceDirect then
        forceDirect = true
        useDirect = true
        print("[Blendshape] Forced direct mode enabled (bypassing Blueprint)")
    elseif not enabled and forceDirect then
        forceDirect = false
        useDirect = nil  -- Reset to auto-detection
        lastVerifyTime = 0
        print("[Blendshape] Direct mode disabled, reverting to auto-detection")
    end
end

---Get the Sonorus Blueprint mod actor (cached, skips /Temp/ world actors)
---@return userdata|nil actor The mod actor or nil if not found
function BlueprintHelpers.GetSonorusModActor()
    return getCache().Get("SonorusModActor", function()
        local modactors = FindAllOf("ModActor_C")
        if not modactors then return nil end

        for _, actor in ipairs(modactors) do
            local ok, valid = pcall(function() return actor:IsValid() end)
            if ok and valid then
                local classOk, className = pcall(function()
                    return actor:GetClass():GetFullName()
                end)
                if classOk and className and className:find("sonorusblueprintmod") then
                    -- Skip actors spawned in /Temp/ worlds - these are stale
                    local nameOk, fullName = pcall(function() return actor:GetFullName() end)
                    if nameOk and fullName and fullName:find("/Temp/") then
                        print("[Blueprint] Skipping ModActor in /Temp/ world: " .. fullName)
                    else
                        print("[Blueprint] Found ModActor: " .. (nameOk and fullName or "unknown"))
                        return actor
                    end
                end
            end
        end
        return nil
    end)
end

---Apply blendshapes directly via mesh:SetMorphTarget
---@param mesh userdata The skeletal mesh component
---@param jaw number Jaw open value
---@param smileL number Left smile value
---@param smileR number Right smile value
---@param lwrFunnelL number Lower funnel left
---@param lwrFunnelR number Lower funnel right
---@param uprFunnelL number Upper funnel left
---@param uprFunnelR number Upper funnel right
---@param lwrDnL number Lower down left
---@param lwrDnR number Lower down right
---@return boolean success True if all morph targets were set
local function applyBlendshapesDirect(mesh, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR)
    local ok, err = pcall(function()
        mesh:SetMorphTarget(FName("jaw_drop"), jaw, false)
        mesh:SetMorphTarget(FName("smile_l"), smileL, false)
        mesh:SetMorphTarget(FName("smile_r"), smileR, false)
        mesh:SetMorphTarget(FName("lwr_lip_funl_l"), lwrFunnelL, false)
        mesh:SetMorphTarget(FName("lwr_lip_funl_r"), lwrFunnelR, false)
        mesh:SetMorphTarget(FName("upr_lip_funl_l"), uprFunnelL, false)
        mesh:SetMorphTarget(FName("upr_lip_funl_r"), uprFunnelR, false)
        mesh:SetMorphTarget(FName("lwr_lip_dn_l"), lwrDnL, false)
        mesh:SetMorphTarget(FName("lwr_lip_dn_r"), lwrDnR, false)
    end)
    if not ok then
        print("[Blendshape] Direct method error: " .. tostring(err))
        return false
    end
    return true
end

---Batched blendshape setter - sets all lip sync morph targets
---Auto-detects if Blueprint method works, falls back to direct mesh calls if not
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
---@param modActor userdata|nil Optional cached mod actor (used for Blueprint path)
---@return boolean success True if call succeeded
function BlueprintHelpers.CallSetBlendshapes(actor, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR, modActor)
    if not actor then
        print("[Blendshape] SetBlendshapes error: Actor is nil")
        return false
    end

    local mesh = actor.Mesh
    if not mesh then
        print("[Blendshape] SetBlendshapes error: Actor has no Mesh property")
        return false
    end

    -- Direct method path (fallback confirmed or Blueprint failed)
    if useDirect == true then
        return applyBlendshapesDirect(mesh, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR)
    end

    -- Blueprint method path
    local mod = modActor or BlueprintHelpers.GetSonorusModActor()
    if not mod then
        print("[Blendshape] ModActor not found, switching to direct method")
        useDirect = true
        return applyBlendshapesDirect(mesh, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR)
    end

    local ok, err = pcall(function()
        mod:setblendshapes(actor, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR)
    end)

    if not ok then
        print("[Blendshape] Blueprint call failed: " .. tostring(err) .. " - switching to direct method")
        useDirect = true
        return applyBlendshapesDirect(mesh, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR)
    end

    -- Verification check (only if method not yet confirmed and interval elapsed)
    if useDirect == nil then
        local currentTime = os.time()
        if currentTime - lastVerifyTime >= VERIFY_INTERVAL then
            lastVerifyTime = currentTime

            -- Verify jaw_drop was actually set
            local actualValue = nil
            local getOk = pcall(function()
                actualValue = mesh:GetMorphTarget(FName("jaw_drop"))
            end)

            if getOk and actualValue ~= nil then
                local diff = math.abs(actualValue - jaw)
                if diff > 0.01 then
                    print(string.format("[Blendshape] Blueprint verification failed (expected %.3f, got %.3f) - switching to direct method", jaw, actualValue))
                    useDirect = true
                    return applyBlendshapesDirect(mesh, jaw, smileL, smileR, lwrFunnelL, lwrFunnelR, uprFunnelL, uprFunnelR, lwrDnL, lwrDnR)
                else
                    useDirect = false
                    print("[Blendshape] Blueprint method verified working")
                end
            end
        end
    end

    return true
end

---Execute an action on an NPC (LookAt, Wave, etc)
---@param actor userdata The NPC actor
---@param actionName string The action name to execute
---@return boolean success True if call succeeded
function BlueprintHelpers.CallActionExecute(actor, actionName)
    local mod = BlueprintHelpers.GetSonorusModActor()
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

---Make an NPC a companion
---@param voiceId string The NPC's voice ID (e.g., "SebastianSallow" or "sebastiansallow")
---@return boolean success True if companion was set
function BlueprintHelpers.MakeCompanion(voiceId)
    if not voiceId or voiceId == "" then
        print("[Blueprint] MakeCompanion error: No voice ID provided")
        return false
    end

    local staticData = getCache().GetStaticData()
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
    local staticData = getCache().GetStaticData()
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
    local Utils = require("Utils.Utils")
    local voiceId = Utils.GetActorVoiceId(currentCompanion, staticData)
    if not voiceId then
        print("[Blueprint] ClearCompanion error: Could not get companion voice ID")
        return false
    end

    -- Use SetSystemicCompanionBP (works with both native and Floo Flame Companions mod)
    local ok, err = pcall(function()
        companionMgr:SetSystemicCompanionBP(voiceId, false)
    end)

    if not ok then
        print("[Blueprint] ClearCompanion error: " .. tostring(err))
        return false
    end

    return true
end

return BlueprintHelpers
