---@class Utils
---@field StringContains fun(text: string, substring: string): boolean
---@field Log fun(Ar: any, Message: string): nil
---@field Summon fun(ObjectName: string, OptionalLocation?: FVector, OptionalRotation?: FRotator): AActor|nil
---@field PrintUEVersion fun(): nil
---@field SetInterval fun(callback: function, delay_seconds: number): number
---@field ClearInterval fun(interval_id: number): boolean
local Utils = {}

local UEHelpers = require('UEHelpers.UEHelpers')
local Cache = require('Utils.Cache')
local active_intervals = {}
local interval_id_counter = 0


---Checks if a string contains a substring
---@param text string The text to search in
---@param substring string The substring to search for
---@return boolean found True if substring is found, false otherwise
function Utils.StringContains(text, substring)
    -- The 'true' argument disables pattern matching for a plain text search, which is faster.
    return string.find(text, substring, 1, true) ~= nil
end

---A logging helper to print to both the in-game console and the debug log
---@param Ar any Archive or logging object (type unknown - has IsValid() and Log() methods)
---@param Message string The message to log
function Utils.Log(Ar, Message)
    print(Message)
    if Ar and Ar:IsValid() then
        Ar:Log(Message)
    end
end

---Spawns an actor at specified location
---@param ObjectName string Full path to the actor class
---@param OptionalLocation? FVector Spawn location (defaults to player location)
---@param OptionalRotation? FRotator Spawn rotation (defaults to player rotation)
---@return AActor|nil SpawnedActor The spawned actor or nil if failed
function Utils.Summon(ObjectName, OptionalLocation, OptionalRotation)
    local world = UEHelpers.GetWorld()
    local pc = UEHelpers.GetPlayerController()

    if world and pc and pc.Pawn then
        -- Use provided location or default to player
        local spawn_loc = OptionalLocation or pc.Pawn:K2_GetActorLocation()
        local spawn_rot = OptionalRotation or pc.Pawn:K2_GetActorRotation()

        local summon_class = StaticFindObject(ObjectName)
        print('Class to summon: ' .. tostring(summon_class and summon_class:GetFullName() or 'NOT_FOUND'))

        if summon_class then
            local new_summon = world:SpawnActor(summon_class, spawn_loc, spawn_rot)
            if new_summon then
                print('✅ Spawned: ' .. tostring(new_summon:GetFullName()))
                print('   Location: ' .. tostring(spawn_loc))
                return new_summon -- Return for further manipulation
            else
                print('❌ Failed to spawn: ' .. ObjectName)
            end
        else
            print('❌ Class not found: ' .. ObjectName)
        end
    else
        print('❌ Missing world/player/pawn')
    end
    return nil
end

---Prints the current Unreal Engine version
function Utils.PrintUEVersion()
    print('UNREAL VERSION: ' .. tostring(UnrealVersion:GetMajor()) .. '.' .. tostring(UnrealVersion:GetMinor()))
end

---Creates a repeating timer that executes a callback function at regular intervals
---@param callback function The function to execute repeatedly
---@param delay_seconds number Time in seconds between executions
---@return number interval_id Unique identifier for this interval (use with ClearInterval)
---@example
--- ```lua
--- -- Create a timer that prints every 2 seconds
--- local my_timer = Utils.SetInterval(function()
---     print('Every 5 seconds')
--- end, 2)
--- ```
function Utils.SetInterval(callback, delay_seconds)
    local cron = require('cron')

    -- Generate unique ID for this interval
    interval_id_counter = interval_id_counter + 1
    local interval_id = interval_id_counter

    -- Create the clock using the provided callback and delay
    local clock_from_cron = cron.every(delay_seconds, callback)
    
    -- Store it so we can clear it later
    active_intervals[interval_id] = clock_from_cron

    -- Background update loop
    ExecuteInGameThread(function()
        local function background_update()
            -- Update all active intervals
            for id, timer in pairs(active_intervals) do
                if timer then
                    timer:update(0.1)
                end
            end

            -- Schedule the next update
            ExecuteWithDelay(100, background_update)
        end

        -- Start the loop
        background_update()
        print("Background update loop started")
    end)
    
    -- Return the ID so it can be cleared later
    return interval_id
end



---Stops a repeating timer created with SetInterval
---@param interval_id number The ID returned by SetInterval
---@return boolean success True if interval was found and cleared, false otherwise
---@example
--- ```lua
--- -- Stop the timer when CAPS_LOCK is pressed
--- RegisterKeyBind(Key.CAPS_LOCK, {}, function()
---     Utils.ClearInterval(my_timer)
--- end)
--- ```
function Utils.ClearInterval(interval_id)
    if active_intervals[interval_id] then
        active_intervals[interval_id] = nil
        print("Cleared interval " .. interval_id)
        return true
    end
    return false
end

---Gets the player's house from UIManager
---@return string house The player's house name (e.g., "Hufflepuff", "Gryffindor")
function Utils.GetPlayerHouse()
    local house = ""

    local uiManager = nil
    pcall(function()
        uiManager = FindFirstOf("UIManager")
    end)

    if not uiManager then
        print("[Sonorus] GetPlayerHouse: UIManager not found")
        return house
    end

    local isValid = false
    pcall(function() isValid = uiManager:IsValid() end)
    if not isValid then
        print("[Sonorus] GetPlayerHouse: UIManager not valid")
        return house
    end

    -- GetPlayerHouse returns FString directly
    local ok, err = pcall(function()
        local result = uiManager:GetPlayerHouse()
        if result then
            pcall(function()
                local str = nil
                pcall(function() str = result:ToString() end)
                if str and str ~= "" then
                    house = str
                else
                    print("[Sonorus] GetPlayerHouse: ToString returned empty")
                end
            end)
        else
            print("[Sonorus] GetPlayerHouse: GetPlayerHouse() returned nil")
        end
    end)

    if not ok then
        print("[Sonorus] GetPlayerHouse error: " .. tostring(err))
    end

    return house
