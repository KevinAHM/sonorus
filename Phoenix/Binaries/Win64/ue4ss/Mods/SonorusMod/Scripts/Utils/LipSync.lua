-- LipSync.lua - Viseme-based lip sync system for Sonorus
-- Handles viseme loading, interpolation, and blendshape animation

---@class LipSync
local LipSync = {}

-- Dependencies
local BlueprintHelpers = require "Utils.BlueprintHelpers"
local FileIO = require "Utils.FileIO"

-- Debug throttle variables (module-local)
local _lastLipsyncDebugTime = 0
local _lastDetailedLipsyncLog = 0

-- ============================================
-- State Initialization
-- ============================================

--- Initialize viseme data state (call once, persists across F11 reloads via _G)
function LipSync.init()
    _G.VisemeData = _G.VisemeData or {}
    local vd = _G.VisemeData
    vd.startTime = vd.startTime or 0
    vd.localStartTime = vd.localStartTime or 0
    vd.frames = vd.frames or {}
    vd.loaded = vd.loaded or false
    vd.lastContentLen = vd.lastContentLen or 0
    vd.currentJaw = vd.currentJaw or 0
    vd.currentSmile = vd.currentSmile or 0
    vd.currentFunnel = vd.currentFunnel or 0
    vd.currentPress = vd.currentPress or 0
    vd.currentLipUp = vd.currentLipUp or 0
    vd.currentEE = vd.currentEE or 0
    vd.currentO = vd.currentO or 0
    vd.currentShh = vd.currentShh or 0
    vd.lastReadTime = vd.lastReadTime or 0
    vd.syncPrinted = vd.syncPrinted or false

    -- State flags
    _G.CloseLipsComplete = _G.CloseLipsComplete or false
    _G.LastSpeakerActorForClosing = _G.LastSpeakerActorForClosing or nil
    _G.CloseLipsIterations = _G.CloseLipsIterations or 0
end

-- ============================================
-- Viseme Data Loading
-- ============================================

