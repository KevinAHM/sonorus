-- NPCFacial.lua - NPC facial component access for Sonorus
-- Handles facial animation state (lip sync detection/control)

---@class NPCFacial
local NPCFacial = {}

-- Cache module for static data access
local Cache = require "Utils.Cache"

---Get FacialComponent from an NPC actor
---@param npc userdata The NPC actor
---@return userdata|nil The FacialComponent, or nil if not found
function NPCFacial.GetNPCFacialComponent(npc)
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

---Stop ambient dialogue lip sync on an NPC
---@param npc userdata The NPC actor
---@return boolean success Whether the cancel succeeded
function NPCFacial.StopNPCDialogueLipSync(npc)
    local facialComp = NPCFacial.GetNPCFacialComponent(npc)
    if not facialComp then return false end

    local result = false
    local ok = pcall(function()
        result = facialComp:EditorCancelPlayingCurrentDialogueLine()
    end)

    return ok and result
end

---Check if NPC is currently playing dialogue lip sync
---@param npc userdata The NPC actor
---@return boolean isPlaying
function NPCFacial.IsNPCPlayingDialogueLipSync(npc)
    local facialComp = NPCFacial.GetNPCFacialComponent(npc)
    if not facialComp then return false end

    local isPlaying = false
    pcall(function()
        isPlaying = facialComp:IsPlayingDialogueLine()
    end)

    return isPlaying
end

return NPCFacial
