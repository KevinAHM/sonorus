-- Utils/TimeDilation.lua
-- Time dilation control - adjusts game time speed based on day/night and conversation state
-- Based on "Tempus Imperium" by Rysel#4780

local TimeDilation = {}

local Utils = require("Utils.Utils")
local Cache = require("Utils.Cache")

-- ============================================
-- Paths & Object References
-- ============================================
local PATHS = {
    DayNightMaster = "/Game/Levels/Overland/Overland_Global_Sky.Overland_Global_Sky:PersistentLevel.BP_DayNightSky_Overland_2.DayNightMaster",
    TimeSourceScheduler = "/Game/Levels/Overland/Overland_Global_Sky.Overland_Global_Sky:PersistentLevel.BP_DayNightSky_Overland_2.DayNightMaster.TimeSourceScheduler_0",
    TimeSourceFromDateTime = "/Game/Levels/Overland/Overland_Global_Sky.Overland_Global_Sky:PersistentLevel.BP_DayNightSky_Overland_2.DayNightMaster.TimeSourceFromDateTime_1",
    -- Note: Alternative path from example, might be needed if above fails
    CurrentModTime = "/Game/Environment/DayNightSky/Overland/BP_DayNightSky_Overland.Default__BP_DayNightSky_Overland_C:DayNightMaster.TimeSourceFromDateTime_1"
}

-- ============================================
-- State (persisted in _G for hot reload)
-- ============================================
_G.TimeDilationState = _G.TimeDilationState or {
    -- Settings from Python (rates as realtime multipliers: 1.0 = realtime, 3.0 = 3x faster)
    enabled = true,
    dayRate = 3.0,  -- 3x realtime
    nightRate = 3.0,  -- 3x realtime
    dayStartHour = 6,
    nightStartHour = 18,

    -- Runtime state
    currentRate = 3.0,  -- Current rate in our format (realtime multiplier)
    initialized = false,
}

-- ============================================
-- Helper Functions
-- ============================================

--- Retrieve cached time objects, refinding if invalid
--- @return table|nil {scheduler, timeSourceScheduler, timeSourceFromDateTime}
local function GetTimeObjects()
    -- Helpers to find objects if not in cache or invalid
    local finders = {
        scheduler = function() return FindFirstOf("Scheduler") end,
        timeSourceScheduler = function() return StaticFindObject(PATHS.TimeSourceScheduler) end,
        timeSourceFromDateTime = function() return StaticFindObject(PATHS.TimeSourceFromDateTime) end
    }

    -- Cache.Get handles validity check and re-finding
    local objs = {
        scheduler = Cache.Get("TimeDilation_Scheduler", finders.scheduler),
        timeSourceScheduler = Cache.Get("TimeDilation_TSS", finders.timeSourceScheduler),
        timeSourceFromDateTime = Cache.Get("TimeDilation_TSFDT", finders.timeSourceFromDateTime)
    }

    if objs.scheduler and objs.timeSourceScheduler and objs.timeSourceFromDateTime then
        return objs
    end
    return nil
end


--- Determine if it's currently daytime
--- @param hour number The current hour (0-23)
--- @return boolean isDay True if daytime, false if nighttime
local function IsDaytime(hour)
    local state = _G.TimeDilationState
    if state.dayStartHour < state.nightStartHour then
        return hour >= state.dayStartHour and hour < state.nightStartHour
    else
        return hour >= state.dayStartHour or hour < state.nightStartHour
    end
end

--- Convert our rate format to game's SimulationTimeFactorOverride format
--- Our format: 1.0 = realtime, 3.0 = 3x faster
--- Game format: 1.0 = 30x realtime (vanilla, 48 real mins per game day), 0.0333 = 1:1 realtime
local function ToGameRate(rate)
    return rate / 30
end

