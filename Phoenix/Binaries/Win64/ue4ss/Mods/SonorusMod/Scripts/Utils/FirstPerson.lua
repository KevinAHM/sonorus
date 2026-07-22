-- FirstPerson.lua - First-person view camera system for Sonorus
-- Disables third-person camera stacks and hides player model.
-- Automatically suspends during cutscenes and restores after.

local FirstPerson = {}

local BlueprintHelpers = require "Utils.BlueprintHelpers"
local SafeIsValid = BlueprintHelpers.SafeIsValid

local TAG = "[FirstPerson]"

-- Persistent state (survives F11 reload)
_G.FirstPersonState = _G.FirstPersonState or {
    enabled = false,        -- User wants FPV on
    active = false,         -- Currently in FPV (false during cutscenes even if enabled)
    suspendedBy = nil,      -- What suspended FPV ("cinematic", "conversation", etc.)
    transitioning = false,  -- True during fade transition (prevents double-toggle)
}

-- Fade settings
local FADE_DURATION = 0.3  -- seconds for each fade (to black / from black)
local FAST_FADE_DURATION = 0.15

-- VR detection: FPV makes no sense in VR (headset already is first-person)
local function isVR()
    return _G.VROffset ~= nil
end

local function isPlayerReady()
    return _G.SonorusState and _G.SonorusState.playerLoaded
end

-- ============================================
-- Camera stack control
-- ============================================

local CAMERA_STACK_CLASSES = {
    "BP_PitchToTransformCurves_Default_C",
    "BP_AmbientCamAnim_Idle_C",
    "BP_AmbientCamAnim_Jog_C",
    "BP_AmbientCamAnim_Sprint_C",
    "BP_AddCameraSpaceTranslation_OpenSpace_C",
    "BP_AddCameraSpaceTranslation_LookAt_C",
    "BP_AddCameraSpaceTranslation_Combat_C",
    "BP_AddCameraSpaceTranslation_MountCharge_C",
    "BP_AddCameraSpaceTranslation_Swimming_OpenSpace_C",
    "BP_AddCameraSpaceTranslation_Broom_Boost_New_C",
}

local function SetCameraStacksDisabled(disable)
    for _, className in ipairs(CAMERA_STACK_CLASSES) do
        local stacks = FindAllOf(className)
        if stacks then
            for _, stack in pairs(stacks) do
                pcall(function()
                    if SafeIsValid(stack) and stack.SetDisabled then
                        stack:SetDisabled(disable, true)
                    end
                end)
            end
        end
    end
end

-- CollisionPrediction: instead of disabling (which breaks its internal state),
-- we zero out ProbeSize to stop it detecting collisions, and restore on deactivate.
-- Saved defaults captured on first call.
_G.FirstPersonCollisionDefaults = _G.FirstPersonCollisionDefaults or nil

local function SetCollisionPredictionActive(active)
    local stacks = FindAllOf("CameraStackBehaviorCollisionPrediction")
    if not stacks then return end
    for _, stack in pairs(stacks) do
        pcall(function()
            if not SafeIsValid(stack) then return end
            if active then
                local defaults = _G.FirstPersonCollisionDefaults
                if defaults then
                    stack.ProbeSize = defaults.ProbeSize
                    stack.MinArmLengthLimit = defaults.MinArmLengthLimit
                end
            else
                if not _G.FirstPersonCollisionDefaults then
                    _G.FirstPersonCollisionDefaults = {
                        ProbeSize = stack.ProbeSize,
                        MinArmLengthLimit = stack.MinArmLengthLimit,
                    }
                end
                stack.ProbeSize = 0
                stack.MinArmLengthLimit = 0
            end
        end)
    end
end

-- ============================================
-- Screen fade transition
-- ============================================

local function GetCameraManager()
    local pc = FindFirstOf("PlayerController")
    if not pc or not SafeIsValid(pc) then return nil end
    local cam = pc.PlayerCameraManager
    if cam and SafeIsValid(cam) then return cam end
    return nil
end

local function GetFadeDuration()
    local mode = string.lower(tostring(_G.ConversationFPVTransition or "normal"))
    if mode == "off" then
        return 0
    end
    if mode == "fast" then
        return FAST_FADE_DURATION
    end
    return FADE_DURATION
end

