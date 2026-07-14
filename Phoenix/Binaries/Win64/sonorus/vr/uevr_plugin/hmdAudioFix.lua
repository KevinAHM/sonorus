-- hmdAudioFix.lua
-- Standalone HMD Audio Fix for UEVR
-- Drop into any game's scripts/ folder — no hard dependencies.
--
-- Unreal Engine defaults the audio listener to the right-controller
-- MotionControllerComponent, causing audio spatialization to be wrong in VR.
-- This script overrides the audio listener to follow the headset instead.
--
-- If a controllers module (libs/controllers) is already loaded and has
-- created an HMD component (hand index 2), that component is reused.
-- Otherwise, a new Actor+SceneComponent is spawned and registered as the HMD.

local hmd_actor    = nil
local hmd_comp     = nil
local hmd_external = false  -- true when reusing another module's component
local last_level   = nil

-- Cached UClass/default-object references (found once, reused)
local actor_class   = nil
local scene_class   = nil
local statics       = nil
local game_engine   = nil

-- Reusable struct objects (allocated once)
local temp_transform = nil
local temp_vec       = nil
local temp_rot       = nil

-- How often (seconds) to re-apply the override as a safety-net.
-- Level-change detection runs every tick regardless.
local SAFETY_INTERVAL = 1.0
local safety_timer    = 0

---------------------------------------------------------------------------
-- Helpers
---------------------------------------------------------------------------

local function get_world()
    if game_engine == nil then
        local ec = uevr.api:find_uobject("Class /Script/Engine.GameEngine")
        if ec == nil then return nil end
        game_engine = UEVR_UObjectHook.get_first_object_by_class(ec, false)
    end
    if game_engine == nil then return nil end
    local vp = game_engine.GameViewport
    return vp and vp.World or nil
end

local function is_valid(obj)
    return obj ~= nil and UEVR_UObjectHook.exists(obj)
end

local function ensure_hmd_state(comp)
    if not is_valid(comp) then return nil end
    if UEVR_UObjectHook.get_or_add_motion_controller_state == nil then return nil end

    local state = UEVR_UObjectHook.get_or_add_motion_controller_state(comp)
    if state == nil then return nil end

    state:set_hand(2)
    state:set_permanent(true)
    return state
end

local function zero_transform()
    if temp_transform == nil then
        local tc = uevr.api:find_uobject("ScriptStruct /Script/CoreUObject.Transform")
        if tc == nil then return nil end
        temp_transform = StructObject.new(tc)
    end
    temp_transform.Translation   = Vector3f.new(0, 0, 0)
    temp_transform.Rotation.X    = 0
    temp_transform.Rotation.Y    = 0
    temp_transform.Rotation.Z    = 0
    temp_transform.Rotation.W    = 1
    temp_transform.Scale3D       = Vector3f.new(1, 1, 1)
    return temp_transform
end

local function zero_vec()
    if temp_vec == nil then
        local vc = uevr.api:find_uobject("ScriptStruct /Script/CoreUObject.Vector")
        if vc == nil then return nil end
        temp_vec = StructObject.new(vc)
    end
    temp_vec.X = 0; temp_vec.Y = 0; temp_vec.Z = 0
    return temp_vec
end

local function zero_rot()
    if temp_rot == nil then
        local rc = uevr.api:find_uobject("ScriptStruct /Script/CoreUObject.Rotator")
        if rc == nil then return nil end
        temp_rot = StructObject.new(rc)
    end
    temp_rot.Pitch = 0; temp_rot.Yaw = 0; temp_rot.Roll = 0
    return temp_rot
end

---------------------------------------------------------------------------
-- Find existing HMD component from controllers module (if loaded)
---------------------------------------------------------------------------

local function find_existing_hmd()
    local ctrl = package.loaded["libs/controllers"]
    if ctrl and ctrl.getController then
        if ctrl.createController then
            pcall(function()
                ctrl.createController(2)
            end)
        end
        local comp = ctrl.getController(2)
        if ensure_hmd_state(comp) ~= nil then
            return comp
        end
    end
    return nil
end

---------------------------------------------------------------------------
-- HMD actor lifecycle (fallback — only used when no external component)
---------------------------------------------------------------------------

local function destroy_own_hmd()
    if is_valid(hmd_comp) then
        pcall(function()
            if UEVR_UObjectHook.remove_motion_controller_state then
                UEVR_UObjectHook.remove_motion_controller_state(hmd_comp)
            end
        end)
    end
    if is_valid(hmd_actor) then
        pcall(function()
            if hmd_actor.K2_DestroyActor then hmd_actor:K2_DestroyActor() end
        end)
    end
    hmd_actor    = nil
    hmd_comp     = nil
    hmd_external = false
