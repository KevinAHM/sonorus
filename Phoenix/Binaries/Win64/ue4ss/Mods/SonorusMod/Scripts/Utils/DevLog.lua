-- Utils/DevLog.lua
-- Blocking file breadcrumbs for crash debugging. Each log call opens,
-- writes, flushes, and closes so the last marker survives process crashes.

local DevLog = {}

local socket = require("socket")

local LOG_PATH = "sonorus\\logs\\lua_devlog.log"
local FALLBACK_PATH = "lua_devlog.log"

_G.SonorusDevLogSeq = _G.SonorusDevLogSeq or 0

local function timestamp()
    local now = socket.gettime()
    local seconds = math.floor(now)
    local fractional = math.floor((now - seconds) * 10000000)
    return string.format("[%s.%07d]", os.date("%Y-%m-%d %H:%M:%S", seconds), fractional)
end

local function stringify(value)
    if value == nil then
        return "nil"
    end
    return tostring(value)
end

local function appendLine(path, line)
    if type(io) ~= "table" or type(io.open) ~= "function" then
        return false
    end

    local handle = io.open(path, "a")
    if not handle then
        return false
    end

    handle:write(line)
    handle:write("\n")
    handle:flush()
    handle:close()
    return true
end

function DevLog.Path()
    return LOG_PATH
end

function DevLog.Log(tag, ...)
    _G.SonorusDevLogSeq = (_G.SonorusDevLogSeq or 0) + 1

    local parts = {}
    for i = 1, select("#", ...) do
        parts[#parts + 1] = stringify(select(i, ...))
    end

    local line = string.format(
        "%s [%06d] [%s] %s",
        timestamp(),
        _G.SonorusDevLogSeq,
        tostring(tag or "DevLog"),
        table.concat(parts, " ")
    )

    if not appendLine(LOG_PATH, line) then
        appendLine(FALLBACK_PATH, line)
    end
end

function DevLog.Mark(tag, message)
    DevLog.Log(tag, message)
end

DevLog.Log("DevLog", "session marker")

_G.DevLog = DevLog

return DevLog
