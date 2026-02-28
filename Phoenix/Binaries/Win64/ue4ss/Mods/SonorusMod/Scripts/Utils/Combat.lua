-- Utils/Combat.lua
-- Combat tracking system - damage/kill attribution and summary generation
-- Extracted from logic.lua for modularity

local Combat = {}

-- Module requires
local Cache = require("Utils.Cache")
local NPCLock = require("Utils.NPCLock")
local Utils = require("Utils.Utils")

-- ============================================
-- State (persisted in _G for hot reload)
-- ============================================
_G.CombatStats = _G.CombatStats or {
    active = false,              -- Currently in combat
    startTimestamp = 0,          -- Unix timestamp when combat started
    startGameTime = "",          -- Game time when combat started (HH:MM)
    startGameDate = "",          -- Game date when combat started
    enemies = {},                -- { [normalizedEnemyType] = { damage, kills, playerDamage, companionDamage, playerKills, companionKills } }
    lastDamage = {},             -- { [enemyInstanceId] = { instigator = "Player"|"Companion", timestamp = os.time() } }
    playerDamage = 0,            -- Total damage dealt by player
    companionDamage = 0,         -- Total damage dealt by companion
    playerKills = 0,             -- Kills attributed to player
    companionKills = 0,          -- Kills attributed to companion
    lastCombatEnd = 0,           -- Unix timestamp of last combat end (for 60s merge window)
    pendingEntry = nil,          -- Pending combat entry for merging
}

-- ============================================
-- Dependency Injection
-- ============================================
-- These functions are injected via init() since they're defined in logic.lua

local getTimeOfDay = nil      -- Returns { hour, minute, formatted, dateFormatted, dateShort, ... }
local getDisplayName = nil    -- Converts internal ID to display name

--- Initialize the module with dependency functions
--- @param deps table { getTimeOfDay: function, getDisplayName: function }
function Combat.init(deps)
    if deps.getTimeOfDay then getTimeOfDay = deps.getTimeOfDay end
    if deps.getDisplayName then getDisplayName = deps.getDisplayName end
end

-- ============================================
-- Internal Helpers
-- ============================================

--- Normalize enemy ID by stripping instance suffixes (_INST_A, _INST_B, etc.)
--- Input: "DW_Extortionist_Grunt_INST_B" -> Output: "DW_Extortionist_Grunt"
--- @param rawId string The raw enemy instance ID
--- @return string normalizedId
local function NormalizeEnemyId(rawId)
    if not rawId or rawId == "" then return "Unknown" end
    -- Strip _INST_X suffix (where X is A-Z or numbers)
    local normalized = rawId:gsub("_INST_[A-Z0-9]+$", "")
    return normalized
end

--- Get instigator type from actor (Player, Companion, or nil for other/enemy)
--- @param instigator userdata The instigator actor
--- @return string|nil "Player", "Companion", or nil
local function GetInstigatorType(instigator)
    if not Utils.SafeIsValid(instigator) then return nil end

    local result = nil
    pcall(function()
        -- Check if it's the player
        local className = instigator:GetClass():GetFName():ToString()
        if className:find("Biped_Player") then
            result = "Player"
            return
        end

        -- Check if it's the companion (using NPCLock module)
        if NPCLock.IsCompanion(instigator) then
            result = "Companion"
            return
        end

        -- Otherwise it's another NPC (enemy vs enemy damage) - return nil
    end)

    return result
end

-- ============================================
-- Core Functions
-- ============================================

--- Reset combat stats for a new combat encounter
function Combat.ResetStats()
    local stats = _G.CombatStats
    stats.active = true
    stats.startTimestamp = os.time()

    if getTimeOfDay then
        local gameTime = getTimeOfDay()
        stats.startGameTime = gameTime.formatted or ""
        stats.startGameDate = gameTime.dateShort or gameTime.dateFormatted or ""
    else
        stats.startGameTime = ""
        stats.startGameDate = ""
    end

    stats.enemies = {}
    stats.lastDamage = {}
    stats.playerDamage = 0
    stats.companionDamage = 0
    stats.playerKills = 0
    stats.companionKills = 0
    stats.startWitnesses = nil  -- Clear stale witnesses from previous combat
    -- Don't reset lastCombatEnd or pendingEntry - those are for merge tracking
end

