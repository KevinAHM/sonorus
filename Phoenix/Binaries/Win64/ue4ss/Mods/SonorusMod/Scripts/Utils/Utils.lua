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
local LocationRegistry = require('Utils.LocationRegistry')
local active_intervals = {}
local interval_id_counter = 0
local interval_loop_handle = nil  -- Single global loop handle

_G.GamePauseState = _G.GamePauseState or {
    isPaused = true,
    hasBPEvent = false,
    updatedAt = 0,
}
_G.GamePauseState.isPaused = _G.GamePauseState.isPaused ~= false
_G.LastKnownPauseState = (_G.LastKnownPauseState == nil) and true or _G.LastKnownPauseState

local function ReadPauseEventValue(...)
    local args = {...}
    for i = #args, 1, -1 do
        local value = args[i]
        if type(value) == "boolean" then
            return value
        end
        if type(value) == "table" then
            if value.IsGamePaused ~= nil then return value.IsGamePaused == true end
            if value.isGamePaused ~= nil then return value.isGamePaused == true end
            if value.ReturnValue ~= nil then return value.ReturnValue == true end
        end
    end
    return nil
end

if RegisterCustomEvent and not _G.GamePauseChangedRegistered then
    _G.GamePauseChangedRegistered = true
    RegisterCustomEvent("GamePauseChanged", function(...)
        local paused = ReadPauseEventValue(...)
        if paused == nil then
            if _G.DevPrint then
                _G.DevPrint("[Pause] GamePauseChanged received without IsGamePaused output")
            end
            return
        end

        _G.GamePauseState.isPaused = paused
        _G.GamePauseState.hasBPEvent = true
        _G.GamePauseState.updatedAt = os.clock()
        if _G.DevPrint then
            _G.DevPrint("[Pause] GamePauseChanged IsGamePaused=" .. tostring(paused))
        end
    end)
elseif not RegisterCustomEvent and not _G.GamePauseChangedUnavailableLogged then
    _G.GamePauseChangedUnavailableLogged = true
    print("[Sonorus] RegisterCustomEvent unavailable; pause falls back to Utils.IsGamePaused polling")
end


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

---Gets the player's house from UIManager
---@return string house The player's house name (e.g., "Hufflepuff", "Gryffindor")
function Utils.GetPlayerHouse()
    local house = ""

    local uiManager = Cache.Get("UIManager", function() return FindFirstOf("UIManager") end)

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

    local uiManager = Cache.Get("UIManager", function() return FindFirstOf("UIManager") end)

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
    _G.GamePauseState = _G.GamePauseState or {
        isPaused = true,
        hasBPEvent = false,
        updatedAt = 0,
    }

    if not _G.SonorusState.playerLoaded then
        print("[Utils] IsGamePaused: Player not loaded, returning true")
        return true
    end

    local modActor = _G.SonorusState and _G.SonorusState.sonorusModActor
    if modActor and Utils.SafeIsValid(modActor) then
        local method = modActor.IsGamePaused
        if method then
            local out = {}
            local ok, result = pcall(function()
                return method(modActor, out)
            end)
            if ok then
                if type(result) == "boolean" then
                    _G.GamePauseState.isPaused = result
                    return result
                end
                local paused = out.IsGamePaused
                if paused == nil then paused = out.isGamePaused end
                if paused == nil then paused = out.ReturnValue end
                if paused ~= nil then
                    paused = paused == true
                    _G.GamePauseState.isPaused = paused
                    return paused
                end
            else
                print("[Utils] ModActor:IsGamePaused error: " .. tostring(result))
            end
        end
    end

    local uiManager = Utils.GetUIManager()
    if not uiManager then
        return _G.GamePauseState.isPaused ~= false
    end

    local paused = false
    pcall(function()
        paused = uiManager:InPauseMode() or uiManager:GetIsUIShown()
    end)

    _G.GamePauseState.isPaused = paused == true
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
        -- Multiple HUD widgets can exist (zombies from loading screens).
        -- Only the active one is in the viewport.
        local all = FindAllOf("PhoenixHUDWidget")
        if all then
            for _, w in ipairs(all) do
                local inVP = false
                pcall(function() inVP = w:IsInViewport() end)
                if inVP then return w end
            end
            return all[1]  -- fallback if none report InViewport
        end
        return nil
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
---@return table mission Table with questName, objective, status, shortObjectives fields (empty strings/table if unavailable)
function Utils.GetCurrentMission()
    local mission = { questName = "", objective = "", status = "", shortObjectives = {} }

    -- Get HUD (auto-invalidates dependents if HUD changed)
    local hud = Cache.Get("HUD", function()
        -- Multiple HUD widgets can exist (zombies from loading screens).
        -- Only the active one is in the viewport.
        local all = FindAllOf("PhoenixHUDWidget")
        if all then
            for _, w in ipairs(all) do
                local inVP = false
                pcall(function() inVP = w:IsInViewport() end)
                if inVP then return w end
            end
            return all[1]  -- fallback if none report InViewport
        end
        return nil
    end)
    if not hud then return mission end

    -- Cache mission banner
    local banner = Cache.Get("MissionBanner", function()
        return hud:GetMissionBanner()
    end, "HUD")
    if not banner then return mission end

    -- If steps aren't showing, the banner is stale (quest completed, no new quest tracked)
    local stepsShowing = false
    pcall(function() stepsShowing = banner.MissionStepsShowing end)
    if not stepsShowing then return mission end

    -- Cache text widgets (depend on MissionBanner)
    local titleWidget = Cache.GetProp("MissionTitle", "MissionBanner", "StepTitleText")
    local descWidget = Cache.GetProp("MissionDesc", "MissionBanner", "MissionDesc_Text")
    local headerWidget = Cache.GetProp("MissionHeader", "MissionBanner", "MissionBannerHeaderText")

    -- Read text from cached widgets (fast)
    mission.questName = ReadTextWidget(titleWidget)
    mission.objective = ReadTextWidget(descWidget)
    mission.status = ReadTextWidget(headerWidget)

    -- Read short objectives from objectiveList (the on-screen checklist items)
    -- These are the actionable tasks like "Follow Professor Weasley"
    pcall(function()
        local objectiveList = banner.objectiveList
        if not objectiveList then return end

        local childCount = objectiveList:GetChildrenCount()
        for i = 0, childCount - 1 do
            pcall(function()
                local child = objectiveList:GetChildAt(i)
                if child then
                    local checkboxText = child.CheckboxText
                    if checkboxText then
                        local text = ReadTextWidget(checkboxText)
                        if text and text ~= "" then
                            table.insert(mission.shortObjectives, text)
                        end
                    end
                end
            end)
        end
    end)

    return mission
