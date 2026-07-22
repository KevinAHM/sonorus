-- Utils/StationUse.lua
-- Helper for sending NPCs to stations and releasing them back to their schedule.
-- Uses voice IDs (strings) instead of actor refs to avoid stale UObject issues.
-- Re-fetches ScheduledEntity from PopulationManager on every call.
print("[StationUse] Loading...")

local StationUse = {}
local TAG = "[StationUse]"

local Cache = require("Utils.Cache")
local Utils = require("Utils.Utils")

-- Ambient station types mapped to category: "sit" or "stand"
-- Excludes beds, quest/job/mission stations, functional stations, NPC-role-specific ones
local AMBIENT_STATION_TYPES = {
    -- sit
    [1]  = "sit",   -- bench
    [2]  = "sit",   -- bench
    [7]  = "sit",   -- chair
    [8]  = "sit",   -- chair
    [11] = "sit",   -- couch
    [12] = "sit",   -- couch
    [13] = "sit",   -- couch
    [14] = "sit",   -- couch
    [15] = "sit",   -- desk
    [16] = "sit",   -- desk
    [18] = "sit",   -- drinking tea
    [20] = "sit",   -- fireside bench
    [23] = "sit",   -- great hall table
    [24] = "sit",   -- great hall table
    [25] = "sit",   -- great hall table
    [26] = "sit",   -- sitting on ground
    [31] = "sit",   -- lounge chair
    [44] = "sit",   -- sitting on stairs
    [45] = "sit",   -- sitting on stairs
    [53] = "sit",   -- tall stool
    [54] = "sit",   -- study desk
    [55] = "sit",   -- table
    [56] = "sit",   -- table
    [57] = "sit",   -- table
    [58] = "sit",   -- table
    [59] = "sit",   -- table
    [60] = "sit",   -- taking notes
    [66] = "sit",   -- wall sit
    -- stand
    [4]  = "stand", -- bookshelf
    [5]  = "stand", -- browsing shelf
    [19] = "stand", -- fireside
    [22] = "stand", -- globe
    [39] = "stand", -- railing lean
    [40] = "stand", -- railing lean
    [46] = "stand", -- standing
    [47] = "stand", -- standing
    [48] = "stand", -- standing
    [49] = "stand", -- standing
    [50] = "stand", -- standing
    [51] = "stand", -- standing
    [63] = "stand", -- telescope
    [65] = "stand", -- wall lean
    [68] = "stand", -- window shopping
}

-- PropType labels for display
local PROP_TYPE_LABELS = {
    [0]  = "bed",           [1]  = "bench",          [2]  = "bench",
    [3]  = "bespoke",       [4]  = "bookshelf",      [5]  = "browsing shelf",
    [6]  = "candy display", [7]  = "chair",          [8]  = "chair",
    [9]  = "chest",         [10] = "cleaning shelves",[11] = "couch",
    [12] = "couch",         [13] = "couch",          [14] = "couch",
    [15] = "desk",          [16] = "desk",           [17] = "dresser",
    [18] = "drinking tea",  [19] = "fireside",       [20] = "fireside bench",
    [21] = "fluid",         [22] = "globe",          [23] = "great hall table",
    [24] = "great hall table",[25]= "great hall table",[26]= "sitting on ground",
    [27] = "guard post",    [28] = "herbology station",[29]= "investigating",
    [30] = "job station",   [31] = "lounge chair",   [32] = "mail interaction",
    [33] = "mission interaction",[35]= "occupation",  [36] = "office desk",
    [37] = "patrol",        [38] = "potion station",  [39] = "railing lean",
    [40] = "railing lean",  [41] = "shop register",  [42] = "service counter",
    [43] = "stairs",        [44] = "sitting on stairs",[45]= "sitting on stairs",
    [46] = "standing",      [47] = "standing",       [48] = "standing",
    [49] = "standing",      [50] = "standing",       [51] = "standing",
    [52] = "standing in queue",[53]= "tall stool",   [54] = "study desk",
    [55] = "table",         [56] = "table",          [57] = "table",
    [58] = "table",         [59] = "table",          [60] = "taking notes",
    [61] = "teacher's chair",[62]= "drinking tea",   [63] = "telescope",
    [64] = "vendor stall",  [65] = "wall lean",      [66] = "wall sit",
    [67] = "wardrobe",      [68] = "window shopping",
}

