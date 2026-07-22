-- CommitmentManager.lua
-- Manages NPC schedule overrides for the commitment system.
-- Loaded via dofile() from logic.lua for hot-reload support.
print("[CommitmentManager] Loading...")

local TAG = "[Commitment]"
local Utils = require("Utils.Utils")
local LocationRegistry = require("Utils.LocationRegistry")
local TickScheduler = require("Utils.TickScheduler")
local CommitmentManager = {}

-- Persistent state across F11 reloads
_G.ActiveCommitments = _G.ActiveCommitments or {}
_G.CommitmentActivitiesInserted = _G.CommitmentActivitiesInserted or false

local function CancelPlacementPoll(entry)
    if not entry then return end

    if entry._pollTaskId then
        TickScheduler.Unregister(entry._pollTaskId)
        entry._pollTaskId = nil
    end

    if entry._pollHandle and type(entry._pollHandle) ~= "string" then
        pcall(CancelDelayedAction, entry._pollHandle)
    end
    entry._pollHandle = nil
end

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

--- Pick a random commitment spot that is NOT in the player's field of view.
--- Falls back to any random spot if all are in FOV.
--- @param spots table Array of {x, y, z, yaw} spot definitions
--- @return table|nil Selected spot, or nil if no spots
local function PickSpotOutOfFOV(spots)
    if not spots or #spots == 0 then return nil end
    if #spots == 1 then return spots[1] end

    -- Get camera info for FOV check
    local camLoc, camFwd, camFOV
    pcall(function()
        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData then return end
        local cam = staticData.cameraManager
        if not cam then return end
        local camRot = cam:GetCameraRotation()
        camLoc = cam:GetCameraLocation()
        camFOV = cam:GetFOVAngle() or 90
        local pitch = math.rad(camRot.Pitch)
        local yaw = math.rad(camRot.Yaw)
        camFwd = {
            X = math.cos(pitch) * math.cos(yaw),
            Y = math.cos(pitch) * math.sin(yaw),
            Z = math.sin(pitch),
        }
    end)

    if not camLoc or not camFwd then
        return spots[math.random(#spots)]
    end

    local halfFOV = math.rad(camFOV * 0.5)
    local cosFOV = math.cos(halfFOV)

    local outOfFOV = {}
    for _, spot in ipairs(spots) do
        local dx = spot.x - camLoc.X
        local dy = spot.y - camLoc.Y
        local dz = (spot.z or camLoc.Z) - camLoc.Z
        local dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        if dist > 1 then
            local dot = (dx * camFwd.X + dy * camFwd.Y + dz * camFwd.Z) / dist
            if dot < cosFOV then
                table.insert(outOfFOV, spot)
            end
        else
            table.insert(outOfFOV, spot)
        end
    end

    if #outOfFOV > 0 then
        return outOfFOV[math.random(#outOfFOV)]
    end
    return spots[math.random(#spots)]
end

--- Place an NPC at a commitment spot once they are in flesh.
--- Polls for flesh load, then uses PlaceScheduledEntityBP + NPCLock.
--- The schedule override + InsertDynamicActivityOnSE must already be active
--- (this is what triggers flesh loading).
--- @param npcId string Voice name
--- @param entry table The ActiveCommitments entry
--- @return boolean success
local function PlaceTeleportCommitment(npcId, entry)
    -- Guard against concurrent placement attempts (proximity + location:change can race)
    if entry._placing then return false end
    if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then return false end
    -- Cancel any existing poll loop from a previous Apply cycle
    CancelPlacementPoll(entry)
    entry._placing = true

    local spots = _G.CommitmentSpots and _G.CommitmentSpots[entry.location_id]
    if not spots or #spots == 0 then
        print(string.format("%s No spots for %s at %s", TAG, npcId, entry.location_id))
        entry._placing = nil
        return false
    end

    -- Filter by spot label if specified
    local filteredSpots = spots
    if entry.spot_label and entry.spot_label ~= "" then
        filteredSpots = {}
        for _, spot in ipairs(spots) do
            if spot.label == entry.spot_label then
                table.insert(filteredSpots, spot)
            end
        end
        if #filteredSpots == 0 then
            print(string.format("%s No spots matching label '%s' at %s, using all spots", TAG, entry.spot_label, entry.location_id))
            filteredSpots = spots
        end
    end

    local spot = PickSpotOutOfFOV(filteredSpots)
    if not spot then entry._placing = nil return false end

    -- Mark placed early to prevent double-fire
    entry.placed = true
    entry.target_pos = { X = spot.x, Y = spot.y, Z = spot.z }

    print(string.format("%s PlaceTeleport: waiting for flesh %s at (%.0f,%.0f,%.0f)",
        TAG, npcId, spot.x, spot.y, spot.z))

    local cNpcId = npcId
    local cEntry = entry
    local cSpot = spot

    local attempts = 0
    local maxAttempts = 30  -- 15 seconds
    local pollTaskId = "commitment_flesh_poll:" .. tostring(npcId)
    TickScheduler.Register(pollTaskId, 509, function()
        if not _G.SonorusState.playerLoaded or Utils.IsGamePaused() then return end
        attempts = attempts + 1

        -- Bail if entry was invalidated (e.g. MarkAllDirty cancelled us)
        if cEntry.dirty or not cEntry._placing then
            CancelPlacementPoll(cEntry)
            return
        end

        local freshRefs = GetFreshRefs(cNpcId)
        if not freshRefs then
            CancelPlacementPoll(cEntry)
            cEntry._placing = nil
            return
        end

        local inFlesh = false
        pcall(function() inFlesh = freshRefs.se:CurrentlyInFlesh() end)

        if attempts <= 3 or attempts == maxAttempts or inFlesh then
            print(string.format("%s FleshPoll %s: attempt=%d inFlesh=%s", TAG, cNpcId, attempts, tostring(inFlesh)))
        end

        if inFlesh then
            CancelPlacementPoll(cEntry)
            print(string.format("%s PlaceScheduledEntityBP START: %s", TAG, cNpcId))

            -- Place at exact vector
            local halfYaw = math.rad(cSpot.yaw) * 0.5
            pcall(function()
                freshRefs.popManager:PlaceScheduledEntityBP(cNpcId, {
                    Translation = { X = cSpot.x, Y = cSpot.y, Z = cSpot.z },
                    Rotation = { X = 0, Y = 0, Z = math.sin(halfYaw), W = math.cos(halfYaw) },
                    Scale3D = { X = 1, Y = 1, Z = 1 },
                })
            end)

            print(string.format("%s PlaceScheduledEntityBP DONE: %s", TAG, cNpcId))

            -- Lock after a short delay to let placement settle
            ExecuteInGameThreadWithDelay(500, function()
                print(string.format("%s CreateCommitmentLock START: %s", TAG, cNpcId))
                local lockRefs = GetFreshRefs(cNpcId)
                if not lockRefs then
                    cEntry._placing = nil
                    return
                end
                local npc = nil
                pcall(function()
                    local flesh = lockRefs.se:GetFlesh()
                    if flesh and flesh:IsValid() then npc = flesh end
                end)
                if npc then
                    local NPCLock = _G.NPCLockModule
                    if NPCLock and NPCLock.CreateCommitmentLock then
                        local lockId = NPCLock.CreateCommitmentLock(npc, lockRefs.se, cNpcId)
                        if lockId then
                            cEntry.lockId = lockId
                            print(string.format("%s Placed %s at %s (lock=%d)", TAG, cNpcId, cEntry.location_id, lockId))
                        end
                    end
                end
                cEntry._placing = nil
            end)
        elseif attempts >= maxAttempts then
            CancelPlacementPoll(cEntry)
            cEntry._placing = nil
            cEntry.placeFails = (cEntry.placeFails or 0) + 1
            cEntry.lastFailTime = os.clock()
            print(string.format("%s PlaceTeleport: %s failed after %d attempts (fail #%d)",
                TAG, cNpcId, maxAttempts, cEntry.placeFails))
        end
    end)
    entry._pollTaskId = pollTaskId
    entry._pollHandle = nil

    return true
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

--- Insert 24hr FreeTime activities for all commitment spot locations.
--- Uses LocationRegistry (schedule_id) + CommitmentSpots to determine which locations need entries.
--- Idempotent: skips if already inserted this session, uses INSERT OR IGNORE for DB safety.
function CommitmentManager.Init()
    if _G.CommitmentActivitiesInserted then return end

    local DbGateway = FindFirstOf("DbGateway")
    if not DbGateway or not DbGateway:IsValid() then
        print(TAG .. " Init: DbGateway not found, deferring\n")
        return
    end

    local registry = _G.LocationRegistry
    local spots = _G.CommitmentSpots
    if not registry or not spots then
        print(TAG .. " Init: LocationRegistry or CommitmentSpots not loaded yet, deferring\n")
        return
    end

    local count = 0
    local skipped = 0
    for modKey, _ in pairs(spots) do
        local entry = registry[modKey]
        local schedId = entry and entry.schedule_id
        if schedId then
            local actId = "SONORUS_24h_" .. modKey
            local sql = string.format(
                "INSERT OR IGNORE INTO ActivityDefinition " ..
                "(ActivityID, ActivityTypeID, StartTime, EndTime, LocationID, ActivityRecurrenceTypeID, " ..
                "Sunday, Monday, Tuesday, Wednesday, Thursday, Friday, Saturday) " ..
                "VALUES ('%s', 'FreeTime', 0, 2400, '%s', 'Daily', '1', '1', '1', '1', '1', '1', '1')",
                actId, schedId)
            pcall(function()
                DbGateway:DbOperate(sql, false)
            end)
            count = count + 1
        else
            skipped = skipped + 1
        end
    end

    _G.CommitmentActivitiesInserted = true
    print(string.format("%s Init: Inserted %d 24hr activities (%d skipped, no schedule_id)\n", TAG, count, skipped))
end

--- Apply a schedule override to redirect an NPC to a location.
--- Unified flow: always uses InsertDynamicActivityOnSE (24hr activity) to get flesh loaded,
--- then PlaceScheduledEntityBP to position at the exact commitment spot vector.
--- @param npcId string Voice name (e.g., "GladwinMoon")
--- @param activityId string Activity ID (e.g., "HM_ThreeBroomsticksHours") — ignored if 24hr activity exists
--- @param modKey string Canonical mod key (e.g., "HM_ThreeBroomsticks")
--- @param spotLabel string|nil Optional spot label for preferred spot selection
function CommitmentManager.Apply(npcId, activityId, modKey, spotLabel)
    local locationId = LocationRegistry.GetScheduleId(modKey) or modKey
    local errorMsg = nil

    local refs = GetFreshRefs(npcId)
    if not refs then
        errorMsg = "Could not get fresh refs"
        print(string.format("%s Apply FAILED for %s: %s", TAG, npcId, errorMsg))
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
    print(string.format("%s StartSchedulingOverride START: %s", TAG, npcId))
    local ok1, err1 = pcall(function()
        refs.se:StartSchedulingOverride(true, 4, refs.provider, true, true, true)
    end)
    if not ok1 then
        print(string.format("%s StartSchedulingOverride FAILED for %s: %s", TAG, npcId, tostring(err1)))
    end
    print(string.format("%s StartSchedulingOverride END: %s", TAG, npcId))

    -- Step 2: InsertDynamicActivityOnSE — triggers flesh loading
    -- Use provided activityId (from LOCATION_ACTIVITIES) or our 24hr activity for spots without one
    local usedActivityId = (activityId and activityId ~= "") and activityId or ("SONORUS_24h_" .. modKey)
    local insertResult = false
    print(string.format("%s InsertDynamic START: %s act=%s loc=%s", TAG, npcId, usedActivityId, locationId))
    if refs.weActor and refs.weActor.IsValid and refs.weActor:IsValid() then
        local ok2, err2 = pcall(function()
            insertResult = refs.weActor:InsertDynamicActivityOnSE(refs.se, usedActivityId, locationId)
        end)
        if not ok2 then
            print(string.format("%s InsertDynamic FAILED for %s: %s", TAG, npcId, tostring(err2)))
        end
        print(string.format("%s InsertDynamic END: %s result=%s", TAG, npcId, tostring(insertResult)))
    else
        print(string.format("%s No WorldEventActor for %s", TAG, npcId))
    end

    local success = insertResult == true
    if not success then
        errorMsg = "InsertDynamicActivity returned false"
        print(string.format("%s Apply FAILED for %s: %s", TAG, npcId, errorMsg))
    end

    -- Determine guide position from spots
    local spots = _G.CommitmentSpots and _G.CommitmentSpots[modKey]
    local guidePos = nil
    if spots and #spots > 0 then
        local guideSpot = spots[math.random(#spots)]
        guidePos = { X = guideSpot.x, Y = guideSpot.y, Z = guideSpot.z }
    end

    if success then
        _G.ActiveCommitments[npcId] = {
            npc_id = npcId,
            activity_id = usedActivityId,
            location_id = modKey,
            applied = true,
            dirty = false,
            mode = "schedule",
            placed = false,
            lockId = nil,
            spot_label = spotLabel,
            target_pos = guidePos,
        }
        print(string.format("%s Applied: %s -> %s (%s)", TAG, npcId, modKey, usedActivityId))

        -- Start polling for flesh and place at spot
        if spots and #spots > 0 then
            PlaceTeleportCommitment(npcId, _G.ActiveCommitments[npcId])
        end
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

    -- Teleport mode: release lock, restore scheduling
    if entry.mode == "teleport" then
        if entry.placed and entry.lockId then
            local NPCLock = _G.NPCLockModule
            if NPCLock and NPCLock.ReleaseNPC then
                pcall(NPCLock.ReleaseNPC, entry.lockId)
            end
        end
        local refs = GetFreshRefs(npcId)
        if refs then
            pcall(function() refs.se:EnableScheduling(true, false, true) end)
        end
        _G.ActiveCommitments[npcId] = nil
        print(string.format("%s Released (teleport): %s", TAG, npcId))
        if _G.PathNav then pcall(_G.PathNav.OnCommitmentReleased, npcId) end
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
    -- Auto-switch guide path if we were guiding to this NPC
    if _G.PathNav then pcall(_G.PathNav.OnCommitmentReleased, npcId) end
end

--- Mark all active commitments as dirty (needing re-apply after loading screen).
function CommitmentManager.MarkAllDirty()
    local count = 0
    for npcId, entry in pairs(_G.ActiveCommitments) do
        -- Cancel any running flesh poll loop
        CancelPlacementPoll(entry)
        entry._placing = nil
        entry.dirty = true
        entry.applied = false
        entry.placed = false
        entry.lockId = nil  -- Lock refs are dead after InvalidateWorld
        count = count + 1
    end
    if count > 0 then
        print(string.format("%s Marked %d commitments as dirty", TAG, count))
    end
    -- Clear guide trail on loading screen (target may have moved)
    if _G.PathNav then pcall(_G.PathNav.Clear) end
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
    -- Restart guide path if it was active before loading screen
    if _G.PathNav then pcall(_G.PathNav.RestartIfPending) end
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

--- Distance-based placement for teleport commitments.
--- Called from unified loop every 2s. If the player is within PROXIMITY_THRESHOLD
--- of an unplaced teleport target, attempt placement early so the NPC doesn't
--- pop in at the location boundary.
local PROXIMITY_THRESHOLD_SQ = 10000 * 10000  -- ~100m in UU, squared to avoid sqrt
function CommitmentManager.ProximityCheck()
    -- Quick exit: any unplaced teleports with a target_pos?
    local hasWork = false
    for _, entry in pairs(_G.ActiveCommitments) do
        if entry.mode == "teleport" and not entry.placed and entry.target_pos then
            hasWork = true
            break
        end
    end
    if not hasWork then return end

    -- Get player position (one UObject call)
    local playerLoc
    pcall(function()
        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData then return end
        local player = staticData.player
        if player and player.IsValid and player:IsValid() then
            playerLoc = player:K2_GetActorLocation()
        end
    end)
    if not playerLoc then return end

    local now = os.clock()
    for npcId, entry in pairs(_G.ActiveCommitments) do
        if entry.mode == "teleport" and not entry.placed and entry.target_pos then
            -- Back off after failed placement: 30s cooldown per failure (30s, 60s, 90s, ...)
            if entry.lastFailTime then
                local cooldown = (entry.placeFails or 1) * 30
                if now - entry.lastFailTime < cooldown then
                    goto continue
                end
            end
            local dx = entry.target_pos.X - playerLoc.X
            local dy = entry.target_pos.Y - playerLoc.Y
            local dz = entry.target_pos.Z - playerLoc.Z
            local distSq = dx * dx + dy * dy + dz * dz
            if distSq <= PROXIMITY_THRESHOLD_SQ then
                print(string.format("%s ProximityCheck: player within range of %s (dist=%.0f), placing",
                    TAG, npcId, math.sqrt(distSq)))
                PlaceTeleportCommitment(npcId, entry)
            end
            ::continue::
        end
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
            mode = entry.mode or "schedule",
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

-- Listen for location:change to manage teleport commitments and clear hobos.
if _G._commitmentLocationListenerId then
    Events.off("location:change", _G._commitmentLocationListenerId)
end
_G._commitmentLocationListenerId = Events.on("location:change", function(data)
    local newDisplayName = data and data.location or ""
    -- Resolve localized display name to canonical mod key
    local newLocation = LocationRegistry.GetModKey(newDisplayName) or newDisplayName

    -- Helper: resolve current tracked location (display name) to mod key
    local function resolveCurrentLocation()
        local tracked = _G.LastTrackedLocation or ""
        return LocationRegistry.GetModKey(tracked) or tracked
    end

    for npcId, entry in pairs(_G.ActiveCommitments) do
        if not entry.applied then goto continue end

        local hasSpots = _G.CommitmentSpots and _G.CommitmentSpots[entry.location_id] and #_G.CommitmentSpots[entry.location_id] > 0

        if entry.location_id == newLocation then
            -- Player entered the target location
            if hasSpots and not entry.placed then
                local placed = PlaceTeleportCommitment(npcId, entry)
                if not placed then
                    -- Retry once after a short delay (NPC may still be streaming in)
                    ExecuteInGameThreadWithDelay(3000, function()
                        local e = _G.ActiveCommitments[npcId]
                        if e and not e.placed and e.location_id == resolveCurrentLocation() then
                            PlaceTeleportCommitment(npcId, e)
                        end
                    end)
                end
            end
            -- Clear hobos near committed NPC
            ExecuteInGameThreadWithDelay(3000, function()
                local e = _G.ActiveCommitments[npcId]
                if e and e.applied then
                    pcall(CommitmentManager.ClearNearbyHobos, npcId)
                end
            end)
        else
            -- Player left the target location: release lock and mark unplaced
            if entry.placed then
                if entry.lockId then
                    local NPCLock = _G.NPCLockModule
                    if NPCLock and NPCLock.ReleaseNPC then
                        pcall(NPCLock.ReleaseNPC, entry.lockId)
                    end
                end
                print(string.format("%s Released lock for %s (left %s)", TAG, npcId, entry.location_id))
            end
            entry.placed = false
            entry.lockId = nil
        end

        ::continue::
    end
end)

-- Init is called externally after LocationRegistry.Init() completes
-- (e.g. from socket_client.lua after handshake)

print(string.format("[CommitmentManager] Loaded (%d active commitments)", #CommitmentManager.GetActive()))
return CommitmentManager