end

---Gets the current companion's display name and voice ID
---@return string|nil displayName The companion's display name (or nil if no companion)
---@return string|nil voiceId The companion's internal voice ID (or nil if no companion)
function Utils.GetCompanionNameAndId()
    local displayName, voiceId = nil, nil
    pcall(function()
        local staticData = Cache.GetStaticData()
        local companionMgr = staticData and staticData.companionManager
        if not companionMgr then return end

        local companionPawn = nil
        pcall(function() companionPawn = companionMgr:GetPrimaryCompanionPawn() end)
        if not companionPawn then return end

        voiceId = Utils.GetActorVoiceId(companionPawn, staticData)
        if voiceId and voiceId ~= "" then
            if _G.GetDisplayName then
                displayName = _G.GetDisplayName(voiceId)
            else
                print("[Utils] WARNING: GetDisplayName not loaded - using voice ID as fallback")
                displayName = voiceId
            end
        end
    end)
    return displayName, voiceId
end

---Gets companion internal ID from pawn actor
---@param companionPawn userdata The companion pawn actor
---@return string|nil companionId Internal ID like "NellieOggspire" or nil if failed
function Utils.GetCompanionId(companionPawn)
    if not companionPawn then return nil end
    if not Utils.SafeIsValid(companionPawn) then return nil end

    local staticData = Cache.GetStaticData()
    return Utils.GetActorVoiceId(companionPawn, staticData)
end

---Gets distance from player to companion
---@param staticData table|nil Optional cached static data (will fetch if nil)
---@return number|nil distance Distance in UE units, or nil if no companion/error
function Utils.GetCompanionDistance(staticData)
    local distance = nil
    pcall(function()
        staticData = staticData or Cache.GetStaticData()
        local player = staticData and staticData.player
        local companionMgr = staticData and staticData.companionManager
        if not player or not companionMgr then return end

        local companionPawn = nil
        pcall(function() companionPawn = companionMgr:GetPrimaryCompanionPawn() end)
        if not companionPawn or not Utils.SafeIsValid(companionPawn) then return end

        local playerLoc = player:K2_GetActorLocation()
        local companionLoc = companionPawn:K2_GetActorLocation()
        if not playerLoc or not companionLoc then return end

        local dx = playerLoc.X - companionLoc.X
        local dy = playerLoc.Y - companionLoc.Y
        local dz = playerLoc.Z - companionLoc.Z
        distance = math.sqrt(dx*dx + dy*dy + dz*dz)
    end)
    return distance
