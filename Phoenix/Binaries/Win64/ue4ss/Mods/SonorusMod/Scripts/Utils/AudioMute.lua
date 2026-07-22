-- AudioMute.lua - NPC audio muting via ModActor Blueprint cache
-- All volume control goes through ModActor:SetNpcAkVolume(voiceId, volume, out).
-- Accepts actor references or voice ID strings.

---@class AudioMute
local AudioMute = {}

local BlueprintHelpers = require "Utils.BlueprintHelpers"
local Utils = require "Utils.Utils"

local LOAD_COOLDOWN_S = 5

---@return boolean
function AudioMute.InLoadCooldown()
    if true then return false end
    local state = _G.SonorusState
    if not state or not state.playerLoadedAt then return true end
    return (os.clock() - state.playerLoadedAt) < LOAD_COOLDOWN_S
end

---@param voiceId string
---@param volume number
---@return boolean
local function setNpcVolumeById(voiceId, volume)
    if not voiceId or voiceId == "" then return false end

    local mod = BlueprintHelpers.GetSonorusModActor()
    if not mod then return false end
    local setNpcAkVolume = mod.SetNpcAkVolume
    if not setNpcAkVolume then
        error("ModActor missing callable SetNpcAkVolume")
    end

    local out = {}
    local ok = pcall(function()
        setNpcAkVolume(mod, voiceId, volume, out)
    end)

    return ok and out.Success == true
end

---Mute an NPC's audio output.
---@param actorOrId userdata|string
---@return string|nil voiceId Captured ID for delayed callbacks
function AudioMute.MuteNPCAudio(actorOrId)
    if not actorOrId then return nil end
    if AudioMute.InLoadCooldown() then return nil end
    local voiceId = BlueprintHelpers.ToVoiceId(actorOrId)
    if voiceId and setNpcVolumeById(voiceId, 0.0) then
        return voiceId
    end
    return nil
end

---Unmute an NPC's audio output.
---@param actorOrId userdata|string
---@return boolean
function AudioMute.UnmuteNPCAudioByActor(actorOrId)
    if not actorOrId then return false end
    if AudioMute.InLoadCooldown() then return false end
    local voiceId = BlueprintHelpers.ToVoiceId(actorOrId)
    return voiceId and setNpcVolumeById(voiceId, 1.0) or false
end

---Unmute ALL nearby NPCs (safety net for F8 reset).
---Skips NPCs in the ambient blocklist.
function AudioMute.UnmuteAllNearbyNPCs()
    local ok, err = pcall(function()
        local blocklist = _G._AmbientBlocklist
        local unmutedCount, skippedCount = 0, 0

        local GetNearbyNPCs = _G.GetNearbyNPCs
        if not GetNearbyNPCs then return end

        local npcResult = GetNearbyNPCs(2000, 0.9)
        if not npcResult or not npcResult.nearbyList then return end

        for _, entry in ipairs(npcResult.nearbyList) do
            local actor = entry.actor
            if actor and BlueprintHelpers.SafeIsValid(actor) then
                if blocklist and entry.name and blocklist[entry.name] then
                    skippedCount = skippedCount + 1
                else
                    local voiceId = BlueprintHelpers.ToVoiceId(actor)
                    if voiceId and setNpcVolumeById(voiceId, 1.0) then
                        unmutedCount = unmutedCount + 1
                    end
                end
            end
        end

        if unmutedCount > 0 then
            print("[Sonorus] Unmuted " .. unmutedCount .. " nearby NPCs" ..
                (skippedCount > 0 and (" (skipped " .. skippedCount .. " blocklist)") or ""))
        end
    end)
    if not ok then
        print("[Sonorus] ERROR in UnmuteAllNearbyNPCs: " .. tostring(err))
    end
end

---Unmute all blocklist NPCs (call on cutscene start).
function AudioMute.UnmuteBlocklistNPCs()
    local blocklist = _G._AmbientBlocklist
    if not blocklist or not next(blocklist) then return end
    local GetCachedNPCs = _G.GetCachedNPCs
    local GetStaticCache = _G.GetStaticCache
    if not GetCachedNPCs or not GetStaticCache then return end

    local npcs = GetCachedNPCs()
    if not npcs then return end
    print("[Sonorus] Unmuting blocklist NPCs")
    local sd = GetStaticCache()
    local count = 0
    for _, npc in pairs(npcs) do
        if BlueprintHelpers.SafeIsValid(npc) then
            local voiceId = Utils.GetActorVoiceId(npc, sd)
            if voiceId and blocklist[voiceId] then
                AudioMute.UnmuteNPCAudioByActor(npc)
                count = count + 1
            end
        end
    end
    if count > 0 then
        print(string.format("[Sonorus] Unmuted %d blocklist NPCs (cutscene)", count))
    end
end

---Re-mute all blocklist NPCs (call on cutscene end).
function AudioMute.MuteBlocklistNPCs()
    if AudioMute.InLoadCooldown() then return end
    local blocklist = _G._AmbientBlocklist
    if not blocklist or not next(blocklist) then return end
    local GetCachedNPCs = _G.GetCachedNPCs
    local GetStaticCache = _G.GetStaticCache
    if not GetCachedNPCs or not GetStaticCache then return end

    local npcs = GetCachedNPCs()
    if not npcs then return end
    print("[Sonorus] Muting blocklist NPCs")
    local sd = GetStaticCache()
    local count = 0
    for _, npc in pairs(npcs) do
        if BlueprintHelpers.SafeIsValid(npc) then
            local voiceId = Utils.GetActorVoiceId(npc, sd)
            if voiceId and blocklist[voiceId] then
                AudioMute.MuteNPCAudio(npc)
                count = count + 1
            end
        end
    end
    if count > 0 then
        print(string.format("[Sonorus] Re-muted %d blocklist NPCs (cutscene end)", count))
    end
end

---Unmute the companion if they're in the blocklist (call on combat start).
function AudioMute.UnmuteBlocklistCompanion()
    local blocklist = _G._AmbientBlocklist
    if not blocklist or not next(blocklist) then return end

    local sd = Cache.GetStaticData()
    local companionMgr = sd and sd.companionManager
    if not companionMgr then return end
    print("[Sonorus] Unmuting blocklist companion")

    local companion = nil
    pcall(function() companion = companionMgr:GetPrimaryCompanionPawn() end)
    if not companion or not BlueprintHelpers.SafeIsValid(companion) then return end

    local voiceId = Utils.GetActorVoiceId(companion, sd)
    if voiceId and blocklist[voiceId] then
        AudioMute.UnmuteNPCAudioByActor(companion)
        print(string.format("[Sonorus] Unmuted blocklist companion: %s (combat)", voiceId))
    end
end

---Re-mute the companion if they're in the blocklist (call on combat end).
function AudioMute.MuteBlocklistCompanion()
    local blocklist = _G._AmbientBlocklist
    if not blocklist or not next(blocklist) then return end

    local sd = Cache.GetStaticData()
    local companionMgr = sd and sd.companionManager
    if not companionMgr then return end
    print("[Sonorus] Muting blocklist companion")

    local companion = nil
    pcall(function() companion = companionMgr:GetPrimaryCompanionPawn() end)
    if not companion or not BlueprintHelpers.SafeIsValid(companion) then return end

    local voiceId = Utils.GetActorVoiceId(companion, sd)
    if voiceId and blocklist[voiceId] then
        AudioMute.MuteNPCAudio(companion)
        print(string.format("[Sonorus] Re-muted blocklist companion: %s (combat end)", voiceId))
    end
end

return AudioMute
