-- ============================================
-- PlayerGear.lua - Player Equipment System
-- ============================================
-- Extracts player gear information (equipped items, transmog, rarity)
-- and formats it for LLM context.

local PlayerGear = {}

-- Cache module for static data access
local Cache = require("Utils.Cache")

-- Slot names mapping
local GearSlotNames = {
    [0] = "HEAD",
    [1] = "OUTFIT",
    [2] = "BACK",
    [3] = "NECK",
    [4] = "HAND",
    [5] = "FACE",
}

-- Extract rarity from GearID (e.g., "Head_068_Legendary" -> "Legendary")
local function GetRarityFromId(gearId)
    if not gearId then return nil end
    local rarity = gearId:match("_(%a+)$")
    if rarity and (rarity == "Common" or rarity == "Uncommon" or rarity == "Rare"
                   or rarity == "Epic" or rarity == "Legendary") then
        return rarity
    end
    return nil
end

-- Get description from localization (item_desc key)
-- Uses _G.Localization and _G.LoadLocalization (defined in logic.lua)
local function GetItemDescription(itemId)
    if not itemId or itemId == "" then return nil end
    if not _G.LocalizationLoaded and _G.LoadLocalization then
        _G.LoadLocalization()
    end
    if not _G.Localization then return nil end
    return _G.Localization[itemId .. "_desc"]
end

-- Helper to get display name (uses _G.GetDisplayName from logic.lua)
local function GetDisplayName(internalName)
    if _G.GetDisplayName then
        return _G.GetDisplayName(internalName)
    end
    -- Fallback: prettify the internal name
    if not internalName or internalName == "" then return "Unknown" end
    return string.gsub(internalName, "(%l)(%u)", "%1 %2")
end

--- Get player's equipped gear as a structured table
-- Returns table with all equipped gear info:
-- {
--   HEAD = { name = "Display Name", id = "Head_068_Legendary", transmogged = true, appearance = "Other Item Name" },
--   OUTFIT = { name = "Display Name", id = "Outfit_089_Legendary", transmogged = false },
--   ...
--   WAND = { equipped = true },
--   HOOD = { up = false }
-- }
function PlayerGear.GetPlayerGear()
    local gear = {}

    -- Get player and GearManager from cache
    local staticData = Cache.GetStaticData()
    local player = staticData and staticData.player
    if not player then return nil end

    local gearManager = staticData.gearManager
    if not gearManager then return nil end

    -- Get player's ActorId for appearance lookups
    local playerActorId = nil
    pcall(function()
        local bpLib = staticData and staticData.bpLibrary
        if bpLib then
            local outTable = {}
            bpLib:GetActorId(player, outTable)
            if outTable.OutActorId then
                playerActorId = outTable.OutActorId:ToString()
            end
        end
    end)

    -- Get each gear slot
    for slotId = 0, 5 do
        local slotName = GearSlotNames[slotId]
        local slotData = { equipped = false }

        pcall(function()
            local gearItemId = gearManager:GetActorEquippedGearItemID(player, slotId)
            if gearItemId and gearItemId.IsEquipped then
                slotData.equipped = true

                -- Get GearID (stats item)
                local gearId = nil
                pcall(function()
                    local fname = gearItemId.GearID
                    if fname then pcall(function() gearId = fname:ToString() end) end
                end)
                slotData.id = gearId
                slotData.name = GetDisplayName(gearId)

                -- Check for transmog
                local hasOverride = false
                pcall(function()
                    hasOverride = gearManager:DoesGearHaveAppearanceOverride(gearItemId)
                end)

                if hasOverride and playerActorId then
                    slotData.transmogged = true
                    -- Get the appearance override
                    pcall(function()
                        local result = gearManager:GetEquippedGearAppearanceOverrideID(playerActorId, slotId)
                        if result then
                            local appearanceId = nil
                            pcall(function() appearanceId = result:ToString() end)
                            if appearanceId and appearanceId ~= "" and appearanceId ~= "None" then
                                slotData.appearanceId = appearanceId
                                slotData.appearance = GetDisplayName(appearanceId)
                            end
                        end
                    end)
                else
                    slotData.transmogged = false
                end
            end
        end)

        gear[slotName] = slotData
    end

    -- Hood status
    pcall(function()
        gear.HOOD = { up = gearManager:IsHoodUp(player) }
    end)

    -- Wand status
    pcall(function()
        gear.WAND = { equipped = player:IsWandEquipped() }
    end)

    return gear
end

--- Format gear for LLM context (human-readable string)
-- Pass existing gear table to avoid redundant GetPlayerGear() call
function PlayerGear.FormatPlayerGearForContext(gear)
    gear = gear or PlayerGear.GetPlayerGear()
    if not gear then return "Unable to get player gear." end

    local lines = {}
    local slotOrder = {"HEAD", "FACE", "NECK", "OUTFIT", "BACK", "HAND"}

    for _, slot in ipairs(slotOrder) do
        local data = gear[slot]
        if data and data.equipped and data.name then
            local rarity = GetRarityFromId(data.id)
            local rarityStr = rarity and (" [" .. rarity .. "]") or ""

            -- Get description: prefer appearance description if transmogged, else base item
            local description = nil
            if data.transmogged and data.appearanceId then
                description = GetItemDescription(data.appearanceId)
            end
            if not description then
                description = GetItemDescription(data.id)
            end

            if data.transmogged and data.appearance then
                -- Transmogged: show what it looks like, note the stats source with rarity
                table.insert(lines, string.format("%s: %s (transmogged, stats from %s%s)",
                    slot, data.appearance, data.name, rarityStr))
            else
                table.insert(lines, string.format("%s: %s%s", slot, data.name, rarityStr))
            end

            -- Add description on next line
            if description then
                table.insert(lines, string.format("  - %s", description))
            end
        end
    end

    -- Accessories
    if gear.HOOD and gear.HOOD.up then
        table.insert(lines, "HOOD: Up")
    end
    -- WAND: Equipped removed - it's always equipped and doesn't indicate wand is drawn/ready

    return table.concat(lines, "\n")
end

return PlayerGear
