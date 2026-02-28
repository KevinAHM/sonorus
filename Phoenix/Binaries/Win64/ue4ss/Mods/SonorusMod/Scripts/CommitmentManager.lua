-- CommitmentManager.lua
-- Manages NPC schedule overrides for the commitment system.
-- Loaded via dofile() from logic.lua for hot-reload support.
print("[CommitmentManager] Loading...")

local TAG = "[Commitment]"
local CommitmentManager = {}

-- Persistent state across F11 reloads
_G.ActiveCommitments = _G.ActiveCommitments or {}

-- Get fresh UObject references (NEVER cache across calls)
local function GetFreshRefs(npcId)
    local refs = {}

    local staticData = _G.GetStaticCache and _G.GetStaticCache()
    if not staticData then
        print(TAG .. " No static cache")
        return nil
    end

    refs.popManager = staticData.populationManager
    if not refs.popManager or not refs.popManager.IsValid or not refs.popManager:IsValid() then
        print(TAG .. " No PopulationManager")
        return nil
    end

    -- Get ScheduledEntity by name
    local ok, err = pcall(function()
        refs.se = refs.popManager:GetScheduledEntityFromName(npcId)
    end)
    if not ok or not refs.se then
        print(string.format("%s Could not get ScheduledEntity for %s: %s", TAG, npcId, tostring(err)))
        return nil
    end

    local seValid = false
    pcall(function() seValid = refs.se:IsValid() end)
    if not seValid then
        print(string.format("%s ScheduledEntity not valid for %s", TAG, npcId))
        return nil
    end

    -- Provider: mod actor or player controller
    pcall(function()
        refs.provider = _G.SonorusState and _G.SonorusState.sonorusModActor
    end)
    if not refs.provider or not refs.provider.IsValid or not refs.provider:IsValid() then
        refs.provider = staticData.playerController
    end

    -- WorldEventActor (always fresh)
    pcall(function()
        refs.weActor = FindFirstOf("WorldEventActor")
    end)

    return refs
end

--- Clear social activity hobos (BP_Tier3_Character) near a committed NPC's station.
--- Called with delay after Apply/ReapplyAll so NPC has time to arrive at station.
--- @param npcId string Voice name
function CommitmentManager.ClearNearbyHobos(npcId)
    local refs = GetFreshRefs(npcId)
    if not refs then return end

    -- Get committed NPC's station location
    local stationLoc = nil
    pcall(function()
        local stationComp = refs.se:GetActiveStation()
        if stationComp then
            local owner = stationComp:GetOwner()
            if owner then stationLoc = owner:K2_GetActorLocation() end
        end
    end)
    if not stationLoc then return end

    -- Get committed NPC's flesh name to exclude
    local commitFleshName = nil
    pcall(function()
        local flesh = refs.se:GetFlesh()
        if flesh then commitFleshName = flesh:GetFullName() end
    end)

    local allChars = nil
    pcall(function() allChars = FindAllOf("Character") end)
    if not allChars then return end

    local cleared = 0
    for _, actor in pairs(allChars) do
        pcall(function()
            if not actor:IsValid() then return end
            local fn = actor:GetFullName()
            if commitFleshName and fn == commitFleshName then return end
            if not fn:find("BP_Tier3_Character") then return end

            local loc = actor:K2_GetActorLocation()
            local dx = loc.X - stationLoc.X
            local dy = loc.Y - stationLoc.Y
            local dz = loc.Z - stationLoc.Z
            local dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            if dist > 300 then return end

            -- Get hobo's ScheduledEntity
            local t3se = refs.popManager:GetScheduledEntityFromActor(actor, false)
            if not t3se then return end

            -- Break social interaction
            pcall(function()
                local sr = actor.SocialReasoning
                if sr then
                    sr:Nevermind()
                    sr:Goodbye()
                end
            end)

            -- Break station link and disable scheduling
            pcall(function() t3se:AbandonStations(0) end)
            pcall(function() t3se:EnableScheduling(false, true, true) end)

            -- Move underground so they walk away naturally
            pcall(function()
                actor:K2_SetActorLocation(
                    {X = stationLoc.X, Y = stationLoc.Y, Z = stationLoc.Z - 5000},
                    false, {}, false
                )
            end)

            cleared = cleared + 1
        end)
    end

    if cleared > 0 then
        print(string.format("%s Cleared %d hobos near %s", TAG, cleared, npcId))
    end