--- Fade to black, run action, fade back in
local function FadeTransition(action)
    local fadeDuration = GetFadeDuration()
    if fadeDuration <= 0 then
        action()
        return
    end

    local cam = GetCameraManager()
    if not cam or not SafeIsValid(cam) then
        action()
        return
    end

    -- Fade to black (hold when finished so screen stays black)
    pcall(function()
        cam:StartCameraFade(0.0, 1.0, fadeDuration, {R = 0, G = 0, B = 0, A = 1}, false, true)
    end)

    local state = _G.FirstPersonState
    state.transitioning = true

    -- After fade-out completes, do the switch and fade back in.
    local delayMs = math.floor(fadeDuration * 1000) + 50
    ExecuteInGameThreadWithDelay(delayMs, function()
        action()

        -- Brief settle time, then fade back in.
        ExecuteInGameThreadWithDelay(50, function()
            pcall(function()
                local cam2 = GetCameraManager()
                if cam2 and SafeIsValid(cam2) then
                    cam2:StartCameraFade(1.0, 0.0, fadeDuration, {R = 0, G = 0, B = 0, A = 1}, false, false)
                end
            end)

            ExecuteInGameThreadWithDelay(math.floor(fadeDuration * 1000), function()
                _G.FirstPersonState.transitioning = false
            end)
        end)
    end)
end

-- ============================================
-- Screen-space aim helpers (shared with GetNearbyNPCs)
-- ============================================

--- Project a world position to screen pixel coords
---@param pc UObject PlayerController
---@param worldPos table {X, Y, Z}
---@param viewportX number Viewport width in pixels
---@param viewportY number Viewport height in pixels
---@return table|nil {x, y} in pixels, or nil if projection failed
function FirstPerson.projectToScreen(pc, worldPos, viewportX, viewportY)
    local result = nil
    pcall(function()
        local screenLoc = {}
        local didProject = pc:ProjectWorldLocationToScreen(worldPos, screenLoc, false)
        if didProject and screenLoc.X ~= nil and screenLoc.Y ~= nil then
            local x, y = screenLoc.X, screenLoc.Y
            if x >= 0 and x <= 1.0 and y >= 0 and y <= 1.0 then
                x = x * viewportX
                y = y * viewportY
            end
            result = { x = x, y = y }
        end
    end)
    return result
end

--- Get the screen-space aim center X and viewport info for the current camera.
--- In third-person the aim center is offset from viewport center.
--- Accepts optional camPos/camRot to avoid re-fetching when caller already has them.
---@param pc UObject PlayerController
---@param cam UObject PlayerCameraManager
---@param camPos table|nil Optional {X,Y,Z} camera position (fetched if nil)
---@param camRot table|nil Optional {Pitch,Yaw,Roll} camera rotation (fetched if nil)
---@return table {screenCenterX, viewportX, viewportY, camPos, camRot, yaw, rightX, rightY, aimOffsetPx}
function FirstPerson.getScreenAimInfo(pc, cam, camPos, camRot)
    if not camPos then pcall(function() camPos = cam:GetCameraLocation() end) end
    if not camRot then pcall(function() camRot = cam:GetCameraRotation() end) end
    if not camPos or not camRot then return nil end

    local viewportX, viewportY = 1920, 1080
    pcall(function()
        local size = pc:GetViewportSize()
        if size and size.X and size.Y and size.X > 0 and size.Y > 0 then
            viewportX, viewportY = size.X, size.Y
        end
    end)

    local pitch = math.rad(camRot.Pitch)
    local yaw = math.rad(camRot.Yaw)
    local fwd = {
        X = math.cos(pitch) * math.cos(yaw),
        Y = math.cos(pitch) * math.sin(yaw),
        Z = math.sin(pitch)
    }

    local aimProbe = {
        X = camPos.X + fwd.X * 100,
        Y = camPos.Y + fwd.Y * 100,
        Z = camPos.Z + fwd.Z * 100,
    }

    local viewportCenterX = viewportX * 0.5
    local aimScreenX = viewportCenterX
    local proj = FirstPerson.projectToScreen(pc, aimProbe, viewportX, viewportY)
    if proj then aimScreenX = proj.x end

    return {
        screenCenterX = aimScreenX,
        viewportCenterX = viewportCenterX,
        viewportX = viewportX,
        viewportY = viewportY,
        camPos = camPos,
        camRot = camRot,
        yaw = yaw,
        rightX = math.cos(yaw + math.pi / 2),
        rightY = math.sin(yaw + math.pi / 2),
        aimOffsetPx = aimScreenX - viewportCenterX,
    }
end

