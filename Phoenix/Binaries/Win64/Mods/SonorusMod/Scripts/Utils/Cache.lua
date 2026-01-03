-- Cache.lua - Unified caching utility for UE4SS UObjects
-- Handles validity checks, dependency chains, and reactive entity caching
-- Uses _G.CacheStore for persistence across hot reloads (F11)

---@class Cache
local Cache = {}

-- ============================================
-- Persistent Storage (survives F11 reload)
-- ============================================
_G.CacheStore = _G.CacheStore or {
    objects = {},       -- key -> UObject
    deps = {},          -- key -> {child_key1, child_key2, ...}
    entities = {},      -- entityType -> {list, set, initialized, hookRegistered, lastCleanup}
    static = {
        data = {},
        lastUpdate = 0,
    },
}

-- Local references for cleaner code
local store = _G.CacheStore

-- ============================================
-- Internal Helpers
-- ============================================

---Check if UObject is valid (safe, pcall-wrapped)
---@param obj any
---@return boolean
local function IsValid(obj)
    if not obj then return false end
    local valid = false
    pcall(function() valid = obj:IsValid() end)
    return valid
end

---Invalidate a key and all its dependents recursively
---@param key string
local function InvalidateWithDeps(key)
    local deps = store.deps[key]
    if deps then
        for _, childKey in ipairs(deps) do
            InvalidateWithDeps(childKey)
        end
    end
    store.objects[key] = nil
    store.deps[key] = nil
end

-- ============================================
-- Object Cache (with validity + dependencies)
-- ============================================

---Get cached object, re-find if invalid. Auto-invalidates dependents.
---@param key string Cache key
---@param finder function Function that returns the object (called if cache miss/invalid)
---@param parentKey? string Optional parent key - this cache invalidates when parent does
---@return any|nil object The cached object or nil
function Cache.Get(key, finder, parentKey)
    local cached = store.objects[key]

    if IsValid(cached) then
        return cached
    end

    -- Invalid - clear this key and dependents
    InvalidateWithDeps(key)

    -- Re-find
    local obj = nil
    pcall(function() obj = finder() end)

    if obj then
        store.objects[key] = obj

        -- Register as dependent of parent
        if parentKey then
            store.deps[parentKey] = store.deps[parentKey] or {}
            table.insert(store.deps[parentKey], key)
        end
    end

    return obj
end

---Get property from a cached parent object
---@param key string Cache key for this property
---@param parentKey string Cache key of parent (must already be cached)
---@param propName string Property name to access on parent
---@return any|nil property The property value or nil
function Cache.GetProp(key, parentKey, propName)
    return Cache.Get(key, function()
        local parent = store.objects[parentKey]
        if parent then
            local prop = nil
            pcall(function() prop = parent[propName] end)
            return prop
        end
        return nil
    end, parentKey)
end

---Get nested property via path
---@param key string Cache key
---@param parentKey string Root cache key
---@param path table Array of property names to traverse
---@return any|nil
function Cache.GetPath(key, parentKey, path)
    return Cache.Get(key, function()
        local obj = store.objects[parentKey]
        for _, prop in ipairs(path) do
            if not obj then return nil end
            local next = nil
            pcall(function() next = obj[prop] end)
            obj = next
        end
        return obj
    end, parentKey)
end

---Invalidate a specific key and its dependents
---@param key string
function Cache.Invalidate(key)
    InvalidateWithDeps(key)
end

---Clear all object caches (not entities or static)
function Cache.ClearObjects()
    store.objects = {}
    store.deps = {}
end

---Clear everything (full reset)
function Cache.ClearAll()
    _G.CacheStore = {
        objects = {},
        deps = {},
        entities = {},
        static = { data = {}, lastUpdate = 0 },
    }
    store = _G.CacheStore
end

-- ============================================
-- Entity Cache (reactive, for NPCs etc)
-- ============================================

---Initialize or get entity cache for a type
---@param entityType string e.g., "NPC"
---@return table cache {list, set, initialized, hookRegistered, lastCleanup}
local function GetEntityCache(entityType)
    if not store.entities[entityType] then
        store.entities[entityType] = {
            list = {},
            set = {},
            initialized = false,
            hookRegistered = false,
            lastCleanup = 0,
        }
    end
    return store.entities[entityType]
end

---Add entity to cache (deduped)
---@param entityType string
---@param actor any UObject actor
function Cache.AddEntity(entityType, actor)
    if not IsValid(actor) then return end

    local cache = GetEntityCache(entityType)
    if cache.set[actor] then return end

    cache.set[actor] = true
    table.insert(cache.list, actor)
end

