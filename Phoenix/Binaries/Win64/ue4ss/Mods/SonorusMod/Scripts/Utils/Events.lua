--[[
    Events.lua - Simple event system for Sonorus

    Usage:
        local Events = require("Utils.Events")

        -- Register a listener
        local id = Events.on("combat:start", function(data)
            print("Combat started!")
        end)

        -- Emit an event
        Events.emit("combat:start", { active = true })

        -- Unregister
        Events.off("combat:start", id)

        -- State tracking with automatic events
        Events.setState("combat", true)   -- Emits combat:change, combat:start
        Events.setState("combat", false)  -- Emits combat:change, combat:end
        local inCombat = Events.getState("combat")

    Built-in events for boolean states:
        - {stateName}:change - Fires on any change with { old = prev, new = current }
        - {stateName}:start  - Fires when state becomes true
        - {stateName}:end    - Fires when state becomes false

    Hot-reload safe: Uses _G.EventStore for persistence.
]]

local Events = {}

-- Initialize persistent storage (survives F11 reload)
_G.EventStore = _G.EventStore or {
    listeners = {},    -- eventName -> { {id, callback, priority}, ... }
    nextId = 1,
    states = {},       -- stateName -> current value
    stateTimestamps = {}, -- stateName -> last change timestamp
}

local store = _G.EventStore

--------------------------------------------------------------------------------
-- Core Event API
--------------------------------------------------------------------------------

--- Register an event listener
--- @param eventName string The event to listen for
--- @param callback function The callback to invoke (receives data table)
--- @param priority number Optional priority (higher = called first, default 0)
--- @return number Listener ID for unregistering
function Events.on(eventName, callback, priority)
    if type(eventName) ~= "string" then
        error("Events.on: eventName must be a string")
    end
    if type(callback) ~= "function" then
        error("Events.on: callback must be a function")
    end

    priority = priority or 0

    local id = store.nextId
    store.nextId = id + 1

    store.listeners[eventName] = store.listeners[eventName] or {}
    table.insert(store.listeners[eventName], {
        id = id,
        callback = callback,
        priority = priority,
    })

    -- Sort by priority (higher first)
    table.sort(store.listeners[eventName], function(a, b)
        return a.priority > b.priority
    end)

    return id
end

--- Register a one-time listener (auto-removes after first call)
--- @param eventName string The event to listen for
--- @param callback function The callback to invoke
--- @param priority number Optional priority
--- @return number Listener ID
function Events.once(eventName, callback, priority)
    local id
    id = Events.on(eventName, function(data)
        Events.off(eventName, id)
        callback(data)
    end, priority)
    return id
end

--- Unregister an event listener
--- @param eventName string The event name
--- @param idOrCallback number|function The listener ID or callback function
--- @return boolean True if listener was found and removed
function Events.off(eventName, idOrCallback)
    local listeners = store.listeners[eventName]
    if not listeners then return false end

    for i, listener in ipairs(listeners) do
        local match = false
        if type(idOrCallback) == "number" then
            match = listener.id == idOrCallback
        elseif type(idOrCallback) == "function" then
            match = listener.callback == idOrCallback
        end

        if match then
            table.remove(listeners, i)
            return true
        end
    end

    return false
end

--- Clear all listeners for an event (or all events if nil)
--- @param eventName string|nil Event to clear, or nil for all
function Events.clear(eventName)
    if eventName then
        store.listeners[eventName] = nil
    else
        store.listeners = {}
    end
end

--- Emit an event to all registered listeners
--- @param eventName string The event to emit
--- @param data table|nil Optional data to pass to listeners
--- @return number Number of listeners that were called
function Events.emit(eventName, data)
    if not _G.SonorusState.playerLoaded then
        print("[Events] Player not loaded, skipping emit for " .. eventName)
        return 0
    end
    
    local listeners = store.listeners[eventName]
    if not listeners or #listeners == 0 then
        return 0
    end

    data = data or {}
    local count = 0

    -- Copy list to allow modifications during iteration
    local listenersCopy = {}
    for i, listener in ipairs(listeners) do
        listenersCopy[i] = listener
    end

    for _, listener in ipairs(listenersCopy) do
        local ok, err = pcall(listener.callback, data)
        if not ok then
            print(string.format("[Events] Error in '%s' listener: %s", eventName, tostring(err)))
        end
        count = count + 1
    end

    return count
