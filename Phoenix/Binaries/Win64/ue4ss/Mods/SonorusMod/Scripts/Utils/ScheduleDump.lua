-- ScheduleDump.lua
-- Dumps the game scheduler DB to the Python server once per session.

local ScheduleDump = {}

local TAG = "[ScheduleDump]"
local CHUNK_ROWS = 150
local SEND_INTERVAL_MS = 50

local _dumpStarted = false
local _startHandle = nil
local _sendHandle = nil
local _retryCount = 0

local function QueryRows(sql)
    local DbGateway = FindFirstOf("DbGateway")
    if not DbGateway or not DbGateway:IsValid() then return nil, "DbGateway not found" end

    local outResult = {}
    local ok, err = pcall(function()
        DbGateway:DbQuery(sql, outResult)
    end)
    if not ok then return nil, tostring(err) end
    if not outResult.Success then
        local msg = "query failed"
        pcall(function() msg = outResult.ErrorMessage:ToString() end)
        return nil, msg
    end

    local rows = {}
    for _, rowElem in pairs(outResult.ResultRows) do
        local row = rowElem:get()
        local dict = {}
        row.Fields:ForEach(function(_, fieldElem)
            local field = fieldElem:get()
            dict[field.Key:ToString()] = field.Value:ToString()
        end)
        table.insert(rows, dict)
    end
    return rows, nil
end

local function ChunkJobs(dumpId, tableName, kind, rows)
    local jobs = {}
    local total = math.max(1, math.ceil(#rows / CHUNK_ROWS))
    for i = 1, total do
        local slice = {}
        for j = (i - 1) * CHUNK_ROWS + 1, math.min(i * CHUNK_ROWS, #rows) do
            table.insert(slice, rows[j])
        end
        table.insert(jobs, {
            type = "schedule_dump",
            dump_id = dumpId,
            table = tableName,
            kind = kind,
            chunk = i,
            total_chunks = total,
            rows = slice,
        })
    end
    return jobs
end

function ScheduleDump.OnPlayerReady()
    local flags = _G.PresenceLedgerPhaseFlags or {}
    if flags.scheduleDump ~= true then
        print(TAG .. " disabled by phase gate\n")
        return
    end
    if _dumpStarted then return end
    _dumpStarted = true

    _startHandle = ExecuteInGameThreadWithDelay(3000, function()
        _startHandle = nil
        local dumpId = tostring(os.time()) .. "-" .. tostring(math.floor(os.clock() * 1000))
        local jobs = {}
        local failures = {}

        local specs = {
            { sql = "SELECT * FROM ActivityDefinition", name = "ActivityDefinition", kind = "activity" },
            { sql = "SELECT * FROM Locations", name = "Locations", kind = "location" },
            { sql = "SELECT * FROM Schedule_Overland", name = "Schedule_Overland", kind = "schedule_entries" },
        }
        local knownTables = { Schedule_Overland = true }

        local tabRows, tabErr = QueryRows(
            "SELECT name AS TableName FROM sqlite_master WHERE type='table' AND name LIKE 'Schedule\\_%' ESCAPE '\\'")
        local scheduleTableCount = 0
        if tabRows then
            for _, row in ipairs(tabRows) do
                local name = row.TableName or row.name or row.Name
                if name and name ~= "SchedulesForLevels" and not knownTables[name] then
                    scheduleTableCount = scheduleTableCount + 1
                    knownTables[name] = true
                    local quotedName = name:gsub('"', '""')
                    table.insert(specs, {
                        sql = "SELECT * FROM \"" .. quotedName .. "\"",
                        name = name,
                        kind = "schedule_entries",
                    })
                end
            end
            if scheduleTableCount == 0 then
                print(TAG .. " optional Schedule_* discovery returned no additional tables\n")
            end
        else
            print(TAG .. " optional schedule table discovery failed: " .. tostring(tabErr) .. "\n")
        end

        for _, spec in ipairs(specs) do
            local rows, err = QueryRows(spec.sql)
            if rows then
                print(string.format("%s %s: %d rows\n", TAG, spec.name, #rows))
                for _, job in ipairs(ChunkJobs(dumpId, spec.name, spec.kind, rows)) do
                    table.insert(jobs, job)
                end
            else
                print(string.format("%s %s FAILED: %s\n", TAG, spec.name, tostring(err)))
                table.insert(failures, spec.name .. ": " .. tostring(err))
            end
        end

        table.insert(jobs, {
            type = "schedule_dump",
            dump_id = dumpId,
            table = "__done__",
            kind = "done",
            chunk = 1,
            total_chunks = 1,
            rows = {},
            success = #failures == 0,
            errors = failures,
        })

        local idx = 0
        local nextRetryAt = 0
        local dumpSucceeded = #failures == 0
        _sendHandle = LoopInGameThreadWithDelay(SEND_INTERVAL_MS, function()
            local job = jobs[idx + 1]
            if not job then
                CancelDelayedAction(_sendHandle)
                _sendHandle = nil
                print(TAG .. " dump send complete (" .. tostring(idx) .. " chunks)\n")
                if not dumpSucceeded then
                    _retryCount = _retryCount + 1
                    if _G.PresenceDebug then
                        ShowHint("Schedule dump failed (attempt " .. tostring(_retryCount) .. ")")
                    end
                    if _retryCount <= 3 then
                        _dumpStarted = false
                        _startHandle = ExecuteInGameThreadWithDelay(10000, function()
                            _startHandle = nil
                            ScheduleDump.OnPlayerReady()
                        end)
                    else
                        _dumpStarted = false
                    end
                else
                    _retryCount = 0
                end
                return
            end
            if os.clock() < nextRetryAt then return end
            local sent = false
            if _G.SocketClient and _G.SocketClient.send then
                sent = _G.SocketClient.send(job) == true
            end
            if sent then
                idx = idx + 1
            else
                nextRetryAt = os.clock() + 1.0
            end
        end)
    end)
end

function ScheduleDump.Stop()
    if _startHandle then
        pcall(CancelDelayedAction, _startHandle)
        _startHandle = nil
    end
    if _sendHandle then
        pcall(CancelDelayedAction, _sendHandle)
        _sendHandle = nil
    end
    _dumpStarted = false
    _retryCount = 0
end

return ScheduleDump
