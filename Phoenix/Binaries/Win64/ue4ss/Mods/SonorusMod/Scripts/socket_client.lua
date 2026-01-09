-- Socket Client for Python server communication
-- Receives lipsync and viseme events via TCP socket
print("[SocketClient] Loading...")

local socket = require("socket")
local json = require("json")
local Utils = require("Utils.Utils")

-- Local dev print helper (DevPrint in logic.lua not loaded yet)
local function DevPrint(...)
    if _G.SonorusDevMode then
        print(...)
    end
end

local SocketClient = {}
local client = nil
local buffer = ""
local SERVER_PORT = 8173

-- Send queue to prevent interleaving (Lua callbacks can interleave)
local sendQueue = {}
local sendInProgress = false

-- Connection state (centralized tracking)
local connectionState = {
    connected = false,              -- True only when socket is verified working
    reconnectTime = 0,              -- Last connection attempt time
    reconnectDelayMode = "fast",    -- "fast" (1s) or "normal" (10s) backoff
    fastRetryCount = 0,             -- Consecutive failures in fast mode
    consecutiveFailures = 0,        -- Total consecutive failures
    lastStatusLog = 0,              -- Throttle status logging
}

-- Config
local RECONNECT_DELAY_NORMAL = 10  -- Normal backoff (server might be down)
local RECONNECT_DELAY_FAST = 1     -- Fast mode after explicit close/server restart
local MAX_FAST_RETRIES = 20        -- Server takes ~10s to start, plus 5s heartbeat delay = 15s minimum

-- Chat input state (static cursor, no blinking)
_G.ChatInputState = _G.ChatInputState or { active = false, text = "" }

-- Pause state tracking for immediate context updates
_G.LastKnownPauseState = _G.LastKnownPauseState or false

-- Activity state from Python (for ambient dialog gating)
_G.GameWindowForeground = (_G.GameWindowForeground == nil) and true or _G.GameWindowForeground  -- Default true until Python says otherwise
_G.PlayerIdleState = _G.PlayerIdleState or false  -- Default false until Python says otherwise

-- Tracking settings from config (for dialogue recording toggles)
_G.TrackAmbientDialogue = (_G.TrackAmbientDialogue == nil) and true or _G.TrackAmbientDialogue  -- Default true
_G.TrackCutsceneDialogue = (_G.TrackCutsceneDialogue == nil) and true or _G.TrackCutsceneDialogue  -- Default true

-- NOTE: Always use _G.SonorusState directly (no local State shadows) to avoid
-- closure capture issues that could corrupt UE4SS Lua registry references

-- Pause state monitor (500ms interval) - pushes context update on change
if not _G.PauseMonitorStarted then
    _G.PauseMonitorStarted = true
    _G.PauseMonitorHandle = LoopInGameThreadWithDelay(500, function()
        local currentPaused = Utils.IsGamePaused()
        if currentPaused ~= _G.LastKnownPauseState then
            _G.LastKnownPauseState = currentPaused
            print("[SocketClient] Pause state changed: " .. tostring(currentPaused))
            -- Send immediate context update to Python
            -- Use _G.SocketClient so this works after F11 reload (closure has stale reference)
            if _G.SocketClient and _G.SocketClient.isConnected() then
                _G.SocketClient.send({
                    type = "pause_state",
                    paused = currentPaused
                })
                -- If paused and chat was active, tell Python to close it
                if currentPaused and _G.ChatInputState.active then
                    _G.SocketClient.send({
                        type = "force_close_chat",
                        reason = "game_paused"
                    })
                    -- Also clear the hint immediately (already on game thread)
                    local uiManager = FindFirstOf("UIManager")
                    if uiManager and uiManager:IsValid() then
                        pcall(function()
                            uiManager:ClearHintMessage()
                        end)
                    end
                    _G.ChatInputState.active = false
                    _G.ChatInputState.text = ""
                end
            end
        end
    end)
    print("[SocketClient] Pause state monitor started")
end

-- NOTE: 30ms chat input poll loop REMOVED - consolidated into 100ms unified loop in logic.lua
-- The 100ms interval is fast enough for responsive chat input while reducing CPU load

