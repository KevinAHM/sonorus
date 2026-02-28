-- AudioZone.lua - Audio reverb zone detection for Sonorus
-- Detects player's current audio zone for 3D audio context

---@class AudioZone
local AudioZone = {}

-- Cache module for static data access
local Cache = require "Utils.Cache"

-- Refresh function for static cache - builds reverb bounds data
local function RefreshReverbBounds(data)
    data.bounds = {}
    data.volumeCount = 0

    local volumes = FindAllOf("AkSpatialAudioVolume")
    if not volumes then return end

    for _, vol in ipairs(volumes) do
        if vol:IsValid() then
            pcall(function()
                local volName = "?"
                pcall(function()
                    local fullName = vol:GetFullName()
                    volName = fullName:match("%.([^%.]+)$") or fullName
                end)

                local outOrigin = {}
                local outExtent = {}
                local boundsOk = pcall(function()
                    vol:GetActorBounds(false, outOrigin, outExtent, false)
                end)

                if not boundsOk then return end

                -- Handle both direct (outOrigin.X) and nested (outOrigin.Origin.X) formats
                local ox = outOrigin.X or (outOrigin.Origin and outOrigin.Origin.X)
                local oy = outOrigin.Y or (outOrigin.Origin and outOrigin.Origin.Y)
                local oz = outOrigin.Z or (outOrigin.Origin and outOrigin.Origin.Z)
                local ex = outExtent.X or (outExtent.BoxExtent and outExtent.BoxExtent.X)
                local ey = outExtent.Y or (outExtent.BoxExtent and outExtent.BoxExtent.Y)
                local ez = outExtent.Z or (outExtent.BoxExtent and outExtent.BoxExtent.Z)

                if not ox or not ex then return end

                local auxBus, priority, sendLevel = nil, 0, 1.0
                pcall(function()
                    local lr = vol.LateReverb
                    if lr and lr:IsValid() and lr.bEnable then
                        priority = lr.Priority or 0
                        sendLevel = lr.SendLevel or 1.0
                        local bus = lr.AuxBusManual
                        if bus and bus:IsValid() then
                            pcall(function()
                                auxBus = bus:GetFullName():match("%.([^%.]+)$")
                            end)
                        end
                    end
                end)

                if auxBus then
                    data.bounds[volName] = {
                        ox = ox, oy = oy, oz = oz,
                        ex = ex, ey = ey, ez = ez,
                        auxBus = auxBus, priority = priority, sendLevel = sendLevel,
                    }
                end
            end)
        end
    end
    data.volumeCount = #volumes
end

---Get current reverb preset based on player position
---@return table|nil {auxBus, sendLevel, zone, priority}
function AudioZone.GetCurrentReverb()
    -- Get player location
    local player = FindFirstOf("Biped_Player")
    if not player or not player:IsValid() then return nil end

    local playerLoc = nil
    pcall(function() playerLoc = player:K2_GetActorLocation() end)
    if not playerLoc then return nil end

    local px, py, pz = playerLoc.X, playerLoc.Y, playerLoc.Z

    -- Get cached bounds (refreshes on static cache invalidation)
    local data = Cache.GetStatic(RefreshReverbBounds, 300) -- 5 min TTL as backup

    -- Force refresh if bounds empty or volume count changed
    local volumes = FindAllOf("AkSpatialAudioVolume")
    local currentCount = volumes and #volumes or 0
    local boundsEmpty = not data.bounds or next(data.bounds) == nil
    if boundsEmpty or currentCount ~= (data.volumeCount or 0) then
        Cache.InvalidateStatic()
        data = Cache.GetStatic(RefreshReverbBounds, 300)
    end

    -- Find volumes containing player
    local insideVolumes = {}
    for volName, b in pairs(data.bounds or {}) do
        if px >= b.ox - b.ex and px <= b.ox + b.ex and
           py >= b.oy - b.ey and py <= b.oy + b.ey and
           pz >= b.oz - b.ez and pz <= b.oz + b.ez then
            table.insert(insideVolumes, {
                zone = volName, auxBus = b.auxBus,
                priority = b.priority, sendLevel = b.sendLevel,
            })
        end
    end

    if #insideVolumes > 0 then
        table.sort(insideVolumes, function(a, b) return a.priority > b.priority end)
        return insideVolumes[1]
    end

    return { auxBus = "OutdoorOverland", sendLevel = 1.0, zone = "Fallback", priority = 0 }
end

---Get cached reverb (fast) or compute if not cached
---@return table|nil {auxBus, sendLevel, zone, priority}
function AudioZone.GetCachedReverb()
    if _G.CachedReverb then
        return _G.CachedReverb
    end
    -- Fallback: compute and cache
    local reverb = AudioZone.GetCurrentReverb()
    if reverb then
        _G.CachedReverb = reverb
    end
    return reverb
end

---Invalidate cached reverb (call on zone changes)
function AudioZone.InvalidateCachedReverb()
    _G.CachedReverb = nil
end

return AudioZone