end

---Check if a companion pawn is in forced wait state (quest/puzzle hold position).
---@param companionPawn userdata The companion pawn actor
---@param companionMgr userdata|nil Optional CompanionManager (fetched from cache if nil)
---@return boolean isWaiting true if companion is in forced wait
function Utils.IsCompanionForcedWaiting(companionPawn, companionMgr)
    if not companionPawn then return false end
    local isWaiting = false
    pcall(function()
        if not companionMgr then
            local staticData = Cache.GetStaticData()
            companionMgr = staticData and staticData.companionManager
        end
        if companionMgr then
            local waitLoc = {}
            isWaiting = companionMgr:IsCompanionWaitingBP(companionPawn, waitLoc)
        end
    end)
    return isWaiting
end

---Check if the companion is actively following the player (not in forced wait from quest/puzzle).
---@param companionPawn userdata|nil Optional pawn (fetched from CompanionManager if nil)
---@return boolean isFollowing true if companion exists and is NOT in forced wait
---@return string|nil voiceId companion voice ID if following
---@return string|nil displayName companion display name if following
function Utils.IsCompanionActivelyFollowing(companionPawn)
    local isFollowing = false
    local voiceId, displayName = nil, nil
    pcall(function()
        local staticData = Cache.GetStaticData()
        local companionMgr = staticData and staticData.companionManager
        if not companionMgr then return end

        if not companionPawn then
            companionPawn = companionMgr:GetPrimaryCompanionPawn()
        end
        if not companionPawn or not Utils.SafeIsValid(companionPawn) then return end

        if Utils.IsCompanionForcedWaiting(companionPawn, companionMgr) then return end

        voiceId = Utils.GetActorVoiceId(companionPawn, staticData)
        if voiceId and voiceId ~= "" then
            if _G.GetDisplayName then
                displayName = _G.GetDisplayName(voiceId)
            else
                displayName = voiceId
            end
            isFollowing = true
        end
    end)
    return isFollowing, voiceId, displayName
end

---Safely convert FString to Lua string
---Handles UE4SS quirk requiring nested pcall for FString:ToString()
---@param fstring userdata|nil The FString to convert
---@return string|nil str The string value or nil if failed/empty
function Utils.SafeFStringToString(fstring)
    if not fstring then return nil end
    local str = nil
    pcall(function() str = fstring:ToString() end)
    return (str and str ~= "") and str or nil
end

---Safely call a no-arg method that returns FString and convert result to string
---Combines outer pcall for method call + inner pcall for ToString (per CLAUDE.md)
---@param obj userdata The object to call method on
---@param methodName string The method name to call
---@return string|nil str The string value or nil if failed/empty
function Utils.SafeMethodToString(obj, methodName)
    if not obj then return nil end
    local result = nil
    pcall(function()
        local fstring = obj[methodName](obj)
        if fstring then
            pcall(function() result = fstring:ToString() end)
        end
    end)
    return (result and result ~= "") and result or nil
end

---Safe IsValid check for UObjects
---Wraps IsValid in pcall since stale/invalid objects can crash
---@param obj userdata|nil The object to check
---@return boolean valid True if object is valid
function Utils.SafeIsValid(obj)
    if not obj then return false end
    local valid = false
    pcall(function() valid = obj:IsValid() end)
    return valid
end

---Get raw voice ID for an actor using PhoenixBPLibrary
---Returns the internal voice ID (e.g., "neridaroberts"), NOT the display name.
---Use GetActorDisplayName() if you need the localized display name (e.g., "Nerida Roberts").
---@param actor userdata The actor to get voice ID for
---@param staticData table|nil Optional cached static data (avoids re-fetch)
---@return string|nil voiceId The internal voice ID or nil if not available
function Utils.GetActorVoiceId(actor, staticData)
    if not actor then return nil end
    staticData = staticData or Cache.GetStaticData()
    local lib = staticData and staticData.bpLibrary
    if not lib then return nil end

    local name = nil
    pcall(function()
        local nameResult = lib:GetActorName(actor)
        if nameResult then
            pcall(function() name = nameResult:ToString() end)
        end
    end)
    return (name and name ~= "") and name or nil
end

