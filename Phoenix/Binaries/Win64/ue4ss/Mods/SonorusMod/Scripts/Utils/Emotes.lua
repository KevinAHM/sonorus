-- Emotes.lua - Facial expression system for Sonorus
-- Drives non-mouth morph targets (brows, eyes, cheeks, nose) alongside lip sync

---@class Emotes
local Emotes = {}

local BlueprintHelpers = require "Utils.BlueprintHelpers"

-- ============================================
-- Emote Presets
-- ============================================
-- Each preset maps morph target names to their target values (0-1).
-- Only non-mouth targets here to avoid fighting the lip sync pipeline.

Emotes.PRESETS = {
    happy = {
        cheek_raise_l = 0.62, cheek_raise_r = 0.62,
        eye_squint_l = 0.24, eye_squint_r = 0.24,
        eye_sqz_l = 0.08, eye_sqz_r = 0.08,
        eye_low_blink_l = 0.14, eye_low_blink_r = 0.14,
        Squinch = 0.4,
        dimple_l = 0.36, dimple_r = 0.36,
        brows_up_in_l = 0.12, brows_up_in_r = 0.12,
    },
    content = {
        cheek_raise_l = 0.28, cheek_raise_r = 0.28,
        eye_squint_l = 0.1, eye_squint_r = 0.1,
        eye_low_blink_l = 0.08, eye_low_blink_r = 0.08,
        eye_low_open_l = 0.12, eye_low_open_r = 0.12,
        Squinch = 0.24,
        dimple_l = 0.16, dimple_r = 0.16,
        brows_up_in_l = 0.06, brows_up_in_r = 0.06,
    },
    tired = {
        eye_blink_l = 0.18, eye_blink_r = 0.18,
        eye_low_blink_l = 0.34, eye_low_blink_r = 0.34,
        eye_squint_l = 0.12, eye_squint_r = 0.12,
        eye_bag_l = 0.18, eye_bag_r = 0.18,
        brows_dn_out_l = 0.16, brows_dn_out_r = 0.16,
        brows_up_in_l = 0.06, brows_up_in_r = 0.06,
        Squinch = 0.12,
    },
    fond = {
        cheek_raise_l = 0.34, cheek_raise_r = 0.34,
        eye_squint_l = 0.14, eye_squint_r = 0.14,
        eye_low_blink_l = 0.12, eye_low_blink_r = 0.12,
        dimple_l = 0.22, dimple_r = 0.22,
        brows_up_in_l = 0.14, brows_up_in_r = 0.14,
        Squinch = 0.1,
    },
    shy = {
        cheek_raise_l = 0.14, cheek_raise_r = 0.14,
        eye_squint_l = 0.18, eye_squint_r = 0.18,
        eye_low_blink_l = 0.18, eye_low_blink_r = 0.18,
        dimple_l = 0.08, dimple_r = 0.08,
        brows_up_in_l = 0.28, brows_up_in_r = 0.28,
        brows_dn_out_l = 0.16, brows_dn_out_r = 0.16,
        frown_l = 0.08, frown_r = 0.08,
        chin_up = 0.12,
        Squinch = 0.04,
    },
    beam = {
        cheek_raise_l = 0.52, cheek_raise_r = 0.52,
        eye_squint_l = 0.2, eye_squint_r = 0.2,
        eye_sqz_l = 0.06, eye_sqz_r = 0.06,
        eye_low_blink_l = 0.16, eye_low_blink_r = 0.16,
        dimple_l = 0.3, dimple_r = 0.3,
        brows_up_in_l = 0.18, brows_up_in_r = 0.18,
        Squinch = 0.18,
    },
    proud = {
        cheek_raise_l = 0.28, cheek_raise_r = 0.28,
        eye_squint_l = 0.12, eye_squint_r = 0.12,
        eye_low_open_l = 0.14, eye_low_open_r = 0.14,
        dimple_l = 0.24, dimple_r = 0.24,
        brows_up_out_l = 0.22, brows_up_out_r = 0.22,
        Squinch = 0.18,
    },
    sad = {
        brows_up_in_l = 0.72, brows_up_in_r = 0.72,
        brows_dn_out_l = 0.28, brows_dn_out_r = 0.28,
        brows_bow_l = 0.22, brows_bow_r = 0.22,
        eye_sqz_l = 0.16, eye_sqz_r = 0.16,
        frown_l = 0.34, frown_r = 0.34,
        chin_up = 0.16,
    },
    angry = {
        brows_dn_in_l = 0.72, brows_dn_in_r = 0.72,
        brows_squez_l = 0.24, brows_squez_r = 0.24,
        brows_dn_out_l = 0.14, brows_dn_out_r = 0.14,
        brow_scrunch = 0.42,
        nose_sneer = 0.16,
        eye_sqz_l = 0.24, eye_sqz_r = 0.24,
        eye_squint_l = 0.12, eye_squint_r = 0.12,
    },
    annoyed = {
        brows_dn_in_l = 0.38, brows_dn_in_r = 0.38,
        brows_dn_out_l = 0.08, brows_dn_out_r = 0.08,
        brow_scrunch = 0.16,
        eye_squint_l = 0.2, eye_squint_r = 0.2,
        eye_sqz_l = 0.06, eye_sqz_r = 0.06,
    },
    surprised = {
        brows_up_in_l = 0.66, brows_up_in_r = 0.66,
        brows_up_out_l = 0.7, brows_up_out_r = 0.7,
        eye_open_l = 0.56, eye_open_r = 0.56,
        eye_low_open_l = 0.26, eye_low_open_r = 0.26,
    },
    confused = {
        brows_up_out_l = 0.52, brows_up_out_r = 0.36,
        brows_dn_in_l = 0.18, brows_dn_in_r = 0.18,
        brows_squez_l = 0.16, brows_squez_r = 0.16,
        eye_squint_l = 0.16, eye_squint_r = 0.16,
        eye_sqz_l = 0.06, eye_sqz_r = 0.06,
        Squinch = 0.08,
    },
    cringe = {
        brows_up_in_l = 0.2, brows_up_in_r = 0.2,
        brows_dn_out_l = 0.14, brows_dn_out_r = 0.14,
        eye_squint_l = 0.24, eye_squint_r = 0.24,
        eye_sqz_l = 0.14, eye_sqz_r = 0.14,
        cheek_raise_l = 0.14, cheek_raise_r = 0.14,
        sneer_l = 0.18, sneer_r = 0.1,
        nose_sneer = 0.08,
        Squinch = 0.12,
    },
    concerned = {
        brows_up_in_l = 0.5, brows_up_in_r = 0.5,
        brows_squez_up_l = 0.22, brows_squez_up_r = 0.22,
        brows_dn_out_l = 0.12, brows_dn_out_r = 0.12,
        eye_squint_l = 0.18, eye_squint_r = 0.18,
        eye_low_open_l = 0.08, eye_low_open_r = 0.08,
    },
    sympathy = {
        brows_up_in_l = 0.42, brows_up_in_r = 0.42,
        brows_bow_l = 0.18, brows_bow_r = 0.18,
        eye_squint_l = 0.12, eye_squint_r = 0.12,
        eye_sqz_l = 0.06, eye_sqz_r = 0.06,
        cheek_raise_l = 0.12, cheek_raise_r = 0.12,
        chin_up = 0.24,
    },
    amused = {
        cheek_raise_l = 0.55, cheek_raise_r = 0.55,
        dimple_l = 0.4, dimple_r = 0.4,
        eye_squint_l = 0.28, eye_squint_r = 0.28,
        eye_sqz_l = 0.08, eye_sqz_r = 0.08,
        brows_up_out_l = 0.12, brows_up_out_r = 0.12,
    },
    embarrassed = {
        brows_up_in_l = 0.42, brows_up_in_r = 0.42,
        brows_dn_out_l = 0.12, brows_dn_out_r = 0.12,
        eye_squint_l = 0.22, eye_squint_r = 0.22,
        cheek_raise_l = 0.16, cheek_raise_r = 0.16,
        dimple_l = 0.12, dimple_r = 0.12,
        frown_l = 0.12, frown_r = 0.12,
    },
    relieved = {
        brows_up_in_l = 0.14, brows_up_in_r = 0.14,
        brows_up_out_l = 0.08, brows_up_out_r = 0.08,
        eye_squint_l = 0.14, eye_squint_r = 0.14,
        eye_low_open_l = 0.12, eye_low_open_r = 0.12,
        cheek_raise_l = 0.2, cheek_raise_r = 0.2,
        dimple_l = 0.14, dimple_r = 0.14,
    },
    curious = {
        brows_up_out_l = 0.5, brows_up_out_r = 0.5,
        brows_up_in_l = 0.22, brows_up_in_r = 0.22,
        eye_open_l = 0.22, eye_open_r = 0.22,
        eye_low_open_l = 0.12, eye_low_open_r = 0.12,
        eye_squint_l = 0.06, eye_squint_r = 0.06,
    },
    determined = {
        brows_dn_in_l = 0.55, brows_dn_in_r = 0.55,
        brows_dn_out_l = 0.12, brows_dn_out_r = 0.12,
        brow_scrunch = 0.3,
        eye_squint_l = 0.22, eye_squint_r = 0.22,
        eye_sqz_l = 0.08, eye_sqz_r = 0.08,
        nose_sneer = 0.08,
    },
    mischievous = {
        brows_up_out_l = 0.5,  -- sly asymmetry
        brows_dn_in_r = 0.28,
        cheek_raise_l = 0.32, cheek_raise_r = 0.16,
        dimple_l = 0.42, dimple_r = 0.16,
        eye_squint_r = 0.22,
        eye_sqz_r = 0.08,
    },
    smug = {
        brows_up_out_l = 0.62,  -- haughty lifted brow
        brows_dn_in_r = 0.24,
        brows_squez_r = 0.1,
        cheek_raise_l = 0.34, cheek_raise_r = 0.12,
        dimple_l = 0.48, dimple_r = 0.08,
        eye_squint_r = 0.18,
        eye_sqz_r = 0.06,
        Squinch = 0.1,
    },
    disgusted = {
        nose_sneer = 0.72,
        nose_open_l = 0.4, nose_open_r = 0.4,
        brows_dn_in_l = 0.5, brows_dn_in_r = 0.5,
        brows_squez_l = 0.14, brows_squez_r = 0.14,
        brow_scrunch = 0.12,
        sneer_l = 0.42, sneer_r = 0.28,
        eye_sqz_l = 0.36, eye_sqz_r = 0.36,
        eye_squint_l = 0.14, eye_squint_r = 0.14,
        Squinch = 0.08,
    },
    skeptical = {
        brows_up_out_l = 0.78,  -- one brow raised
        brows_dn_in_r = 0.22,   -- other brow lowered
        brows_squez_r = 0.08,
        eye_squint_r = 0.18,
        eye_sqz_r = 0.03,
        Squinch = 0.03,
    },
    afraid = {
        brows_up_in_l = 0.82, brows_up_in_r = 0.82,
        brows_up_out_l = 0.28, brows_up_out_r = 0.28,
        brows_squez_up_l = 0.42, brows_squez_up_r = 0.42,
        brows_dn_in_l = 0.16, brows_dn_in_r = 0.16,
        eye_open_l = 0.4, eye_open_r = 0.4,
        eye_low_open_l = 0.24, eye_low_open_r = 0.24,
        eye_low_blink_l = 0.1, eye_low_blink_r = 0.1,
        eye_sqz_l = 0.16, eye_sqz_r = 0.16,
    },
}
-- Alias: LLM prompt uses [fearful], map to afraid preset
Emotes.PRESETS.fearful = Emotes.PRESETS.afraid

