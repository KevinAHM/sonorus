-- AudioMute.lua - NPC audio muting functions for Sonorus
-- Allows muting/unmuting NPC voice audio during conversations

---@class AudioMute
local AudioMute = {}

-- Cache module for static data access
local Cache = require "Utils.Cache"

-- BlueprintHelpers for SafeIsValid
local BlueprintHelpers = require "Utils.BlueprintHelpers"

---Mute an NPC's audio output
---@param actor userdata The NPC actor to mute
---@return userdata|nil comp The AkComponent (for later unmuting) or nil on failure
function AudioMute.MuteNPCAudio(actor)
    if not actor or not BlueprintHelpers.SafeIsValid(actor) then
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

    if BlueprintHelpers.SafeIsValid(comp) then
        print("[Sonorus] Found AkComponent, muting...")
        pcall(function()
            comp:SetOutputBusVolume(0)
        end)
        return comp
    end

    return nil
end

---Unmute an NPC's audio output (by component reference - legacy)
---@param comp userdata The AkComponent returned from MuteNPCAudio
function AudioMute.UnmuteNPCAudio(comp)
    if BlueprintHelpers.SafeIsValid(comp) then
        print("[Sonorus] Restoring audio volume...")
        pcall(function()
            comp:SetOutputBusVolume(1.0)
        end)
    end
end

---Unmute an NPC's audio output by actor (finds current AkComponent)
---@param actor userdata The NPC actor to unmute
---@return boolean success Whether unmute succeeded
function AudioMute.UnmuteNPCAudioByActor(actor)
    if not actor or not BlueprintHelpers.SafeIsValid(actor) then
        return false
    end

    local staticData = Cache.GetStaticData()
    local akClass = staticData and staticData.akComponentClass
    if not akClass then
        return false
    end

    local comp = nil
    pcall(function()
        comp = actor:GetComponentByClass(akClass)
    end)

    if BlueprintHelpers.SafeIsValid(comp) then
        pcall(function()
            comp:SetOutputBusVolume(1.0)
        end)
        return true
    end

    return false
end

---Unmute ALL nearby NPCs (safety net for F8 reset)
---This unmutes every NPC in range, not just tracked conversation participants
function AudioMute.UnmuteAllNearbyNPCs()
    local ok, err = pcall(function()
        local staticData = Cache.GetStaticData()
        local akClass = staticData and staticData.akComponentClass
        if not akClass then
            print("[Sonorus] UnmuteAllNearbyNPCs: No AkComponent class")
            return
        end

        local unmutedCount = 0

        -- Get nearby NPCs using global function
        local GetNearbyNPCs = _G.GetNearbyNPCs
        if GetNearbyNPCs then
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                for _, entry in ipairs(npcResult.nearbyList) do
                    local actor = entry.actor
                    if actor and BlueprintHelpers.SafeIsValid(actor) then
                        local comp = nil
                        pcall(function()
                            comp = actor:GetComponentByClass(akClass)
                        end)
                        if BlueprintHelpers.SafeIsValid(comp) then
                            pcall(function()
                                comp:SetOutputBusVolume(1.0)
                            end)
                            unmutedCount = unmutedCount + 1
                        end
                    end
                end
            end
        end

        if unmutedCount > 0 then
            print("[Sonorus] Unmuted " .. unmutedCount .. " nearby NPCs")
        end
    end)

    if not ok then
        print("[Sonorus] ERROR in UnmuteAllNearbyNPCs: " .. tostring(err))
    end
end

return AudioMute