---Remove invalid entities from cache (call periodically)
---@param entityType string
---@param interval? number Throttle interval in seconds (default 5)
---@return number removedCount
function Cache.CleanEntities(entityType, interval)
    interval = interval or 5
    local cache = GetEntityCache(entityType)
    local now = os.clock()

    if (now - cache.lastCleanup) < interval then
        return 0
    end
    cache.lastCleanup = now

    local validList = {}
    local removedCount = 0

    for _, entity in ipairs(cache.list) do
        if IsValid(entity) then
            table.insert(validList, entity)
        else
            cache.set[entity] = nil
            removedCount = removedCount + 1
        end
    end

    cache.list = validList

    if removedCount > 0 then
        print(string.format("[Cache] Cleaned %d invalid %s, %d remain",
            removedCount, entityType, #cache.list))
    end

    return removedCount
end

---Get all cached entities of a type
---@param entityType string
---@return table list Array of actors
function Cache.GetEntities(entityType)
    local cache = GetEntityCache(entityType)
    return cache.list
end

---Get entity count
---@param entityType string
---@return number
function Cache.GetEntityCount(entityType)
    local cache = GetEntityCache(entityType)
    return #cache.list
end

---Initialize entity cache with FindAllOf (one-time)
---@param entityType string
---@param className string Class to FindAllOf
---@return number count Number of entities found
function Cache.InitEntities(entityType, className)
    local cache = GetEntityCache(entityType)
    if cache.initialized then
        return #cache.list
    end

    local t0 = os.clock()
    local existing = FindAllOf(className)
    if existing then
        for _, entity in ipairs(existing) do
            Cache.AddEntity(entityType, entity)
        end
    end

    cache.initialized = true
    print(string.format("[Cache] Initialized %s: %d entities in %.1fms",
        entityType, #cache.list, (os.clock() - t0) * 1000))

    return #cache.list
end

---Register spawn hook for entity type (one-time, persists across reloads)
---@param entityType string
---@param classPaths table Array of class paths to hook
function Cache.RegisterSpawnHook(entityType, classPaths)
    local cache = GetEntityCache(entityType)
    if cache.hookRegistered then return end

    for _, classPath in ipairs(classPaths) do
        pcall(function()
            NotifyOnNewObject(classPath, function(newEntity)
                Cache.AddEntity(entityType, newEntity)
            end)
            print("[Cache] Registered spawn hook: " .. classPath)
        end)
    end

    cache.hookRegistered = true
end

---Check if entity cache is initialized
---@param entityType string
---@return boolean
function Cache.IsEntityCacheReady(entityType)
    local cache = GetEntityCache(entityType)
    return cache.initialized
end

-- ============================================
-- Static Cache (periodic refresh)
-- ============================================

local STATIC_CACHE_DURATION = 30  -- seconds

---Get/refresh static cache with custom refresh function
---@param refreshFn function Called to refresh data, receives data table
---@param duration? number Cache duration in seconds (default 30)
---@return table data The static cache data
function Cache.GetStatic(refreshFn, duration)
    duration = duration or STATIC_CACHE_DURATION
    local now = os.clock()

    local needsRefresh = (now - store.static.lastUpdate) >= duration

    -- Also refresh if primary cached object is invalid
    if not needsRefresh and store.static.data._primary then
        needsRefresh = not IsValid(store.static.data._primary)
    end

    if needsRefresh then
        local t0 = os.clock()
        pcall(function() refreshFn(store.static.data) end)
        store.static.lastUpdate = now
        store.static.lastRefreshTime = (os.clock() - t0) * 1000
    end

    return store.static.data
end

---Get static cache data without triggering refresh
---@return table data
function Cache.GetStaticData()
    return store.static.data
end

---Force refresh static cache on next access
function Cache.InvalidateStatic()
    store.static.lastUpdate = 0
end

---Get time since last static refresh
---@return number seconds
function Cache.GetStaticAge()
    return os.clock() - store.static.lastUpdate
end

-- ============================================
-- Debug / Introspection
-- ============================================

---Get cache statistics
---@return table stats
function Cache.GetStats()
    local objectCount = 0
    for _ in pairs(store.objects) do objectCount = objectCount + 1 end

    local entityStats = {}
    for entityType, cache in pairs(store.entities) do
        entityStats[entityType] = {
            count = #cache.list,
            initialized = cache.initialized,
            hookRegistered = cache.hookRegistered,
        }
    end

    return {
        objects = objectCount,
        entities = entityStats,
        staticAge = Cache.GetStaticAge(),
        staticLastRefresh = store.static.lastRefreshTime or 0,
    }
end

return Cache