end

local function create_own_hmd()
    local world = get_world()
    if world == nil then return false end

    -- Find classes once
    if actor_class == nil then
        actor_class = uevr.api:find_uobject("Class /Script/Engine.Actor")
    end
    if scene_class == nil then
        scene_class = uevr.api:find_uobject("Class /Script/Engine.SceneComponent")
    end
    if statics == nil then
        local sc = uevr.api:find_uobject("Class /Script/Engine.GameplayStatics")
        if sc then statics = sc:get_class_default_object() end
    end
    if actor_class == nil or scene_class == nil or statics == nil then return false end

    local t = zero_transform()
    if t == nil then return false end

    -- Spawn a bare actor to own the component
    local actor = statics:BeginDeferredActorSpawnFromClass(world, actor_class, t, 1, nil)
    if actor == nil then return false end
    statics:FinishSpawningActor(actor, t)

    -- Add a SceneComponent and register it as the HMD (hand index 2)
    local comp = actor:AddComponentByClass(scene_class, true, t, false)
    if comp == nil then
        pcall(function() actor:K2_DestroyActor() end)
        return false
    end

    local state = ensure_hmd_state(comp)
    if state == nil then
        pcall(function() actor:K2_DestroyActor() end)
        return false
    end

    hmd_actor    = actor
    hmd_comp     = comp
    hmd_external = false
    return true
end

---------------------------------------------------------------------------
-- Acquire HMD component: prefer existing, fallback to own
---------------------------------------------------------------------------

local function acquire_hmd()
    -- Prefer the shared controllers-module HMD when available so we match
    -- the behavior of the working all-in-one script.
    local existing = find_existing_hmd()
    if existing then
        if hmd_comp ~= existing and not hmd_external then
            destroy_own_hmd()
        end
        hmd_comp     = existing
        hmd_external = true
        return true
    end

    -- Already have a valid local component with an active HMD state.
    if ensure_hmd_state(hmd_comp) ~= nil then
        return true
    end

    -- Clear stale reference
    if hmd_external then
        hmd_comp     = nil
        hmd_external = false
    else
        destroy_own_hmd()
    end

    -- Fallback: create our own
    return create_own_hmd()
end

---------------------------------------------------------------------------
-- Audio pin
---------------------------------------------------------------------------

local function pin_audio()
    if not acquire_hmd() then return end

    local pc  = uevr.api:get_player_controller(0)
    local vec = zero_vec()
    local rot = zero_rot()
    if pc ~= nil and vec ~= nil and rot ~= nil
    and pc.SetAudioListenerOverride ~= nil then
        pc:SetAudioListenerOverride(hmd_comp, vec, rot)
    end
end

---------------------------------------------------------------------------
-- Tick: detect level changes instantly, safety-net pin on interval
---------------------------------------------------------------------------

local pending_pin = false

uevr.sdk.callbacks.on_pre_engine_tick(function(engine, delta)
    if get_world() == nil then return end

    -- Level-change detection (cheap pointer compare every tick)
    local level = get_world() and get_world().PersistentLevel or nil
    if level ~= last_level then
        last_level  = level
        pending_pin = true
        -- Drop our reference; don't destroy external components
        if hmd_external then
            hmd_comp     = nil
            hmd_external = false
        else
            destroy_own_hmd()
        end
    end

    -- Apply immediately after a level change
    if pending_pin then
        pin_audio()
        if hmd_comp ~= nil then pending_pin = false end
        return
    end

    -- Periodic safety-net
    safety_timer = safety_timer + delta
    if safety_timer >= SAFETY_INTERVAL then
        safety_timer = 0
        pin_audio()
    end
end)

---------------------------------------------------------------------------
-- Script reset / unload cleanup
---------------------------------------------------------------------------

uevr.sdk.callbacks.on_script_reset(function()
    local pc = uevr.api:get_player_controller(0)
    if pc ~= nil and pc.ClearAudioListenerOverride ~= nil then
        pc:ClearAudioListenerOverride()
    end
    -- Only destroy components we own
    if not hmd_external then
        destroy_own_hmd()
    end
    hmd_comp     = nil
    hmd_external = false
    last_level   = nil
    pending_pin  = false
    safety_timer = 0
end)