--- Get interpolated viseme values at a given elapsed time
---@param elapsed number Elapsed time in seconds
---@return table viseme {jaw, smile, funnel} interpolated values
function LipSync.GetVisemeAtTime(elapsed)
    local data = _G.VisemeData
    if not data.loaded or #data.frames == 0 then
        return {jaw = 0, smile = 0, funnel = 0, press = 0, lip_up = 0, ee = 0, o_shape = 0, shh = 0}
    end

    local frames = data.frames

    -- Before first frame
    if elapsed <= frames[1].t then
        return frames[1]
    end

    -- After last frame
    if elapsed >= frames[#frames].t then
        return frames[#frames]
    end

    -- Find surrounding frames and interpolate
    for i = 1, #frames - 1 do
        if elapsed >= frames[i].t and elapsed < frames[i + 1].t then
            local f1 = frames[i]
            local f2 = frames[i + 1]
            local alpha = (elapsed - f1.t) / (f2.t - f1.t)

            return {
                jaw = f1.jaw + (f2.jaw - f1.jaw) * alpha,
                smile = f1.smile + (f2.smile - f1.smile) * alpha,
                funnel = f1.funnel + (f2.funnel - f1.funnel) * alpha,
                press = (f1.press or 0) + ((f2.press or 0) - (f1.press or 0)) * alpha,
                lip_up = (f1.lip_up or 0) + ((f2.lip_up or 0) - (f1.lip_up or 0)) * alpha,
                ee = (f1.ee or 0) + ((f2.ee or 0) - (f1.ee or 0)) * alpha,
                o_shape = (f1.o_shape or 0) + ((f2.o_shape or 0) - (f1.o_shape or 0)) * alpha,
                shh = (f1.shh or 0) + ((f2.shh or 0) - (f1.shh or 0)) * alpha,
            }
        end
    end

    return frames[#frames]
end

--- Get detailed frame info for diagnostic logging
---@param elapsed number Elapsed time in seconds
---@return table frameInfo {index, total, t, jaw}
function LipSync.GetCurrentFrameInfo(elapsed)
    local data = _G.VisemeData
    if not data.loaded or #data.frames == 0 then
        return { index = 0, total = 0, t = 0, jaw = 0 }
    end

    local frames = data.frames

    -- Before first frame
    if elapsed <= frames[1].t then
        return { index = 1, total = #frames, t = frames[1].t, jaw = frames[1].jaw }
    end

    -- After last frame
    if elapsed >= frames[#frames].t then
        return { index = #frames, total = #frames, t = frames[#frames].t, jaw = frames[#frames].jaw }
    end

    -- Find current frame
    for i = 1, #frames - 1 do
        if elapsed >= frames[i].t and elapsed < frames[i + 1].t then
            return { index = i, total = #frames, t = frames[i].t, jaw = frames[i].jaw }
        end
    end

    return { index = #frames, total = #frames, t = frames[#frames].t, jaw = frames[#frames].jaw }
end

-- ============================================
-- Blendshape Animation
-- ============================================

--- Force reset all lip blendshapes on an actor (instant, no smooth transition)
---@param actor userdata The actor to reset
function LipSync.ForceResetBlendshapes(actor)
    if not actor then return end
    local valid = false
    pcall(function() valid = actor:IsValid() end)
    if not valid then return end

    -- Single batched call to reset all lip sync blendshapes to zero
    BlueprintHelpers.CallSetBlendshapes(actor, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    -- Reset new morph targets directly
    pcall(function()
        local mesh = actor.Mesh
        if mesh then
            mesh:SetMorphTarget(FName("mouth_press"), 0, false)
            mesh:SetMorphTarget(FName("upr_lip_up_l"), 0, false)
            mesh:SetMorphTarget(FName("upr_lip_up_r"), 0, false)
            mesh:SetMorphTarget(FName("ee"), 0, false)
            mesh:SetMorphTarget(FName("o"), 0, false)
            mesh:SetMorphTarget(FName("shh"), 0, false)
        end
    end)
end

--- Animate lips on the current speaker actor
--- Uses GetCurrentSpeakerActor and GetSonorusModActor from logic.lua (via _G)
function LipSync.AnimateLips()
    -- Get actor from logic.lua (via _G)
    local GetCurrentSpeakerActor = _G.GetCurrentSpeakerActor
    if not GetCurrentSpeakerActor then
        return
    end

    local actor = GetCurrentSpeakerActor()
    if not actor then
        -- Debug: log if actor is nil (throttled)
        if (os.clock() - _lastLipsyncDebugTime) > 2 then
            _lastLipsyncDebugTime = os.clock()
            print("[AnimateLips] No actor from GetCurrentSpeakerActor")
        end
        return
    end

    -- Store actor for CloseLips (survives cache clears)
    _G.LastSpeakerActorForClosing = actor

    -- Cache modActor once for all blendshape calls (avoid 8+ lookups)
    local modActor = BlueprintHelpers.GetSonorusModActor()
    if not modActor then
        print("[AnimateLips] No modActor from GetSonorusModActor")
        return
    end

    local data = _G.VisemeData

    -- Target values
    local targetJaw, targetSmile, targetFunnel = 0, 0, 0
    local targetPress, targetLipUp, targetEE, targetO, targetShh = 0, 0, 0, 0, 0

    -- If no visemes yet, use fallback animation
    if not data.loaded or #data.frames == 0 then
        -- Fallback: simple sine wave
        local t = os.clock()
        targetJaw = 0.4 * math.abs(math.sin(2 * math.pi * 0.8 * t))
    else
        -- Calculate elapsed time since audio start
        -- If paused (soft interrupt), use frozen position instead of advancing clock
        local elapsed
        if data.pausedAt then
            elapsed = data.pausedAt
        else
            -- Apply audioOffset for drift correction (from audio_sync messages)
            elapsed = os.clock() - data.localStartTime + (data.audioOffset or 0)
        end

        -- Get interpolated viseme from timeline
        local v = LipSync.GetVisemeAtTime(elapsed)

        -- Detailed timing diagnostic (every 500ms)
        if _G.SonorusDevMode and (os.clock() - _lastDetailedLipsyncLog) > 0.5 then
            _lastDetailedLipsyncLog = os.clock()
            local frameInfo = LipSync.GetCurrentFrameInfo(elapsed)
            _G.DevPrint(string.format(
                "[LipsyncTiming] elapsed=%.3fs, frame=%d/%d, frameT=%.3fs, frameJaw=%.2f, sysTime=%.3f",
                elapsed,
                frameInfo.index,
                frameInfo.total,
                frameInfo.t,
                frameInfo.jaw,
                os.clock()
            ))
        end

        -- Simple scaling
        targetJaw = v.jaw * 2.5
        targetSmile = v.smile * 1.0
        targetFunnel = v.funnel * 1.0
        targetPress = (v.press or 0) * 1.0
        targetLipUp = (v.lip_up or 0) * 1.0
        targetEE = (v.ee or 0) * 1.0
        targetO = (v.o_shape or 0) * 1.0
        targetShh = (v.shh or 0) * 1.0
    end

    -- Smooth lerp toward target (higher = snappier)
    -- Adjusted for 40Hz tick rate (25ms interval)
    local lerpSpeed = 0.4
    local fastLerp = 0.7  -- snappier for brief consonants (press, lip_up, shh)
    data.currentJaw = data.currentJaw + (targetJaw - data.currentJaw) * lerpSpeed
    data.currentSmile = data.currentSmile + (targetSmile - data.currentSmile) * lerpSpeed
    data.currentFunnel = data.currentFunnel + (targetFunnel - data.currentFunnel) * lerpSpeed
    data.currentPress = data.currentPress + (targetPress - data.currentPress) * fastLerp
    data.currentLipUp = data.currentLipUp + (targetLipUp - data.currentLipUp) * fastLerp
    data.currentEE = data.currentEE + (targetEE - data.currentEE) * lerpSpeed
    data.currentO = data.currentO + (targetO - data.currentO) * lerpSpeed
    data.currentShh = data.currentShh + (targetShh - data.currentShh) * fastLerp

    -- Apply blendshapes with per-character scale
    local scale = data.scale or 1.0
    local jaw = data.currentJaw * scale
    local smile = data.currentSmile * scale
    local funnel = data.currentFunnel * scale

    -- Debug: log jaw value periodically
    if _G.SonorusDevMode and (os.clock() - _lastLipsyncDebugTime) > 1 then
        _lastLipsyncDebugTime = os.clock()
        local scaleStr = scale ~= 1.0 and string.format(" (scale=%.2f)", scale) or ""
        _G.DevPrint(string.format("[Lipsync] jaw=%.2f, frames=%d, loaded=%s%s", jaw, #data.frames, tostring(data.loaded), scaleStr))
    end

    -- Single batched call for existing lip sync blendshapes (jaw, smile, funnel)
    BlueprintHelpers.CallSetBlendshapes(
        actor,
        jaw,                -- jaw_drop
        smile,              -- smile_l
        smile,              -- smile_r
        funnel,             -- lwr_lip_funl_l
        funnel,             -- lwr_lip_funl_r
        funnel * 0.7,       -- upr_lip_funl_l
        funnel * 0.7,       -- upr_lip_funl_r
        jaw * 0.3,          -- lwr_lip_dn_l
        jaw * 0.3,          -- lwr_lip_dn_r
        modActor
    )

    -- Apply new morph targets directly via mesh:SetMorphTarget
    local mesh = nil
    pcall(function() mesh = actor.Mesh end)
    if mesh then
        pcall(function()
            local press = data.currentPress * scale
            local lipUp = data.currentLipUp * scale
            local ee = data.currentEE * scale
            local o = data.currentO * scale
            local shh = data.currentShh * scale
            mesh:SetMorphTarget(FName("mouth_press"), press, false)
            mesh:SetMorphTarget(FName("upr_lip_up_l"), lipUp, false)
            mesh:SetMorphTarget(FName("upr_lip_up_r"), lipUp, false)
            mesh:SetMorphTarget(FName("ee"), ee, false)
            mesh:SetMorphTarget(FName("o"), o, false)
            mesh:SetMorphTarget(FName("shh"), shh, false)
        end)
    end
end

--- Smoothly close lips over multiple frames (call in a loop until complete)
--- Uses stored LastSpeakerActorForClosing
function LipSync.CloseLips()
    -- Timeout protection: force complete after ~1.5 seconds (30 ticks at 50ms)
    _G.CloseLipsIterations = (_G.CloseLipsIterations or 0) + 1
    if _G.CloseLipsIterations >= 30 then
        print("[Sonorus] CloseLips: Timeout - forcing completion")
        local actor = _G.LastSpeakerActorForClosing
        if actor then
            LipSync.ForceResetBlendshapes(actor)
        end
        -- Call ResetNearbyNPCLips via _G (defined in logic.lua)
        if _G.ResetNearbyNPCLips then
            _G.ResetNearbyNPCLips()
        end
        _G.CloseLipsComplete = true
        _G.CloseLipsIterations = 0
        _G.LastSpeakerActorForClosing = nil
        -- Reset viseme data
        LipSync.ResetVisemeData()
        LipSync.SignalTurnComplete()
        return
    end

    -- Use stored actor from AnimateLips - DO NOT call GetCurrentSpeakerActor()
    -- because currentTurnId may have already changed to the next speaker (pre-buffering)
    local actor = _G.LastSpeakerActorForClosing
    if actor then
        local valid = false
        pcall(function() valid = actor:IsValid() end)
        if not valid then
            actor = nil
            _G.LastSpeakerActorForClosing = nil
        end
    end

    if not actor then
        print("[Sonorus] CloseLips: No actor found - resetting all nearby NPC lips")
        -- Call ResetNearbyNPCLips via _G (defined in logic.lua)
        if _G.ResetNearbyNPCLips then
            _G.ResetNearbyNPCLips()
        end
        _G.CloseLipsComplete = true
        _G.LastSpeakerActorForClosing = nil
        LipSync.SignalTurnComplete()
        return
    end

    -- Smoothly close mouth over several frames instead of snapping
    -- Adjusted for 40Hz tick rate (25ms interval)
    local data = _G.VisemeData
    local closeSpeed = 0.17

    -- Lerp current values toward 0
    data.currentJaw = data.currentJaw * (1 - closeSpeed)
    data.currentSmile = data.currentSmile * (1 - closeSpeed)
    data.currentFunnel = data.currentFunnel * (1 - closeSpeed)
    data.currentPress = (data.currentPress or 0) * (1 - closeSpeed)
    data.currentLipUp = (data.currentLipUp or 0) * (1 - closeSpeed)
    data.currentEE = (data.currentEE or 0) * (1 - closeSpeed)
    data.currentO = (data.currentO or 0) * (1 - closeSpeed)
    data.currentShh = (data.currentShh or 0) * (1 - closeSpeed)

    -- Apply per-character scale (same as AnimateLips)
    local scale = data.scale or 1.0
    local jaw = data.currentJaw * scale
    local smile = data.currentSmile * scale
    local funnel = data.currentFunnel * scale

    -- Apply smoothed + scaled values (existing blendshapes)
    BlueprintHelpers.CallSetBlendshapes(actor, jaw, smile, smile, funnel, funnel, funnel * 0.7, funnel * 0.7, jaw * 0.3, jaw * 0.3)

    -- Apply new morph targets
    pcall(function()
        local mesh = actor.Mesh
        if mesh then
            mesh:SetMorphTarget(FName("mouth_press"), (data.currentPress or 0) * scale, false)
            mesh:SetMorphTarget(FName("upr_lip_up_l"), (data.currentLipUp or 0) * scale, false)
            mesh:SetMorphTarget(FName("upr_lip_up_r"), (data.currentLipUp or 0) * scale, false)
            mesh:SetMorphTarget(FName("ee"), (data.currentEE or 0) * scale, false)
            mesh:SetMorphTarget(FName("o"), (data.currentO or 0) * scale, false)
            mesh:SetMorphTarget(FName("shh"), (data.currentShh or 0) * scale, false)
        end
    end)

    -- If values are near zero, fully reset and signal done
    local maxNew = math.max(data.currentPress or 0, data.currentLipUp or 0, data.currentEE or 0, data.currentO or 0, data.currentShh or 0)
    if data.currentJaw < 0.01 and data.currentSmile < 0.01 and data.currentFunnel < 0.01 and maxNew < 0.01 then
        LipSync.ForceResetBlendshapes(actor)
        -- Reset viseme data for next conversation
        LipSync.ResetVisemeData()
        _G.CloseLipsComplete = true
        _G.LastSpeakerActorForClosing = nil
        LipSync.SignalTurnComplete()
        print("[Sonorus] CloseLips: Complete")
    end
    -- Still closing - flag remains false
end

--- Reset viseme data for next conversation
function LipSync.ResetVisemeData()
    local data = _G.VisemeData
    data.loaded = false
    data.syncPrinted = false
    data.frames = {}
    data.localStartTime = 0
    data.lastContentLen = 0
    data.currentJaw = 0
    data.currentSmile = 0
    data.currentFunnel = 0
    data.currentPress = 0
    data.currentLipUp = 0
    data.currentEE = 0
    data.currentO = 0
    data.currentShh = 0
end

--- Signal Python that turn's mouth animation is complete
function LipSync.SignalTurnComplete()
    if _G.SocketClient and _G.SocketClient.send then
        _G.SocketClient.send({ type = "turn_complete" })
    end
end

-- ============================================
-- Reset Nearby NPC Lips
-- ============================================
-- This function needs GetStaticCache and GetNearbyNPCs from logic.lua
-- Accesses them via _G at call time

--- Reset lips on player and all nearby NPCs (for F8 reset / stuck blendshapes fix)
function LipSync.ResetNearbyNPCLips()
    print("[Sonorus] Resetting lips on player and nearby NPCs...")

    -- Wrap in pcall to catch and log any errors
    local ok, err = pcall(function()
        -- Collect valid actors (using set to dedupe)
        local actorSet = {}
        local actors = {}

        local function addActor(actor, source)
            if not actor then return end
            local valid = false
            pcall(function() valid = actor:IsValid() end)
            if valid and not actorSet[actor] then
                actorSet[actor] = true
                table.insert(actors, actor)
            end
        end

        -- PRIORITY: Add the last speaker actor first (the one most likely to have stuck lips)
        local lastSpeaker = _G.LastSpeakerActorForClosing
        if lastSpeaker then
            addActor(lastSpeaker, "lastSpeaker")
        end

        -- Add player actor (from static cache)
        local GetStaticCache = _G.GetStaticCache
        if GetStaticCache then
            local staticData = GetStaticCache()
            local player = staticData and staticData.player
            if player then
                addActor(player, "player")
            end
        end

        -- Add nearby NPCs
        local GetNearbyNPCs = _G.GetNearbyNPCs
        if GetNearbyNPCs then
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                for _, entry in ipairs(npcResult.nearbyList) do
                    addActor(entry.actor, "nearby")
                end
            end
        end

        if #actors == 0 then
            print("[Sonorus] No valid actors found for lip reset")
            return
        end

        -- Loop reset over multiple frames to overcome Blueprint lerping
        local iterations = 0
        local maxIterations = 20
        local resetHandle
        resetHandle = LoopInGameThreadAfterFrames(1, function()
            iterations = iterations + 1
            for _, actor in ipairs(actors) do
                LipSync.ForceResetBlendshapes(actor)
            end
            if iterations >= maxIterations then
                CancelDelayedAction(resetHandle)
                print("[Sonorus] Reset blendshapes on " .. #actors .. " actors (complete)")
            end
        end)
        print("[Sonorus] Started lip reset loop for " .. #actors .. " actors")
    end)

    if not ok then
        print("[Sonorus] ERROR in ResetNearbyNPCLips: " .. tostring(err))
    end
end

return LipSync