--- Format combat summary message
--- Example: "Defeated: Ashwinder Scout (3), Ashwinder Duellist | Damage: 8007 (Adri 85%, Natsai 15%)"
--- @return string summary
function Combat.FormatSummary()
    local stats = _G.CombatStats

    -- Get actual player and companion names
    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
    end
    local companionName = Utils.GetCompanionNameAndId() or "Companion"

    -- Collect enemy kills by display name
    local enemyKills = {}  -- { displayName = count }
    for enemyType, data in pairs(stats.enemies) do
        local displayName = getDisplayName and getDisplayName(enemyType) or enemyType
        local kills = (data.playerKills or 0) + (data.companionKills or 0)
        if kills > 0 then
            enemyKills[displayName] = (enemyKills[displayName] or 0) + kills
        end
    end

    -- Format kills part
    local killParts = {}
    for name, count in pairs(enemyKills) do
        if count > 1 then
            table.insert(killParts, name .. " (" .. count .. ")")
        else
            table.insert(killParts, name)
        end
    end
    table.sort(killParts)  -- Alphabetical for consistency

    -- Calculate damage percentages
    local totalDamage = stats.playerDamage + stats.companionDamage
    local playerPct = totalDamage > 0 and math.floor((stats.playerDamage / totalDamage) * 100 + 0.5) or 0
    local companionPct = totalDamage > 0 and (100 - playerPct) or 0

    -- Build summary
    local parts = {}

    -- Kills section
    if #killParts > 0 then
        table.insert(parts, "Defeated: " .. table.concat(killParts, ", "))
    end

    -- Damage section (use actual names)
    if totalDamage > 0 then
        local damagePart = string.format("Damage: %d", math.floor(totalDamage))
        if stats.companionDamage > 0 then
            damagePart = damagePart .. string.format(" (%s %d%%, %s %d%%)", playerName, playerPct, companionName, companionPct)
        end
        table.insert(parts, damagePart)
    end

    -- If no kills or damage, just say "Combat encounter"
    if #parts == 0 then
        return "Combat encounter"
    end

    return table.concat(parts, " | ")
end

--- Create combat entry for dialogue history
--- @param earshot table|nil List of nearby NPC IDs who witnessed the combat
--- @return table entry Dialogue history entry
function Combat.CreateEntry(earshot)
    local stats = _G.CombatStats
    local gameTime = getTimeOfDay and getTimeOfDay() or { formatted = "", dateShort = "", dateFormatted = "" }

    local playerName = "Player"
    if _G.SonorusState and _G.SonorusState.playerName and _G.SonorusState.playerName ~= "" then
        playerName = _G.SonorusState.playerName
    end

    return {
        timestamp = os.time(),
        gameTime = gameTime.formatted or "",
        gameDate = gameTime.dateShort or gameTime.dateFormatted or "",
        firstTimestamp = stats.startTimestamp,
        firstGameTime = stats.startGameTime,
        firstGameDate = stats.startGameDate,
        speaker = playerName,
        voiceName = "Player",
        text = Combat.FormatSummary(),
        isPlayer = true,
        isAIResponse = false,
        type = "combat",
        earshot = earshot or {},
        -- Combat-specific metadata
        playerDamage = stats.playerDamage,
        companionDamage = stats.companionDamage,
        playerKills = stats.playerKills,
        companionKills = stats.companionKills,
    }
end

--- Check if combat is currently active
--- @return boolean
function Combat.IsActive()
    return _G.CombatStats.active
end

--- Get combat stats (read-only access)
--- @return table stats
function Combat.GetStats()
    return _G.CombatStats
end

--- End combat tracking (called on combat:end event)
function Combat.EndCombat()
    local stats = _G.CombatStats
    stats.active = false
    stats.lastCombatEnd = os.time()
end

--- Check if combat should merge with previous (within 60s window)
--- @return boolean shouldMerge
function Combat.ShouldMergeWithPrevious()
    local stats = _G.CombatStats
    local now = os.time()
    return stats.lastCombatEnd > 0 and (now - stats.lastCombatEnd) < 60
end

--- Reactivate combat tracking (for merge scenario)
function Combat.ReactivateTracking()
    _G.CombatStats.active = true
end

-- ============================================
-- Hook Handlers (called from main.lua hooks)
-- ============================================