end

--- Apply a schedule override to redirect an NPC to a location.
--- @param npcId string Voice name (e.g., "GladwinMoon")
--- @param activityId string Activity ID (e.g., "HM_ThreeBroomsticksHours")
--- @param locationId string Location ID (e.g., "HM_ThreeBroomsticks")
function CommitmentManager.Apply(npcId, activityId, locationId)
    local success = false
    local errorMsg = nil

    local refs = GetFreshRefs(npcId)
    if not refs then
        errorMsg = "Could not get fresh refs"
        print(string.format("%s Apply FAILED for %s: %s", TAG, npcId, errorMsg))
        -- Send failure ACK
        if _G.SocketClient and _G.SocketClient.send then
            _G.SocketClient.send({
                type = "commitment_status",
                npc_id = npcId,
                action = "apply",
                success = false,
                error = errorMsg,
            })
        end
        return false
    end

    -- Step 1: StartSchedulingOverride
    local ok1, err1 = pcall(function()
        refs.se:StartSchedulingOverride(true, 4, refs.provider, true, true, true)
    end)
    if not ok1 then
        print(string.format("%s StartSchedulingOverride FAILED for %s: %s", TAG, npcId, tostring(err1)))
    end

    -- Step 2: InsertDynamicActivityOnSE
    local insertResult = false
    if refs.weActor and refs.weActor.IsValid and refs.weActor:IsValid() then
        local ok2, err2 = pcall(function()
            insertResult = refs.weActor:InsertDynamicActivityOnSE(refs.se, activityId, locationId)
        end)
        if not ok2 then
            print(string.format("%s InsertDynamicActivity FAILED for %s: %s", TAG, npcId, tostring(err2)))
        end
    else
        print(string.format("%s No WorldEventActor for %s", TAG, npcId))
    end

    success = insertResult == true
    if success then
        _G.ActiveCommitments[npcId] = {
            npc_id = npcId,
            activity_id = activityId,
            location_id = locationId,
            applied = true,
            dirty = false,
        }
        print(string.format("%s Applied: %s -> %s (%s)", TAG, npcId, locationId, activityId))
    else
        errorMsg = "InsertDynamicActivity returned false"
        print(string.format("%s Apply result for %s: %s", TAG, npcId, tostring(insertResult)))
    end

    -- Send ACK to Python
    if _G.SocketClient and _G.SocketClient.send then
        _G.SocketClient.send({
            type = "commitment_status",
            npc_id = npcId,
            action = "apply",
            success = success,
            error = errorMsg,
        })
    end

    return success
end

--- Release a schedule override, restoring normal NPC schedule.
--- @param npcId string Voice name
function CommitmentManager.Release(npcId)
    local entry = _G.ActiveCommitments[npcId]
    if not entry then
        print(string.format("%s Release: %s not in ActiveCommitments", TAG, npcId))
        return
    end

    local refs = GetFreshRefs(npcId)
    if not refs then
        print(string.format("%s Release: can't get fresh refs for %s - clearing state anyway", TAG, npcId))
        _G.ActiveCommitments[npcId] = nil
        return
    end

    -- RemoveDynamicActivityFromSE
    if refs.weActor and refs.weActor.IsValid and refs.weActor:IsValid() and entry.activity_id then
        local ok, err = pcall(function()
            refs.weActor:RemoveDynamicActivityFromSE(refs.se, entry.activity_id)
        end)
        if not ok then
            print(string.format("%s RemoveDynamic FAILED for %s: %s", TAG, npcId, tostring(err)))
        end
    end

    -- FinishSchedulingOverride
    pcall(function()
        refs.se:FinishSchedulingOverride(4, refs.provider, true, false, true)
    end)

    -- Re-enable scheduling
    pcall(function()
        refs.se:EnableScheduling(true, false, true)
    end)

    _G.ActiveCommitments[npcId] = nil
    print(string.format("%s Released: %s", TAG, npcId))
