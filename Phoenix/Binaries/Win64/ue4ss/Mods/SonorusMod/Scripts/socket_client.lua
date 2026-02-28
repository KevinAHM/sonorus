-- Socket Client for Python server communication
-- Receives lipsync and viseme events via TCP socket
print("[SocketClient] Loading...")

local socket = require("socket")
local json = require("json")
local Utils = require("Utils.Utils")
local TimeDilation = require("Utils.TimeDilation")
local Cache = require("Utils.Cache")

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

-- Mod enabled state (master toggle from config page)
-- Defaults to true - will be synced from Python when connected
_G.SonorusModEnabled = (_G.SonorusModEnabled == nil) and true or _G.SonorusModEnabled

-- Chat input state (static cursor, no blinking)
-- mode: "chat" = normal player chat, "prompt" = director mode prompt
_G.ChatInputState = _G.ChatInputState or { active = false, text = "", mode = "chat" }

-- Chat preview lock state (NPC locked while player is typing)
-- Fields: { lockId, npcName, npc, state, startTime }
-- state: "typing" (user typing) | "submitted" (message sent, waiting for server)
_G.ChatPreviewLock = _G.ChatPreviewLock or nil

-- STT preview lock state (NPC locked while player is speaking)
-- Fields: { lockId, npcName, npc, source, state, startTime }
-- state: "speaking" (user speaking) | "processing" (speech ended, waiting for server)
_G.STTPreviewLock = _G.STTPreviewLock or nil