---Get the localized display name for an actor (e.g., "Nerida Roberts")
---This is the proper human-readable name from localization data.
---@param actor userdata The actor to get name for
---@param staticData table|nil Optional cached static data (avoids re-fetch)
---@return string|nil displayName Localized display name or nil
function Utils.GetActorDisplayName(actor, staticData)
    local voiceId = Utils.GetActorVoiceId(actor, staticData)
    if not voiceId then return nil end

    -- Get localized display name (e.g., "NeridaRoberts" -> "Nerida Roberts")
    if _G.GetDisplayName then
        return _G.GetDisplayName(voiceId)
    end

    return voiceId  -- Fallback to ID
end

---Gets companion info (ID, swimming, broom state) with floo mod fallback
---@param staticData table Cached static data
---@param isPlayerOnBroom boolean Whether player is on broom
---@param isPlayerInStealth boolean Whether player is in stealth
---@param IsCompanionOnBroom function Function to check if actor is on broom
---@param GetNearbyNPCs function Function to get nearby NPCs
---@return table|nil companionInfo Table with hasCompanion, companionId, companionInStealth, companionIsSwimming, companionIsOnBroom or nil
function Utils.GetCompanionInfo(staticData, isPlayerOnBroom, isPlayerInStealth, IsCompanionOnBroom, GetNearbyNPCs)
    local companionMgr = staticData and staticData.companionManager
    if not companionMgr or not Utils.SafeIsValid(companionMgr) then return nil end

    local companionPawn = companionMgr:GetPrimaryCompanionPawn()
    if companionPawn and Utils.SafeIsValid(companionPawn) then
        local isForcedWaiting = Utils.IsCompanionForcedWaiting(companionPawn, companionMgr)

        -- Valid companion
        local info = {
            hasCompanion = not isForcedWaiting,
            companionForcedWaiting = isForcedWaiting,
            companionInStealth = isPlayerInStealth,
            companionIsSwimming = false,
            companionIsOnBroom = false,
            companionId = nil
        }

        -- Swimming check
        pcall(function()
            local npcCompClass = staticData.npcComponentClass
            if npcCompClass then
                local npcComp = companionPawn:GetComponentByClass(npcCompClass)
                if npcComp then
                    info.companionIsSwimming = npcComp:IsSwimming() or false
                end
            end
        end)

        -- Broom check (only if player is on broom)
        if isPlayerOnBroom and IsCompanionOnBroom then
            pcall(function()
                info.companionIsOnBroom = IsCompanionOnBroom(companionPawn, staticData)
            end)
        end

        -- Get companion ID
        local companionId = Utils.GetCompanionId(companionPawn)
        if companionId then
            info.companionId = companionId
        end

        return info
    elseif isPlayerOnBroom and GetNearbyNPCs and IsCompanionOnBroom then
        -- Floo mod fallback: scan nearby NPCs for flying companion
        local npcResult = GetNearbyNPCs(2000, 0.9)
        if npcResult and npcResult.nearbyList then
            for _, npcEntry in ipairs(npcResult.nearbyList) do
                local isFlying = false
                pcall(function()
                    isFlying = IsCompanionOnBroom(npcEntry.actor, staticData)
                end)
                if isFlying then
                    return {
                        hasCompanion = true,
                        companionForcedWaiting = false,
                        companionId = npcEntry.name,
                        companionIsOnBroom = true,
                        companionInStealth = isPlayerInStealth,
                        companionIsSwimming = false
                    }
                end
            end
        end
    end

    return nil
end

---Get a fresh actor reference from the engine, bypassing all Lua caches.
---Uses PopulationManager → GetScheduledEntityFromName → GetFlesh.
---@param voiceId string The NPC's voice ID (e.g. "SebastianSallow"), or "player"
---@return userdata|nil actor Fresh actor reference, or nil
function Utils.GetFreshActorByVoiceId(voiceId)
    if not voiceId or voiceId == "" then return nil end
    if voiceId == "player" then
        local player = nil
        pcall(function() player = FindFirstOf("Biped_Player") end)
        if player and Utils.SafeIsValid(player) then return player end
        return nil
    end

    local staticData = Cache.GetStaticData()
    local popManager = staticData and staticData.populationManager
    if not popManager or not Utils.SafeIsValid(popManager) then return nil end

    local actor = nil
    pcall(function()
        local se = popManager:GetScheduledEntityFromName(voiceId)
        if se and se:IsValid() and se:CurrentlyInFlesh() then
            actor = se:GetFlesh()
        end
    end)
    if actor and Utils.SafeIsValid(actor) then
        return actor
    end
    return nil
end