end

--- Mark all active commitments as dirty (needing re-apply after loading screen).
function CommitmentManager.MarkAllDirty()
    local count = 0
    for npcId, entry in pairs(_G.ActiveCommitments) do
        entry.dirty = true
        entry.applied = false
        count = count + 1
    end
    if count > 0 then
        print(string.format("%s Marked %d commitments as dirty", TAG, count))
    end
end

--- Re-apply all dirty commitments (called after loading screen with delay).
function CommitmentManager.ReapplyAll()
    local count = 0
    for npcId, entry in pairs(_G.ActiveCommitments) do
        if entry.dirty then
            print(string.format("%s Re-applying: %s -> %s", TAG, npcId, entry.location_id))
            local success = CommitmentManager.Apply(npcId, entry.activity_id, entry.location_id)
            if success then
                entry.dirty = false
                count = count + 1
            else
                print(string.format("%s Re-apply FAILED for %s - removing from active", TAG, npcId))
                _G.ActiveCommitments[npcId] = nil
            end
        end
    end
    if count > 0 then
        print(string.format("%s Re-applied %d commitments", TAG, count))
    end
end

--- Release all active commitments (called on F8 reset).
function CommitmentManager.ReleaseAll()
    local count = 0
    -- Collect keys first to avoid modifying table during iteration
    local npcIds = {}
    for npcId, _ in pairs(_G.ActiveCommitments) do
        table.insert(npcIds, npcId)
    end
    for _, npcId in ipairs(npcIds) do
        CommitmentManager.Release(npcId)
        count = count + 1
    end
    if count > 0 then
        print(string.format("%s Released all %d commitments", TAG, count))
    end
end

--- Get serializable table of active commitments (for Python).
function CommitmentManager.GetActive()
    local result = {}
    for npcId, entry in pairs(_G.ActiveCommitments) do
        table.insert(result, {
            npc_id = entry.npc_id,
            activity_id = entry.activity_id,
            location_id = entry.location_id,
            applied = entry.applied,
            dirty = entry.dirty,
        })
    end
    return result
end

-- Listen for timeUpdated events to re-apply dirty commitments
-- (replaces hardcoded 3s delays in main.lua hooks)
local Events = require("Utils.Events")
if _G._commitmentTimeListenerId then
    Events.off("timeUpdated", _G._commitmentTimeListenerId)
end
_G._commitmentTimeListenerId = Events.on("timeUpdated", function()
    -- Check if any dirty entries need re-apply
    local hasDirty = false
    for _, entry in pairs(_G.ActiveCommitments) do
        if entry.dirty then hasDirty = true break end
    end
    if hasDirty then
        CommitmentManager.ReapplyAll()
    end
end)

-- Listen for location:change to clear hobos near committed NPCs.
-- Uses a 3s delay so NPCs/hobos have time to stream in and settle at stations.
if _G._commitmentLocationListenerId then
    Events.off("location:change", _G._commitmentLocationListenerId)
end
_G._commitmentLocationListenerId = Events.on("location:change", function(data)
    -- Check if any active (or soon-active) commitments exist
    local hasActive = false
    for _, entry in pairs(_G.ActiveCommitments) do
        if entry.applied then hasActive = true break end
    end
    if not hasActive then return end

    -- Delay so NPCs stream in and hobos settle at stations
    ExecuteInGameThreadWithDelay(3000, function()
        for npcId, entry in pairs(_G.ActiveCommitments) do
            if entry.applied then
                pcall(CommitmentManager.ClearNearbyHobos, npcId)
            end
        end
    end)
end)

print(string.format("[CommitmentManager] Loaded (%d active commitments)", #CommitmentManager.GetActive()))
return CommitmentManager