-- Collect all morph target names used across all presets (for reset)
local _allEmoteMorphTargets = {}
do
    local seen = {}
    for _, preset in pairs(Emotes.PRESETS) do
        for name, _ in pairs(preset) do
            if not seen[name] then
                seen[name] = true
                table.insert(_allEmoteMorphTargets, name)
            end
        end
    end
end

-- ============================================
-- State (persists across F11 via _G)
-- ============================================

function Emotes.init()
    _G.EmoteState = _G.EmoteState or {}
    local es = _G.EmoteState
    es.active = es.active or false
    es.presetName = es.presetName or nil
    es.targets = es.targets or {}       -- target morph values {name = value}
    es.current = es.current or {}       -- current (lerped) morph values
    es.intensity = es.intensity or 1.0
    es.fadeIn = es.fadeIn or 0.3         -- seconds
    es.fadeOut = es.fadeOut or 0.5       -- seconds
    es.startTime = es.startTime or 0
    es.stopTime = es.stopTime or nil    -- set when fading out
    es.actor = es.actor or nil          -- the actor to animate
end

-- ============================================
-- Core API
-- ============================================

--- Start an emote on a specific actor
---@param actor userdata The actor to animate
---@param presetName string Name of the preset (e.g. "happy")
---@param intensity number|nil Intensity multiplier (default 1.0)
---@param fadeIn number|nil Fade-in time in seconds (default 0.3)
---@param fadeOut number|nil Fade-out time in seconds (default 0.5)
function Emotes.Play(actor, presetName, intensity, fadeIn, fadeOut)
    if not actor then
        print("[Emotes] Play error: No actor")
        return false
    end

    local preset = Emotes.PRESETS[presetName]
    if not preset then
        print("[Emotes] Play error: Unknown preset '" .. tostring(presetName) .. "'")
        return false
    end

    Emotes.init()
    local es = _G.EmoteState

    -- If already playing, reset current values for smooth transition
    -- (don't snap — the lerp will handle the transition)

    es.active = true
    es.presetName = presetName
    es.intensity = intensity or 1.0
    es.fadeIn = fadeIn or 0.3
    es.fadeOut = fadeOut or 0.5
    es.startTime = os.clock()
    es.stopTime = nil
    es.actor = actor
    es.actorFullName = nil
    pcall(function() es.actorFullName = actor:GetFullName() end)
    _G.CurrentEmoteName = presetName
    _G.CurrentEmoteActor = actor
    _G.CurrentEmoteGazeSide = (math.random(0, 1) == 0) and -1 or 1

    -- Build target table (preset values * intensity)
    es.targets = {}
    for name, value in pairs(preset) do
        es.targets[name] = value * es.intensity
    end

    -- Initialize current values for any new morph targets (keep existing for smooth transitions)
    es.current = es.current or {}
    for name, _ in pairs(es.targets) do
        if not es.current[name] then
            es.current[name] = 0
        end
    end

    print(string.format("[Emotes] Playing '%s' (intensity=%.1f, fadeIn=%.1fs)", presetName, es.intensity, es.fadeIn))
    return true
end

--- Stop the current emote (smooth fade-out)
function Emotes.Stop()
    local es = _G.EmoteState
    if not es or not es.active then return end

    es.stopTime = os.clock()
    print(string.format("[Emotes] Stopping '%s' (fadeOut=%.1fs)", tostring(es.presetName), es.fadeOut))
end

--- Force-reset all emote morph targets on an actor
function Emotes.ForceReset(actor)
    local es = _G.EmoteState
    local actorFullName = nil
    if actor then
        pcall(function() actorFullName = actor:GetFullName() end)
    end
    if es and es.actorFullName and actorFullName and es.actorFullName == actorFullName then
        es.active = false
        es.presetName = nil
        es.targets = {}
        es.current = {}
        es.stopTime = nil
        es.actor = nil
        es.actorFullName = nil
        _G.CurrentEmoteName = nil
        _G.CurrentEmoteActor = nil
        _G.CurrentEmoteGazeSide = nil
    end

    if not actor then return end
    BlueprintHelpers.CallClearMorphTargets(actor, _allEmoteMorphTargets)
end

-- ============================================
-- Animation Tick (driven by OnTick in logic.lua)
-- ============================================

function Emotes._Tick()
    local es = _G.EmoteState
    if not es or not es.active then return end

    -- Need a valid actor ref / captured id context
    if not es.actor or not es.actorFullName then
        Emotes._Finish()
        return
    end

    local now = os.clock()

    -- Calculate envelope (fade in / fade out multiplier)
    local envelope = 1.0

    if es.stopTime then
        -- Fading out
        local fadeElapsed = now - es.stopTime
        if es.fadeOut > 0 then
            envelope = 1.0 - math.min(fadeElapsed / es.fadeOut, 1.0)
        else
            envelope = 0
        end

        if envelope <= 0 then
            Emotes._Finish()
            return
        end
    else
        -- Fading in
        local elapsed = now - es.startTime
        if es.fadeIn > 0 then
            envelope = math.min(elapsed / es.fadeIn, 1.0)
        end
    end

    -- Lerp current values toward targets (or toward 0 if fading out)
    local lerpSpeed = 0.25  -- smooth but responsive at 40Hz
    local allNearZero = true

    for name, _ in pairs(es.current) do
        local target = (es.targets[name] or 0) * envelope
        es.current[name] = es.current[name] + (target - es.current[name]) * lerpSpeed

        if math.abs(es.current[name]) > 0.005 then
            allNearZero = false
        end
    end

    -- Apply all morph targets through the ModActor Blueprint bridge.
    BlueprintHelpers.CallSetMorphTargets(es.actor, es.current)

    -- Old keys from previous emotes lerp toward 0 naturally (targets[name] is nil -> 0)

    -- If fading out and all values near zero, finish
    if es.stopTime and allNearZero then
        Emotes._Finish()
    end
end

function Emotes._Finish()
    local es = _G.EmoteState
    if not es then return end

    -- Force-reset morph targets
    if es.actor then
        Emotes.ForceReset(es.actor)
    end

    es.active = false
    es.presetName = nil
    es.targets = {}
    es.current = {}
    es.stopTime = nil
    es.actor = nil
    _G.CurrentEmoteName = nil
    _G.CurrentEmoteActor = nil
    _G.CurrentEmoteGazeSide = nil
    print("[Emotes] Finished")
end

return Emotes
