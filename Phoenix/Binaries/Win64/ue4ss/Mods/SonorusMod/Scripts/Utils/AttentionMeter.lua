-- Attention meter: per-NPC gaze accumulator
-- Uses a single line trace from camera forward to detect looked-at NPC (cheap).
-- Charges while player looks at an NPC within close range, drains when not.
-- When charge reaches 1.0, sends attention:threshold event to Python.

local Utils = require("Utils.Utils")

local AttentionMeter = {}

-- Constants
local CHARGE_RATE = 0.20              -- per tick (1s) while looking
local DRAIN_RATE = 0.08               -- per tick (1s) while not looking
local WARM_BASELINE = 0.4             -- reset value after firing
local RANGE = 140                     -- max distance in UE units for cold approach
local RANGE_FPV = 175                 -- 25% larger range for first-person / VR cold approach
local RANGE_CONTINUATION = 280        -- double range for continuation (recently spoke with NPC)
local FIRE_COOLDOWN = 120             -- seconds between fires for same NPC
local PLAYER_BODY_ANGLE = 70          -- max player body angle to NPC (third-person only)
local NPC_AWARENESS_ANGLE = 90        -- third-person parity with ambient gaze trigger
local NPC_AWARENESS_ANGLE_FPV = 90    -- first-person/VR parity with ambient gaze trigger
local MEET_GAZE_RANGE = 220           -- close-range ambient eye contact outside conversations
local MEET_GAZE_NPC_ANGLE = 90        -- NPC must already be roughly facing the player
local RECENT_CONVERSATION_WINDOW = 120 -- seconds after conversation_finished to treat as continuation
local AMBIENT_COOLDOWN = 10            -- seconds to suppress attention charging after ambient line captured

-- Persisted across F11 reloads
_G.AttentionMeters = _G.AttentionMeters or {}
_G.LastConversationEnd = _G.LastConversationEnd or {}  -- voiceId -> os.clock() when conversation truly finished

-- Module-local cooldown state (not exposed as globals)
local ambientDialogueCooldowns = {}    -- voiceId -> os.clock() when ambient line was captured

local function isFPVOrVR()
    local isFPV = _G.FirstPersonState and _G.FirstPersonState.active
    local isVR = _G.VRCamRot ~= nil
    return isFPV or isVR
end

local function getColdRange()
    return isFPVOrVR() and RANGE_FPV or RANGE
end

