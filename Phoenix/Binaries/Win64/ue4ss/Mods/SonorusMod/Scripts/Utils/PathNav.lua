-- PathNav.lua
-- Guides the player to the nearest NPC with an active commitment using the golden trail.
-- Loaded via dofile() from logic.lua for hot-reload support.
print("[PathNav] Loading...")

local TAG = "[PathNav]"
local PathNav = {}
local TickScheduler = require("Utils.TickScheduler")

-- Persistent state across F11 reloads
-- pendingRestart: true when guide was active before a loading screen (Clear was called)
_G._PathNavState = _G._PathNavState or {
    active = false,
    targetNpcId = nil,
    loopHandle = nil,
    loopToken = nil,
    pendingRestart = false,
}

-- Kill any stale loop from previous load (dofile re-runs this on F11)
if _G._PathNavState.loopHandle then
    pcall(CancelDelayedAction, _G._PathNavState.loopHandle)
    _G._PathNavState.loopHandle = nil
    _G._PathNavState.loopToken = nil
    _G._PathNavState.active = false
    _G._PathNavState.targetNpcId = nil
    print(TAG .. " Cleaned up stale loop from previous load")
end
TickScheduler.Unregister("path_nav_arrival")
if _G._PathNavState.loopToken then
    _G._PathNavState.loopToken = nil
    _G._PathNavState.active = false
    _G._PathNavState.targetNpcId = nil
    print(TAG .. " Cleaned up stale scheduler task from previous load")
end

local ARRIVAL_THRESHOLD = 200   -- ~2m in Unreal units
local POLL_INTERVAL = 2000      -- 2 seconds

-- Get fresh PathNavigationManager reference (never cache UObjects)
local function GetPathNavManager()
    local mgr = nil
    pcall(function() mgr = FindFirstOf("BP_PathNavigationManager_C") end)
    if mgr then
        local valid = false
        pcall(function() valid = mgr:IsValid() end)
        if valid then return mgr end
    end
    return nil
end

-- Get player location (fresh refs)
local function GetPlayerLocation()
    local loc = nil
    pcall(function()
        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData or not staticData.player then return end
        local valid = false
        pcall(function() valid = staticData.player:IsValid() end)
        if valid then
            loc = staticData.player:K2_GetActorLocation()
        end
    end)
    return loc
end