function SocketClient.connect()
    if client and connectionState.connected then return true end

    -- If client exists but not connected, close it first (stale socket)
    if client and not connectionState.connected then
        pcall(function() client:close() end)
        client = nil
    end

    -- Use fast or normal delay based on current mode
    local delay = connectionState.reconnectDelayMode == "fast" and RECONNECT_DELAY_FAST or RECONNECT_DELAY_NORMAL
    local now = os.clock()
    if now - connectionState.reconnectTime < delay then
        return false  -- Don't spam reconnect attempts
    end
    connectionState.reconnectTime = now
    print("[SocketClient] Attempting connect to port " .. SERVER_PORT .. "...")

    local ok, err = pcall(function()
        client = socket.tcp()
        client:settimeout(0.1)  -- Short timeout for connect
        local result, cerr = client:connect("127.0.0.1", SERVER_PORT)
        if not result then
            print("[SocketClient] Connect failed: " .. tostring(cerr))
            client:close()
            client = nil
            return
        end
        client:settimeout(0)  -- Non-blocking for receive
        buffer = ""  -- Clear receive buffer on new connection
        -- Clear send queue too (old queued messages may be stale/corrupted)
        sendQueue = {}
        sendInProgress = false
        print("[SocketClient] Connected to Python server on port " .. SERVER_PORT)
    end)

    if not ok then
        print("[SocketClient] Connect error: " .. tostring(err))
        client = nil
    end

    -- Handle connection result
    if client then
        -- Success! Reset state
        connectionState.connected = true
        connectionState.reconnectDelayMode = "normal"
        connectionState.fastRetryCount = 0
        connectionState.consecutiveFailures = 0
    else
        -- Failed - track retries
        connectionState.connected = false
        connectionState.consecutiveFailures = connectionState.consecutiveFailures + 1
        if connectionState.reconnectDelayMode == "fast" then
            connectionState.fastRetryCount = connectionState.fastRetryCount + 1
            if connectionState.fastRetryCount >= MAX_FAST_RETRIES then
                connectionState.reconnectDelayMode = "normal"
                print("[SocketClient] Switching to normal reconnect interval (10s)")
            end
        end
    end

    return client ~= nil and connectionState.connected
end

-- Debug: track last receive log time to avoid spam
local _lastReceiveDebugLog = 0

function SocketClient.update()
    -- Try to connect if not connected
    if not client or not connectionState.connected then
        -- Log status periodically when disconnected (every 30 seconds)
        local now = os.clock()
        if (now - connectionState.lastStatusLog) > 30 then
            connectionState.lastStatusLog = now
            print(string.format("[SocketClient] Status: disconnected, mode=%s, failures=%d",
                connectionState.reconnectDelayMode, connectionState.consecutiveFailures))
        end
        SocketClient.connect()
        return
    end

    -- Receive data (non-blocking)
    while true do
        local chunk, err, partial = client:receive(1024)

        if chunk then
            buffer = buffer .. chunk
        elseif partial and #partial > 0 then
            buffer = buffer .. partial
        elseif err == "closed" then
            print("[SocketClient] Connection closed by server")
            pcall(function() client:close() end)
            client = nil
            buffer = ""
            -- Mark as disconnected and switch to fast reconnect
            connectionState.connected = false
            connectionState.reconnectDelayMode = "fast"
            connectionState.fastRetryCount = 0
            return
        elseif err == "timeout" then
            -- No more data available (non-blocking)
            break
        else
            -- Other error
            print("[SocketClient] Receive error: " .. tostring(err))
            pcall(function() client:close() end)
            client = nil
            buffer = ""
            connectionState.connected = false
            connectionState.reconnectDelayMode = "fast"
            connectionState.fastRetryCount = 0
            return
        end
    end

    -- Process complete messages (newline-delimited JSON)
    while true do
        local newlinePos = string.find(buffer, "\n")
        if not newlinePos then break end

        local msg = string.sub(buffer, 1, newlinePos - 1)
        buffer = string.sub(buffer, newlinePos + 1)

        if msg and #msg > 0 then
            local ok, data = pcall(json.decode, msg)
            if ok and data then
                SocketClient.handleMessage(data)
            else
                print("[SocketClient] Failed to decode: " .. msg:sub(1, 100))
            end
        end
    end
end

