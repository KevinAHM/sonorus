-- LocationRegistry.lua
-- Loads location_registry.json + main_localization.json + commitment_spots.json
-- and builds lookup tables for converting between display names and mod keys.
--
-- Call LocationRegistry.Init() at handshake time (when localization
-- files are guaranteed to exist in the right language).
--
-- Globals set:
--   _G.LocationRegistry          = registry table (mod_key -> {localized_id, desc_localized_id, schedule_id})
--   _G.LocationDisplayToId       = reverse lookup (display_name_lower -> mod_key)
--   _G.LocationModKeyToDisplay   = forward lookup (mod_key -> display_name)
--   _G.CommitmentSpots           = commitment spot positions (mod_key -> [{x,y,z,yaw}, ...])

local FileIO = require("Utils.FileIO")

local LocationRegistry = {}

local _initialized = false

local REGISTRY_PATH = "sonorus\\data\\location_registry.json"
local SPOTS_PATH = "sonorus\\data\\commitment_spots.json"

-- localized_id -> mod_key (for resolving game-internal IDs to mod keys)
local _locIdToModKey = {}
-- localized_id (lowered) -> mod_key (case-insensitive fallback)
local _locIdLowerToModKey = {}

function LocationRegistry.Init()
    if _initialized then return true end

    -- Load registry JSON
    if not FileIO.LoadJsonCached("LocationRegistry", REGISTRY_PATH, "location_registry.json") then
        print("[LocationRegistry] Failed to load registry\n")
        return false
    end

    -- Load localization (language-aware, already cached if loaded before)
    if not LoadLocalization() then
        print("[LocationRegistry] Failed to load localization\n")
        return false
    end

    local registry = _G.LocationRegistry
    local loc = _G.Localization

    -- Build lookup tables
    local displayToId = {}
    local modKeyToDisplay = {}
    local matched = 0

    for modKey, entry in pairs(registry) do
        local locId = entry.localized_id
        if locId then
            -- localized_id -> mod_key reverse maps
            _locIdToModKey[locId] = modKey
            _locIdLowerToModKey[locId:lower()] = modKey

            -- Display name lookups (requires localization loaded)
            if loc[locId] then
                local displayName = loc[locId]
                displayToId[displayName:lower()] = modKey
                modKeyToDisplay[modKey] = displayName
                matched = matched + 1
            end
        end
    end

    _G.LocationDisplayToId = displayToId
    _G.LocationModKeyToDisplay = modKeyToDisplay

    -- Load commitment spots (mod_key -> [{x,y,z,yaw}, ...])
    -- Clear loaded flag so hot reloads pick up new spots
    _G.CommitmentSpotsLoaded = nil
    FileIO.LoadJsonCached("CommitmentSpots", SPOTS_PATH, "commitment_spots.json")

    _initialized = true

    print(string.format("[LocationRegistry] Built lookup tables: %d locations mapped\n", matched))
    return true
end

--- Get the canonical mod key for a HUD display name.
---@param displayName string The localized display name from the HUD
---@return string|nil modKey The canonical mod key, or nil if not found
function LocationRegistry.GetModKey(displayName)
    if not displayName or displayName == "" then return nil end
    if not _initialized then LocationRegistry.Init() end
    return _G.LocationDisplayToId and _G.LocationDisplayToId[displayName:lower()]
end

--- Get the canonical mod key from a game-internal localization key.
--- Handles raw game IDs like "HOG_AstronomyTower" that come from actor names,
--- the schedule system, etc.
---@param locId string A game localization key
---@return string|nil modKey The canonical mod key, or nil if not found
function LocationRegistry.GetModKeyFromLocId(locId)
    if not locId or locId == "" then return nil end
    if not _initialized then LocationRegistry.Init() end
    return _locIdToModKey[locId] or _locIdLowerToModKey[locId:lower()]
end

--- Get the localized display name for a mod key.
---@param modKey string The canonical mod key
---@return string|nil displayName The localized display name, or nil if not found
function LocationRegistry.GetDisplayName(modKey)
    if not modKey or modKey == "" then return nil end
    if not _initialized then LocationRegistry.Init() end
    return _G.LocationModKeyToDisplay and _G.LocationModKeyToDisplay[modKey]
end

--- Resolve any location identifier (mod key, game localization key, or display name)
--- to a localized display name. This replaces GetLocationDisplayName().
---@param id string A mod key, game localization key, or display name
---@return string|nil displayName The localized display name
function LocationRegistry.ResolveDisplayName(id)
    if not id or id == "" then return nil end
    if not _initialized then LocationRegistry.Init() end

    local loc = _G.Localization

    -- 1. Direct mod key lookup
    local fromModKey = _G.LocationModKeyToDisplay and _G.LocationModKeyToDisplay[id]
    if fromModKey then return fromModKey end

    -- 2. Direct localization key lookup (game IDs like "HOG_AstronomyTower")
    if loc and loc[id] then return loc[id] end

    -- 3. Try as localized_id -> mod key -> display name
    local modKey = _locIdToModKey[id]
    if modKey and _G.LocationModKeyToDisplay then
        return _G.LocationModKeyToDisplay[modKey]
    end

    -- 4. Strip common suffixes and retry (e.g., "HogwartsArea" -> "Hogwarts")
    local stripped = id:gsub("Bounds$", ""):gsub("Area$", ""):gsub("Region$", "")
    if stripped ~= id then
        local result = LocationRegistry.ResolveDisplayName(stripped)
        if result then return result end
    end

    -- 5. Case-insensitive localization key match
    if loc then
        local lowerKey = id:lower()
        -- Check via locId reverse map
        local mk = _locIdLowerToModKey[lowerKey]
        if mk and _G.LocationModKeyToDisplay then
            return _G.LocationModKeyToDisplay[mk]
        end
    end

    -- 6. Longest prefix match in localization (e.g., "HOG_Class_Charms_Patrol_Prof" -> "Charms Class")
    if loc then
        local bestName = nil
        local bestLen = 0
        for locKey, displayName in pairs(loc) do
            if #locKey > bestLen and id:sub(1, #locKey) == locKey then
                bestLen = #locKey
                bestName = displayName
            end
        end
        if bestName then return bestName end
    end

    return nil
end

--- Get the localized description for a location.
---@param modKeyOrLocId string A mod key or game localization key
---@return string|nil description The localized description
function LocationRegistry.GetDescription(modKeyOrLocId)
    if not modKeyOrLocId or modKeyOrLocId == "" then return nil end
    if not _initialized then LocationRegistry.Init() end

    local registry = _G.LocationRegistry
    local loc = _G.Localization
    if not registry or not loc then return nil end

    -- Try as mod key first
    local entry = registry[modKeyOrLocId]
    -- Try as localized_id
    if not entry then
        local mk = _locIdToModKey[modKeyOrLocId] or _locIdLowerToModKey[modKeyOrLocId:lower()]
        if mk then entry = registry[mk] end
    end

    if entry and entry.desc_localized_id and loc[entry.desc_localized_id] then
        return loc[entry.desc_localized_id]
    end
    return nil
end

--- Get the schedule_id for a mod key.
---@param modKey string The canonical mod key
---@return string|nil scheduleId The game scheduler LocationID, or nil
function LocationRegistry.GetScheduleId(modKey)
    if not modKey or modKey == "" then return nil end
    if not _initialized then LocationRegistry.Init() end
    local reg = _G.LocationRegistry
    if reg and reg[modKey] then
        return reg[modKey].schedule_id
    end
    return nil
end

return LocationRegistry
