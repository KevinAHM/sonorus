-- PresenceWatcher.lua
-- Samples loaded named NPCs and reports enter/update/leave band changes.

local PresenceWatcher = {}

local TAG = "[PresenceWatcher]"

local SAMPLE_INTERVAL_MS = 5000
local NEAR_DIST = 2000.0
local EYESHOT_DIST = 6000.0
local EYESHOT_COS = math.cos(math.rad(30))
local RESEGMENT_DIST = 3000.0
local LEAVE_GRACE_TICKS = 2

local _deps = nil
local _tracked = {}
local _running = false
local _loopHandle = nil

function PresenceWatcher.Init(deps)
    _deps = deps
end

local function SafeCall(fn)
    local ok, res = pcall(fn)
    if ok then return res end
    return nil
end

local function IsValidActor(actor)
    if not actor then return false end
    if _deps and _deps.safeIsValid then
        return _deps.safeIsValid(actor)
    end
    return SafeCall(function() return actor:IsValid() end) == true
end

local function SampleOnce()
    if not _deps then return end
    local staticData = _deps.getStaticData()
    if not staticData then return end

    local player = staticData.player
    local playerLoc = nil
    local playerKey = nil
    if IsValidActor(player) then
        playerLoc = SafeCall(function() return player:K2_GetActorLocation() end)
        playerKey = SafeCall(function() return player:GetFullName() end)
    end

    local camLoc, camRot = nil, nil
    local cam = staticData.cameraManager
    if IsValidActor(cam) then
        camLoc = SafeCall(function() return cam:GetCameraLocation() end)
        camRot = SafeCall(function() return cam:GetCameraRotation() end)
    end

    local forward = nil
    if camRot then
        local pitch = math.rad(camRot.Pitch)
        local yaw = math.rad(camRot.Yaw)
        forward = {
            X = math.cos(pitch) * math.cos(yaw),
            Y = math.cos(pitch) * math.sin(yaw),
            Z = math.sin(pitch),
        }
    end

    local seen = {}
    local changes = {}
    local npcs = _deps.getCachedNPCs() or {}

    for _, npc in pairs(npcs) do
        if IsValidActor(npc) then
            local npcKey = SafeCall(function() return npc:GetFullName() end)
            local voiceId = SafeCall(function()
                return _deps.getVoiceId(npc, staticData)
            end)
            if (not playerKey or not npcKey or npcKey ~= playerKey)
                    and voiceId and voiceId ~= "Unknown"
                    and _deps.isSignificant(voiceId) then
                local loc = SafeCall(function() return npc:K2_GetActorLocation() end)
                if loc then
                    seen[voiceId] = true
                    local near, eyes = false, false
                    if playerLoc then
                        local dx = loc.X - playerLoc.X
                        local dy = loc.Y - playerLoc.Y
                        local dz = loc.Z - playerLoc.Z
                        local dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                        near = dist <= NEAR_DIST
                        if forward and camLoc and dist <= EYESHOT_DIST then
                            local tx = loc.X - camLoc.X
                            local ty = loc.Y - camLoc.Y
                            local tz = loc.Z - camLoc.Z
                            local mag = math.sqrt(tx * tx + ty * ty + tz * tz)
                            if mag > 0 then
                                local dot = (forward.X * tx + forward.Y * ty + forward.Z * tz) / mag
                                eyes = dot >= EYESHOT_COS
                            end
                        end
                    end

                    local prev = _tracked[voiceId]
                    if not prev then
                        _tracked[voiceId] = {
                            x = loc.X, y = loc.Y, z = loc.Z,
                            near = near, eyes = eyes, missedTicks = 0,
                        }
                        table.insert(changes, {
                            id = voiceId, ev = "enter",
                            x = loc.X, y = loc.Y, z = loc.Z,
                            near = near, eyes = eyes,
                        })
                    else
                        prev.missedTicks = 0
                        local mdx = loc.X - prev.x
                        local mdy = loc.Y - prev.y
                        local mdz = loc.Z - prev.z
                        local moved = math.sqrt(mdx * mdx + mdy * mdy + mdz * mdz)
                        if near ~= prev.near or eyes ~= prev.eyes or moved > RESEGMENT_DIST then
                            prev.x, prev.y, prev.z = loc.X, loc.Y, loc.Z
                            prev.near, prev.eyes = near, eyes
                            table.insert(changes, {
                                id = voiceId, ev = "update",
                                x = loc.X, y = loc.Y, z = loc.Z,
                                near = near, eyes = eyes,
                            })
                        end
                    end
                end
            end
        end
    end

    for voiceId, state in pairs(_tracked) do
        if not seen[voiceId] then
            state.missedTicks = (state.missedTicks or 0) + 1
            if state.missedTicks >= LEAVE_GRACE_TICKS then
                _tracked[voiceId] = nil
                table.insert(changes, { id = voiceId, ev = "leave" })
            end
        end
    end

    if #changes > 0 then
        local time = _deps.getGameDateTime() or {}
        _deps.send({
            type = "presence_update",
            gameDate = time.gameDate or "",
            gameTime = time.gameTime or "",
            changes = changes,
        })
        if _G.PresenceDebug then
            local tracked = 0
            for _ in pairs(_tracked) do tracked = tracked + 1 end
            ShowHint(string.format("Presence: %d tracked, %d changes", tracked, #changes))
        end
    end
end

function PresenceWatcher.OnPlayerReady()
    local flags = _G.PresenceLedgerPhaseFlags or {}
    if flags.presenceWatcher ~= true then
        PresenceWatcher.Stop()
        print(TAG .. " disabled by phase gate\n")
        return
    end
    if not _deps then
        print(TAG .. " OnPlayerReady before Init; skipping\n")
        return
    end
    if _running then
        -- A new handshake usually means the Python server reconnected. Force
        -- fresh enter messages so its in-memory/open-interval state catches up.
        _tracked = {}
        print(TAG .. " resync requested\n")
        return
    end
    _running = true
    _loopHandle = LoopInGameThreadWithDelay(SAMPLE_INTERVAL_MS, function()
        local ok, err = pcall(SampleOnce)
        if not ok then
            print(TAG .. " sample error: " .. tostring(err) .. "\n")
        end
    end)
    print(TAG .. " started\n")
end

function PresenceWatcher.Stop()
    if _loopHandle then
        CancelDelayedAction(_loopHandle)
        _loopHandle = nil
    end
    _running = false
    _tracked = {}
end

return PresenceWatcher
