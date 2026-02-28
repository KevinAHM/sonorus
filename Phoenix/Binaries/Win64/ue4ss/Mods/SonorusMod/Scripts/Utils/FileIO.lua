-- FileIO.lua - File I/O helpers for Sonorus
-- Pure utilities with no game logic dependencies

---@class FileIO
local FileIO = {}

-- JSON library (rxi/json) - needed for ParseJsonResponse
local json = require "json"

---Read entire file contents
---@param path string File path to read
---@return string content File contents or empty string on error
function FileIO.ReadFile(path)
    -- Defensive: check io.open exists and is a function
    if type(io) ~= "table" or type(io.open) ~= "function" then
        print("[Sonorus] ERROR: io.open corrupted, skipping file read")
        return ""
    end

    local ok, f = pcall(function() return io.open(path, "r") end)
    if not ok or not f then
        return ""
    end

    -- Verify f is a file handle, not something weird
    if type(f) ~= "userdata" then
        print("[Sonorus] ERROR: io.open returned unexpected type: " .. type(f))
        return ""
    end

    local content = ""
    ok = pcall(function()
        content = f:read("*a") or ""
        f:close()
    end)

    return content
end

---Write content to file
---@param path string File path to write
---@param content string Content to write
---@return boolean success True if write succeeded
function FileIO.WriteFile(path, content)
    local f = io.open(path, "w")
    if f then
        f:write(content)
        f:close()
        return true
    end
    return false
end

---Clear file contents (write empty string)
---@param path string File path to clear
function FileIO.ClearFile(path)
    FileIO.WriteFile(path, "")
end

---Parse JSON string safely
---@param jsonStr string JSON string to parse
---@return table result Parsed table or empty table on error
function FileIO.ParseJsonResponse(jsonStr)
    if not jsonStr or jsonStr == "" then return {} end
    local ok, result = pcall(json.decode, jsonStr)
    if ok and result then return result end
    return {}
end

---Load JSON file with caching in globals
---Handles the common pattern: check loaded flag, read file, parse JSON, store in _G, set flag
---@param globalKey string The key to store data in _G (e.g., "Subtitles" -> _G.Subtitles)
---@param filePath string Path to the JSON file
---@param description string|nil Human-readable description for logging (e.g., "subtitle")
---@return boolean success True if loaded (or already loaded), false on error
function FileIO.LoadJsonCached(globalKey, filePath, description)
    local loadedKey = globalKey .. "Loaded"

    -- Already loaded check
    if _G[loadedKey] then return true end

    description = description or globalKey
    print(string.format("[Sonorus] Loading %s...", description))

    local content = FileIO.ReadFile(filePath)
    if content == "" then
        print(string.format("[Sonorus] Warning: %s not found or empty", filePath))
        return false
    end

    local ok, result = pcall(json.decode, content)
    if not ok or not result then
        print(string.format("[Sonorus] Error parsing %s", filePath))
        return false
    end

    _G[globalKey] = result
    _G[loadedKey] = true

    local count = 0
    for _ in pairs(result) do count = count + 1 end
    print(string.format("[Sonorus] Loaded %d %s entries", count, description))

    return true
end

return FileIO