end

---Gets the player's first and last name from UIManager
---@return string firstName The player's first name
---@return string lastName The player's last name
---@return string fullName The player's full name (first + last)
function Utils.GetPlayerName()
    local firstName = ""
    local lastName = ""

    local uiManager = nil
    pcall(function()
        uiManager = FindFirstOf("UIManager")
    end)

    if not uiManager then
        return firstName, lastName, ""
    end

    local isValid = false
    pcall(function() isValid = uiManager:IsValid() end)
    if not isValid then
        return firstName, lastName, ""
    end

    -- GetPlayerFirstAndLastName has two out params - UE4SS puts both in first table
    local outTable = {}
    local ok, err = pcall(function()
        uiManager:GetPlayerFirstAndLastName(outTable, {})
    end)

    if not ok then
        print("[Utils] GetPlayerName error: " .. tostring(err))
        return firstName, lastName, ""
    end

    -- Extract first name (FString needs nested pcall for ToString)
    local rawFirst = outTable.PlayerFirstName
    if rawFirst and type(rawFirst) == "userdata" then
        pcall(function()
            local str = nil
            pcall(function() str = rawFirst:ToString() end)
            if str then firstName = str end
        end)
    end

    -- Extract last name
    local rawLast = outTable.PlayerLastName
    if rawLast and type(rawLast) == "userdata" then
        pcall(function()
            local str = nil
            pcall(function() str = rawLast:ToString() end)
            if str then lastName = str end
        end)
    end

    local fullName = firstName
    if lastName ~= "" then
        fullName = firstName .. " " .. lastName
    end

    return firstName, lastName, fullName
end

---Gets the UIManager with caching (re-finds if stale)
---@return userdata|nil uiManager The UIManager instance or nil
function Utils.GetUIManager()
    return Cache.Get("UIManager", function()
        return FindFirstOf("UIManager")
    end)
end

---Checks if the game is paused or a UI menu is shown
---@return boolean paused True if game is paused or UI is shown
function Utils.IsGamePaused()
    local uiManager = Utils.GetUIManager()
    if not uiManager then
        return false
    end

    local paused = false
    pcall(function()
        paused = uiManager:InPauseMode() or uiManager:GetIsUIShown()
    end)

    return paused
end

---Helper to read text from a PhoenixTextBlock widget
---@param widget userdata The text widget
---@return string text The text or empty string
local function ReadTextWidget(widget)
    if not widget then return "" end
    local str = ""
    pcall(function()
        local text = widget:GetText()
        if text then
            pcall(function() str = text:ToString() or "" end)
        end
    end)
    return str
end

---Gets the current zone/location from the ZoneNotification HUD widget
---@return table zone Table with header (e.g. "New Location Discovered") and location name
function Utils.GetZoneLocation()
    local zone = { header = "", location = "" }

    -- Get HUD (auto-invalidates dependents if HUD changed)
    local hud = Cache.Get("HUD", function()
        return FindFirstOf("PhoenixHUDWidget")
    end)
    if not hud then return zone end

    -- Cache zone notification widget
    local zoneNotif = Cache.GetProp("ZoneNotif", "HUD", "HUD_ZoneNotification")
    if not zoneNotif then return zone end

    -- Cache text widgets (depend on ZoneNotif)
    local header = Cache.GetProp("ZoneHeader", "ZoneNotif", "ZoneNotification_Header")
    local label = Cache.GetProp("ZoneLabel", "ZoneNotif", "ZoneNotification_Label")

    -- Read text from cached widgets (fast)
    zone.header = ReadTextWidget(header)
    zone.location = ReadTextWidget(label)

    return zone
end

---Gets the current mission/quest info from the MissionBanner HUD widget
---@return table mission Table with questName, objective, status fields (empty strings if unavailable)
function Utils.GetCurrentMission()
    local mission = { questName = "", objective = "", status = "" }

    -- Get HUD (auto-invalidates dependents if HUD changed)
    local hud = Cache.Get("HUD", function()
        return FindFirstOf("PhoenixHUDWidget")
    end)
    if not hud then return mission end

    -- Cache mission banner
    local banner = Cache.Get("MissionBanner", function()
        return hud:GetMissionBanner()
    end, "HUD")
    if not banner then return mission end

    -- Cache text widgets (depend on MissionBanner)
    local titleWidget = Cache.GetProp("MissionTitle", "MissionBanner", "StepTitleText")
    local descWidget = Cache.GetProp("MissionDesc", "MissionBanner", "MissionDesc_Text")
    local headerWidget = Cache.GetProp("MissionHeader", "MissionBanner", "MissionBannerHeaderText")

    -- Read text from cached widgets (fast)
    mission.questName = ReadTextWidget(titleWidget)
    mission.objective = ReadTextWidget(descWidget)
    mission.status = ReadTextWidget(headerWidget)

    return mission
end

return Utils