--- NPC death event handler - extracts dead NPC's ID and tracks kills
--- @param Context userdata Hook context
function Combat.OnNPCDied(Context)
    local npcId = "Unknown"
    local level = 0

    pcall(function()
        local npc = Context:get()
        if npc then
            -- Get character ID
            local charId = npc.OverrideCharacterID
            if charId then
                npcId = charId:ToString()
            end
            -- Get level
            level = npc.Level or 0
        end
    end)

    print(string.format("[Sonorus] NPC_DIED: %s (Level %d)", npcId, level))

    -- Track kill if in combat
    local stats = _G.CombatStats
    if stats.active and npcId ~= "Unknown" then
        -- Look up who last damaged this NPC
        local lastDamageInfo = stats.lastDamage[npcId]
        local attacker = lastDamageInfo and lastDamageInfo.instigator or "Player"  -- Default to player if unknown

        -- Normalize enemy ID for grouping (strip _INST_X suffix)
        local normalizedId = NormalizeEnemyId(npcId)

        -- Initialize enemy type if needed
        if not stats.enemies[normalizedId] then
            stats.enemies[normalizedId] = {
                damage = 0,
                kills = 0,
                playerDamage = 0,
                companionDamage = 0,
                playerKills = 0,
                companionKills = 0
            }
        end

        -- Attribute kill
        local enemy = stats.enemies[normalizedId]
        enemy.kills = enemy.kills + 1
        if attacker == "Player" then
            enemy.playerKills = enemy.playerKills + 1
            stats.playerKills = stats.playerKills + 1
            print(string.format("[Combat] Kill: %s -> Player", normalizedId))
        elseif attacker == "Companion" then
            enemy.companionKills = enemy.companionKills + 1
            stats.companionKills = stats.companionKills + 1
            print(string.format("[Combat] Kill: %s -> Companion", normalizedId))
        end

        -- Clean up lastDamage entry
        stats.lastDamage[npcId] = nil
    end
end

--- Companion damaged handler - extracts damage and instigator ID
--- @param Context userdata Hook context
--- @param InActor userdata Damaged actor
--- @param InInstigator userdata Damage instigator
--- @param InDamage userdata Damage amount
--- @param InHit userdata Hit info
function Combat.OnCompanionDamaged(Context, InActor, InInstigator, InDamage, InHit)
    -- Get damage amount
    local damage = 0
    pcall(function()
        if InDamage then damage = InDamage:get() end
    end)

    -- Get instigator ID from OverrideCharacterID property
    local instigatorId = "Unknown"
    pcall(function()
        if InInstigator then
            local instigator = InInstigator:get()
            if Utils.SafeIsValid(instigator) then
                local charId = instigator.OverrideCharacterID
                if charId then
                    instigatorId = charId:ToString()
                end
            end
        end
    end)

    print(string.format("[Sonorus] COMPANION_DAMAGED: %.1f damage from %s", damage, instigatorId))
end

--- Enemy damaged handler (EnemyAIComponent:OnActorDamaged)
--- @param Context userdata Hook context
--- @param InActor userdata Damaged actor
--- @param InInstigator userdata Damage instigator
--- @param InDamage userdata Damage amount
--- @param InHit userdata Hit info
function Combat.OnEnemyDamaged(Context, InActor, InInstigator, InDamage, InHit)
    -- Get damage amount
    local damage = 0
    pcall(function()
        if InDamage then damage = InDamage:get() end
    end)

    -- Get damaged actor info
    local actorId = "Unknown"
    pcall(function()
        if InActor then
            local actor = InActor:get()
            if Utils.SafeIsValid(actor) then
                local charId = actor.OverrideCharacterID
                if charId then
                    actorId = charId:ToString()
                end
            end
        end
    end)

    -- Get instigator actor for type detection
    local instigator = nil
    local instigatorType = nil
    pcall(function()
        if InInstigator then
            instigator = InInstigator:get()
            if Utils.SafeIsValid(instigator) then
                instigatorType = GetInstigatorType(instigator)
            end
        end
    end)

    -- Track damage if in combat and from player/companion
    local stats = _G.CombatStats
    if stats.active and damage > 0 and actorId ~= "Unknown" and instigatorType then
        -- Normalize enemy ID for grouping
        local normalizedId = NormalizeEnemyId(actorId)

        -- Initialize enemy type if needed
        if not stats.enemies[normalizedId] then
            stats.enemies[normalizedId] = {
                damage = 0,
                kills = 0,
                playerDamage = 0,
                companionDamage = 0,
                playerKills = 0,
                companionKills = 0
            }
        end

        -- Track damage by attacker type
        local enemy = stats.enemies[normalizedId]
        enemy.damage = enemy.damage + damage
        if instigatorType == "Player" then
            enemy.playerDamage = enemy.playerDamage + damage
            stats.playerDamage = stats.playerDamage + damage
        elseif instigatorType == "Companion" then
            enemy.companionDamage = enemy.companionDamage + damage
            stats.companionDamage = stats.companionDamage + damage
        end

        -- Track last damage source for kill attribution
        stats.lastDamage[actorId] = {
            instigator = instigatorType,
            timestamp = os.time()
        }
    end

    -- Debug logging (only in dev mode to reduce spam)
    if _G.SonorusDevMode then
        print(string.format("[Combat] Damage: %.1f to %s from %s",
            damage, actorId, instigatorType or "Unknown"))
    end
end

return Combat