end

--- Check if an event has any listeners
--- @param eventName string The event to check
--- @return boolean
function Events.hasListeners(eventName)
    local listeners = store.listeners[eventName]
    return listeners ~= nil and #listeners > 0
end

--- Get listener count for an event
--- @param eventName string The event to check
--- @return number
function Events.listenerCount(eventName)
    local listeners = store.listeners[eventName]
    return listeners and #listeners or 0
end

--------------------------------------------------------------------------------
-- State Tracking API
--------------------------------------------------------------------------------

--- Set a tracked state value, emitting events on change
--- For boolean states, emits:
---   - {stateName}:change with { old, new, timestamp } (always on change)
---   - {stateName}:start when becoming true (skipped on first init from nil)
---   - {stateName}:end when becoming false (skipped on first init from nil)
--- Note: :start/:end are skipped on first initialization (nil -> value) to avoid
---       spurious events on mod load. Use :change if you need all transitions.
--- @param stateName string The state name (e.g., "combat", "mount")
--- @param newValue any The new value
--- @return boolean True if state changed
function Events.setState(stateName, newValue)
    -- Block state changes during loading screens to prevent phantom transitions
    -- (e.g. GetIsOnAMountOrInTransition returning true mid-load → spurious dismount after)
    if not _G.SonorusState or not _G.SonorusState.playerLoaded then
        return false
    end

    local oldValue = store.states[stateName]

    -- No change
    if newValue == oldValue then
        return false
    end

    -- Update state
    store.states[stateName] = newValue
    store.stateTimestamps[stateName] = os.time()

    local eventData = {
        state = stateName,
        old = oldValue,
        new = newValue,
        timestamp = store.stateTimestamps[stateName],
    }

    -- Emit change event
    Events.emit(stateName .. ":change", eventData)

    -- For boolean states, emit start/end events
    -- Skip if oldValue was nil (first initialization) to avoid spurious :start/:end events
    -- nil → false is not a real "end", nil → true is not a real "start"
    if type(newValue) == "boolean" and oldValue ~= nil then
        if newValue then
            Events.emit(stateName .. ":start", eventData)
        else
            Events.emit(stateName .. ":end", eventData)
        end
    end

    return true
end

--- Get current state value
--- @param stateName string The state name
--- @return any Current value (nil if never set)
function Events.getState(stateName)
    return store.states[stateName]
end

--- Get timestamp of last state change
--- @param stateName string The state name
--- @return number|nil Unix timestamp or nil if never set
function Events.getStateTimestamp(stateName)
    return store.stateTimestamps[stateName]
end

--- Check if a boolean state is currently active
--- @param stateName string The state name
--- @return boolean
function Events.isActive(stateName)
    return store.states[stateName] == true
end

--------------------------------------------------------------------------------
-- Debug / Introspection
--------------------------------------------------------------------------------

--- Get statistics about the event system
--- @return table Stats table
function Events.getStats()
    local stats = {
        listenerCount = 0,
        eventCount = 0,
        stateCount = 0,
        events = {},
        states = {},
    }

    for eventName, listeners in pairs(store.listeners) do
        stats.eventCount = stats.eventCount + 1
        stats.listenerCount = stats.listenerCount + #listeners
        stats.events[eventName] = #listeners
    end

    for stateName, value in pairs(store.states) do
        stats.stateCount = stats.stateCount + 1
        stats.states[stateName] = {
            value = value,
            lastChanged = store.stateTimestamps[stateName],
        }
    end

    return stats
end

--- Print event system status (for debugging)
function Events.debug()
    local stats = Events.getStats()
    print(string.format("[Events] %d events, %d listeners, %d states",
        stats.eventCount, stats.listenerCount, stats.stateCount))

    for eventName, count in pairs(stats.events) do
        print(string.format("  Event '%s': %d listeners", eventName, count))
    end

    for stateName, info in pairs(stats.states) do
        print(string.format("  State '%s': %s (changed %s)",
            stateName, tostring(info.value),
            info.lastChanged and os.date("%H:%M:%S", info.lastChanged) or "never"))
    end
end

return Events