-- Get NPC location via PopulationManager (fresh refs every call).
-- For teleport commitments, uses the pre-picked target_pos from the commitment entry
-- (the NPC isn't at the target location until the player arrives and we spawn them).
local function GetNpcLocation(npcId)
    -- Check for teleport commitment with a stored target position
    local entry = _G.ActiveCommitments and _G.ActiveCommitments[npcId]
    if entry and entry.mode == "teleport" and entry.target_pos then
        return entry.target_pos
    end

    local loc = nil
    pcall(function()
        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData then return end
        local popManager = staticData.populationManager
        if not popManager then return end
        local valid = false
        pcall(function() valid = popManager:IsValid() end)
        if not valid then return end
        local se = popManager:GetScheduledEntityFromName(npcId)
        if se then
            loc = se:GetLocation()
        end
    end)
    return loc
end

-- 3D distance between two FVector-like tables
local function Distance3D(a, b)
    local dx = a.X - b.X
    local dy = a.Y - b.Y
    local dz = a.Z - b.Z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end

-- Fire the golden guide trail to a location
local function FireTrail(loc)
    local mgr = GetPathNavManager()
    if not mgr then return false end
    local destVec = { X = loc.X, Y = loc.Y, Z = loc.Z }
    pcall(function() mgr:ClearWaypointPathTarget() end)
    pcall(function() mgr:RemoveGuideSpline() end)
    local ok, err = pcall(function()
        mgr:AddWaypointPathTarget(destVec)
        mgr:GiveMeHelp()
    end)
    if not ok then
        print(string.format("%s FireTrail FAILED: %s", TAG, tostring(err)))
        return false
    end
    return true
end

-- Remove the golden trail
local function ClearTrail()
    pcall(function()
        local mgr = GetPathNavManager()
        if mgr then
            mgr:RemoveGuideSpline()
            mgr:ClearPathTarget()
        end
    end)
end

--- Stop guiding (cancel polling loop + clear trail).
function PathNav.StopGuide()
    local state = _G._PathNavState
    if state.loopHandle then
        pcall(CancelDelayedAction, state.loopHandle)
        state.loopHandle = nil
    end
    TickScheduler.Unregister("path_nav_arrival")
    state.loopToken = nil
    ClearTrail()
    if state.active then
        print(string.format("%s Stopped guiding to %s", TAG, tostring(state.targetNpcId)))
    end
    state.active = false
    state.targetNpcId = nil
    state.pendingRestart = false
end

--- Clear guide for loading screen / reset. Remembers if guide was active so it can restart.
function PathNav.Clear()
    local state = _G._PathNavState
    local wasActive = state.active
    if state.loopHandle then
        pcall(CancelDelayedAction, state.loopHandle)
        state.loopHandle = nil
    end
    TickScheduler.Unregister("path_nav_arrival")
    state.loopToken = nil
    ClearTrail()
    state.active = false
    state.targetNpcId = nil
    state.pendingRestart = wasActive
end

--- Restart guide after fast travel / loading screen if it was active before.
function PathNav.RestartIfPending()
    local state = _G._PathNavState
    if not state.pendingRestart then return end
    state.pendingRestart = false
    print(TAG .. " Restarting guide after fast travel")
    PathNav.GuideToNearest()
end

--- Start guiding to a specific NPC. No-op if already guiding to same NPC.
--- @param npcId string Voice name (e.g. "GladwinMoon")
function PathNav.StartGuide(npcId)
    local state = _G._PathNavState

    if state.active and state.targetNpcId == npcId then
        return
    end

    if state.active then
        PathNav.Clear()
    end

    local npcLoc = GetNpcLocation(npcId)
    if not npcLoc then
        print(string.format("%s Cannot get location for %s", TAG, npcId))
        return
    end

    if not FireTrail(npcLoc) then return end

    state.active = true
    state.targetNpcId = npcId
    state.pendingRestart = false
    print(string.format("%s Guiding to %s", TAG, npcId))

    -- Start arrival polling loop
    local myToken = tostring(npcId) .. ":" .. tostring(os.clock())
    state.loopHandle = nil
    state.loopToken = myToken
    TickScheduler.Register("path_nav_arrival", POLL_INTERVAL, function()
        local s = _G._PathNavState
        if not s.active or s.targetNpcId ~= npcId or s.loopToken ~= myToken then
            TickScheduler.Unregister("path_nav_arrival")
            return
        end

        local playerLoc = GetPlayerLocation()
        local currentNpcLoc = GetNpcLocation(npcId)
        if not playerLoc or not currentNpcLoc then return end

        local dist = Distance3D(playerLoc, currentNpcLoc)

        if dist <= ARRIVAL_THRESHOLD then
            print(string.format("%s Arrived at %s (dist=%.0f)", TAG, npcId, dist))
            PathNav.StopGuide()
            if _G.SocketClient and _G.SocketClient.send then
                pcall(function()
                    _G.SocketClient.send({
                        type = "guide_path_arrived",
                        npc_id = npcId,
                    })
                end)
            end
        else
            pcall(function() FireTrail(currentNpcLoc) end)
        end
    end)
end

--- Find the nearest NPC with an applied commitment and start guiding to them.
--- MUST be called from the game thread (callers wrap in ExecuteInGameThread).
function PathNav.GuideToNearest()
    local playerLoc = GetPlayerLocation()
    if not playerLoc then return end

    local nearestId = nil
    local nearestDist = math.huge

    for npcId, entry in pairs(_G.ActiveCommitments or {}) do
        if entry.applied then
            local npcLoc = GetNpcLocation(npcId)
            if npcLoc then
                local dist = Distance3D(playerLoc, npcLoc)
                if dist < nearestDist then
                    nearestDist = dist
                    nearestId = npcId
                end
            end
        end
    end

    if nearestId then
        PathNav.StartGuide(nearestId)
    else
        PathNav.StopGuide()
    end
end

--- Called when a commitment is released. If guiding to this NPC, switch to next nearest or stop.
--- @param npcId string Voice name
function PathNav.OnCommitmentReleased(npcId)
    local state = _G._PathNavState
    if not state.active or state.targetNpcId ~= npcId then return end
    print(string.format("%s Target %s released, switching", TAG, npcId))
    PathNav.GuideToNearest()
end

print("[PathNav] Loaded")
return PathNav
