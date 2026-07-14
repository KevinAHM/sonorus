-- NPCFacial.lua - NPC facial component access for Sonorus
-- Dialogue lip sync stop and eye-default control go through ModActor Blueprint cache by voiceId.

---@class NPCFacial
local NPCFacial = {}

local BlueprintHelpers = require "Utils.BlueprintHelpers"

---Stop ambient dialogue lip sync on an NPC
---@param npc userdata|string
---@return boolean
function NPCFacial.StopNPCDialogueLipSync(npc)
    local voiceId = BlueprintHelpers.ToVoiceId(npc)
    if not voiceId then return false end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then return false end

    local stopNpcDialogueLipSyncById = mod.StopNpcDialogueLipSyncById
    if not stopNpcDialogueLipSyncById then
        error("ModActor missing callable StopNpcDialogueLipSyncById")
    end

    local out = {}
    local ok = pcall(function()
        stopNpcDialogueLipSyncById(mod, voiceId, out)
    end)
    if not ok then return false end
    if out.Success ~= nil then
        return out.Success == true
    end
    return true
end

---Set NPC eye-default flags through the ModActor Blueprint bridge.
---@param npc userdata|string
---@param ambientValue boolean
---@param additiveValue boolean
---@return boolean
function NPCFacial.SetNpcEyeDefaults(npc, ambientValue, additiveValue)
    local voiceId = BlueprintHelpers.ToVoiceId(npc)
    if not voiceId then return false end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then return false end

    local setNpcEyeDefaultsById = mod.SetNpcEyeDefaultsById
    if not setNpcEyeDefaultsById then
        error("ModActor missing callable SetNpcEyeDefaultsById")
    end

    local out = {}
    local ok = pcall(function()
        setNpcEyeDefaultsById(mod, voiceId, ambientValue, additiveValue, out)
    end)
    if not ok then return false end
    return out.Success == true
end

return NPCFacial