--- Check if a specific actor is horizontally centered on the camera aim point.
--- Same logic as the "looked at" check in GetNearbyNPCs (non-VR path).
--- Accepts optional npcLoc/npcHH to avoid re-fetching when caller already has them.
---@param pc UObject PlayerController
---@param actor UObject The actor to check
---@param aimInfo table From getScreenAimInfo()
---@param npcLoc table|nil Optional {X,Y,Z} actor location (fetched if nil)
---@param npcHH number|nil Optional capsule half height (fetched if nil)
---@return boolean
function FirstPerson.isActorOnAim(pc, actor, aimInfo, npcLoc, npcHH)
    if not actor or not SafeIsValid(actor) then return false end

    local onAim = false
    pcall(function()
        if not npcLoc then npcLoc = actor:K2_GetActorLocation() end
        if not npcLoc then return end

        -- Dot product check: actor must be in front of camera.
        -- ProjectWorldLocationToScreen mirrors behind-camera points to valid coords.
        local camPos = aimInfo.camPos
        local dx = npcLoc.X - camPos.X
        local dy = npcLoc.Y - camPos.Y
        local yaw = aimInfo.yaw
        local fwdX = math.cos(yaw)
        local fwdY = math.sin(yaw)
        local dot = dx * fwdX + dy * fwdY
        if dot <= 0 then return end

        if not npcHH then
            npcHH = 88
            pcall(function()
                local cap = actor.CapsuleComponent
                if cap and cap.CapsuleHalfHeight then npcHH = cap.CapsuleHalfHeight end
            end)
        end

        local vx, vy = aimInfo.viewportX, aimInfo.viewportY
        local feetProj = FirstPerson.projectToScreen(pc, {X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z}, vx, vy)
        local topProj = FirstPerson.projectToScreen(pc, {X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z + npcHH * 2}, vx, vy)
        if not feetProj or not topProj then return end

        local bandCenterX = (feetProj.x + topProj.x) * 0.5
        local bandMinY = math.min(feetProj.y, topProj.y)
        local bandMaxY = math.max(feetProj.y, topProj.y)
        local horizontalErrorPx = math.abs(bandCenterX - aimInfo.screenCenterX)
        local horizontalTolerancePx = math.abs(bandMaxY - bandMinY) * 0.25

        local onScreen =
            feetProj.x >= -vx and feetProj.x <= vx * 2 and
            topProj.x >= -vx and topProj.x <= vx * 2 and
            bandMaxY >= 0 and bandMinY <= vy

        onAim = onScreen and horizontalErrorPx <= horizontalTolerancePx
    end)
    return onAim
end

-- ============================================
-- Player visibility control
-- ============================================

-- player.Mesh = head/neck mesh; body + turban/headgear are children.
-- SetVisibility(false, true) propagates to all children = hides entire player.
local function SetPlayerVisible(visible)
    local player = FindFirstOf("Biped_Player")
    if not player or not SafeIsValid(player) then return end

    local mesh = player.Mesh
    if not mesh or not SafeIsValid(mesh) then return end

    pcall(function() mesh:SetVisibility(visible, true) end)
end

-- ============================================
-- Activate / Deactivate FPV
-- ============================================

local function ActivateFPV()
    local state = _G.FirstPersonState
    if state.active then return end

    SetCameraStacksDisabled(true)
    SetCollisionPredictionActive(false)
    SetPlayerVisible(false)
    state.active = true
    print(TAG .. " Activated")
end

local function DeactivateFPV()
    local state = _G.FirstPersonState
    if not state.active then return end

    SetCameraStacksDisabled(false)
    SetCollisionPredictionActive(true)
    SetPlayerVisible(true)
    state.active = false
    print(TAG .. " Deactivated")
end

-- ============================================
-- Public API
-- ============================================

--- Toggle first-person view on/off
function FirstPerson.toggle()
    if isVR() then print(TAG .. " Disabled in VR mode") return end
    if not isPlayerReady() then return end
    local state = _G.FirstPersonState
    if state.transitioning then return end

    state.enabled = not state.enabled
    state.suspendedBy = nil
    print(TAG .. " " .. (state.enabled and "ENABLED" or "DISABLED"))

    FadeTransition(function()
        if state.enabled then
            ActivateFPV()
        else
            DeactivateFPV()
        end
    end)
end

--- Enable first-person view
function FirstPerson.enable()
    if isVR() then print(TAG .. " Disabled in VR mode") return end
    if not isPlayerReady() then return end
    local state = _G.FirstPersonState
    if state.enabled then return end
    if state.transitioning then return end

    state.enabled = true
    state.suspendedBy = nil
    print(TAG .. " ENABLED")

    FadeTransition(function()
        ActivateFPV()
    end)
end

--- Disable first-person view
function FirstPerson.disable()
    if isVR() then return end
    if not isPlayerReady() then return end
    local state = _G.FirstPersonState
    if not state.enabled then return end
    if state.transitioning then return end

    state.enabled = false
    state.suspendedBy = nil
    print(TAG .. " DISABLED")

    if state.active then
        FadeTransition(function()
            DeactivateFPV()
        end)
    end
end