--- Get an NPC's current schedule info (location, activity) from the PopulationManager.
--- Works even when the NPC is not streamed in.
--- @param voiceId string - entity name (e.g. "SebastianSallow", "PhineasBlack")
--- @param staticData table|nil - optional static cache (will fetch if nil)
--- @return table|nil - { locationId, locationName, locationDesc, activity, activityType, isInTransit, inFlesh } or nil
function Utils.GetNPCScheduleInfo(voiceId, staticData)
    if not voiceId or voiceId == "" then return nil end

    staticData = staticData or (GetStaticCache and GetStaticCache())
    local popManager = staticData and staticData.populationManager
    if not popManager or not Utils.SafeIsValid(popManager) then return nil end

    local se = nil
    local ok = pcall(function()
        se = popManager:GetScheduledEntityFromName(voiceId)
    end)
    if not ok or not se then return nil end

    local seValid = false
    pcall(function() seValid = se:IsValid() end)
    if not seValid then return nil end

    local info = {
        locationId = nil,
        locationName = nil,
        locationDesc = nil,
        activity = nil,
        activityType = nil,
        isInTransit = false,
        inFlesh = false,
        scheduledEntity = se,
    }

    pcall(function() info.inFlesh = se:CurrentlyInFlesh() end)
    pcall(function() info.isInTransit = se:IsInTransit() end)

    -- Read current activity
    pcall(function()
        local out = {}
        local out2 = {}
        se:GetCurrentActivity(out, out2)
        if out.ActivityIsValid then
            pcall(function() info.activity = out.Activity:ToString() end)
            pcall(function() info.activityType = out.ActivityType:ToString() end)

            local locKey = nil
            pcall(function() locKey = out.LocationKey:ToString() end)

            if locKey and locKey ~= "" then
                info.locationId = locKey
                info.locationName = LocationRegistry.ResolveDisplayName(locKey)
                info.locationDesc = LocationRegistry.GetDescription(locKey)
            end
        end
    end)

    return info
end

--- Lightweight NPC proximity scan — distance only, no camera/LOS/screen checks.
--- Uses globals set by logic.lua: GetCachedNPCs, GetStaticCache, IsSignificantNPC.
---@param maxDistance number Max distance in UE units
---@return table[] Array of {name: string, distance: number}
function Utils.ScanNearbyLean(maxDistance)
    maxDistance = maxDistance or 10000
    local result = {}

    if not _G.SonorusState or not _G.SonorusState.playerLoaded then
        return result
    end

    local staticData = _G.GetStaticCache and _G.GetStaticCache()
    if not staticData then return result end

    local player = staticData.player
    if not player then return result end

    local playerLoc = nil
    pcall(function() playerLoc = player:K2_GetActorLocation() end)
    if not playerLoc then return result end

    local npcs = _G.GetCachedNPCs and _G.GetCachedNPCs()
    if not npcs then return result end

    local playerFullName = staticData.playerFullName
    local SafeIsValid = Utils.SafeIsValid

    for _, npc in pairs(npcs) do
        if SafeIsValid(npc) then
            local fullName = nil
            pcall(function() fullName = npc:GetFullName() end)
            if fullName and fullName ~= playerFullName then
                local npcLoc = nil
                pcall(function() npcLoc = npc:K2_GetActorLocation() end)
                if npcLoc then
                    local dx = npcLoc.X - playerLoc.X
                    local dy = npcLoc.Y - playerLoc.Y
                    local dz = npcLoc.Z - playerLoc.Z
                    local dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if dist <= maxDistance then
                        local npcId = Utils.GetActorVoiceId(npc, staticData) or "Unknown"
                        if _G.IsSignificantNPC and _G.IsSignificantNPC(npcId) then
                            table.insert(result, {
                                name = npcId,
                                distance = math.floor(dist)
                            })
                        end
                    end
                end
            end
        end
    end

    return result
end

--- Returns the absolute yaw angle (degrees) between an actor's facing direction and a target position.
--- 0 = facing directly at target, 180 = facing away.
---@param actorPos FVector Position of the actor
---@param actorRot FRotator Rotation of the actor
---@param targetPos FVector Position of the target
---@return number angle Absolute yaw angle in degrees
function Utils.GetAngleToTarget(actorPos, actorRot, targetPos)
    local dx = targetPos.X - actorPos.X
    local dy = targetPos.Y - actorPos.Y
    local dist = math.sqrt(dx * dx + dy * dy)
    if dist < 1 then return 0 end
    local angleToTarget = math.atan(dy, dx) * 180 / math.pi
    local yaw = actorRot.Yaw or 0
    local diff = angleToTarget - yaw
    while diff > 180 do diff = diff - 360 end
    while diff < -180 do diff = diff + 360 end
    return math.abs(diff)
end

return Utils