--- Sync the time source with the current game time
--- This is critical for the change to take effect properly
local function SyncWithGameTime(targetRate)
    local objs = GetTimeObjects()
    if not objs then
        print("[TimeDilation] Cannot sync - missing objects")
        return false
    end

    local gameScheduler = objs.scheduler
    local timeSourceScheduler = objs.timeSourceScheduler
    local timeSourceFromDateTime = objs.timeSourceFromDateTime
    local gameRate = ToGameRate(targetRate)

    -- 1. Disable scheduler first (like Rysel's Init)
    timeSourceScheduler.bDisable = true
    
    -- 2. Apply simulation factor immediately (like Rysel's SetClockRate call before Init)
    gameScheduler:SetSimulationTimeFactorOverride(gameRate)
    
    -- 3. After a delay, reconfigure the time source (longer delay for load stability)
    ExecuteInGameThreadWithDelay(1000, function()
        -- Re-get objects in case they changed
        local objs2 = GetTimeObjects()
        if not objs2 then
            print("[TimeDilation] Cannot complete sync - objects gone")
            return
        end

        local scheduler2 = objs2.scheduler
        local tss2 = objs2.timeSourceScheduler
        local tsfdt2 = objs2.timeSourceFromDateTime

        -- Check validity of objects before proceeding
        if not scheduler2 or not scheduler2:IsValid() then
            print("[TimeDilation] Scheduler invalid during sync")
            return
        end
        if not tsfdt2 or not tsfdt2:IsValid() then
            print("[TimeDilation] TimeSourceFromDateTime invalid during sync")
            return
        end

        -- Check if DateTime struct exists (may not in interiors)
        local dateTime = tsfdt2.DateTime
        if not dateTime then
            print("[TimeDilation] DateTime struct not available - using simple mode")
            -- Fall back to just setting the scheduler override
            scheduler2:SetSimulationTimeFactorOverride(gameRate)
            return
        end

        -- Scrape current time from Scheduler
        local currentDayMinute = scheduler2:GetMinuteOfTheDay()
        local currentYear      = scheduler2:GetCalendarYear()
        local currentMonth     = scheduler2:GetMonthOfTheYear()
        local currentDay       = scheduler2:GetDayOfTheMonth()
        local currentHour      = scheduler2:GetHourOfTheDay()
        local currentMinute    = math.floor(((currentDayMinute / 60) - currentHour) * 60)
        local currentAmPm      = currentHour < 12 and 0 or 1

        -- Configure TimeSourceFromDateTime (wrapped in pcall for safety)
        local ok, err = pcall(function()
            tsfdt2.isEnabled       = false
            tsfdt2.Rate            = gameRate * 1.333333
            tsfdt2.bUseRate        = true
            dateTime.Year   = currentYear
            dateTime.Month  = currentMonth
            dateTime.Day    = currentDay
            dateTime.Hour   = currentHour
            dateTime.Minute = currentMinute
            dateTime.AmPm   = currentAmPm

            -- Re-enable
            tss2.bDisable = false
            tsfdt2.isEnabled = true
        end)

        if ok then
            print(string.format("[TimeDilation] Sync complete: gameRate=%.4f, Rate=%.4f", gameRate, gameRate * 1.333333))
        else
            print("[TimeDilation] DateTime config failed, using simple mode: " .. tostring(err))
            scheduler2:SetSimulationTimeFactorOverride(gameRate)
        end
    end)
    
    return true
end

--- Apply a time dilation rate to the game
--- @param rate number The rate to apply in our format (1.0 = realtime, 3.0 = 3x faster)
--- @param force boolean If true, force re-sync
local function ApplyRate(rate, force)
    local state = _G.TimeDilationState

    -- Helper to get scheduler just for basic check
    local scheduler = Cache.Get("TimeDilation_Scheduler", function() return FindFirstOf("Scheduler") end)
    if not scheduler then
        if _G.DevPrint then _G.DevPrint("[TimeDilation] Scheduler not found") end
        return
    end

    local gameRate = ToGameRate(rate)

    -- Try the "Advanced" method if objects are found
    local advancedSuccess = false
    -- Check if we have the advanced objects cached or can find them
    local objs = GetTimeObjects()
    
    if objs then
        local ok, err = pcall(function()
            SyncWithGameTime(rate)
        end)
        if ok then
            advancedSuccess = true
        else
            print("[TimeDilation] SyncWithGameTime failed: " .. tostring(err))
        end
    else
        -- Fallback to simple overrides if objects missing (e.g. interior)
        local ok, err = pcall(function()
            scheduler:SetSimulationTimeFactorOverride(gameRate)
        end)
        if not ok then
             if _G.DevPrint then _G.DevPrint("[TimeDilation] Failed to apply simple rate: " .. tostring(err)) end
        end
    end

    state.lastAppliedRate = rate
    state.currentRate = rate
    local method = advancedSuccess and "Advanced" or "Simple"
    local forcedStr = force and " (FORCED)" or ""
    print(string.format("[TimeDilation] %s: %.1fx realtime (game factor: %.4f)%s", 
        method, rate, gameRate, forcedStr))
end

-- ============================================
-- Public API
-- ============================================

--- Update settings from Python
function TimeDilation.UpdateSettings(settings)
    if not settings then return end

    local state = _G.TimeDilationState
    local wasEnabled = state.enabled
    local wasInitialized = state.initialized

    state.enabled = settings.enabled or false
    state.dayRate = settings.day_rate or 1.0
    state.nightRate = settings.night_rate or 1.0
    state.dayStartHour = settings.day_start_hour or 6
    state.nightStartHour = settings.night_start_hour or 18

    print(string.format("[TimeDilation] Settings updated: enabled=%s, day=%.2f, night=%.2f",
        tostring(state.enabled), state.dayRate, state.nightRate))

    if state.enabled then
        TimeDilation.UpdateRate(true) -- Force update on settings change
    elseif wasInitialized and wasEnabled and not state.enabled then
        -- Only reset to vanilla when ACTIVELY turning off (not on initial load)
        -- Reset to vanilla game speed (30x realtime, 48 real mins per game day)
        ApplyRate(30.0, true)
    end
    -- If disabled on initial load, don't touch time flow at all - let game handle it
    state.initialized = true
end

function TimeDilation.GetTargetRate()
    local state = _G.TimeDilationState
    if not state.enabled then return 30.0 end  -- Vanilla game speed (30x realtime)

    local scheduler = FindFirstOf("Scheduler")
    local hour = scheduler and scheduler:GetHourOfTheDay() or 12

    if IsDaytime(hour) then
        return state.dayRate
    else
        return state.nightRate
    end
end

function TimeDilation.UpdateRate(force)
    local state = _G.TimeDilationState
    if not state.enabled then return end
    
    local targetRate = TimeDilation.GetTargetRate()
    
    -- Only apply if changed or forced
    if force or targetRate ~= state.currentRate then
         ApplyRate(targetRate, force)
    end
end

-- No-op: conversation rate setting removed, time continues at day/night rate
function TimeDilation.OnConversationStart()
end

-- No-op: conversation rate setting removed, time continues at day/night rate
function TimeDilation.OnConversationEnd()
end

function TimeDilation.IsActive()
    local state = _G.TimeDilationState
    return state.enabled and state.initialized
end

function TimeDilation.OnTick()
    local state = _G.TimeDilationState
    if not state.enabled then return end

    -- Check if rate needs update (day/night transition)
    local targetRate = TimeDilation.GetTargetRate()
    if targetRate ~= state.currentRate then
        print(string.format("[TimeDilation] Day/night transition: %.2f -> %.2f", state.currentRate, targetRate))
        ApplyRate(targetRate, true)
    end
end

-- Initialize one-time lookups if needed (though we do them dynamically to handle level loads)
-- We rely on main.lua hooks to call UpdateRate(true) on load, which will trigger the object find.

return TimeDilation