--- Suspend FPV temporarily (e.g. during cutscene or mounting)
---@param reason string Why FPV is being suspended (e.g. "cinematic")
---@param instant boolean|nil If true, skip fade transition (default: false)
function FirstPerson.suspend(reason, instant)
    local state = _G.FirstPersonState
    if not state.enabled then return end
    if not state.active then return end

    state.suspendedBy = reason or "unknown"

    if instant then
        DeactivateFPV()
    else
        FadeTransition(function()
            DeactivateFPV()
        end)
    end
    print(TAG .. " Suspended (" .. state.suspendedBy .. ")")
end

--- Resume FPV after suspension - only if it was suspended and user still wants it
---@param reason string|nil Only resume if suspended by this reason (nil = resume regardless)
---@param instant boolean|nil If true, skip fade transition (default: false)
function FirstPerson.resume(reason, instant)
    if isVR() then return end
    local state = _G.FirstPersonState
    if not state.enabled then return end
    if state.active then return end
    if reason and state.suspendedBy ~= reason then return end

    state.suspendedBy = nil

    if instant then
        ActivateFPV()
    else
        FadeTransition(function()
            ActivateFPV()
        end)
    end
    print(TAG .. " Resumed")
end

--- Check if FPV is enabled (user wants it on)
function FirstPerson.isEnabled()
    return _G.FirstPersonState.enabled
end

--- Check if FPV is currently active (not suspended)
function FirstPerson.isActive()
    return _G.FirstPersonState.active
end

--- Point camera at an actor (one-shot, mouse takes over after hold expires)
---@param actor UObject The actor to look at
function FirstPerson.lookAt(actor)
    if isVR() then return end
    if not actor or not SafeIsValid(actor) then return end

    -- Skip if actor is in motion.
    local speed = 0
    pcall(function()
        local vel = actor:GetVelocity()
        if vel then speed = math.sqrt(vel.X * vel.X + vel.Y * vel.Y + vel.Z * vel.Z) end
    end)
    if speed > 1 then return end

    local ok, err = pcall(function()
        local pc = FindFirstOf("Biped_PlayerController")
        if not pc or not SafeIsValid(pc) then error("no Biped_PlayerController") end
        local cam = pc.PlayerCameraManager
        if not cam or not SafeIsValid(cam) then error("no CameraManager") end

        local aimInfo = FirstPerson.getScreenAimInfo(pc, cam)
        if not aimInfo then error("could not get aim info") end

        local npcLoc = actor:K2_GetActorLocation()
        local npcHH = 88
        pcall(function()
            local capsule = actor.CapsuleComponent
            if capsule and capsule.CapsuleHalfHeight then npcHH = capsule.CapsuleHalfHeight end
        end)

        if FirstPerson.isActorOnAim(pc, actor, aimInfo, npcLoc, npcHH) then
            print(TAG .. " LookAt: already looking at target, skipping")
            return
        end

        if _G.FirstPersonState.active then
            local targetPos = {X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z + npcHH * 0.5}

            -- Horizontal correction: project NPC + shifted point to get pixels-per-unit scale.
            local vx = aimInfo.viewportX
            local baseProj = FirstPerson.projectToScreen(pc, npcLoc, vx, aimInfo.viewportY)
            local shiftDist = 100
            local shiftedProj = FirstPerson.projectToScreen(pc,
                {X = npcLoc.X + aimInfo.rightX * shiftDist, Y = npcLoc.Y + aimInfo.rightY * shiftDist, Z = npcLoc.Z},
                vx, aimInfo.viewportY)

            if baseProj and shiftedProj and math.abs(shiftedProj.x - baseProj.x) > 1 then
                local pixelsPerUnit = math.abs(shiftedProj.x - baseProj.x) / shiftDist
                local worldOffset = aimInfo.aimOffsetPx / pixelsPerUnit
                targetPos.X = targetPos.X + aimInfo.rightX * worldOffset
                targetPos.Y = targetPos.Y + aimInfo.rightY * worldOffset
                print(TAG .. string.format(" LookAt: offset=%.0fpx worldOff=%.1f", aimInfo.aimOffsetPx, worldOffset))
            end

            pc:SetCameraLookAt_TimeBased(targetPos, 2.5)
        else
            -- Third person: let the game handle actor targeting natively.
            pc:SetCameraLookAt_ActorAndTime(actor, 2.5)
        end
        print(TAG .. " LookAt: started")
    end)
    if not ok then
        print(TAG .. " LookAt failed: " .. tostring(err))
    end
end

--- Re-apply FPV state after hot reload (F11)
--- Camera stacks reset on reload, so re-apply if FPV was active
function FirstPerson.onReload()
    if isVR() then return end
    local state = _G.FirstPersonState
    if state.enabled and state.active then
        -- State says active but camera stacks were reset by reload.
        state.active = false
        ActivateFPV()
        print(TAG .. " Re-applied after reload")
    end
end

return FirstPerson