function SocketClient.handleMessage(data)
    local msgType = data.type

    -- Handle request_context early - doesn't need SonorusState/VisemeData
    if msgType == "request_context" then
        local groups = data.groups
        if groups and #groups > 0 and groups[1] ~= "all" then
            if WriteSelectiveContext then
                print("[Socket] Sending selective context: " .. table.concat(groups, ", "))
                WriteSelectiveContext(groups)
            else
                print("[Socket] WriteSelectiveContext not available, falling back to full context")
                if WriteGameContext then WriteGameContext() end
            end
        else
            if WriteGameContext then
                print("[Socket] Sending full game context on request")
                WriteGameContext()
            else
                print("[Socket] WriteGameContext not available!")
            end
        end
        return
    end

    -- Handle activity_state early - doesn't need SonorusState/VisemeData
    if msgType == "activity_state" then
        _G.GameWindowForeground = data.foreground
        return
    end

    -- Handle tracking_settings - dialogue recording toggles from config
    if msgType == "tracking_settings" then
        _G.TrackAmbientDialogue = data.track_ambient
        _G.TrackCutsceneDialogue = data.track_cutscene
        print(string.format("[Socket] Tracking settings: ambient=%s, cutscene=%s",
            tostring(data.track_ambient), tostring(data.track_cutscene)))
        return
    end

    -- Ensure globals exist (socket may connect before logic.lua initializes)
    local vd = _G.VisemeData
    if not _G.SonorusState or not vd then
        print("[Socket] Warning: globals not initialized, ignoring: " .. tostring(msgType))
        return
    end

    if msgType == "lipsync_start" then
        -- Handshake: Python sends this before starting audio
        -- We set up state, load initial visemes, then ACK so audio can start
        local turnId = data.turn_id

        -- Use _G.SonorusState directly (not local State which may be stale)
        if _G.SonorusState then
            -- Set current turn ID (this is when we actually start playing this turn)
            if turnId then
                _G.SonorusState.currentTurnId = turnId
            end
            -- Phase-based state machine
            _G.SonorusState.phase = "playing"
            -- CRITICAL: Reset messageShown for new turn
            -- This fixes race condition where lipsync_start arrives before closing phase completes
            -- Without this, messageShown stays true from previous turn and subtitle is skipped
            _G.SonorusState.messageShown = false
        end

        -- CRITICAL: Set PlaybackState.playing = true so GetCurrentSpeakerActor works
        if _G.PlaybackState then
            _G.PlaybackState.playing = true
            -- Find this turn in the queue and set currentIndex to match
            -- This handles rapid turn transitions where closing phase is skipped
            if turnId then
                local foundIndex = nil
                for i, item in ipairs(_G.PlaybackState.queue or {}) do
                    if item.turnId == turnId then
                        foundIndex = i
                        break
                    end
                end
                if foundIndex then
                    _G.PlaybackState.currentIndex = foundIndex
                else
                    _G.PlaybackState.currentIndex = _G.PlaybackState.currentIndex or 1
                end
            else
                _G.PlaybackState.currentIndex = _G.PlaybackState.currentIndex or 1
            end
        end

        -- NOTE: OnTick loop is now persistent (started once at module load)
        -- using LoopInGameThreadWithDelay for proper timer control
        print("[Socket] Lipsync active - OnTick loop will process\n")

        -- Initialize timing - this is our t=0 reference
        vd.localStartTime = os.clock()
        vd.lastAudioSync = nil  -- Will be set by audio_sync messages
        vd.audioOffset = 0      -- Drift correction offset
        vd.syncPrinted = false

        -- Clear old frames and load initial visemes from this message
        vd.frames = {}
        vd.loaded = false

        -- Load embedded visemes (sent with lipsync_start for initial sync)
        local initialVisemes = data.visemes
        if initialVisemes and #initialVisemes > 0 then
            for _, f in ipairs(initialVisemes) do
                table.insert(vd.frames, {
                    t = f[1],
                    jaw = f[2],
                    smile = f[3],
                    funnel = f[4]
                })
            end
            vd.loaded = true
            print(string.format("[Socket] Loaded %d initial visemes with lipsync_start\n", #initialVisemes))
        end

        -- Store per-character lipsync scale (default 1.0)
        vd.scale = data.scale or 1.0

        local scaleStr = vd.scale ~= 1.0 and string.format(", scale=%.2f", vd.scale) or ""
        print("[Socket] Lipsync start - turn=" .. tostring(turnId) ..
              ", speaker=" .. tostring(data.speaker) ..
              ", visemes=" .. tostring(initialVisemes and #initialVisemes or 0) .. scaleStr .. "\n")

        -- Lock NPCs for this turn (now that it's actually playing)
        -- NOTE: Already on game thread via LoopInGameThreadWithDelay, no wrapper needed
        DevPrint("[DEBUG] lipsync_start lock NPCs START turn=" .. tostring(turnId))
        if _G.PlaybackState and _G.PlaybackState.queue then
            -- Find the queue item for this turn
            local currentItem = nil
            for _, item in ipairs(_G.PlaybackState.queue) do
                if item.turnId == turnId then
                    currentItem = item
                    break
                end
            end

            if currentItem and LockNPCToTarget then
                local speakerActor = currentItem.speakerActor
                local targetActor = currentItem.targetActor
                local targetId = currentItem.targetId
                local speakerId = currentItem.speakerId
                local isPlayerSpeaking = (speakerId == "player")

                if speakerActor and targetActor then
                    -- Only lock speaker if it's an NPC (not the player)
                    if not isPlayerSpeaking then
                        LockNPCToTarget(speakerActor, targetActor)
                        -- Stop any ambient lip sync before AI lipsync starts
                        if StopNPCDialogueLipSync then
                            StopNPCDialogueLipSync(speakerActor)
                        end
                        print("[Socket] Turn start: locked speaker facing target\n")
                    else
                        print("[Socket] Turn start: player is speaking, not locking player\n")
                    end

                    -- If target is NPC (not player), target faces speaker
                    if targetId and targetId ~= "player" then
                        LockNPCToTarget(targetActor, speakerActor)
                        print("[Socket] Turn start: locked target facing speaker\n")
                    end
                end
            end
        end
        DevPrint("[DEBUG] lipsync_start lock NPCs END")

        -- ACK to Python: We're ready, start audio now!
        -- This completes the handshake - Python waits for this before playing audio
        SocketClient.send({ type = "lipsync_ready", turn_id = turnId })

    elseif msgType == "lipsync_stop" then
        -- Audio ended - trigger closing sequence
        DevPrint("[Socket] Lipsync stop received\n")
        -- Clear frames for next utterance
        vd.frames = {}
        vd.loaded = false
        -- Reset timing state for next turn
        vd.audioOffset = 0
        vd.localStartTime = nil
        -- Phase-based state machine
        if _G.SonorusState then
            _G.SonorusState.phase = "closing"
            _G.CloseLipsIterations = 0  -- Reset timeout counter for new close
        end

    elseif msgType == "visemes" then
        -- Batch of viseme frames received
        local frames = data.frames
        if frames and #frames > 0 then
            -- Append to existing frames (streaming)
            if not vd.frames then vd.frames = {} end

            for _, f in ipairs(frames) do
                table.insert(vd.frames, {
                    t = f[1],
                    jaw = f[2],
                    smile = f[3],
                    funnel = f[4]
                })
            end
            vd.loaded = true
            print(string.format("[Socket] Received %d viseme frames (total: %d)\n",
                #frames, #vd.frames))
        end

    elseif msgType == "audio_sync" then
        -- Audio position sync from Python - correct drift between our clock and actual audio
        -- Python sends this every ~100ms during playback
        local audioPosition = data.position  -- Actual audio playback position in seconds
        local turnId = data.turn_id

        -- Only process if this is for the current turn
        if _G.SonorusState and _G.SonorusState.currentTurnId == turnId then
            local now = os.clock()
            local localElapsed = now - vd.localStartTime  -- Our estimate of audio position
            local drift = audioPosition - localElapsed     -- Positive = we're behind, negative = we're ahead

            -- Update offset for drift correction
            -- Use smoothing to avoid sudden jumps (lerp toward new offset)
            local alpha = 0.3  -- How fast to correct (0.3 = 30% toward new value each update)
            vd.audioOffset = (vd.audioOffset or 0) * (1 - alpha) + drift * alpha

            -- Store for debugging
            vd.lastAudioSync = {
                audioPos = audioPosition,
                localElapsed = localElapsed,
                drift = drift,
                offset = vd.audioOffset,
                time = now
            }

            -- Only log very large drift (> 200ms) and only once per session
            if math.abs(drift) > 0.2 and not vd.syncPrinted then
                vd.syncPrinted = true
                print(string.format("[Socket] Large drift detected: %.0fms\n", drift * 1000))
            end
        end

    elseif msgType == "queue_item" then
        -- New queue item pushed from server
        local item = data.item
        if item then
            local pState = _G.PlaybackState
            if pState then
                table.insert(pState.queue, item)
                print("[Socket] Queue item received: " .. tostring(item.speaker))
                -- Mute speaker (function defined in logic.lua)
                if MuteQueueSpeakers then
                    MuteQueueSpeakers({item})
                end
            end
        end

    elseif msgType == "conversation_state" then
        -- State change from server
        local pState = _G.PlaybackState
        if pState then
            local prevState = pState.serverState  -- Save before updating
            pState.serverState = data.state

            if data.interrupted then
                -- Clear pending turns, keep only current
                local currentTurnId = _G.SonorusState and _G.SonorusState.currentTurnId
                local currentActor = currentTurnId and _G.TurnActorCache and _G.TurnActorCache[currentTurnId]

                -- Clear turn cache except current
                _G.TurnActorCache = {}
                if currentTurnId and currentActor then
                    _G.TurnActorCache[currentTurnId] = currentActor
                end

                -- Clear queue except current
                local current = pState.queue[pState.currentIndex]
                pState.queue = current and {current} or {}
                pState.currentIndex = 1
                print("[Socket] Conversation interrupted - cleared pending turns")
            elseif data.state == "playing" and prevState ~= "playing" then
                -- New conversation starting (not interrupt) - clear old queue
                -- This prevents old queue items from accumulating across conversations
                pState.queue = {}
                pState.currentIndex = 1
                pState.playing = false
                _G.TurnActorCache = {}
                if _G.SonorusState then
                    _G.SonorusState.phase = "preparing"
                    _G.SonorusState.currentTurnId = nil
                end
                print("[Socket] New conversation - cleared queue\n")
            end

            -- Handle idle state
            if data.state == "idle" and _G.SonorusState then
                -- If we're still closing the mouth, defer the idle transition
                -- The OnTick closing handler in logic.lua will complete the cleanup
                if _G.SonorusState.phase == "closing" or _G.SonorusState.closing then
                    _G.SonorusState.pendingIdle = true
                    DevPrint("[Socket] Deferring idle - still closing mouth\n")
                else
                    _G.SonorusState.phase = "idle"
                    _G.SonorusState.currentTurnId = nil
                    _G.SonorusState.pendingIdle = false
                    _G.TurnActorCache = {}

                    -- Unmute all speakers when conversation ends
                    if UnmuteAllSpeakers then
                        UnmuteAllSpeakers()
                    end

                    -- Release all locked NPCs when conversation ends
                    if ReleaseAllNPCs then
                        ReleaseAllNPCs()
                    end

                    -- Hide subtitles when conversation ends
                    if HideMessage then
                        HideMessage()
                    end
                end
            end

            DevPrint("[Socket] Conversation state: " .. tostring(data.state) .. "\n")
        end

    elseif msgType == "player_message" then
        -- Player message - show immediately as subtitle, auto-hide after delay
        local speaker = data.speaker or "You"
        local text = data.text or ""
        if text ~= "" then
            print("[Socket] Player message: " .. text)
            local msg = speaker .. ": " .. text
            DevPrint("[DEBUG] player_message show START")
            local ok, err = pcall(function()
                if _G.ShowMessage then
                    _G.ShowMessage(msg)
                end
            end)
            if not ok then DevPrint("[DEBUG] player_message show error: " .. tostring(err)) end
            DevPrint("[DEBUG] player_message show END")
            -- Auto-hide after 3 seconds (NPC response will replace it anyway)
            ExecuteInGameThreadWithDelay(3000, function()
                DevPrint("[DEBUG] player_message hide START")
                local ok, err = pcall(function()
                    if _G.HideMessage then
                        _G.HideMessage()
                    end
                end)
                if not ok then DevPrint("[DEBUG] player_message hide error: " .. tostring(err)) end
                DevPrint("[DEBUG] player_message hide END")
            end)
        end

    elseif msgType == "chat_input" then
        -- In-game text input update (from keyboard capture)
        -- Just update global state - display is handled by logic.lua (hot-reloadable)
        local state = _G.ChatInputState
        state.text = data.text or ""
        state.active = data.active
        state.dirty = true  -- Signal logic.lua to update display

    elseif msgType == "chat_submit" then
        -- Chat submitted - clear hint (spell detection + chat processing happens Python-side)
        local text = data.text or ""
        print("[Socket] Chat submitted: " .. text)

        -- Clear global state so blink loop stops
        local state = _G.ChatInputState
        state.active = false
        state.text = ""

        local uiManager = FindFirstOf("UIManager")
        if uiManager then
            local valid = false
            pcall(function() valid = uiManager:IsValid() end)
            if valid then
                pcall(function()
                    uiManager:ClearHintMessage()
                end)
            end
        end

    elseif msgType == "prepare_speaker" then
        -- Pre-TTS speaker preparation (async-safe handshake)
        -- Server sends this BEFORE starting TTS so we can cache actor and start WritePositions
        local speakerId = data.speaker_id
        -- local speakerName = data.speaker_name
        print("[Socket] Preparing speaker: " .. tostring(speakerId))

        local found = false

        -- Initialize cache if needed
        if not _G.SpeakerActorCache then
            _G.SpeakerActorCache = {}
        end

        -- Set the current speaker ID (used by GetCurrentSpeakerActor fallback)
        _G.CurrentSpeakerId = speakerId

        -- Scan nearby NPCs to populate cache (text input flow doesn't call StartConversation)
        if GetNearbyNPCs then
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                for _, entry in ipairs(npcResult.nearbyList) do
                    if entry.name and entry.name ~= "Unknown" and entry.actor then
                        _G.SpeakerActorCache[entry.name] = entry.actor
                    end
                end
                print("[Socket] Cached " .. #npcResult.nearbyList .. " nearby NPCs")
            end
        end

        -- Now try to find the speaker in cache
        local actor = _G.SpeakerActorCache[speakerId]
        if actor then
            found = true
            print("[Socket] Speaker actor ready: " .. tostring(speakerId))
        else
            print("[Socket] Speaker actor not found: " .. tostring(speakerId))
        end

        -- Send ready signal back to Python (even if not found - don't block forever)
        SocketClient.send({
            type = "speaker_ready",
            speaker_id = speakerId,
            found = found
        })

    elseif msgType == "play_turn" then
        -- NEW: Atomic turn processing (replaces prepare_speaker + queue_item)
        -- Everything happens on game thread to eliminate race conditions
        local turnId = data.turn_id
        local speakerId = data.speaker_id
        local displayName = data.display_name
        local text = data.text
        local turnIndex = data.turn_index or 1
        local targetId = data.target_id or "player"
        print("[Socket] Processing play_turn: " .. tostring(turnId) .. " speaker=" .. tostring(speakerId) .. " -> " .. tostring(targetId) .. "\n")

        -- Store turn data in globals to avoid closure capture issues
        _G._PendingTurn = {
            turnId = turnId,
            speakerId = speakerId,
            displayName = displayName,
            text = text,
            turnIndex = turnIndex,
            targetId = targetId
        }

        DevPrint("[DEBUG] play_turn game thread START turn=" .. tostring(turnId))
        local actorFound = false
        local targetActor = nil

        -- Initialize caches if needed
        _G.TurnActorCache = _G.TurnActorCache or {}
        _G.SpeakerActorCache = _G.SpeakerActorCache or {}

        -- Get player actor (needed if speaker or target is player)
        local player = nil
        pcall(function() player = FindFirstOf("Biped_Player") end)

        -- Check if speaker is the player
        local actor = nil
        local isPlayerSpeaker = (speakerId == "player")

        if isPlayerSpeaker then
            -- Player is speaking - use player actor
            local playerValid = false
            if player then pcall(function() playerValid = player:IsValid() end) end
            if playerValid then
                actor = player
                actorFound = true
                print("[Socket] Speaker is PLAYER, using Biped_Player actor\n")
            else
                print("[Socket] Speaker is PLAYER but player actor is nil!\n")
            end

            -- Still scan nearby NPCs to find the target and populate cache
            if GetNearbyNPCs then
                local npcResult = GetNearbyNPCs(2000, 0.9)
                if npcResult and npcResult.nearbyList then
                    for _, entry in ipairs(npcResult.nearbyList) do
                        -- Populate SpeakerActorCache with ALL nearby NPCs
                        if entry.name and entry.name ~= "Unknown" and entry.actor then
                            _G.SpeakerActorCache[entry.name] = entry.actor
                        end
                        -- Find the target NPC
                        if entry.name == targetId and entry.actor then
                            targetActor = entry.actor
                            print("[Socket] Found target actor: " .. entry.name .. "\n")
                        end
                    end
                end
            end
        else
            -- NPC is speaking - scan nearby NPCs and populate BOTH caches
            if GetNearbyNPCs then
                local npcResult = GetNearbyNPCs(2000, 0.9)
                if npcResult and npcResult.nearbyList then
                    print("[Socket] Looking for speaker='" .. tostring(speakerId) .. "' target='" .. tostring(targetId) .. "'\n")
                    for _, entry in ipairs(npcResult.nearbyList) do
                        -- Populate SpeakerActorCache with ALL nearby NPCs (for muting)
                        if entry.name and entry.name ~= "Unknown" and entry.actor then
                            _G.SpeakerActorCache[entry.name] = entry.actor
                        end
                        -- Find the specific speaker
                        if entry.name == speakerId and entry.actor then
                            actor = entry.actor
                            actorFound = true
                            print("[Socket] Found speaker actor: " .. entry.name .. "\n")
                        end
                        -- Find the target (if NPC)
                        if entry.name == targetId and entry.actor then
                            targetActor = entry.actor
                            print("[Socket] Found target actor: " .. entry.name .. "\n")
                        end
                    end
                    print("[Socket] Scanned " .. #npcResult.nearbyList .. " nearby NPCs, speaker found=" .. tostring(actorFound) .. "\n")
                end
            else
                print("[Socket] GetNearbyNPCs not available!\n")
            end
        end

        -- If target is player, use player actor
        if targetId == "player" and player then
            targetActor = player
            print("[Socket] Target is player, using player actor\n")
        elseif targetId == "player" then
            print("[Socket] Target is player but player actor is nil!\n")
        end

        -- Cache actor by turn ID (for 3D audio/lipsync)
        _G.TurnActorCache[turnId] = actor

        -- Add to playback queue (with target info for NPC attention)
        local pState = _G.PlaybackState
        if pState then
            table.insert(pState.queue, {
                turnId = turnId,
                speakerId = speakerId,
                speaker = displayName,
                full_text = text,
                turnIndex = turnIndex,
                targetId = targetId,
                speakerActor = actor,
                targetActor = targetActor
            })
            print("[Socket] Added to queue: " .. tostring(turnId) .. " (queue size: " .. #pState.queue .. ")\n")
        end

        -- NOTE: NPC locking now happens in lipsync_start (when turn actually plays)
        -- Queue items store speakerActor/targetActor for use at lipsync_start

        -- DON'T set currentTurnId here - it will be set by lipsync_start
        -- This allows us to queue up next turn while current is still playing
        -- Just update phase if we're idle
        if _G.SonorusState and _G.SonorusState.phase == "idle" then
            _G.SonorusState.phase = "preparing"
            _G.SonorusState.currentTurnId = turnId  -- Only set if idle (first turn)
        end

        -- Mute the speaker's original game audio (skip for player - no game audio to mute)
        if MuteQueueSpeakers and pState and #pState.queue > 0 and not isPlayerSpeaker then
            MuteQueueSpeakers({pState.queue[#pState.queue]})
        end

        -- Get initial positions for 3D audio (so Python doesn't start at 0,0,0)
        local camX, camY, camZ = 0, 0, 0
        local camYaw, camPitch = 0, 0
        local npcX, npcY, npcZ = 0, 0, 0
        local hasPositions = false

        pcall(function()
            -- Get camera position
            local pc = FindFirstOf("PlayerController")
            if pc and pc:IsValid() then
                local cam = pc.PlayerCameraManager
                if cam and cam:IsValid() then
                    local camPos = cam:GetCameraLocation()
                    local camRot = cam:GetCameraRotation()
                    if camPos and camRot then
                        camX = camPos.X
                        camY = camPos.Y
                        camZ = camPos.Z
                        camYaw = camRot.Yaw
                        camPitch = camRot.Pitch
                        hasPositions = true
                    end
                end
            end

            -- Get NPC position
            if actor and actor:IsValid() then
                local npcPos = actor:K2_GetActorLocation()
                if npcPos then
                    npcX = npcPos.X
                    npcY = npcPos.Y
                    npcZ = npcPos.Z
                end
            end
        end)

        -- Send ready response to Python with initial positions
        SocketClient.send({
            type = "turn_ready",
            turn_id = turnId,
            actor_found = actorFound,
            is_player_speaker = isPlayerSpeaker,  -- For 3D audio handling
            -- Initial positions for 3D audio
            camX = camX, camY = camY, camZ = camZ,
            camYaw = camYaw, camPitch = camPitch,
            npcX = npcX, npcY = npcY, npcZ = npcZ,
            has_positions = hasPositions
        })

        if actorFound then
            local speakerType = isPlayerSpeaker and "PLAYER" or "NPC"
            print(string.format("[Socket] Turn ready (%s): %s, pos=(%.0f,%.0f,%.0f)\n",
                speakerType, tostring(turnId), npcX, npcY, npcZ))
        else
            print("[Socket] Turn ready WITHOUT actor: " .. tostring(turnId) .. "\n")
        end
        DevPrint("[DEBUG] play_turn game thread END turn=" .. tostring(turnId))

    elseif msgType == "reset" then
        -- Server requests full state reset (triggered by stop conversation hotkey)
        print("[Socket] Reset requested from server")
        if ResetState then
            ResetState()
        end

    elseif msgType == "reload_history" then
        -- Legacy: Lua no longer maintains dialogue history (Python is sole owner)
        -- This message is now a no-op but kept for backwards compatibility
        print("[Socket] reload_history received (no-op, Python manages history)")

    elseif msgType == "notification" then
        -- Show in-game notification (top-left text notification panel)
        local text = data.text or ""
        if text ~= "" then
            print("[Socket] Notification: " .. text)
            if ShowNotification then
                ShowNotification(text)
            end
        end

    elseif msgType == "cast_spell" then
        -- Voice spell casting from Python
        local spellName = data.spell
        if spellName then
            -- Get display name for notifications
            local displayName = GetDisplayName(spellName) or spellName
            -- Check if spell is unlocked first
            if not IsSpellUnlocked(spellName) then
                ShowNotification("You haven't learned " .. displayName .. " yet")
                return
            end
            local success = CastSpellByName(spellName)
            if not success then
                ShowNotification("Cannot cast " .. displayName .. " right now")
            end
        end

    elseif msgType == "set_loop_interval" then
        -- Performance setting: change unified loop tick rate
        local interval = data.interval
        if interval and type(interval) == "number" and interval >= 100 and interval <= 1000 then
            if _G.StartUnifiedLoop then
                _G.StartUnifiedLoop(interval)
            else
                -- Fallback: just update interval, will take effect on next reload
                _G.UnifiedLoop = _G.UnifiedLoop or {}
                _G.UnifiedLoop.interval = interval
                print("[Sonorus] Loop interval set to " .. interval .. "ms (takes effect on reload)")
            end
        end

    end
end

function SocketClient.isConnected()
    return client ~= nil and connectionState.connected
end

-- Throttle warning for "not connected" state
SocketClient._lastSendWarn = 0

-- Pack a 4-byte big-endian length prefix
local function PackLength(len)
    return string.char(
        math.floor(len / 16777216) % 256,
        math.floor(len / 65536) % 256,
        math.floor(len / 256) % 256,
        len % 256
    )
end

-- Send with length-prefixed framing (guarantees message integrity)
function SocketClient.send(data)
    -- If not connected, try reconnecting first
    if not client or not connectionState.connected then
        local now = os.clock()
        if (now - SocketClient._lastSendWarn) > 5 then
            SocketClient._lastSendWarn = now
            print("[SocketClient] Not connected - attempting reconnect...")
        end
        if not SocketClient.connect() then
            return false
        end
    end

    local sendSuccess = false
    local ok, err = pcall(function()
        local msg = json.encode(data)
        local msgLen = #msg
        -- Frame: [4-byte length][message]
        local frame = PackLength(msgLen) .. msg

        -- Send entire frame with blocking mode
        client:settimeout(5.0)  -- 5 second timeout for full send
        local sent = 0
        local frameLen = #frame

        while sent < frameLen do
            -- send(data, i, j) sends bytes from i to j (1-based, inclusive)
            -- Returns: last byte index sent on success, or (nil, err, lastSent) on error
            local lastIdx, sendErr, partialIdx = client:send(frame, sent + 1, frameLen)
            if lastIdx then
                sent = lastIdx  -- lastIdx is 1-based index of last byte sent
            elseif partialIdx and partialIdx > 0 then
                sent = partialIdx
            else
                error("send failed: " .. tostring(sendErr))
            end
        end

        client:settimeout(0)  -- Back to non-blocking for receive
        sendSuccess = true
    end)

    if not ok then
        print("[SocketClient] Send error: " .. tostring(err))
        pcall(function() if client then client:close() end end)
        client = nil
        buffer = ""
        connectionState.connected = false
        connectionState.reconnectDelayMode = "fast"
        connectionState.fastRetryCount = 0
    end

    return sendSuccess
end

function SocketClient.close()
    if client then
        pcall(function() client:close() end)
        client = nil
    end
    buffer = ""
    sendQueue = {}  -- Clear pending sends
    sendInProgress = false
    -- Reset state for immediate reconnect on next update()
    connectionState.connected = false
    connectionState.reconnectTime = 0
    connectionState.reconnectDelayMode = "fast"
    connectionState.fastRetryCount = 0
end

function SocketClient.forceReconnect()
    SocketClient.close()
    connectionState.reconnectTime = 0
    return SocketClient.connect()
end

-- Make available globally for hot reload compatibility
_G.SocketClient = SocketClient

-- ============================================
-- Persistent OnTick Loop (50ms interval)
-- ============================================
-- Started once at module load, runs forever, checks state to decide if it should process
-- Uses LoopInGameThreadWithDelay for proper timer control
if not _G.OnTickLoopStarted then
    _G.OnTickLoopStarted = true
    _G.OnTickLoopHandle = LoopInGameThreadWithDelay(50, function()
        -- Only process if we're in an active conversation phase
        local state = _G.SonorusState
        if not state then return end

        local phase = state.phase or "idle"
        if phase == "idle" then return end  -- Keep running, just skip processing

        -- Call OnTick if available (defined in logic.lua)
        if _G.OnTick then
            pcall(_G.OnTick)
        end
    end)
    print("[SocketClient] Persistent OnTick loop started (50ms)")
end

print("[SocketClient] Module loaded")
return SocketClient