-- Preview lock timeout in seconds (release if server doesn't respond)
local PREVIEW_LOCK_TIMEOUT = 15

-- Pause state tracking for immediate context updates
_G.LastKnownPauseState = _G.LastKnownPauseState or false

-- Activity state from Python (for ambient dialog gating)
_G.GameWindowForeground = (_G.GameWindowForeground == nil) and true or _G.GameWindowForeground  -- Default true until Python says otherwise
_G.PlayerIdleState = _G.PlayerIdleState or false  -- Default false until Python says otherwise

-- Conversation mode (runtime only, cycled via Home key)
-- "default" = normal (max turns, interjections), "1to1" = no interjections, "continuous" = no turn limit
_G.ConversationMode = _G.ConversationMode or "default"

-- Tracking settings from config (for dialogue recording toggles)
_G.TrackAmbientDialogue = (_G.TrackAmbientDialogue == nil) and true or _G.TrackAmbientDialogue  -- Default true
_G.TrackCutsceneDialogue = (_G.TrackCutsceneDialogue == nil) and true or _G.TrackCutsceneDialogue  -- Default true
-- Companion callout repeat blocking: 0 = disabled, -1 = never repeat, >0 = block within N game minutes
-- Default: 1440 (24 hours = 1 game day)
_G.CompanionCalloutBlockMinutes = (_G.CompanionCalloutBlockMinutes == nil) and 1440 or _G.CompanionCalloutBlockMinutes

-- Significant NPCs list (synced from Python on connect)
-- voiceName -> true for quick lookup
_G.SignificantNPCs = _G.SignificantNPCs or {}
-- Prefixes that are always insignificant (synced from Python)
_G.InsignificantPrefixes = _G.InsignificantPrefixes or {"t3", "midres"}

-- NOTE: Always use _G.SonorusState directly (no local State shadows) to avoid
-- closure capture issues that could corrupt UE4SS Lua registry references

-- Fast socket poll: temporary 25ms loop for when Python is waiting on a response.
-- The unified loop runs at 100ms, so messages can wait 0-100ms to be read.
-- When we know a follow-up message is imminent (e.g. after sending turn_ready,
-- Python will send lipsync_start), we spin up a fast poll to cut that wait.
-- Persists across F11 reloads via _G, self-cancels after expiry.
-- Cancel any stale fast poll from previous F11 reload
if _G._FastPoll and _G._FastPoll.handle then
    pcall(CancelDelayedAction, _G._FastPoll.handle)
end
_G._FastPoll = { handle = nil, expiry = 0 }

local function EnableFastPoll(durationSeconds)
    local fp = _G._FastPoll
    fp.expiry = os.clock() + (durationSeconds or 2.0)

    if fp.handle then return end  -- already running

    fp.handle = LoopInGameThreadWithDelay(25, function()
        if os.clock() > _G._FastPoll.expiry then
            CancelDelayedAction(_G._FastPoll.handle)
            _G._FastPoll.handle = nil
            return
        end
        -- Only do socket I/O (cheap, non-blocking)
        if client and connectionState.connected then
            pcall(SocketClient.update)
        end
    end)
end

local function DisableFastPoll()
    local fp = _G._FastPoll
    fp.expiry = 0  -- loop will self-cancel on next tick
end

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
            -- If paused and chat was active, close it locally (even if Python is dead)
            if currentPaused and _G.ChatInputState.active then
                -- Restore normal game input first
                if SetInputModeGameOnly then
                    SetInputModeGameOnly()
                end
                -- Clear the hint immediately (already on game thread)
                local uiManager = FindFirstOf("UIManager")
                if Utils.SafeIsValid(uiManager) then
                    pcall(function()
                        uiManager:ClearHintMessage()
                    end)
                end
                _G.ChatInputState.active = false
                _G.ChatInputState.text = ""
                -- Release chat preview lock if active AND in interruptible state
                if _G.ChatPreviewLock then
                    local lockState = _G.ChatPreviewLock.state
                    if lockState == "typing" then
                        -- Still typing, release on pause
                        if ReleaseNPC then
                            ReleaseNPC(_G.ChatPreviewLock.lockId)
                        end
                        _G.ChatPreviewLock = nil
                        print("[Chat] Preview lock released (game paused while typing)")
                    else
                        -- submitted state - keep lock, let timeout handle it
                        -- (user might unpause and server might still respond)
                        print("[Chat] Game paused but lock state=" .. tostring(lockState) .. ", keeping for timeout")
                    end
                end
                -- Release STT preview lock if active AND in interruptible state
                if _G.STTPreviewLock then
                    local lockState = _G.STTPreviewLock.state
                    if lockState == "speaking" then
                        -- Still speaking, release on pause
                        if ReleaseNPC then
                            ReleaseNPC(_G.STTPreviewLock.lockId)
                        end
                        _G.STTPreviewLock = nil
                        print("[STT] Preview lock released (game paused while speaking)")
                    else
                        -- processing state - keep lock, let timeout handle it
                        print("[STT] Game paused but lock state=" .. tostring(lockState) .. ", keeping for timeout")
                    end
                end
            end

            -- Send pause state to Python (if connected)
            if _G.SocketClient and _G.SocketClient.isConnected() then
                _G.SocketClient.send({
                    type = "pause_state",
                    paused = currentPaused
                })
                -- Tell Python to close chat input capture
                if currentPaused and _G.ChatInputState then
                    _G.SocketClient.send({
                        type = "force_close_chat",
                        reason = "game_paused"
                    })
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
        -- Clear stale VR offset from previous session (persists across hot reloads)
        _G.VROffset = nil
        _G.VRDebug = nil
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

        -- Send current reverb to Python on connect (for server restarts)
        -- Note: connect() is called from ExecuteInGameThread in logic.lua, so no wrapper needed
        if GetCachedReverb then
            local reverb = GetCachedReverb()
            if reverb and reverb.auxBus then
                SocketClient.send({
                    type = "reverb_update",
                    auxBus = reverb.auxBus,
                    sendLevel = reverb.sendLevel or 1.0,
                    zone = reverb.zone or ""
                })
                print("[SocketClient] Sent initial reverb: " .. reverb.auxBus)
            end
        end

        -- Refresh house points on connect (for server restarts)
        if RefreshHousePoints then
            RefreshHousePoints()
        end
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

    -- These message types are always processed regardless of mod enabled state
    local alwaysAllowed = {
        request_context = true,
        activity_state = true,
        tracking_settings = true,
        sync_significant_npcs = true,
        reset = true,
        reload_history = true,
        notification = true,
        set_loop_interval = true,
        fast_poll = true,
        activate_commitment = true,
        deactivate_commitment = true,
    }

    -- Block active mod functionality when mod is disabled
    if not _G.SonorusModEnabled and not alwaysAllowed[msgType] then
        -- Allow chat_input only for closing (not opening)
        if msgType == "chat_input" and not data.active then
            -- Let close through
        elseif msgType == "stt_input" and not data.active then
            -- Let close through
        else
            DevPrint("[Socket] Mod disabled - ignoring message: " .. tostring(msgType))
            return
        end
    end

    -- Handle fast_poll early - lightweight, just enables fast socket polling
    if msgType == "fast_poll" then
        EnableFastPoll(data.duration or 2.0)
        return
    end

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
        -- Master mod toggle - when off, Lua disables all mod functions except communication
        local wasEnabled = _G.SonorusModEnabled
        _G.SonorusModEnabled = data.mod_enabled
        if wasEnabled ~= nil and wasEnabled ~= data.mod_enabled then
            if data.mod_enabled then
                print("[Socket] Mod ENABLED - resuming all functions")
            else
                print("[Socket] Mod DISABLED - pausing all functions except communication")
                -- Restore normal input mode (in case chat was open)
                if SetInputModeGameOnly then pcall(SetInputModeGameOnly) end
                -- Release all locked NPCs when disabling
                if ReleaseAllNPCs then pcall(ReleaseAllNPCs) end
                -- Hide any subtitles
                if HideMessage then pcall(HideMessage) end
            end
        end
        _G.TrackAmbientDialogue = data.track_ambient
        _G.TrackCutsceneDialogue = data.track_cutscene
        -- Companion callout repeat blocking: 0 = disabled, -1 = never repeat, >0 = N game minutes
        _G.CompanionCalloutBlockMinutes = data.companion_callout_block_minutes or 1440
        -- Preview lock: lock NPC while typing/speaking (before sending message)
        if data.preview_lock ~= nil then
            _G.PreviewLockEnabled = data.preview_lock
        end
        -- Sync dev mode from Python settings
        if data.dev_mode ~= nil then
            _G.SonorusDevMode = data.dev_mode
        end
        -- Sync game language for localization file loading
        local newLang = data.language or "EN_US"
        local oldLang = _G.SonorusLanguage
        _G.SonorusLanguage = newLang
        -- Invalidate localization caches if language changed
        if oldLang and oldLang ~= newLang then
            print(string.format("[Socket] Language changed from %s to %s, invalidating caches", oldLang, newLang))
            _G.LocalizationLoaded = false
            _G.SubtitlesLoaded = false
            _G.Localization = nil
            _G.Subtitles = nil
        end
        -- Time dilation settings
        if data.time_dilation and TimeDilation then
            TimeDilation.UpdateSettings(data.time_dilation)
        end
        -- Companion follow distance (meters -> UU, 1m = 100uu)
        if data.companion_follow_distance_m then
            _G.CompanionFollowDistanceUU = data.companion_follow_distance_m * 100
            if CompanionFollow then pcall(CompanionFollow.applySettings) end
        end
        -- TTS provider ("none" = disabled, shows bracketed text in subtitles)
        _G.TtsProvider = data.tts_provider or ""
        print(string.format("[Socket] Tracking settings: mod_enabled=%s, ambient=%s, cutscene=%s, companion_block_mins=%s, dev_mode=%s, lang=%s, tts=%s",
            tostring(data.mod_enabled), tostring(data.track_ambient), tostring(data.track_cutscene), tostring(_G.CompanionCalloutBlockMinutes), tostring(_G.SonorusDevMode), newLang, _G.TtsProvider))
        return
    end

    -- Handle sync_significant_npcs - list of significant NPC names for filtering
    if msgType == "sync_significant_npcs" then
        -- Build lookup table from both voice IDs and display names
        -- Voice IDs: internal IDs from GetActorVoiceId like "sebastiansallow"
        -- Display names: localized names from GetActorDisplayName like "Sebastian Sallow"
        local newSet = {}
        local voiceNames = data.voice_names or {}
        local displayNames = data.display_names or {}
        for _, name in ipairs(voiceNames) do
            newSet[name] = true
        end
        for _, name in ipairs(displayNames) do
            newSet[name] = true
        end
        _G.SignificantNPCs = newSet
        -- Update insignificant prefixes if provided
        if data.insignificant_prefixes then
            _G.InsignificantPrefixes = data.insignificant_prefixes
        end
        print(string.format("[Socket] Synced %d significant NPC names (%d voice + %d display)",
            #voiceNames + #displayNames, #voiceNames, #displayNames))
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
            -- Clean up any debug playback state (DebugF7)
            if _G.DebugBlendshapeLoop then
                pcall(function() CancelDelayedAction(_G.DebugBlendshapeLoop) end)
                _G.DebugBlendshapeLoop = nil
            end
            if _G._OrigGetCurrentSpeakerActor then
                _G.GetCurrentSpeakerActor = _G._OrigGetCurrentSpeakerActor
                _G._OrigGetCurrentSpeakerActor = nil
            end
            _G.DebugBlendshapeActor = nil
            _G.DebugBlendshapeTestIdx = 0

            -- Phase-based state machine
            _G.SonorusState.phase = "playing"
            -- CRITICAL: Reset messageShown for new turn
            -- This fixes race condition where lipsync_start arrives before closing phase completes
            -- Without this, messageShown stays true from previous turn and subtitle is skipped
            _G.SonorusState.messageShown = false
            -- CRITICAL: Reset CloseLipsComplete for new turn
            -- Without this, stale flag from previous turn causes OnTick to skip CloseLips()
            _G.CloseLipsComplete = false
            _G.CloseLipsIterations = 0
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
        vd.pausedAt = nil       -- Clear any stale pause state
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
                    funnel = f[4],
                    press = f[5] or 0,
                    lip_up = f[6] or 0,
                    ee = f[7] or 0,
                    o_shape = f[8] or 0,
                    shh = f[9] or 0
                })
            end
            -- Sort by timestamp (amplitude + word visemes may be in generation order, not time order)
            table.sort(vd.frames, function(a, b) return a.t < b.t end)
            vd.loaded = true
            print(string.format("[Socket] Loaded %d initial visemes with lipsync_start\n", #initialVisemes))
        end

        -- Store per-character lipsync scale (default 1.0)
        vd.scale = data.scale or 1.0

        -- Handle lipsync fallback (force direct mode)
        local BlueprintHelpers = require("Utils.BlueprintHelpers")
        BlueprintHelpers.SetForceDirect(data.fallback == true)

        local scaleStr = vd.scale ~= 1.0 and string.format(", scale=%.2f", vd.scale) or ""
        print("[Socket] Lipsync start - turn=" .. tostring(turnId) ..
              ", speaker=" .. tostring(data.speaker) ..
              ", visemes=" .. tostring(initialVisemes and #initialVisemes or 0) .. scaleStr .. "\n")

        -- Lock NPCs for this turn (now that it's actually playing)
        -- NOTE: Already on game thread via LoopInGameThreadWithDelay, no wrapper needed
        DevPrint("[DEBUG] lipsync_start lock NPCs START turn=" .. tostring(turnId))

        -- Find the queue item for this turn (declared outside if block so actions can use it)
        local currentItem = nil
        if _G.PlaybackState and _G.PlaybackState.queue then
            for _, item in ipairs(_G.PlaybackState.queue) do
                if item.turnId == turnId then
                    currentItem = item
                    break
                end
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

                -- Soft-orient companion to face player
                if OrientCompanionToPlayer then
                    OrientCompanionToPlayer(speakerActor)
                end
            end
        end
        DevPrint("[DEBUG] lipsync_start lock NPCs END")

        -- Execute action if specified (JoinAsCompanion / LeaveCompanion)
        -- Uses SetSystemicCompanionBP via MakeCompanion/ClearCompanion (works with Floo mod)
        if currentItem and currentItem.action and currentItem.action ~= "None" then
            local action = currentItem.action
            local speakerId = currentItem.speakerId or currentItem.speaker

            if action == "JoinAsCompanion" then
                if speakerId and MakeCompanion then
                    local ok = MakeCompanion(speakerId)
                    print("[Socket] JoinAsCompanion: " .. tostring(speakerId) .. " -> " .. (ok and "OK" or "FAILED") .. "\n")
                else
                    print("[Socket] Cannot JoinAsCompanion - no speaker ID or MakeCompanion unavailable\n")
                end
            elseif action == "LeaveCompanion" then
                if ClearCompanion then
                    local ok = ClearCompanion()
                    print("[Socket] LeaveCompanion -> " .. (ok and "OK" or "FAILED") .. "\n")
                else
                    print("[Socket] Cannot LeaveCompanion - ClearCompanion unavailable\n")
                end
            end
        end

        -- Execute house point actions if specified (professor awards/deducts points)
        if currentItem and currentItem.housePointActions and #currentItem.housePointActions > 0 then
            for _, hpAction in ipairs(currentItem.housePointActions) do
                local actionType = hpAction.action  -- "AwardPoints" or "DeductPoints"
                local house = hpAction.house        -- "Gryffindor", "Slytherin", etc.
                local amount = hpAction.amount      -- Number of points

                if actionType and house and amount then
                    -- The House Points mod uses stats like "Gryffindor_Housepoints"
                    -- Adding to this stat awards points, negative values deduct
                    local statName = house .. "_Housepoints"
                    local pointsToAdd = amount
                    if actionType == "DeductPoints" then
                        pointsToAdd = -amount
                    end

                    pcall(function()
                        local statsManager = Cache.Get("StatsManager", function()
                            return FindFirstOf("StatsManager")
                        end)
                        if statsManager then
                            local statFName = FName(statName)
                            -- Check if stat exists (mod is installed)
                            local exists = statsManager:StatExists(statFName)
                            if exists then
                                -- UpdateStat adds to the current value
                                statsManager:UpdateStat(statFName, pointsToAdd)
                                local verb = actionType == "AwardPoints" and "Awarded" or "Deducted"
                                print(string.format("[Socket] House Points: %s %d points %s %s\n",
                                    verb, amount, actionType == "AwardPoints" and "to" or "from", house))
                            else
                                print("[Socket] House Points stat not found: " .. statName .. " (mod may not be installed)\n")
                            end
                        else
                            print("[Socket] StatsManager not found for house points\n")
                        end
                    end)
                end
            end
            -- Refresh house points cache after processing actions (delay to let mod update readout stats)
            if RefreshHousePoints then
                ExecuteInGameThreadWithDelay(500, function()
                    RefreshHousePoints()
                end)
            end
        end

        -- ACK to Python: We're ready, start audio now!
        -- This completes the handshake - Python waits for this before playing audio
        SocketClient.send({ type = "lipsync_ready", turn_id = turnId })
        -- Handshake complete - no more urgent messages expected
        DisableFastPoll()

    elseif msgType == "lipsync_stop" then
        -- Audio ended - trigger closing sequence
        DevPrint("[Socket] Lipsync stop received\n")
        -- Clear frames for next utterance
        vd.frames = {}
        vd.loaded = false
        -- Reset timing state for next turn
        vd.audioOffset = 0
        vd.pausedAt = nil
        vd.localStartTime = nil
        -- Phase-based state machine
        if _G.SonorusState then
            _G.SonorusState.phase = "closing"
            _G.CloseLipsIterations = 0  -- Reset timeout counter for new close
        end
        -- Clear subtitle immediately when turn ends (don't wait for idle)
        -- Already on game thread via LoopInGameThreadWithDelay
        if _G.SonorusState then
            _G.SonorusState.messageShown = false
        end
        if HideMessage then
            HideMessage()
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
                    funnel = f[4],
                    press = f[5] or 0,
                    lip_up = f[6] or 0,
                    ee = f[7] or 0,
                    o_shape = f[8] or 0,
                    shh = f[9] or 0
                })
            end
            -- Sort by timestamp — async word visemes may arrive after amplitude
            -- visemes for the same time range, so append order != time order
            table.sort(vd.frames, function(a, b) return a.t < b.t end)
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

    elseif msgType == "subtitle_update" then
        -- Per-sentence subtitle update from Python during streaming TTS playback
        local turnId = data.turn_id
        local subtitleText = data.text or ""
        local sentenceIdx = data.sentence_idx or 0

        -- Find the queue item for this turn
        local pState = _G.PlaybackState
        if pState and _G.SonorusState and _G.SonorusState.currentTurnId == turnId then
            local currentItem = nil
            for _, item in ipairs(pState.queue or {}) do
                if item.turnId == turnId then
                    currentItem = item
                    break
                end
            end

            if currentItem then
                local npcName = GetDisplayName(currentItem.speaker or "NPC")

                -- Strip bracketed text for cloud TTS (same logic as OnTick subtitle)
                local displayText = subtitleText
                local ttsProvider = (_G.TtsProvider or ""):lower()
                local keepBrackets = ttsProvider == "" or ttsProvider == "none"
                    or ttsProvider == "pocket" or ttsProvider == "pocket_onnx"
                if not keepBrackets then
                    displayText = string.gsub(displayText, "%[[^%]]*%]", "")
                end
                displayText = string.gsub(displayText, "%s+", " ")
                displayText = string.gsub(displayText, "^%s+", "")
                displayText = string.gsub(displayText, "%s+$", "")

                -- Narration detection fallback:
                -- Prefer explicit flag from server, but also detect pre-formatted
                -- narration text in case a message arrives without is_narration.
                local isNarration = (data.is_narration == true)
                if not isNarration then
                    if string.match(displayText, "^%s*<i>.*</i>%s*$") then
                        isNarration = true
                    else
                        local starWrapped = string.match(displayText, "^%s*%*(.-)%*%s*$")
                        if starWrapped and string.find(starWrapped, "%s") then
                            displayText = starWrapped
                            isNarration = true
                        end
                    end
                end

                local displayMessage
                if isNarration then
                    -- Narration: italic, no NPC name prefix
                    displayText = string.gsub(displayText, "^%s*<i>%s*", "")
                    displayText = string.gsub(displayText, "%s*</i>%s*$", "")
                    displayMessage = "<i>" .. displayText .. "</i>"
                else
                    displayMessage = npcName .. ": " .. displayText
                end

                local now = os.clock()
                local lastSubtitleAt = currentItem._lastSubtitleUpdateAt or 0
                local updateGap = now - lastSubtitleAt

                if sentenceIdx == 0 then
                    -- First sentence: ShowMessage (remove+add)
                    ShowMessage(displayMessage)
                else
                    -- Subsequent sentences: prefer in-place update (no flash).
                    -- If there has been a long gap (e.g., narration span), the
                    -- standalone subtitle widget may have expired; re-show it.
                    if (not _G.SonorusState.messageShown) or updateGap > 3.5 then
                        ShowMessage(displayMessage)
                    else
                        UpdateMessage(displayMessage)
                    end
                end
                _G.SonorusState.messageShown = true
                currentItem._subtitleReceived = true
                currentItem._lastSubtitleUpdateAt = now
                DevPrint(string.format("[Socket] Subtitle update [%d]: %s\n", sentenceIdx, displayText:sub(1, 50)))
            end
        end

    elseif msgType == "turn_actions" then
        -- Deferred actions from streaming path: actions parsed after LLM done, sent separately
        local turnId = data.turn_id
        local action = data.action or "None"
        local housePointActions = data.house_point_actions

        print("[Socket] Received turn_actions: turn=" .. tostring(turnId) ..
              " action=" .. tostring(action) ..
              " hp_actions=" .. tostring(housePointActions and #housePointActions or 0) .. "\n")

        -- Find the queue item for this turn
        local currentItem = nil
        local pState = _G.PlaybackState
        if pState and pState.queue then
            for _, item in ipairs(pState.queue) do
                if item.turnId == turnId then
                    currentItem = item
                    break
                end
            end
        end

        if currentItem then
            -- Update the queue item with actions
            currentItem.action = action
            currentItem.housePointActions = housePointActions

            -- If this turn is already playing (lipsync_start already fired), execute actions now
            if _G.SonorusState and _G.SonorusState.currentTurnId == turnId then
                print("[Socket] Turn already playing - executing deferred actions now\n")

                -- Execute companion action (same logic as lipsync_start)
                if action and action ~= "None" then
                    local speakerId = currentItem.speakerId or currentItem.speaker

                    if action == "JoinAsCompanion" then
                        if speakerId and MakeCompanion then
                            local ok = MakeCompanion(speakerId)
                            print("[Socket] Deferred JoinAsCompanion: " .. tostring(speakerId) .. " -> " .. (ok and "OK" or "FAILED") .. "\n")
                        end
                    elseif action == "LeaveCompanion" then
                        if ClearCompanion then
                            local ok = ClearCompanion()
                            print("[Socket] Deferred LeaveCompanion -> " .. (ok and "OK" or "FAILED") .. "\n")
                        end
                    end
                end

                -- Execute house point actions (same logic as lipsync_start)
                if housePointActions and #housePointActions > 0 then
                    for _, hpAction in ipairs(housePointActions) do
                        local actionType = hpAction.action
                        local house = hpAction.house
                        local amount = hpAction.amount

                        if actionType and house and amount then
                            local statName = house .. "_Housepoints"
                            local pointsToAdd = amount
                            if actionType == "DeductPoints" then
                                pointsToAdd = -amount
                            end

                            pcall(function()
                                local statsManager = Cache.Get("StatsManager", function()
                                    return FindFirstOf("StatsManager")
                                end)
                                if statsManager then
                                    local statFName = FName(statName)
                                    local exists = statsManager:StatExists(statFName)
                                    if exists then
                                        statsManager:UpdateStat(statFName, pointsToAdd)
                                        local verb = actionType == "AwardPoints" and "Awarded" or "Deducted"
                                        print(string.format("[Socket] Deferred House Points: %s %d points %s %s\n",
                                            verb, amount, actionType == "AwardPoints" and "to" or "from", house))
                                    end
                                end
                            end)
                        end
                    end
                    if RefreshHousePoints then
                        ExecuteInGameThreadWithDelay(500, function()
                            RefreshHousePoints()
                        end)
                    end
                end
            else
                print("[Socket] Turn not yet playing - actions stored for lipsync_start\n")
            end
        else
            print("[Socket] turn_actions: queue item not found for turn=" .. tostring(turnId) .. "\n")
        end

    elseif msgType == "lipsync_pause" then
        -- Soft interrupt: freeze lip animation at current position
        if vd.localStartTime then
            local elapsed = os.clock() - vd.localStartTime + (vd.audioOffset or 0)
            vd.pausedAt = elapsed
            print(string.format("[Socket] Lipsync paused at %.2fs\n", elapsed))
        end

    elseif msgType == "lipsync_resume" then
        -- Resume from soft interrupt: adjust localStartTime so elapsed picks up from frozen point
        -- Formula: elapsed = os.clock() - localStartTime + audioOffset
        -- We want elapsed = pausedAt right after resume, so:
        --   localStartTime = os.clock() - pausedAt, audioOffset = 0
        if vd.pausedAt then
            vd.localStartTime = os.clock() - vd.pausedAt
            vd.audioOffset = 0  -- Reset drift — Python shifted playback_start_time
            local resumed = vd.pausedAt
            vd.pausedAt = nil
            print(string.format("[Socket] Lipsync resumed from %.2fs\n", resumed))
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

                -- Preview lock: Conversation taking over, clear preview state
                -- (Lock itself will be managed by conversation system)
                if _G.ChatPreviewLock then
                    print("[Chat] Preview lock absorbed by conversation system")
                    _G.ChatPreviewLock = nil
                end
                if _G.STTPreviewLock then
                    print("[STT] Preview lock absorbed by conversation system")
                    _G.STTPreviewLock = nil
                end

                -- Time dilation: Switch to conversation rate
                if TimeDilation then
                    TimeDilation.OnConversationStart()
                end
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

                    -- Log preview lock state before releasing (debugging)
                    if _G.ChatPreviewLock then
                        print("[Chat] Preview lock will be released on idle: " ..
                            _G.ChatPreviewLock.npcName .. " (state=" .. tostring(_G.ChatPreviewLock.state) .. ")")
                    end
                    if _G.STTPreviewLock then
                        print("[STT] Preview lock will be released on idle: " ..
                            tostring(_G.STTPreviewLock.npcName) .. " (state=" .. tostring(_G.STTPreviewLock.state) .. ")")
                    end

                    -- Linger NPCs instead of releasing — they stay frozen ~10s
                    if LingerAllNPCs then
                        LingerAllNPCs()
                    end

                    -- Hide subtitles when conversation ends
                    -- But NOT if chat input is active (user may have just interrupted to respond)
                    if HideMessage and not (_G.ChatInputState and _G.ChatInputState.active) then
                        HideMessage()
                    end

                    -- Note: Preview lock state is cleared by LingerAllNPCs() in NPCLock.lua

                    -- Time dilation: Restore day/night rate
                    if TimeDilation then
                        TimeDilation.OnConversationEnd()
                    end
                end
            end

            print("[Socket] Conversation state: " .. tostring(data.state))
        end

    elseif msgType == "player_message" then
        -- Player message - show immediately as subtitle, auto-hide after delay
        local speaker = data.speaker or "You"
        local text = data.text or ""
        if text ~= "" then
            print("[Socket] Player message: " .. text)
            local msg = speaker .. ": " .. text
            DevPrint("[DEBUG] player_message show START")
            -- Increment generation counter so stale hide timers don't remove NPC subtitles
            _G.SubtitleGen = (_G.SubtitleGen or 0) + 1
            local myGen = _G.SubtitleGen
            local ok, err = pcall(function()
                if _G.ShowMessage then
                    _G.ShowMessage(msg)
                end
            end)
            if not ok then DevPrint("[DEBUG] player_message show error: " .. tostring(err)) end
            DevPrint("[DEBUG] player_message show END")
            -- Auto-hide after 3 seconds, but only if no newer subtitle has been shown
            ExecuteInGameThreadWithDelay(3000, function()
                if (_G.SubtitleGen or 0) ~= myGen then
                    DevPrint("[DEBUG] player_message hide SKIPPED (subtitle replaced)")
                    return
                end
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
        local wasActive = state.active
        state.text = data.text or ""
        state.active = data.active
        state.mode = data.mode or "chat"  -- "chat" = normal, "prompt" = director mode
        state.dirty = true  -- Signal logic.lua to update display

        -- Preview lock system: Lock NPC immediately when chat opens
        -- NOTE: Already on game thread via unified loop, no wrapper needed
        if data.active and not wasActive then
            -- Chat just opened - switch to UI-only input mode to block game/other mods
            if SetInputModeUIOnly then
                SetInputModeUIOnly()
            end
            -- Lock the NPC player is looking at (if enabled)
            -- Default to true if setting hasn't been synced yet
            if _G.PreviewLockEnabled == false then
                DevPrint("[Chat] Chat opened, preview lock disabled in settings")
            elseif GetLookedAtNPC and LockNPCToTarget then
                local npc, npcName, distance = GetLookedAtNPC(0.85, 2000)
                if npc and npcName then
                    -- Check if looked-at NPC is the current speaker - if so, interrupt!
                    -- This lets the player quickly respond by looking at the speaker and pressing Enter
                    local isCurrentSpeaker = false
                    local pState = _G.PlaybackState
                    if pState and pState.playing and pState.queue and pState.currentIndex then
                        local currentTurn = pState.queue[pState.currentIndex]
                        if currentTurn and currentTurn.speakerId then
                            -- Normalize for comparison (remove spaces, lowercase)
                            local currentSpeakerNorm = currentTurn.speakerId:gsub(" ", ""):lower()
                            local lookedAtNorm = npcName:gsub(" ", ""):lower()
                            isCurrentSpeaker = (currentSpeakerNorm == lookedAtNorm)
                        end
                    end

                    local createdInterruptLock = false
                    if isCurrentSpeaker then
                        -- Interrupt the conversation - player wants to respond to this NPC
                        print("[Chat] Looked at current speaker " .. npcName .. " - interrupting conversation")
                        SocketClient.send({ type = "interrupt_conversation", reason = "looked_at_speaker" })
                        createdInterruptLock = true  -- Mark so we preserve lock during reset
                        -- Fall through to create preview lock below
                    elseif IsNPCInConversation and IsNPCInConversation(npcName) then
                        -- NPC is in conversation but not current speaker - skip preview lock
                        -- (prevents stealing lock from active conversation, which would release it on ESC)
                        DevPrint("[Chat] NPC in conversation but not current speaker, skipping preview lock")
                    end

                    -- Create preview lock (for current speaker after interrupt, or NPC not in conversation)
                    if isCurrentSpeaker or not (IsNPCInConversation and IsNPCInConversation(npcName)) then
                        -- Get player actor from static cache
                        local player = nil
                        pcall(function()
                            local staticData = Cache and Cache.GetStaticData()
                            player = staticData and staticData.player
                            if not player then
                                player = FindFirstOf("Biped_Player")
                            end
                        end)

                        if player and Utils.SafeIsValid(player) then
                            local lockId = LockNPCToTarget(npc, player)
                            if lockId then
                                -- Mark as preview lock so re-facing system skips it
                                if _G.LockedNPCs and _G.LockedNPCs[lockId] then
                                    _G.LockedNPCs[lockId].isPreviewLock = true
                                end

                                _G.ChatPreviewLock = {
                                    lockId = lockId,
                                    npcName = npcName,
                                    npc = npc,
                                    state = "typing",
                                    startTime = os.clock(),
                                    interruptLock = createdInterruptLock  -- Preserve during reset if true
                                }
                                print("[Chat] Preview locked: " .. npcName .. " (distance: " .. string.format("%.0f", distance) .. ")")

                                -- Soft-orient companion to face player
                                if OrientCompanionToPlayer then
                                    OrientCompanionToPlayer(npc)
                                end
                            end
                        end
                    end
                else
                    print("[Chat] No NPC in crosshairs for preview lock")
                end
            end
        elseif not data.active and wasActive then
            -- Chat closed - restore normal game input
            if SetInputModeGameOnly then
                SetInputModeGameOnly()
            end
            -- Check state to decide whether to release NPC lock
            if _G.ChatPreviewLock then
                local lockState = _G.ChatPreviewLock.state
                if lockState == "typing" then
                    -- ESC pressed or chat closed before submit - release lock
                    if ReleaseNPC then
                        ReleaseNPC(_G.ChatPreviewLock.lockId)
                        print("[Chat] Preview lock released (chat closed/cancelled)")
                    end
                    _G.ChatPreviewLock = nil
                elseif lockState == "submitted" then
                    -- Chat was submitted, waiting for server - DO NOT release
                    -- Lock will be absorbed by conversation system or timeout
                    print("[Chat] Chat closed but state=submitted, keeping lock for " .. _G.ChatPreviewLock.npcName)
                else
                    -- Unknown state, release to be safe
                    print("[Chat] Warning: Unknown preview lock state '" .. tostring(lockState) .. "', releasing")
                    if ReleaseNPC then
                        ReleaseNPC(_G.ChatPreviewLock.lockId)
                    end
                    _G.ChatPreviewLock = nil
                end
            end
        end

    elseif msgType == "chat_submit" then
        -- Chat submitted - clear hint (spell detection + chat processing happens Python-side)
        local text = data.text or ""
        print("[Socket] Chat submitted: " .. text)

        -- Restore normal game input (chat is closing)
        if SetInputModeGameOnly then
            SetInputModeGameOnly()
        end

        -- Clear global state so blink loop stops
        local state = _G.ChatInputState
        state.active = false
        state.text = ""

        -- Preview lock: Transition to 'submitted' state, keep lock for server response
        if _G.ChatPreviewLock then
            _G.ChatPreviewLock.state = "submitted"
            _G.ChatPreviewLock.startTime = os.clock()  -- Reset timeout for server response wait
            print("[Chat] Preview lock state -> submitted (target: " .. _G.ChatPreviewLock.npcName .. ")")
            -- Lock will be absorbed by conversation system or released by timeout
        end

        local uiManager = FindFirstOf("UIManager")
        if Utils.SafeIsValid(uiManager) then
            pcall(function()
                uiManager:ClearHintMessage()
            end)
        end

    elseif msgType == "stt_input" then
        -- Speech-to-text input state (from voice capture)
        -- Similar to chat_input but for PTT/open mic speech
        -- NOTE: Already on game thread via unified loop, no wrapper needed
        local wasActive = _G.STTPreviewLock ~= nil

        if data.active and not wasActive then
            -- Speech started - lock the NPC player is looking at (if enabled)
            -- Default to true if setting hasn't been synced yet
            if _G.PreviewLockEnabled == false then
                DevPrint("[STT] Speech started, preview lock disabled in settings")
            elseif GetLookedAtNPC and LockNPCToTarget then
                local npc, npcName, distance = GetLookedAtNPC(0.85, 2000)
                if npc and npcName then
                    -- Skip preview lock if NPC is already in a conversation
                    -- (prevents stealing lock from active conversation, which would release it on cancel)
                    if IsNPCInConversation and IsNPCInConversation(npcName) then
                        DevPrint("[STT] NPC already in conversation, skipping preview lock")
                    else
                        -- Get player actor from static cache
                        local player = nil
                        pcall(function()
                            local staticData = Cache and Cache.GetStaticData()
                            player = staticData and staticData.player
                            if not player then
                                player = FindFirstOf("Biped_Player")
                            end
                        end)

                        if player and Utils.SafeIsValid(player) then
                            local lockId = LockNPCToTarget(npc, player)
                            if lockId then
                                -- Mark as preview lock so re-facing system skips it
                                if _G.LockedNPCs and _G.LockedNPCs[lockId] then
                                    _G.LockedNPCs[lockId].isPreviewLock = true
                                end

                                _G.STTPreviewLock = {
                                    lockId = lockId,
                                    npcName = npcName,
                                    npc = npc,
                                    source = data.source or "ptt",
                                    state = "speaking",
                                    startTime = os.clock()
                                }
                                print("[STT] Preview locked: " .. npcName .. " (distance: " .. string.format("%.0f", distance) .. ")")

                                -- Soft-orient companion to face player
                                if OrientCompanionToPlayer then
                                    OrientCompanionToPlayer(npc)
                                end
                            end
                        end
                    end
                else
                    DevPrint("[STT] No NPC in crosshairs for preview lock")
                end
            end
        elseif not data.active and wasActive then
            -- Speech ended - transition to 'processing' state, waiting for server response
            -- DO NOT release immediately - server may still be transcribing/processing
            if _G.STTPreviewLock then
                _G.STTPreviewLock.state = "processing"
                _G.STTPreviewLock.startTime = os.clock()  -- Reset timeout for processing wait
                print("[STT] Preview lock state -> processing (source: " .. tostring(_G.STTPreviewLock.source) .. ")")
                -- Lock will be absorbed by conversation system or released by timeout
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

    elseif msgType == "lock_npc" then
        -- Lock an NPC in place EARLY, before LLM response generation
        -- This prevents them from walking away while we generate the response
        -- NOTE: Already on game thread via unified loop, no wrapper needed
        local speakerId = data.speaker_id
        local targetId = data.target_id or "player"
        print("[Socket] lock_npc: " .. tostring(speakerId) .. " -> " .. tostring(targetId) .. "\n")

        -- Find the speaker NPC
        local speakerActor = nil
        local targetActor = nil

        -- Get player for target resolution
        local player = nil
        pcall(function() player = FindFirstOf("Biped_Player") end)

        -- Find speaker in nearby NPCs
        if GetNearbyNPCs then
            local npcResult = GetNearbyNPCs(2000, 0.9)
            if npcResult and npcResult.nearbyList then
                for _, entry in ipairs(npcResult.nearbyList) do
                    if entry.name == speakerId and entry.actor then
                        speakerActor = entry.actor
                    end
                    if entry.name == targetId and entry.actor then
                        targetActor = entry.actor
                    end
                end
            end
        end

        -- Fallback: try GetSpeakerActor
        if not speakerActor and GetSpeakerActor then
            speakerActor = GetSpeakerActor(speakerId)
        end

        -- Resolve target
        if targetId == "player" then
            targetActor = player
        elseif not targetActor and GetSpeakerActor then
            targetActor = GetSpeakerActor(targetId)
        end

        -- Lock the NPC if found
        if speakerActor and Utils.SafeIsValid(speakerActor) and targetActor and Utils.SafeIsValid(targetActor) then
            local canLock, reason = CanLockNPCs()
            if canLock then
                LockNPCToTarget(speakerActor, targetActor, function()
                    print("[Socket] lock_npc: " .. tostring(speakerId) .. " locked successfully\n")
                end)

                -- Soft-orient companion to face player
                if OrientCompanionToPlayer then
                    OrientCompanionToPlayer(speakerActor)
                end
            else
                print("[Socket] lock_npc: Cannot lock - " .. tostring(reason) .. "\n")
            end
        else
            print("[Socket] lock_npc: Could not find actors for " .. tostring(speakerId) .. "\n")
        end

    elseif msgType == "play_turn" then
        -- NEW: Atomic turn processing (replaces prepare_speaker + queue_item)
        -- Everything happens on game thread to eliminate race conditions
        local turnId = data.turn_id
        local speakerId = data.speaker_id
        local displayName = data.display_name
        local text = data.text
        local turnIndex = data.turn_index or 1
        local targetId = data.target_id or "player"
        local action = data.action or "None"
        local housePointActions = data.house_point_actions  -- List of {action, house, amount}
        print("[Socket] Processing play_turn: " .. tostring(turnId) .. " speaker=" .. tostring(speakerId) .. " -> " .. tostring(targetId) .. " action=" .. tostring(action) .. "\n")
        if housePointActions and #housePointActions > 0 then
            print("[Socket] House point actions: " .. #housePointActions .. " actions\n")
        end

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
            if Utils.SafeIsValid(player) then
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

            -- FALLBACK: If target NPC not found (visibility filter may have excluded them)
            if targetId and targetId ~= "player" and not targetActor and GetSpeakerActor then
                local fallbackTarget = GetSpeakerActor(targetId)
                if fallbackTarget then
                    targetActor = fallbackTarget
                    print("[Socket] Target found via GetSpeakerActor fallback: " .. targetId .. "\n")
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

            -- FALLBACK: If speaker not found (visibility filter may have excluded them),
            -- use GetSpeakerActor which searches NPC cache directly as fallback
            if not actorFound and GetSpeakerActor then
                local fallbackActor = GetSpeakerActor(speakerId)
                if fallbackActor then
                    actor = fallbackActor
                    actorFound = true
                    print("[Socket] Speaker found via GetSpeakerActor fallback: " .. speakerId .. "\n")
                end
            end

            -- Also try fallback for target NPC if not found
            if targetId and targetId ~= "player" and not targetActor and GetSpeakerActor then
                local fallbackTarget = GetSpeakerActor(targetId)
                if fallbackTarget then
                    targetActor = fallbackTarget
                    print("[Socket] Target found via GetSpeakerActor fallback: " .. targetId .. "\n")
                end
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
                targetActor = targetActor,
                action = action,
                housePointActions = housePointActions,
                streamingSubtitles = data.streaming_subtitles or false
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

            -- Get NPC position (with head height offset for 3D audio)
            if actor and actor:IsValid() then
                local npcPos = actor:K2_GetActorLocation()
                if npcPos then
                    npcX = npcPos.X
                    npcY = npcPos.Y
                    npcZ = npcPos.Z + 60  -- Head height offset (~60cm above center)
                end
            end
        end)

        -- Get FRESH reverb for audio effects (not cached - zones are more granular than locations)
        local reverbAuxBus = nil
        local reverbSendLevel = 1.0
        pcall(function()
            if GetCurrentReverb then
                local reverb = GetCurrentReverb()
                if reverb then
                    reverbAuxBus = reverb.auxBus
                    reverbSendLevel = reverb.sendLevel or 1.0
                end
            end
        end)

        -- Send ready response to Python with initial positions and reverb
        SocketClient.send({
            type = "turn_ready",
            turn_id = turnId,
            actor_found = actorFound,
            is_player_speaker = isPlayerSpeaker,  -- For 3D audio handling
            -- Initial positions for 3D audio
            camX = camX, camY = camY, camZ = camZ,
            camYaw = camYaw, camPitch = camPitch,
            npcX = npcX, npcY = npcY, npcZ = npcZ,
            has_positions = hasPositions,
            -- Reverb info for audio effects
            reverb_auxbus = reverbAuxBus,
            reverb_send = reverbSendLevel
        })

        if actorFound then
            local speakerType = isPlayerSpeaker and "PLAYER" or "NPC"
            print(string.format("[Socket] Turn ready (%s): %s, pos=(%.0f,%.0f,%.0f)\n",
                speakerType, tostring(turnId), npcX, npcY, npcZ))
        else
            print("[Socket] Turn ready WITHOUT actor: " .. tostring(turnId) .. "\n")
        end
        -- lipsync_start is coming next - fast poll to catch it quickly
        EnableFastPoll(2.0)
        DevPrint("[DEBUG] play_turn game thread END turn=" .. tostring(turnId))

    elseif msgType == "player_turn_start" then
        -- FAST PATH: Lightweight player turn setup (no NPC scanning)
        -- Used when player TTS is buffered early and we just need lip sync setup
        local turnId = data.turn_id
        local playerName = data.player_name
        local text = data.text
        DevPrint("[Socket] player_turn_start: " .. tostring(turnId) .. " player=" .. tostring(playerName))

        local actorFound = false

        -- Get player actor directly (no NPC scan needed)
        local player = nil
        pcall(function() player = FindFirstOf("Biped_Player") end)

        if Utils.SafeIsValid(player) then
            actorFound = true
            DevPrint("[Socket] Player actor found for lip sync")
        else
            print("[Socket] WARNING: Player actor not found!")
        end

        -- Initialize caches
        _G.TurnActorCache = _G.TurnActorCache or {}
        _G.TurnActorCache[turnId] = player

        -- Add to playback queue (for lip sync)
        local pState = _G.PlaybackState
        if pState then
            table.insert(pState.queue, {
                turnId = turnId,
                speakerId = "player",
                speaker = playerName,
                full_text = text,
                turnIndex = 1,
                targetId = nil,  -- Player speech doesn't target anyone specific
                speakerActor = player,
                targetActor = nil
            })
            DevPrint("[Socket] Added player turn to queue (size: " .. #pState.queue .. ")")
        end

        -- Update phase if idle
        if _G.SonorusState and _G.SonorusState.phase == "idle" then
            _G.SonorusState.phase = "preparing"
            _G.SonorusState.currentTurnId = turnId
        end

        -- Get initial positions for 3D audio (player voice spatialized at player actor)
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

            -- Get player actor position (with head height offset for 3D audio)
            if player and player:IsValid() then
                local playerPos = player:K2_GetActorLocation()
                if playerPos then
                    npcX = playerPos.X
                    npcY = playerPos.Y
                    npcZ = playerPos.Z + 60  -- Head height offset (~60cm above center)
                end
            end
        end)

        -- Get FRESH reverb for audio effects
        local reverbAuxBus = nil
        local reverbSendLevel = 1.0
        pcall(function()
            if GetCurrentReverb then
                local reverb = GetCurrentReverb()
                if reverb then
                    reverbAuxBus = reverb.auxBus
                    reverbSendLevel = reverb.sendLevel or 1.0
                end
            end
        end)

        -- Send ready response with positions and reverb (same as NPC path)
        SocketClient.send({
            type = "turn_ready",
            turn_id = turnId,
            actor_found = actorFound,
            is_player_speaker = true,
            -- Initial positions for 3D audio
            camX = camX, camY = camY, camZ = camZ,
            camYaw = camYaw, camPitch = camPitch,
            npcX = npcX, npcY = npcY, npcZ = npcZ,
            has_positions = hasPositions,
            -- Reverb info for audio effects
            reverb_auxbus = reverbAuxBus,
            reverb_send = reverbSendLevel
        })

        -- lipsync_start is coming next - fast poll to catch it quickly
        EnableFastPoll(2.0)
        DevPrint("[Socket] player_turn_start complete: " .. tostring(turnId))

    elseif msgType == "reset" then
        -- Server requests full state reset (triggered by stop conversation hotkey)
        print("[Socket] Reset requested from server")
        if ResetState then
            ResetState()
        end
        -- Release all commitment schedule overrides
        if _G.CommitmentManager then pcall(_G.CommitmentManager.ReleaseAll) end

    elseif msgType == "reload_history" then
        -- Legacy: Lua no longer maintains dialogue history (Python is sole owner)
        -- This message is now a no-op but kept for backwards compatibility
        print("[Socket] reload_history received (no-op, Python manages history)")

    elseif msgType == "activate_commitment" then
        -- Python requests NPC schedule override for commitment
        local npcId = data.npc_id or ""
        local activityId = data.activity_id or ""
        local locationId = data.location_id or ""
        if npcId ~= "" and _G.CommitmentManager then
            ExecuteInGameThread(function()
                pcall(function()
                    _G.CommitmentManager.Apply(npcId, activityId, locationId)
                end)
            end)
        else
            print("[Socket] activate_commitment: missing npc_id or CommitmentManager not loaded")
        end

    elseif msgType == "deactivate_commitment" then
        -- Python requests NPC schedule override release
        local npcId = data.npc_id or ""
        if npcId ~= "" and _G.CommitmentManager then
            ExecuteInGameThread(function()
                pcall(function()
                    _G.CommitmentManager.Release(npcId)
                end)
            end)
        else
            print("[Socket] deactivate_commitment: missing npc_id or CommitmentManager not loaded")
        end

    elseif msgType == "notification" then
        -- Show in-game notification (top-left text notification panel)
        local text = data.text or ""
        if text ~= "" then
            print("[Socket] Notification: " .. text)
            if ShowNotification then
                ShowNotification(text)
            end
        end

    elseif msgType == "conversation_mode" then
        -- Conversation mode changed (via configurable hotkey, default Home)
        _G.ConversationMode = data.mode or "default"
        local modeNames = {
            default = "Default Mode",
            ["1to1"] = "1-to-1 Mode",
            continuous = "Continuous Mode"
        }
        local displayName = modeNames[data.mode] or data.mode
        -- Show hint at top-left for 2 seconds
        if ShowHint then
            ShowHint(displayName, 2)
        end
        print("[Socket] Conversation mode: " .. tostring(data.mode))

    elseif msgType == "cast_spell" then
        -- Voice spell casting from Python
        local spellName = data.spell
        if spellName then
            -- Wakeword detection (no text field) requires aim mode
            -- Text-based fallback (has text field) always passes
            if not data.text then
                local shouldCast = false
                -- VR mode: Python already gates on spell pose, trust it
                if _G.VROffset then
                    shouldCast = true
                else
                    -- Flat-screen: check right-click aim blend
                    pcall(function()
                        local pc = FindFirstOf("Biped_PlayerController")
                        if pc and pc:IsValid() then
                            shouldCast = (pc:GetAimBlendParameter() > 0.5)
                        end
                    end)
                end
                if not shouldCast then return end
            end

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

    elseif msgType == "vr_offset" then
        -- VR headset yaw/pitch offset for gaze targeting
        _G.VROffset = { yaw = data.yaw or 0, pitch = data.pitch or 0 }
        if data.debug then
            _G.VRDebug = data.debug
        end

    elseif msgType == "refresh_house_points" then
        -- On-demand house points refresh (for fresh context before professor conversations)
        DevPrint("[Socket] Refreshing house points on demand")
        local hasData = false
        if RefreshHousePoints then
            hasData = RefreshHousePoints()
        end
        -- Send acknowledgment back to Python
        SocketClient.send({
            type = "house_points_refreshed",
            has_data = hasData
        })

    elseif msgType == "move_companion" then
        -- Player commanded companion to move to where they're looking
        ExecuteInGameThread(function()
            local staticData = Cache.GetStaticData()
            if not staticData then return end

            -- Get companion manager + pawn
            local companionMgr = staticData.companionManager
            if not companionMgr then return end
            local companionPawn = nil
            pcall(function() companionPawn = companionMgr:GetPrimaryCompanionPawn() end)
            if not companionPawn or not companionPawn:IsValid() then return end

            -- Abort if companion is already in a forced wait (quest/puzzle)
            if Utils.IsCompanionForcedWaiting(companionPawn, companionMgr) then
                print("[MoveCompanion] Companion already in forced wait (quest?) - aborting")
                return
            end

            -- Get camera info
            local cam = staticData.cameraManager
            if not cam then return end
            local camLoc, camRot
            pcall(function()
                camLoc = cam:GetCameraLocation()
                camRot = cam:GetCameraRotation()
            end)
            if not camLoc or not camRot then return end

            -- Trace origin: VR = player head, flat = camera
            local vrOff = _G.VROffset
            local originX, originY, originZ = camLoc.X, camLoc.Y, camLoc.Z
            local player = staticData.player
            if vrOff and player then
                local playerLoc
                pcall(function() playerLoc = player:K2_GetActorLocation() end)
                if playerLoc then
                    local playerHalfHeight = 88
                    pcall(function()
                        local capsule = player.CapsuleComponent
                        if capsule and capsule.CapsuleHalfHeight then
                            playerHalfHeight = capsule.CapsuleHalfHeight
                        end
                    end)
                    originX = playerLoc.X
                    originY = playerLoc.Y
                    originZ = playerLoc.Z + playerHalfHeight * 2
                end
            end

            -- Raycast to find world hit position
            local KismetSystem = staticData.kismetSystem
            local KismetMath = staticData.kismetMath
            if not KismetSystem or not KismetMath then return end

            local ActorsToIgnore = {}
            if player then table.insert(ActorsToIgnore, player) end
            table.insert(ActorsToIgnore, companionPawn)
            local TraceColor = { R = 0, G = 0, B = 0, A = 0 }

            -- Forward vector (with VR offset if active)
            local pitch = math.rad(camRot.Pitch + (vrOff and vrOff.pitch or 0))
            local yaw = math.rad(camRot.Yaw + (vrOff and vrOff.yaw or 0))
            local fwd = {
                X = math.cos(pitch) * math.cos(yaw),
                Y = math.cos(pitch) * math.sin(yaw),
                Z = math.sin(pitch)
            }

            local traceLen = 1500 -- ~15m max range
            local traceStart, traceEnd
            pcall(function()
                traceStart = KismetMath:MakeVector(originX, originY, originZ)
                traceEnd = KismetMath:MakeVector(
                    originX + fwd.X * traceLen,
                    originY + fwd.Y * traceLen,
                    originZ + fwd.Z * traceLen
                )
            end)
            if not traceStart or not traceEnd then return end

            local HitResult = {}
            local WasHit = false
            local traceOk, traceErr = pcall(function()
                WasHit = KismetSystem:LineTraceSingle(
                    player or companionPawn, traceStart, traceEnd,
                    0, false, ActorsToIgnore,
                    0, HitResult, true,
                    TraceColor, TraceColor, 0.0
                )
            end)

            if not traceOk then
                print("[MoveCompanion] Trace error: " .. tostring(traceErr))
                return
            end
            if not WasHit then
                print("[MoveCompanion] Raycast missed")
                return
            end

            local hitLoc = nil
            pcall(function()
                hitLoc = {
                    X = HitResult.ImpactPoint_X or HitResult.ImpactPoint.X,
                    Y = HitResult.ImpactPoint_Y or HitResult.ImpactPoint.Y,
                    Z = HitResult.ImpactPoint_Z or HitResult.ImpactPoint.Z
                }
            end)
            if not hitLoc then
                print("[MoveCompanion] Could not extract hit location")
                return
            end

            -- Handle wall hits: offset + floor trace
            local hitNormal = nil
            pcall(function()
                hitNormal = {
                    X = HitResult.ImpactNormal_X or HitResult.ImpactNormal.X,
                    Y = HitResult.ImpactNormal_Y or HitResult.ImpactNormal.Y,
                    Z = HitResult.ImpactNormal_Z or HitResult.ImpactNormal.Z
                }
            end)

            if hitNormal and hitNormal.Z < 0.7 then
                local offset = 150
                local offsetPos = {
                    X = hitLoc.X + hitNormal.X * offset,
                    Y = hitLoc.Y + hitNormal.Y * offset,
                    Z = hitLoc.Z
                }
                local floorStart, floorEnd
                pcall(function()
                    floorStart = KismetMath:MakeVector(offsetPos.X, offsetPos.Y, offsetPos.Z + 200)
                    floorEnd = KismetMath:MakeVector(offsetPos.X, offsetPos.Y, offsetPos.Z - 500)
                end)
                if floorStart and floorEnd then
                    local FloorHit = {}
                    local FloorWasHit = false
                    pcall(function()
                        FloorWasHit = KismetSystem:LineTraceSingle(
                            player or companionPawn, floorStart, floorEnd,
                            0, false, ActorsToIgnore,
                            0, FloorHit, true,
                            TraceColor, TraceColor, 0.0
                        )
                    end)
                    if FloorWasHit then
                        pcall(function()
                            hitLoc = {
                                X = FloorHit.ImpactPoint_X or FloorHit.ImpactPoint.X,
                                Y = FloorHit.ImpactPoint_Y or FloorHit.ImpactPoint.Y,
                                Z = FloorHit.ImpactPoint_Z or FloorHit.ImpactPoint.Z
                            }
                        end)
                        print("[MoveCompanion] Wall hit - adjusted to floor position")
                    else
                        hitLoc = offsetPos
                        local playerLoc = nil
                        pcall(function() playerLoc = player:K2_GetActorLocation() end)
                        if playerLoc then hitLoc.Z = playerLoc.Z end
                        print("[MoveCompanion] Wall hit - no floor found, using player Z")
                    end
                end
            end

            print(string.format("[MoveCompanion] hit=(%.0f,%.0f,%.0f) fwd=(%.3f,%.3f,%.3f)",
                hitLoc.X, hitLoc.Y, hitLoc.Z, fwd.X, fwd.Y, fwd.Z))

            -- Sanity check: abort if hit point is too far from player
            local dx = hitLoc.X - originX
            local dy = hitLoc.Y - originY
            local dz = hitLoc.Z - originZ
            local dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist > 1500 then -- ~15m max
                print(string.format("[MoveCompanion] Too far (%.0f units) - aborting", dist))
                return
            end

            -- Use SetCompanionForcedWaitLocation to move companion (same as NPCLock companion pattern)
            -- Direction vector from hit location toward the player (so companion faces back at player)
            local dirX = originX - hitLoc.X
            local dirY = originY - hitLoc.Y
            local dirLen = math.sqrt(dirX * dirX + dirY * dirY)
            local waitDir = { X = 0, Y = 1, Z = 0 }
            if dirLen > 0.01 then
                waitDir = { X = dirX / dirLen, Y = dirY / dirLen, Z = 0 }
            end

            local moveOk, moveErr = pcall(function()
                companionMgr:SetCompanionForcedWaitLocation(hitLoc, waitDir)
            end)
            if not moveOk then
                print("[MoveCompanion] SetCompanionForcedWaitLocation error: " .. tostring(moveErr))
                return
            end
            print(string.format("[MoveCompanion] Moving %s to (%.0f, %.0f, %.0f)",
                data.npc_id or "companion", hitLoc.X, hitLoc.Y, hitLoc.Z))

            -- Release forced wait after 5s so companion resumes following
            -- Use generation counter so rapid commands don't cancel each other
            _G._MoveCompanionGen = (_G._MoveCompanionGen or 0) + 1
            local gen = _G._MoveCompanionGen
            ExecuteInGameThreadWithDelay(5000, function()
                if _G._MoveCompanionGen ~= gen then return end -- superseded by newer command
                pcall(function() companionMgr:StopCompanionForcedWaiting() end)
                print("[MoveCompanion] Released forced wait")
            end)
        end)

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
-- Persistent OnTick Loop (25ms interval)
-- ============================================
-- Started once at module load, runs forever, checks state to decide if it should process
-- Uses LoopInGameThreadWithDelay for proper timer control
if not _G.OnTickLoopStarted then
    _G.OnTickLoopStarted = true
    _G.OnTickLoopHandle = LoopInGameThreadWithDelay(25, function()
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
