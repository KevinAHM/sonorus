-- Shared Lua tick scheduler.
-- Keeps the main always-on work on one UE4SS timer, then fans out by elapsed time.

local TickScheduler = {}

local DEFAULT_INTERVAL_MS = 23
local DISABLE_FOR_LAG_TEST = false

local state = _G.SonorusTickScheduler or {}

if state.handle and CancelDelayedAction then
    pcall(CancelDelayedAction, state.handle)
end

state.handle = nil
state.intervalMs = state.intervalMs or DEFAULT_INTERVAL_MS
state.tasks = {}
state.version = (state.version or 0) + 1
_G.SonorusTickScheduler = state

local function log(...)
    if _G.DevPrint then
        _G.DevPrint("[TickScheduler]", ...)
    else
        print("[TickScheduler] " .. table.concat({...}, " "))
    end
end

local function secondsFromMs(intervalMs)
    return (tonumber(intervalMs) or state.intervalMs or DEFAULT_INTERVAL_MS) / 1000.0
end

function TickScheduler.Register(id, intervalMs, fn, opts)
    if not id or type(fn) ~= "function" then
        return false
    end

    opts = opts or {}
    local now = os.clock()
    local intervalSec = secondsFromMs(intervalMs)

    state.tasks[id] = {
        id = id,
        intervalMs = intervalMs or state.intervalMs,
        intervalSec = intervalSec,
        fn = fn,
        enabled = true,
        nextRun = opts.runImmediately and now or (now + intervalSec),
    }

    return true
end

function TickScheduler.Unregister(id)
    if id then
        state.tasks[id] = nil
    end
end

function TickScheduler.SetEnabled(id, enabled)
    local task = id and state.tasks[id]
    if not task then return false end
    task.enabled = enabled and true or false
    return true
end

function TickScheduler.SetInterval(id, intervalMs)
    local task = id and state.tasks[id]
    if not task then return false end
    task.intervalMs = intervalMs
    task.intervalSec = secondsFromMs(intervalMs)
    task.nextRun = os.clock() + task.intervalSec
    return true
end

local function runDueTasks()
    local now = os.clock()
    local due = {}

    for id, task in pairs(state.tasks) do
        if task.enabled ~= false and now >= (task.nextRun or 0) then
            due[#due + 1] = id
        end
    end

    for _, id in ipairs(due) do
        local task = state.tasks[id]
        if task and task.enabled ~= false then
            task.nextRun = now + (task.intervalSec or 0)
            local ok, err = pcall(task.fn, now, id)

            if not ok then
                log("task error", tostring(id), tostring(err))
            end
        end
    end
end

function TickScheduler.Start(intervalMs)
    if intervalMs then
        state.intervalMs = intervalMs
    end

    if DISABLE_FOR_LAG_TEST then
        if state.handle and CancelDelayedAction then
            pcall(CancelDelayedAction, state.handle)
        end
        state.handle = nil
        print("[TickScheduler] Shared loop DISABLED for lag test")
        return
    end

    if state.handle and IsValidDelayedActionHandle and IsValidDelayedActionHandle(state.handle) then
        return
    end

    state.handle = LoopInGameThreadWithDelay(state.intervalMs or DEFAULT_INTERVAL_MS, runDueTasks)
    print("[TickScheduler] Shared loop started (" .. tostring(state.intervalMs or DEFAULT_INTERVAL_MS) .. "ms)")
end

function TickScheduler.Stop()
    if state.handle and CancelDelayedAction then
        pcall(CancelDelayedAction, state.handle)
    end
    state.handle = nil
end

function TickScheduler.GetState()
    return state
end

TickScheduler.Start(state.intervalMs)

return TickScheduler