function AttentionMeter.Update()
    local function StopAmbientAttentionGaze(reason)
        if StopAmbientGaze then
            pcall(function() StopAmbientGaze(reason or "attention guard") end)
        end
    end

    -- Master toggle from settings (default true until settings sync arrives)
    if _G.AttentionMeterEnabled == false then
        StopAmbientAttentionGaze()
        return
    end

    -- Freeze: don't charge/drain during active conversation, input, stealth, or pause
    if _G.SonorusState and _G.SonorusState.phase and _G.SonorusState.phase ~= "idle" then
        -- Freeze attention meters during conversation flow, but don't tear down
        -- ambient gaze here; the turn handoff logic adopts or releases it precisely.
        return
    end
    if _G.ChatInputState and _G.ChatInputState.active then
        StopAmbientAttentionGaze("attention guard: chat input")
        return
    end
    if _G.ChatPreviewLock or _G.STTPreviewLock then
        StopAmbientAttentionGaze("attention guard: preview lock")
        return
    end
    if Utils and Utils.IsGamePaused and Utils.IsGamePaused() then
        StopAmbientAttentionGaze("attention guard: paused")
        return
    end
    if _G.PlayerIdleState then
        StopAmbientAttentionGaze("attention guard: player idle")
        return
    end

    local staticData = GetStaticCache()
    local player = staticData and staticData.player
    if not player then
        StopAmbientAttentionGaze("attention guard: no player")
        return
    end

    if (_G.MountState and _G.MountState.mounted)
        or (_G.CombatState and _G.CombatState.active)
        or IsInCinematicState(player)
    then
        StopAmbientAttentionGaze("attention guard: mount/combat/cinematic")
        return
    end

    local playerPos = nil
    pcall(function() playerPos = player:K2_GetActorLocation() end)
    if not playerPos then
        StopAmbientAttentionGaze("attention guard: no player position")
        return
    end

    -- Check stealth
    local inStealth = false
    pcall(function() inStealth = player.InStealthMode or false end)
    if inStealth then
        StopAmbientAttentionGaze("attention guard: stealth")
        return
    end

    -- Single line trace from camera forward to find looked-at NPC
    -- Matches the working UE4SS line trace pattern exactly
    local hitNpcName = nil
    local hitNpcActor = nil
    local hitDist = 99999
    -- VR debug info (captured inside pcall, displayed after)
    local vrDbg = nil
    pcall(function()
        local pc = staticData.playerController
        if not pc then return end
        local cam = pc.PlayerCameraManager
        if not cam then return end

        local KismetSystem = staticData.kismetSystem
        local KismetMath = staticData.kismetMath
        if not KismetSystem or not KismetMath then return end

        -- VR: trace from player head using HMD rotation; Flat: trace from camera
        local vrCam = _G.VRCamRot
        local StartVector, EndVector
        if vrCam then
            -- Use actual camera position (tracks HMD in VR), direction from VRCamRot
            StartVector = cam:GetCameraLocation()
            local sx, sy, sz = StartVector.X, StartVector.Y, StartVector.Z
            local pitch = math.rad(vrCam.Pitch)
            local yaw = math.rad(vrCam.Yaw)
            local fwd = {
                X = math.cos(pitch) * math.cos(yaw),
                Y = math.cos(pitch) * math.sin(yaw),
                Z = math.sin(pitch)
            }
            local traceDist = 2000.0
            EndVector = KismetMath:MakeVector(sx + fwd.X * traceDist, sy + fwd.Y * traceDist, sz + fwd.Z * traceDist)
            -- Capture VR debug info
            -- vrDbg = {
            --     pitch = vrCam.Pitch, yaw = vrCam.Yaw,
            --     startX = sx, startY = sy, startZ = sz,
            --     fwdX = fwd.X, fwdY = fwd.Y, fwdZ = fwd.Z,
            --     endX = sx + fwd.X * traceDist, endY = sy + fwd.Y * traceDist, endZ = sz + fwd.Z * traceDist,
            -- }
        else
            StartVector = cam:GetCameraLocation()
            local camRot = cam:GetCameraRotation()
            local AddValue = KismetMath:Multiply_VectorInt(KismetMath:GetForwardVector(camRot), 2000.0)
            EndVector = KismetMath:Add_VectorVector(StartVector, AddValue)
        end

        local TraceColor = { R = 0, G = 0, B = 0, A = 0 }

        local HitResult = {}
        local WasHit = KismetSystem:LineTraceSingle(
            player, StartVector, EndVector,
            8,              -- Channel 8: hits NPC pawns
            false, { player }, 0, HitResult, true,
            TraceColor, TraceColor, 0.0
        )

        if not WasHit then
            if vrDbg then vrDbg.wasHit = false end
            return
        end

        -- Get hit actor from the winning channel's HitResult
        local hitActor = nil
        local hitClassName = "?"
        pcall(function()
            local a = HitResult.Actor
            if a then
                local obj = nil
                pcall(function() obj = a:Get() end)
                hitActor = obj or a
            end
        end)
        -- Capture hit actor class for debug
        if hitActor then
            pcall(function()
                hitClassName = hitActor:GetClass():GetFName():ToString()
            end)
        end

        -- Capture trace hit distance from HitResult
        local traceDist2D = nil
        pcall(function()
            if HitResult.Distance then
                traceDist2D = HitResult.Distance
            end
        end)

        if vrDbg then
            vrDbg.wasHit = true
            vrDbg.hitClass = hitClassName
            vrDbg.hitTraceDist = traceDist2D
        end

        if not hitActor then
            if vrDbg then vrDbg.noActor = true end
            return
        end

        -- Get voice ID and check significance
        local npcId = Utils.GetActorVoiceId(hitActor, staticData)
        if vrDbg then
            vrDbg.voiceId = npcId or "nil"
            vrDbg.isSignificant = npcId and IsSignificantNPC(npcId) or false
        end
        if not npcId or not IsSignificantNPC(npcId) then return end

        -- Calculate distance from player
        local npcLoc = hitActor:K2_GetActorLocation()
        local dx = npcLoc.X - playerPos.X
        local dy = npcLoc.Y - playerPos.Y
        local dist = math.sqrt(dx * dx + dy * dy)

        hitNpcName = npcId
        hitNpcActor = hitActor
        hitDist = dist
    end)

    -- VR debug overlay (dev mode only)
    if vrDbg and _G.SonorusDevMode and ShowHint then
        local lines = {}
        table.insert(lines, string.format("VR TRACE P=%.1f Y=%.1f", vrDbg.pitch, vrDbg.yaw))
        table.insert(lines, string.format("Start: %.0f,%.0f,%.0f (cam)", vrDbg.startX, vrDbg.startY, vrDbg.startZ))
        table.insert(lines, string.format("Fwd: %.2f,%.2f,%.2f", vrDbg.fwdX, vrDbg.fwdY, vrDbg.fwdZ))
        if vrDbg.wasHit == false then
            table.insert(lines, "HIT: NOTHING (ch8 miss)")
        elseif vrDbg.noActor then
            table.insert(lines, "HIT: actor=nil")
        else
            table.insert(lines, string.format("HIT: %s d=%.0f", vrDbg.hitClass or "?", vrDbg.hitTraceDist or -1))
            table.insert(lines, string.format("Voice: %s sig=%s", vrDbg.voiceId or "nil", tostring(vrDbg.isSignificant)))
        end
        if hitNpcName then
            table.insert(lines, string.format("NPC: %s dist=%.0f", hitNpcName, hitDist))
        end
        ShowHint(table.concat(lines, "\n"), 0.4)
    end

    local now = os.clock()
    local ambientShouldLook = false
    if hitNpcActor and hitDist <= MEET_GAZE_RANGE then
        pcall(function()
            local npcPos = hitNpcActor:K2_GetActorLocation()
            local npcRot = hitNpcActor:K2_GetActorRotation()
            local npcAngle = Utils.GetAngleToTarget(npcPos, npcRot, playerPos)
            ambientShouldLook = npcAngle <= MEET_GAZE_NPC_ANGLE
        end)
    end

    if ambientShouldLook and StartAmbientGaze then
        pcall(function() StartAmbientGaze(hitNpcActor, player) end)
    end

    -- Charge the hit NPC (if valid), drain all others
    for name, meter in pairs(_G.AttentionMeters) do
        local shouldCharge = false

        if name == hitNpcName and hitNpcActor then
            local lastConvEnd = _G.LastConversationEnd[name]
            local isContinuation = lastConvEnd and (now - lastConvEnd) < RECENT_CONVERSATION_WINDOW

            -- Skip cold approaches when disabled in settings (shouldCharge stays false -> meter drains)
            local coldBlocked = not isContinuation and _G.AttentionColdApproachEnabled == false

            local effectiveRange = isContinuation and RANGE_CONTINUATION or getColdRange()
            local ambientCooldownActive = ambientDialogueCooldowns[name] and (now - ambientDialogueCooldowns[name]) < AMBIENT_COOLDOWN

            if not coldBlocked and not ambientCooldownActive and hitDist <= effectiveRange then
                local maxAngle = isFPVOrVR() and NPC_AWARENESS_ANGLE_FPV or NPC_AWARENESS_ANGLE
                pcall(function()
                    local npcPos = hitNpcActor:K2_GetActorLocation()
                    local npcRot = hitNpcActor:K2_GetActorRotation()
                    local npcAngle = Utils.GetAngleToTarget(npcPos, npcRot, playerPos)
                    if npcAngle <= maxAngle then
                        if isFPVOrVR() then
                            shouldCharge = true
                        else
                            local playerRot = player:K2_GetActorRotation()
                            local playerAngle = Utils.GetAngleToTarget(playerPos, playerRot, npcPos)
                            shouldCharge = playerAngle <= PLAYER_BODY_ANGLE
                        end
                    end
                end)
            end
        end

        if shouldCharge then
            meter.charge = math.min(1.0, meter.charge + CHARGE_RATE)
        else
            meter.charge = math.max(0.0, meter.charge - DRAIN_RATE)
        end

        -- Check threshold
        if meter.charge >= 1.0 then
            if (now - meter.lastFiredAt) >= FIRE_COOLDOWN then
                meter.charge = WARM_BASELINE
                meter.lastFiredAt = now
                if SocketClient and SocketClient.send then
                    SocketClient.send({
                        type = "game_event",
                        event = "attention:threshold",
                        data = {
                            voiceId = name,
                            distance = math.floor(hitDist)
                        }
                    })
                    DevPrint("[Attention] Fired threshold for " .. name)
                end
            else
                meter.charge = 0.99
            end
        end

        -- Clean up fully drained meters
        if meter.charge <= 0 and (now - meter.lastFiredAt) > FIRE_COOLDOWN then
            _G.AttentionMeters[name] = nil
        end
    end

    -- If the hit NPC doesn't have a meter yet, create one and start charging
    -- Skip if this would be a cold approach and cold approach is disabled
    -- Must also be within effective range (trace reaches 2000 but range is much shorter)
    if hitNpcName and not _G.AttentionMeters[hitNpcName] then
        local lastConvEnd = _G.LastConversationEnd[hitNpcName]
        local isContinuation = lastConvEnd and (now - lastConvEnd) < RECENT_CONVERSATION_WINDOW
        local createRange = isContinuation and RANGE_CONTINUATION or getColdRange()
        local ambientCooldownActive = ambientDialogueCooldowns[hitNpcName] and (now - ambientDialogueCooldowns[hitNpcName]) < AMBIENT_COOLDOWN
        if not ambientCooldownActive and hitDist <= createRange and (isContinuation or _G.AttentionColdApproachEnabled ~= false) then
            _G.AttentionMeters[hitNpcName] = { charge = CHARGE_RATE, lastFiredAt = -math.huge }
        end
    end

    -- Dev mode: show ASCII meter for looked-at NPC
    if false and _G.SonorusDevMode and hitNpcName and ShowHint then
        local meter = _G.AttentionMeters[hitNpcName]
        local ambCooldownEnd = ambientDialogueCooldowns[hitNpcName]
        local ambCooldownLeft = ambCooldownEnd and math.max(0, AMBIENT_COOLDOWN - (now - ambCooldownEnd)) or 0
        if meter then
            local barLen = 20
            local filled = math.floor(meter.charge * barLen)
            local bar = string.rep("|", filled) .. string.rep(".", barLen - filled)
            local pct = math.floor(meter.charge * 100)
            local devLastConv = _G.LastConversationEnd[hitNpcName]
            local devIsCont = devLastConv and (now - devLastConv) < RECENT_CONVERSATION_WINDOW
            local devRange = devIsCont and RANGE_CONTINUATION or getColdRange()
            local inRange = hitDist <= devRange
            local cooldownLeft = math.max(0, FIRE_COOLDOWN - (now - meter.lastFiredAt))
            local npcFacing = true
            local playerFacing = true
            if playerPos and hitNpcActor then
                pcall(function()
                    local npcPos = hitNpcActor:K2_GetActorLocation()
                    local npcRot = hitNpcActor:K2_GetActorRotation()
                    npcFacing = Utils.GetAngleToTarget(npcPos, npcRot, playerPos) <= NPC_AWARENESS_ANGLE
                    if not isFPVOrVR() then
                        local playerRot = player:K2_GetActorRotation()
                        playerFacing = Utils.GetAngleToTarget(playerPos, playerRot, npcPos) <= PLAYER_BODY_ANGLE
                    end
                end)
            end
            local status = ""
            if ambCooldownLeft > 0 then
                status = " AMB:" .. math.floor(ambCooldownLeft) .. "s"
            elseif cooldownLeft > 0 then
                status = " CD:" .. math.floor(cooldownLeft) .. "s"
            elseif not inRange then
                status = " OUT"
            elseif not npcFacing then
                status = " AWAY"
            elseif not playerFacing then
                status = " BODY"
            end
            ShowHint(string.format("[%s] %d%% %s%s", bar, pct, hitNpcName, status), 0.4)
        elseif ambCooldownLeft > 0 then
            -- Meter was drained/cleaned up but ambient cooldown still active
            local bar = string.rep(".", 20)
            ShowHint(string.format("[%s]  0%% %s AMB:%.0fs", bar, hitNpcName, ambCooldownLeft), 0.4)
        end
    end
end

-- Called when an ambient dialogue line is captured and not blocked for this NPC.
-- Zeros their charge (lets Update drain/cleanup naturally) and suppresses charging for AMBIENT_COOLDOWN seconds.
function AttentionMeter.OnAmbientDialogue(voiceId)
    if not voiceId or voiceId == "" then return end
    if _G.AttentionMeters[voiceId] then
        _G.AttentionMeters[voiceId].charge = 0
    end
    ambientDialogueCooldowns[voiceId] = os.clock()
    DevPrint("[Attention] Ambient cooldown set for " .. voiceId)
end

return AttentionMeter