--- Get fresh ScheduledEntity from a voice ID. Always re-fetches to avoid stale refs.
--- @param voiceId string NPC voice name (e.g. "PatrickRedding")
--- @return userdata|nil se
local function GetSE(voiceId)
    local staticData = Cache.GetStaticData()
    if not staticData then return nil end
    local popMgr = staticData.populationManager
    if not popMgr or not popMgr.IsValid or not popMgr:IsValid() then return nil end
    local se = nil
    pcall(function() se = popMgr:GetScheduledEntityFromName(voiceId) end)
    if not se then return nil end
    local valid = false
    pcall(function() valid = se:IsValid() end)
    return valid and se or nil
end

--- Find the nearest available ambient station near a position.
--- @param nearPos table {X,Y,Z} position to search near
--- @param excludePos table|nil {X,Y,Z} skip stations within 1.5m of this (NPC's current pos)
--- @param maxDist number|nil Max search radius in UE units (default 2000 = ~20m)
--- @param filter string|nil "sit", "stand", or nil/any for all ambient types
--- @return userdata|nil station, table|nil info {name, typeLabel, category, dist}
function StationUse.FindNearestAmbientStation(nearPos, excludePos, maxDist, filter)
    maxDist = maxDist or 2000
    local allStations = FindAllOf("Station")
    if not allStations then return nil, nil end

    local bestStation, bestDist, bestInfo = nil, math.huge, nil
    for _, station in pairs(allStations) do
        pcall(function()
            if not station:IsValid() then return end

            local loc = station:K2_GetActorLocation()
            if not loc then return end
            local dx = loc.X - nearPos.X
            local dy = loc.Y - nearPos.Y
            local dz = loc.Z - nearPos.Z
            local dist = math.sqrt(dx*dx + dy*dy + dz*dz)

            if dist >= bestDist or dist > maxDist then return end

            -- Skip stations near the exclude position (NPC's current station)
            if excludePos then
                local ex = loc.X - excludePos.X
                local ey = loc.Y - excludePos.Y
                local ez = loc.Z - excludePos.Z
                if math.sqrt(ex*ex + ey*ey + ez*ez) < 150 then return end
            end

            local stationComp = nil
            pcall(function() stationComp = station:GetStationComponent() end)
            if not stationComp then return end

            local active = false
            pcall(function() active = stationComp:IsStationActive() end)
            if not active then return end

            local numConns = 0
            pcall(function() numConns = stationComp:GetNumConnections() end)
            if numConns <= 0 then return end

            local numUsers = 0
            pcall(function()
                local users = {}
                stationComp:GetStationUsers(users)
                for _ in pairs(users) do numUsers = numUsers + 1 end
            end)
            if numConns - numUsers <= 0 then return end

            local propType = -1
            pcall(function() propType = stationComp:GetPropType() end)
            local category = AMBIENT_STATION_TYPES[propType]
            if not category then return end
            if filter and filter ~= "any" and category ~= filter then return end

            local stationName = "?"
            pcall(function() stationName = station:GetFullName():match("([^%.]+)$") end)

            bestStation = station
            bestDist = dist
            bestInfo = {
                name = stationName,
                typeLabel = PROP_TYPE_LABELS[propType] or "unknown",
                category = category,
                dist = dist,
            }
        end)
    end
    return bestStation, bestInfo
end

--- Send an NPC to a station.
--- @param voiceId string NPC voice name
--- @param station userdata Station actor (from FindAllOf("Station") or FindNearestAmbientStation)
--- @param teleport boolean|nil If true, teleport instantly; if false/nil, walk
--- @return boolean success
function StationUse.SendToStation(voiceId, station, teleport)
    if not voiceId or voiceId == "" then return false end

    local se = GetSE(voiceId)
    if not se then
        print(string.format("%s SendToStation: no SE for %s", TAG, voiceId))
        return false
    end

    -- Validate station ref
    local stationValid = false
    pcall(function() stationValid = station and station:IsValid() end)
    if not stationValid then
        print(string.format("%s SendToStation: invalid station for %s", TAG, voiceId))
        return false
    end

    pcall(function() se:AbandonStations(0) end)

    local ok, err
    if teleport then
        ok, err = pcall(function()
            return se:PerformTask_TeleportToStation(
                station,
                true,       -- bInInteract
                "",         -- InSocialAction
                0,          -- InConnectionIndex
                -1.0,       -- InStationDurationOverride
                false       -- InSkipValidation — keep for proper Z placement
            )
        end)
    else
        ok, err = pcall(function()
            return se:PerformTask_MoveToStation(
                station,
                FName("None"),
                true,       -- bInInteract
                0.0,        -- InForceSpeed
                0,          -- InConnectionIndex
                -1.0        -- InStationDurationOverride
            )
        end)
    end

    local method = teleport and "Teleport" or "Move"
    if ok then
        print(string.format("%s %sToStation OK: %s", TAG, method, voiceId))
    else
        print(string.format("%s %sToStation FAILED for %s: %s", TAG, method, voiceId, tostring(err)))
    end
    return ok == true
end

--- Release an NPC from a station override back to their normal schedule.
--- Graceful station exit + MoveToLocation(self, TriggerNextActivity) to cancel
--- the stuck PerformTask and hand control back to the scheduler.
--- @param voiceId string NPC voice name
function StationUse.Release(voiceId)
    if not voiceId or voiceId == "" then return end

    local se = GetSE(voiceId)
    if not se then
        print(string.format("%s Release: no SE for %s", TAG, voiceId))
        return
    end

    -- Graceful station exit if at a station
    local stationComp = nil
    pcall(function() stationComp = se:GetActiveStation() end)
    if stationComp then
        pcall(function() se:RequestStationExit(stationComp) end)
    end

    -- After exit animation, re-fetch SE (avoid stale ref across delay),
    -- AbandonStations + MoveToLocation(self) to cancel the stuck PerformTask
    local capturedVoiceId = voiceId
    ExecuteInGameThreadWithDelay(stationComp and 2200 or 100, function()
        local freshSe = GetSE(capturedVoiceId)
        if not freshSe then
            print(string.format("%s Release delayed: lost SE for %s", TAG, capturedVoiceId))
            return
        end
        pcall(function() freshSe:AbandonStations(0) end)
        -- PerformTask_MoveToStation creates a persistent task-level override that
        -- AbandonStations alone cannot clear — the NPC walks right back to the
        -- station because the task is still active. The only way to cancel it is
        -- to issue a new PerformTask that replaces it. MoveToLocation to the NPC's
        -- current position completes instantly, and InBTriggerNextActivity=true
        -- tells the scheduler to reassert and resume normal scheduled behavior.
        local loc = nil
        pcall(function() loc = freshSe:GetLocation() end)
        if loc then
            pcall(function()
                freshSe:PerformTask_MoveToLocation(
                    loc,    -- current position
                    0.0,    -- InForceSpeed
                    10.0,   -- InClearanceDistance
                    true,   -- InBTriggerNextActivity — scheduler reasserts
                    10.0,   -- InRadius
                    nil     -- InPath
                )
            end)
        end
        print(string.format("%s Released %s (TriggerNextActivity)", TAG, capturedVoiceId))
    end)
end

--- Expose tables for external use.
StationUse.AMBIENT_STATION_TYPES = AMBIENT_STATION_TYPES
StationUse.PROP_TYPE_LABELS = PROP_TYPE_LABELS

print("[StationUse] Loaded")
return StationUse
