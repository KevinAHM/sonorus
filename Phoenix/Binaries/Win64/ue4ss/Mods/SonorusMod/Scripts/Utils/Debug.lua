-- Persistent scene capture state (survives F11)
_G._SceneCapture = _G._SceneCapture or { actor = nil, rt = nil }
_G._DebugF7MuteState = _G._DebugF7MuteState or {}

local BlueprintHelpers = require("Utils.BlueprintHelpers")
local Utils = require("Utils.Utils")

function DebugF7RenderScene()
    local TAG = "[SceneCapture]"
    print(TAG .. " === NPC Vision Test ===")

    -- Step 1: basics
    local player = FindFirstOf("Biped_Player")
    if not player or not player:IsValid() then print(TAG .. " FAIL: no player") return end
    local pc = FindFirstOf("PlayerController")
    if not pc or not pc:IsValid() then print(TAG .. " FAIL: no PC") return end
    print(TAG .. " OK: player + PC")

    -- Step 2: create render target (once, reuse across F7 presses)
    -- Force re-create RT if format changed (clear cache once)
    if _G._SceneCapture.rtVersion ~= 11 then
        _G._SceneCapture.rt = nil
        _G._SceneCapture.actor = nil
        _G._SceneCapture.rtVersion = 11
    end
    local rt = _G._SceneCapture.rt
    if not rt or not pcall(function() return rt:IsValid() end) or not rt:IsValid() then
        local krl = StaticFindObject("/Script/Engine.Default__KismetRenderingLibrary")
        if not krl then print(TAG .. " FAIL: no KismetRenderingLibrary CDO") return end

        local ok, result = pcall(function()
            -- CreateRenderTarget2D(WorldCtx, W, H, Format=RTF_RGBA8=2, ClearColor, bAutoMips)
            -- RTF_RGBA8 = 2
            return krl:CreateRenderTarget2D(pc, 256, 256, 2, { R = 0, G = 0, B = 0, A = 1 }, false)
        end)
        if not ok or not result then
            print(TAG .. " FAIL: CreateRenderTarget2D: " .. tostring(result))
            -- retry without optional args
            ok, result = pcall(function() return krl:CreateRenderTarget2D(pc, 256, 256) end)
            if not ok or not result then
                print(TAG .. " FAIL: CreateRenderTarget2D (minimal): " .. tostring(result))
                return
            end
        end
        rt = result
        _G._SceneCapture.rt = rt
        rt.TargetGamma = 1.0
        print(TAG .. " OK: RT created: " .. rt:GetFullName())
    else
        print(TAG .. " OK: RT reused: " .. rt:GetFullName())
    end

    -- Step 3: spawn ASceneCapture2D actor (once, reuse)
    local captureActor = _G._SceneCapture.actor
    if not captureActor or not pcall(function() return captureActor:IsValid() end) or not captureActor:IsValid() then
        local sc2dClass = StaticFindObject("/Script/Engine.SceneCapture2D")
        if not sc2dClass then print(TAG .. " FAIL: no SceneCapture2D class") return end

        local gps = StaticFindObject("/Script/Engine.Default__GameplayStatics")
        if not gps then print(TAG .. " FAIL: no GameplayStatics CDO") return end

        local playerLoc = player:K2_GetActorLocation()
        local spawnTransform = {
            Translation = { X = playerLoc.X, Y = playerLoc.Y, Z = playerLoc.Z + 80 },
            Rotation = { X = 0, Y = 0, Z = 0, W = 1 },
            Scale3D = { X = 1, Y = 1, Z = 1 },
        }

        local ok1, err1 = pcall(function()
            captureActor = gps:BeginDeferredActorSpawnFromClass(pc, sc2dClass, spawnTransform, 2, nil)
        end)
        if not ok1 or not captureActor then
            print(TAG .. " FAIL: BeginDeferred: ok=" .. tostring(ok1) .. " err=" .. tostring(err1))
            return
        end

        local ok2, err2 = pcall(function()
            gps:FinishSpawningActor(captureActor, spawnTransform)
        end)
        if not ok2 then
            print(TAG .. " WARN: FinishSpawning: " .. tostring(err2))
        end

        _G._SceneCapture.actor = captureActor
        print(TAG .. " OK: actor spawned: " .. captureActor:GetFullName())
    else
        print(TAG .. " OK: actor reused: " .. captureActor:GetFullName())
    end

    -- Step 4: get CaptureComponent2D and wire up render target
    local comp = nil
    local ok3, err3 = pcall(function() comp = captureActor.CaptureComponent2D end)
    if not ok3 or not comp then
        print(TAG .. " FAIL: no CaptureComponent2D: " .. tostring(err3))
        return
    end
    print(TAG .. " OK: component: " .. comp:GetFullName())

    pcall(function()
        comp.TextureTarget = rt
        comp.FOVAngle = 90.0
        comp.CaptureSource = 2              -- SCS_FinalColorLDR
        comp.bCaptureEveryFrame = false
        comp.bAlwaysPersistRenderingState = true

        comp.PostProcessBlendWeight = 0.0   -- use world PP, not component's empty one
    end)

    -- Step 5: position at player's eye level, facing forward
    local loc = player:K2_GetActorLocation()
    local camRot = nil
    pcall(function()
        local cam = pc.PlayerCameraManager
        if cam and cam:IsValid() then
            camRot = cam:GetCameraRotation()
        end
    end)

    local ok4, err4 = pcall(function()
        captureActor:K2_SetActorLocation({ X = loc.X, Y = loc.Y, Z = loc.Z + 80 }, false, {}, false)
    end)
    if not ok4 then print(TAG .. " WARN: SetLocation: " .. tostring(err4)) end

    if camRot then
        local ok5, err5 = pcall(function()
            captureActor:K2_SetActorRotation(camRot, false)
        end)
        if not ok5 then print(TAG .. " WARN: SetRotation: " .. tostring(err5)) end
        print(TAG .. " OK: positioned at player eye, rot=" .. string.format("P=%.0f Y=%.0f", camRot.Pitch, camRot.Yaw))
    else
        print(TAG .. " OK: positioned at player eye (no rotation)")
    end

    -- Step 6: delay for component init, then single capture
    print(TAG .. " OK: waiting 100ms for init...")

    ExecuteInGameThreadWithDelay(100, function()
        pcall(function() comp:CaptureScene() end)
        print(TAG .. " OK: CaptureScene called")

        -- Export
        local krl2 = StaticFindObject("/Script/Engine.Default__KismetRenderingLibrary")
        local outDir = "C:/HogwartsAI/misc"
        local outFile = "npc_vision_test.png"
        local ok7, err7 = pcall(function()
            krl2:ExportRenderTarget(pc, rt, outDir, outFile)
        end)
        if not ok7 then
            print(TAG .. " FAIL: Export: " .. tostring(err7))
            return
        end
        print(TAG .. " OK: exported to " .. outDir .. "/" .. outFile)
        print(TAG .. " === Test Complete ===")
    end)
end

function DebugF7()
    ExecuteInGameThread(function()
        local TAG = "[PresenceValidation]"
        local staticData = GetStaticCache and GetStaticCache()
        if not staticData then
            print(TAG .. " no static cache")
            if ShowHint then ShowHint("Presence validation: no game cache", 3) end
            return
        end

        if not _G.SocketClient or not _G.SocketClient.send then
            print(TAG .. " socket unavailable")
            if ShowHint then ShowHint("Presence validation: server disconnected", 3) end
            return
        end

        local gameTime = GetTimeOfDay and GetTimeOfDay() or nil
        if not gameTime then
            print(TAG .. " game time unavailable")
            if ShowHint then ShowHint("Presence validation: no game time", 3) end
            return
        end

        local observations = {}
        local seenActors = {}
        local handledVoiceIds = {}

        local function canonicalVoiceId(voiceId)
            if not voiceId or voiceId == "" then return voiceId end
            local lower = voiceId:lower()
            local alias = _G.SignificantNPCVoiceAliases
                    and _G.SignificantNPCVoiceAliases[lower]
            if alias then return alias end
            if _G.NormalizeNpcId then
                return _G.NormalizeNpcId(voiceId)
            end
            return (_G.VoiceIdNormalize and _G.VoiceIdNormalize[lower]) or voiceId
        end

        local function voiceKey(voiceId)
            return voiceId and voiceId:lower() or ""
        end

        local function addScheduleObservation(voiceId, scheduleInfo, cacheMiss)
            table.insert(observations, {
                id = voiceId,
                source = "scheduler",
                inFlesh = scheduleInfo and scheduleInfo.inFlesh or false,
                cacheMiss = cacheMiss == true,
                scheduleLookupFailed = scheduleInfo == nil,
                scheduleLocationId = scheduleInfo and scheduleInfo.locationId or nil,
                scheduleLocationName = scheduleInfo and scheduleInfo.locationName or nil,
                activity = scheduleInfo and scheduleInfo.activity or nil,
                activityType = scheduleInfo and scheduleInfo.activityType or nil,
                isInTransit = scheduleInfo and scheduleInfo.isInTransit or false,
            })
        end

        -- First collect live significant NPCs from the actor cache. Verify flesh
        -- state through the scheduler so retained placeholder actors are not
        -- reported as physical observations.
        local npcs = GetCachedNPCs and GetCachedNPCs() or {}
        for _, actor in pairs(npcs) do
            if BlueprintHelpers.SafeIsValid(actor) then
                local ok, actorKey, voiceId, location = pcall(function()
                    local key = actor:GetFullName()
                    local id = Utils.GetActorVoiceId(actor, staticData)
                    local loc = actor:K2_GetActorLocation()
                    return key, id, loc
                end)
                if ok and actorKey and not seenActors[actorKey]
                        and voiceId and voiceId ~= "" and voiceId ~= "Unknown"
                        and location and _G.IsSignificantNPC
                        and _G.IsSignificantNPC(voiceId) then
                    seenActors[actorKey] = true
                    voiceId = canonicalVoiceId(voiceId)
                    local key = voiceKey(voiceId)
                    if not handledVoiceIds[key] then
                        handledVoiceIds[key] = true
                        local scheduleInfo = Utils.GetNPCScheduleInfo(voiceId, staticData)
                        if scheduleInfo and not scheduleInfo.inFlesh then
                            addScheduleObservation(voiceId, scheduleInfo, false)
                        else
                            table.insert(observations, {
                                id = voiceId,
                                source = "flesh",
                                inFlesh = true,
                                actorKey = actorKey,
                                x = location.X,
                                y = location.Y,
                                z = location.Z,
                            })
                        end
                    end
                end
            end
        end

        -- Then query every significant voice ID not represented by the live
        -- cache. GetNPCScheduleInfo works for non-streamed scheduled entities.
        local significantVoiceIds = {}
        local queuedVoiceIds = {}
        for voiceId, enabled in pairs(_G.SignificantNPCVoiceIds or {}) do
            local canonicalId = canonicalVoiceId(voiceId)
            local lower = voiceKey(canonicalId)
            if enabled and lower ~= "player" and lower ~= "playermale"
                    and lower ~= "playerfemale" and not queuedVoiceIds[lower] then
                queuedVoiceIds[lower] = true
                table.insert(significantVoiceIds, canonicalId)
            end
        end
        table.sort(significantVoiceIds)

        for _, voiceId in ipairs(significantVoiceIds) do
            local key = voiceKey(voiceId)
            if not handledVoiceIds[key] then
                handledVoiceIds[key] = true
                local scheduleInfo = Utils.GetNPCScheduleInfo(voiceId, staticData)
                addScheduleObservation(voiceId, scheduleInfo,
                    scheduleInfo and scheduleInfo.inFlesh or false)
            end
        end

        table.sort(observations, function(a, b)
            if a.id == b.id then
                return tostring(a.actorKey or "") < tostring(b.actorKey or "")
            end
            return a.id < b.id
        end)

        local sent = _G.SocketClient.send({
            type = "presence_validation_sample",
            gameDate = gameTime.dateShort or gameTime.dateFormatted or "",
            gameTime = gameTime.formatted or "",
            dayOfWeek = gameTime.dayOfWeek,
            minutesOfDay = (gameTime.hour or 0) * 60 + (gameTime.minute or 0),
            observations = observations,
        })

        if sent == false then
            print(TAG .. " send failed")
            if ShowHint then ShowHint("Presence validation: send failed", 3) end
            return
        end

        print(string.format("%s sent %d named NPCs at %s", TAG,
            #observations, tostring(gameTime.formatted)))
        if ShowHint then
            ShowHint(string.format("Presence validation: sampled %d NPCs", #observations), 3)
        end
    end)
end

-- ============================================
-- F7: Station Exit Animation Diagnostic
-- Toggle: fast-polls (250ms) nearest NPC's actual animation during exit
-- Start diag, then trigger the exit — watch for montage/anim changes
-- ============================================
_G._StationExitDiag = _G._StationExitDiag or { active = false, loopHandle = nil, npc = nil, se = nil }

function DebugF7_StationExit()
    ExecuteInGameThread(function()
        local diag = _G._StationExitDiag

        -- Toggle off
        if diag.active then
            diag.active = false
            if diag.loopHandle then
                pcall(CancelDelayedAction, diag.loopHandle)
                diag.loopHandle = nil
            end
            diag.npc = nil
            diag.se = nil
            print("[StationDiag] STOPPED\n")
            if ShowHint then ShowHint("Station Diag: OFF", 2) end
            return
        end

        -- Find nearest NPC with a ScheduledEntity
        local player = nil
        pcall(function() player = FindFirstOf("Biped_Player") end)
        if not player or not player:IsValid() then
            if ShowHint then ShowHint("No player", 2) end
            return
        end
        local pLoc = player:K2_GetActorLocation()

        local popMgr = nil
        pcall(function() popMgr = FindFirstOf("PopulationManager") end)
        if not popMgr or not popMgr:IsValid() then
            if ShowHint then ShowHint("No PopulationManager", 2) end
            return
        end

        local bestNpc, bestSe, bestName, bestDist = nil, nil, nil, math.huge
        local allChars = nil
        pcall(function() allChars = FindAllOf("Character") end)
        if allChars then
            local playerFN = player:GetFullName()
            for _, actor in pairs(allChars) do
                pcall(function()
                    if not actor:IsValid() then return end
                    local fn = actor:GetFullName()
                    if fn == playerFN then return end
                    if fn:find("BP_Tier3_Character") then return end
                    local loc = actor:K2_GetActorLocation()
                    local d = math.sqrt((loc.X - pLoc.X)^2 + (loc.Y - pLoc.Y)^2)
                    if d >= bestDist or d > 3000 then return end
                    local se = popMgr:GetScheduledEntityFromActor(actor, false)
                    if not se then return end
                    local voiceName = nil
                    pcall(function()
                        local mn = se:GetMyName()
                        pcall(function() voiceName = mn:ToString() end)
                    end)
                    bestDist, bestNpc, bestSe, bestName = d, actor, se, voiceName or "?"
                end)
            end
        end

        if not bestNpc then
            if ShowHint then ShowHint("No NPC nearby", 2) end
            return
        end

        diag.npc = bestNpc
        diag.se = bestSe
        diag.active = true
        diag.startPos = nil
        pcall(function() diag.startPos = bestNpc:K2_GetActorLocation() end)

        print(string.format("[StationDiag] STARTED on %s (dist=%.0fm)\n", bestName, bestDist / 100))
        if ShowHint then ShowHint("Anim Diag: ON\n" .. bestName .. "\nTrigger exit now", 2) end

        -- Fast poll: 250ms to catch animation transitions
        diag.loopHandle = LoopInGameThreadWithDelay(251, function()
            if not diag.active then
                if diag.loopHandle then pcall(CancelDelayedAction, diag.loopHandle) end
                diag.loopHandle = nil
                return
            end

            local npc = diag.npc
            local se = diag.se
            if not npc or not se then
                diag.active = false
                if diag.loopHandle then pcall(CancelDelayedAction, diag.loopHandle) end
                diag.loopHandle = nil
                return
            end

            local npcValid = false
            pcall(function() npcValid = npc:IsValid() end)
            local seValid = false
            pcall(function() seValid = se:IsValid() end)
            if not npcValid or not seValid then
                diag.active = false
                if diag.loopHandle then pcall(CancelDelayedAction, diag.loopHandle) end
                diag.loopHandle = nil
                if ShowHint then ShowHint("Anim Diag: NPC lost", 2) end
                return
            end

            local lines = {}

            -- Get animInst
            local animInst = nil
            pcall(function()
                local mesh = npc.Mesh
                if mesh then animInst = mesh:GetAnimInstance() end
            end)

            -- 1. Montage: IsAnyMontagePlaying + name
            local montage = "?"
            if animInst then
                pcall(function()
                    local playing = animInst:IsAnyMontagePlaying()
                    local montageName = "nil"
                    if playing then
                        pcall(function()
                            local current = animInst:GetCurrentActiveMontage()
                            if current then montageName = current:GetName() end
                        end)
                    end
                    montage = playing and ("PLAYING: " .. montageName) or "none"
                end)
            end
            table.insert(lines, "Montage: " .. montage)

            -- 2. Character:GetCurrentMontage (shortcut on Character, might differ)
            local charMontage = "?"
            pcall(function()
                local cm = npc:GetCurrentMontage()
                if cm then
                    charMontage = cm:GetName()
                else
                    charMontage = "nil"
                end
            end)
            table.insert(lines, "CharMontage: " .. charMontage)

            -- 3. GetActiveStation
            local station = "?"
            pcall(function()
                local sc = se:GetActiveStation()
                station = sc and "AT STATION" or "nil"
            end)
            table.insert(lines, "Station: " .. station)

            -- 4. IsInTransit
            local transit = "?"
            pcall(function() transit = tostring(se:IsInTransit()) end)
            table.insert(lines, "Transit: " .. transit)

            -- 5. Position delta from start (detect movement)
            local posDelta = "?"
            pcall(function()
                local loc = npc:K2_GetActorLocation()
                if diag.startPos then
                    local dx = loc.X - diag.startPos.X
                    local dy = loc.Y - diag.startPos.Y
                    local dz = loc.Z - diag.startPos.Z
                    local dist = math.sqrt(dx*dx + dy*dy + dz*dz)
                    posDelta = string.format("%.0f", dist / 100) .. "m"
                end
            end)
            table.insert(lines, "Moved: " .. posDelta)

            -- 6. Velocity (from CharacterMovement)
            local vel = "?"
            pcall(function()
                local cmc = npc.CharacterMovement
                if cmc then
                    local v = cmc.Velocity
                    local speed = math.sqrt(v.X*v.X + v.Y*v.Y + v.Z*v.Z)
                    vel = string.format("%.0f", speed)
                end
            end)
            table.insert(lines, "Speed: " .. vel)

            -- 7. IsWaitingForStation
            local waiting = "?"
            pcall(function() waiting = tostring(se:IsWaitingForStation()) end)
            table.insert(lines, "Waiting: " .. waiting)

            local msg = table.concat(lines, "\n")
            print("[StationDiag] " .. msg:gsub("\n", " | ") .. "\n")
            if ShowHint then ShowHint(msg, 0.5) end
        end)
    end)
end

-- Station test state (persists until game restart)
-- Reset step on config change
if _G.StationTestACT ~= "HOG_AstronomyTower" then
    _G.StationTestStep = 3  -- skip step 1, no DB insert needed
    _G.StationTestACT = "HOG_AstronomyTower"
end
_G.StationTestStep = _G.StationTestStep or 3
_G.StationTestSE = nil       -- cached ScheduledEntity
_G.StationTestProvider = nil -- cached provider for override

-- Test: existing 24hr activity at Hogwarts while player is in Hogsmeade
local STATION_TEST_LOC = "HOG_AstronomyTower"
local STATION_TEST_ACT = "HOG_AstronomyTower"
local STATION_TEST_NPC = "NatsaiOnai"
local STATION_TEST_POS = { x = 389060.72, y = -512386.69, z = -83232.04 }
local STATION_TEST_YAW = -76.04

function DebugF7_StationTest()
    ExecuteInGameThread(function()
        local step = _G.StationTestStep
        print(string.format("[StationTest] === Step %d ===\n", step))

        local DbGateway = FindFirstOf("DbGateway")
        if not DbGateway:IsValid() then
            print("[StationTest] ERROR: DbGateway not found\n")
            return
        end

        -- Helper: run query and return first row as key-value table
        local function queryFirst(query)
            local outResult = {}
            local success = DbGateway:DbQuery(query, outResult)
            if not success or not outResult.Success then return nil end
            local row = nil
            for _, rowElem in pairs(outResult.ResultRows) do
                local r = rowElem:get()
                row = {}
                r.Fields:ForEach(function(_, fieldElem)
                    local f = fieldElem:get()
                    row[f.Key:ToString()] = f.Value:ToString()
                end)
                break  -- first row only
            end
            return row
        end

        ---------------------------------------------------------------
        -- STEP 1: Insert DB entries (Location + Activity)
        ---------------------------------------------------------------
        if step == 1 then
            -- Only need custom ActivityDefinition (0-2400) pointing at existing LocationID
            local existing = queryFirst("SELECT ActivityID FROM ActivityDefinition WHERE ActivityID = '" .. STATION_TEST_ACT .. "'")
            if existing then
                print("[StationTest] Activity already exists, skipping to step 2\n")
                if ShowHint then ShowHint("Step 1: Activity exists\nPress F7 to send " .. STATION_TEST_NPC, 3) end
                _G.StationTestStep = 3
                return
            end

            local actSQL = string.format(
                "INSERT INTO ActivityDefinition (ActivityID, ActivityTypeID, StartTime, EndTime, LocationID, ActivityRecurrenceTypeID, Sunday, Monday, Tuesday, Wednesday, Thursday, Friday, Saturday) " ..
                "VALUES ('%s', 'FreeTime', 0, 2400, '%s', 'Daily', '1', '1', '1', '1', '1', '1', '1')",
                STATION_TEST_ACT, STATION_TEST_LOC)
            local ok = DbGateway:DbOperate(actSQL, false)
            print(string.format("[StationTest] INSERT Activity: %s\n", tostring(ok)))

            local verify = queryFirst("SELECT ActivityID, LocationID FROM ActivityDefinition WHERE ActivityID = '" .. STATION_TEST_ACT .. "'")
            if verify then
                print(string.format("[StationTest] Verified: %s -> %s\n", verify.ActivityID, verify.LocationID))
                _G.StationTestStep = 3
                print("[StationTest] DB ready. Press F7 to send NPC.\n")
                if ShowHint then ShowHint("Step 1 DONE: Activity inserted\n" .. STATION_TEST_ACT .. " -> " .. STATION_TEST_LOC .. "\nPress F7 to send " .. STATION_TEST_NPC, 8) end
            else
                print("[StationTest] ERROR: Verification failed\n")
                if ShowHint then ShowHint("Step 1 FAILED", 5) end
            end

        ---------------------------------------------------------------
        -- STEP 3: Send EverettClopton via commitment-style override
        ---------------------------------------------------------------
        elseif step == 3 then
            local popManager = FindFirstOf("PopulationManager")
            if not popManager:IsValid() then
                print("[StationTest] ERROR: PopulationManager not found\n")
                return
            end

            local se = popManager:GetScheduledEntityFromName(STATION_TEST_NPC)
            if not se:IsValid() then
                print(string.format("[StationTest] ERROR: SE not found for %s\n", STATION_TEST_NPC))
                return
            end
            print(string.format("[StationTest] Found SE: %s\n", se:GetFullName()))

            local weActor = FindFirstOf("WorldEventActor")
            if not weActor:IsValid() then
                print("[StationTest] ERROR: WorldEventActor not found\n")
                return
            end

            -- StartSchedulingOverride
            local ok1, err1 = pcall(function()
                se:StartSchedulingOverride(true, 4, weActor, true, true, true)
            end)
            if not ok1 then
                print(string.format("[StationTest] StartSchedulingOverride ERROR: %s\n", tostring(err1)))
                return
            end
            print("[StationTest] StartSchedulingOverride done\n")

            -- InsertDynamicActivityOnSE
            local insertResult = false
            local ok2, err2 = pcall(function()
                insertResult = weActor:InsertDynamicActivityOnSE(se, STATION_TEST_ACT, STATION_TEST_LOC)
            end)
            if not ok2 then
                print(string.format("[StationTest] InsertDynamic ERROR: %s\n", tostring(err2)))
            end
            print(string.format("[StationTest] InsertDynamicActivityOnSE: %s\n", tostring(insertResult)))

            -- Cache for release
            _G.StationTestSE = se
            _G.StationTestProvider = weActor
            _G.StationTestStep = 4
            print(string.format("[StationTest] Sent %s to %s. Checking status in 5s...\n", STATION_TEST_NPC, STATION_TEST_LOC))
            if ShowHint then ShowHint("Step 3: Sent " .. STATION_TEST_NPC .. "\nInsertDynamic: " .. tostring(insertResult) .. "\nChecking status in 5s...", 5) end


            -- After 5 seconds, check status and teleport if in flesh
            ExecuteInGameThreadWithDelay(5000, function()
                local inFlesh = false
                pcall(function() inFlesh = se:CurrentlyInFlesh() end)
                if inFlesh then
                    local popManager2 = FindFirstOf("PopulationManager")
                    local yawRad = STATION_TEST_YAW * math.pi / 180.0
                    local halfYaw = yawRad / 2.0
                    local transform = {
                        Rotation = { X = 0.0, Y = 0.0, Z = math.sin(halfYaw), W = math.cos(halfYaw) },
                        Translation = { X = STATION_TEST_POS.x, Y = STATION_TEST_POS.y, Z = STATION_TEST_POS.z },
                        Scale3D = { X = 1.0, Y = 1.0, Z = 1.0 }
                    }
                    local result = popManager2:PlaceScheduledEntityBP(STATION_TEST_NPC, transform)
                    print(string.format("[StationTest] PlaceScheduledEntityBP: %s\n", tostring(result)))
                    if ShowHint then ShowHint("Placed " .. STATION_TEST_NPC .. " at vector: " .. tostring(result), 5) end
                end
                local statusLines = {}
                table.insert(statusLines, STATION_TEST_NPC .. " status:")

                pcall(function()
                    local inFlesh = se:CurrentlyInFlesh()
                    table.insert(statusLines, "InFlesh: " .. tostring(inFlesh))
                end)
                pcall(function()
                    local inTransit = se:IsInTransit()
                    table.insert(statusLines, "InTransit: " .. tostring(inTransit))
                end)
                pcall(function()
                    local loc = se:GetLocation()
                    table.insert(statusLines, string.format("Pos: (%.0f, %.0f, %.0f)", loc.X, loc.Y, loc.Z))
                end)
                pcall(function()
                    local out = {}
                    local out2 = {}
                    se:GetCurrentActivity(out, out2)
                    if out.ActivityIsValid then
                        local act = ""
                        pcall(function() act = out.Activity:ToString() end)
                        local actType = ""
                        pcall(function() actType = out.ActivityType:ToString() end)
                        local locKey = ""
                        pcall(function() locKey = out.Location:ToString() end)
                        table.insert(statusLines, "Activity: " .. act)
                        table.insert(statusLines, "Type: " .. actType)
                        table.insert(statusLines, "Location: " .. locKey)
                    else
                        table.insert(statusLines, "Activity: NONE")
                    end
                end)
                pcall(function()
                    local enabled = se:IsEnabled()
                    table.insert(statusLines, "Enabled: " .. tostring(enabled))
                end)

                local msg = table.concat(statusLines, "\n")
                print("[StationTest] " .. msg:gsub("\n", "\n[StationTest] ") .. "\n")
                if ShowHint then
                    ShowHint(msg, 10)
                end
            end)

        ---------------------------------------------------------------
        -- STEP 4: Release NPC, reset to step 2
        ---------------------------------------------------------------
        elseif step == 4 then
            local se = _G.StationTestSE
            local provider = _G.StationTestProvider

            if not se or not se:IsValid() then
                print("[StationTest] ERROR: No cached SE to release\n")
                _G.StationTestStep = 2
                return
            end

            -- Release from station via StationUse
            local StationUse = require("Utils.StationUse")
            StationUse.Release(STATION_TEST_NPC)
            print("[StationTest] StationUse.Release done\n")

            -- RemoveDynamicActivityFromSE
            if provider and provider:IsValid() then
                pcall(function()
                    provider:RemoveDynamicActivityFromSE(se, STATION_TEST_ACT)
                end)
                print("[StationTest] RemoveDynamicActivityFromSE done\n")
            end

            -- FinishSchedulingOverride
            pcall(function()
                se:FinishSchedulingOverride(4, provider, true, false, true)
            end)
            print("[StationTest] FinishSchedulingOverride done\n")

            -- Re-enable scheduling
            pcall(function()
                se:EnableScheduling(true, false, true)
            end)
            print("[StationTest] EnableScheduling done\n")

            _G.StationTestSE = nil
            _G.StationTestProvider = nil
            _G.StationTestStep = 3
            print("[StationTest] Released. Back to step 3. Press F7 to send again.\n")
            if ShowHint then ShowHint("Step 4 DONE: " .. STATION_TEST_NPC .. " released\nPress F7 to send again", 8) end
        end
    end)
end

-- ============================================================
-- Schedule Override Probe (F7 toggle) - Hardcoded to GladwinMoon
-- All references fetched fresh each time (safe across fast travel)
-- First press: override schedule to Three Broomsticks
-- Second press: undo override, restore normal schedule
-- ============================================================
_G._ScheduleOverrideState = _G._ScheduleOverrideState or nil

function DebugF7_ScheduleOverride()
    ExecuteInGameThread(function()
        local TAG = "[SchedProbe]"
        local ENTITY_NAME = "GladwinMoon"

        -- Helper: get fresh references every time (safe after fast travel)
        local function GetFreshRefs()
            local refs = {}
            local staticData = GetStaticCache()
            if not staticData then print(TAG .. " No static cache") return nil end
            refs.staticData = staticData

            refs.popManager = staticData.populationManager
            if not refs.popManager or not SafeIsValid(refs.popManager) then
                print(TAG .. " No PopulationManager")
                return nil
            end

            -- Get ScheduledEntity by name (works even when not in flesh)
            pcall(function() refs.se = refs.popManager:GetScheduledEntityFromName(ENTITY_NAME) end)
            if not refs.se then
                print(TAG .. " Could not get ScheduledEntity for " .. ENTITY_NAME)
                return nil
            end
            local seValid = false
            pcall(function() seValid = refs.se:IsValid() end)
            if not seValid then
                print(TAG .. " ScheduledEntity not valid")
                return nil
            end

            -- Provider: mod actor or player controller
            pcall(function() refs.provider = _G.SonorusState and _G.SonorusState.sonorusModActor end)
            if not refs.provider or not SafeIsValid(refs.provider) then
                refs.provider = staticData.playerController
            end

            -- WorldEventActor (fresh find)
            pcall(function() refs.weActor = FindFirstOf("WorldEventActor") end)

            return refs
        end

        -- Helper: print current schedule
        local function PrintSchedule(refs, label)
            local info = Utils.GetNPCScheduleInfo(ENTITY_NAME, refs.staticData)
            if info then
                print(string.format("%s %s: location=%s, activity=%s, type=%s, inFlesh=%s, inTransit=%s",
                    TAG, label, tostring(info.locationName), tostring(info.activity),
                    tostring(info.activityType), tostring(info.inFlesh), tostring(info.isInTransit)))
            else
                print(string.format("%s %s: GetNPCScheduleInfo returned nil", TAG, label))
            end

            -- Also read raw activity data
            pcall(function()
                local out, out2 = {}, {}
                refs.se:GetCurrentActivity(out, out2)
                local activity = out.Activity
                pcall(function() activity = out.Activity:ToString() end)
                local locKey = out.LocationKey
                pcall(function() locKey = out.LocationKey:ToString() end)
                print(string.format("%s %s (raw): activity=%s, locKey=%s, start=%s, end=%s",
                    TAG, label, tostring(activity), tostring(locKey),
                    tostring(out.StartTime), tostring(out.EndTime)))
            end)

            -- Check flesh/transit state
            pcall(function()
                print(string.format("%s %s: inFlesh=%s, isInTransit=%s, isEnabled=%s",
                    TAG, label, tostring(refs.se:CurrentlyInFlesh()),
                    tostring(refs.se:IsInTransit()), tostring(refs.se:IsEnabled())))
            end)
        end

        -- ============================================================
        -- RELEASE PATH
        -- ============================================================
        if _G._ScheduleOverrideState then
            print(string.format("%s === RELEASING %s ===", TAG, ENTITY_NAME))
            local refs = GetFreshRefs()
            if not refs then
                print(TAG .. " Can't get fresh refs - clearing state anyway")
                _G._ScheduleOverrideState = nil
                return
            end

            PrintSchedule(refs, "PRE-RELEASE")

            -- RemoveDynamicActivityFromSE (fresh WorldEventActor)
            if refs.weActor and SafeIsValid(refs.weActor) and _G._ScheduleOverrideState.injectedActivity then
                local ok, err = pcall(function()
                    local result = refs.weActor:RemoveDynamicActivityFromSE(refs.se, _G._ScheduleOverrideState.injectedActivity)
                    print(string.format("%s RemoveDynamicActivityFromSE: %s", TAG, tostring(result)))
                end)
                if not ok then print(string.format("%s RemoveDynamic FAILED: %s", TAG, tostring(err))) end
            else
                print(TAG .. " No WorldEventActor for RemoveDynamic (or no activity to remove)")
            end

            -- FinishSchedulingOverride (fresh provider)
            local ok2, err2 = pcall(function()
                local result = refs.se:FinishSchedulingOverride(4, refs.provider, true, false, true)
                print(string.format("%s FinishSchedulingOverride(priority=4): %s", TAG, tostring(result)))
            end)
            if not ok2 then print(string.format("%s FinishOverride FAILED: %s", TAG, tostring(err2))) end

            -- Re-enable scheduling
            pcall(function() refs.se:EnableScheduling(true, false, true) end)
            print(TAG .. " EnableScheduling(true) called")

            -- Print after release (with delay for scheduler to process)
            ExecuteInGameThreadWithDelay(1000, function()
                local refs2 = GetFreshRefs()
                if refs2 then PrintSchedule(refs2, "POST-RELEASE (1s)") end
            end)

            _G._ScheduleOverrideState = nil
            print(string.format("%s === RELEASE COMPLETE ===", TAG))
            return
        end

        -- ============================================================
        -- OVERRIDE PATH
        -- ============================================================
        print(string.format("%s === SCHEDULE OVERRIDE: %s -> Three Broomsticks ===", TAG, ENTITY_NAME))
        local refs = GetFreshRefs()
        if not refs then return end

        PrintSchedule(refs, "BEFORE")

        local state = { injectedActivity = nil }

        -- NOTE: FinishSchedulingOverride always returns true regardless of existing overrides.
        -- It's an action, not a query. Cannot be used to detect existing overrides.

        -- Step 1: StartSchedulingOverride
        local overrideResult = false
        local ok1, err1 = pcall(function()
            overrideResult = refs.se:StartSchedulingOverride(true, 4, refs.provider, true, true, true)
            print(string.format("%s StartSchedulingOverride(priority=4): %s", TAG, tostring(overrideResult)))
        end)
        if not ok1 then print(string.format("%s StartSchedulingOverride FAILED: %s", TAG, tostring(err1))) end

        -- Print current game time for time window testing
        pcall(function()
            local timeData = GetTimeOfDay and GetTimeOfDay()
            if timeData then
                print(string.format("%s Current game time: %02d:%02d (minute of day: %d)",
                    TAG, timeData.hour or 0, timeData.minute or 0, timeData.minuteOfDay or 0))
            end
        end)

        -- Step 2: InsertDynamicActivityOnSE
        -- Testing TWO activities: one all-day (0-2400) and one restricted (600-2130)
        if refs.weActor and SafeIsValid(refs.weActor) then
            -- Test A: Restricted window activity (600-2130)
            local okA, errA = pcall(function()
                local result = refs.weActor:InsertDynamicActivityOnSE(refs.se, "ThreeBroomsticksHours", "HM_ThreeBroomsticks")
                print(string.format("%s InsertDynamic('ThreeBroomsticksHours' [600-2130]): %s", TAG, tostring(result)))
                if result then state.injectedActivity = "ThreeBroomsticksHours" end
            end)
            if not okA then print(string.format("%s InsertDynamic(restricted) FAILED: %s", TAG, tostring(errA))) end

            -- Test B: All-day activity (0-2400) - only if restricted one failed
            if not state.injectedActivity then
                local okB, errB = pcall(function()
                    local result = refs.weActor:InsertDynamicActivityOnSE(refs.se, "HM_ThreeBroomsticksHours", "HM_ThreeBroomsticks")
                    print(string.format("%s InsertDynamic('HM_ThreeBroomsticksHours' [0-2400]) FALLBACK: %s", TAG, tostring(result)))
                    if result then state.injectedActivity = "HM_ThreeBroomsticksHours" end
                end)
                if not okB then print(string.format("%s InsertDynamic(allday) FAILED: %s", TAG, tostring(errB))) end
            end
        else
            print(TAG .. " No WorldEventActor available")
        end

        -- Print after override (with delay)
        ExecuteInGameThreadWithDelay(1000, function()
            local refs2 = GetFreshRefs()
            if refs2 then PrintSchedule(refs2, "AFTER (1s)") end
        end)

        _G._ScheduleOverrideState = state
        print(string.format("%s === OVERRIDE APPLIED (press F7 again to release) ===", TAG))
    end)
end

-- BACKUP: original DebugF7 (MeshTrace) moved to DebugF7_MeshTrace
function DebugF7_MeshTrace()
    ExecuteInGameThread(function()
        local TAG = "[DebugF7-MeshTrace]"

        local staticData = GetStaticCache()
        if not staticData then print(TAG .. " No static cache") return end

        local player = staticData.player
        local pc = staticData.playerController
        if not player or not SafeIsValid(player) then print(TAG .. " No player") return end
        if not pc then print(TAG .. " No PlayerController") return end

        local cam = pc.PlayerCameraManager
        if not cam then print(TAG .. " No CameraManager") return end

        local KismetSystem = staticData.kismetSystem
        local KismetMath = staticData.kismetMath
        if not KismetSystem or not KismetMath then print(TAG .. " No Kismet") return end

        -- Build trace start/end from camera (same as attention meter)
        local StartVector, EndVector
        local vrCam = _G.VRCamRot
        if vrCam then
            -- Use actual camera position (tracks HMD in VR), direction from VRCamRot
            StartVector = cam:GetCameraLocation()
            local sx, sy, sz = StartVector.X, StartVector.Y, StartVector.Z
            local pitch = math.rad(vrCam.Pitch)
            local yaw = math.rad(vrCam.Yaw)
            local fwd = {
                X = math.cos(pitch) * math.cos(yaw),
                Y = math.cos(pitch) * math.sin(yaw),
                Z = math.sin(pitch)
            }
            local traceDist = 5000.0
            EndVector = KismetMath:MakeVector(sx + fwd.X * traceDist, sy + fwd.Y * traceDist, sz + fwd.Z * traceDist)
        else
            StartVector = cam:GetCameraLocation()
            local camRot = cam:GetCameraRotation()
            local AddValue = KismetMath:Multiply_VectorInt(KismetMath:GetForwardVector(camRot), 5000.0)
            EndVector = KismetMath:Add_VectorVector(StartVector, AddValue)
        end

        local EDrawDebugTrace_None = 0
        local TraceColor = { R = 0, G = 0, B = 0, A = 0 }

        local HitResult = {}
        local WasHit = KismetSystem:LineTraceSingle(
            player, StartVector, EndVector,
            8,                            -- Channel 8: same as attention meter
            false,                        -- bTraceComplex
            { player },                   -- ActorsToIgnore
            EDrawDebugTrace_None,
            HitResult, true,
            TraceColor, TraceColor, 0.0
        )

        if not WasHit then
            print(TAG .. " No hit")
            if ShowHint then ShowHint("Trace: no hit", 3) end
            return
        end

        -- Extract everything we can from HitResult
        local lines = {}
        table.insert(lines, TAG .. " === HIT ===")

        -- Hit location
        pcall(function()
            local loc = HitResult.ImpactPoint
            if loc then
                table.insert(lines, string.format("  ImpactPoint: (%.0f, %.0f, %.0f)", loc.X, loc.Y, loc.Z))
            end
        end)

        -- Distance
        pcall(function()
            if HitResult.Distance then
                table.insert(lines, string.format("  Distance: %.0f (%.1fm)", HitResult.Distance, HitResult.Distance / 100))
            end
        end)

        -- Hit actor
        local hitActor = nil
        pcall(function()
            local a = HitResult.Actor
            if a then
                local obj = nil
                pcall(function() obj = a:Get() end)
                hitActor = obj or a
            end
        end)

        if hitActor then
            pcall(function()
                table.insert(lines, "  Actor: " .. hitActor:GetFullName())
            end)
            pcall(function()
                local cls = hitActor:GetClass()
                if cls then
                    table.insert(lines, "  ActorClass: " .. cls:GetFullName())
                end
            end)
        else
            table.insert(lines, "  Actor: nil")
        end

        -- Hit component (the mesh we actually hit)
        local hitComp = nil
        pcall(function()
            local c = HitResult.Component
            if c then
                local obj = nil
                pcall(function() obj = c:Get() end)
                hitComp = obj or c
            end
        end)

        if hitComp then
            pcall(function()
                table.insert(lines, "  Component: " .. hitComp:GetFullName())
            end)
            pcall(function()
                local cls = hitComp:GetClass()
                if cls then
                    table.insert(lines, "  CompClass: " .. cls:GetFullName())
                end
            end)
            -- Try to get the static mesh asset name
            pcall(function()
                if hitComp.StaticMesh then
                    local mesh = hitComp.StaticMesh
                    table.insert(lines, "  StaticMesh: " .. mesh:GetFullName())
                end
            end)
            pcall(function()
                if hitComp.SkeletalMesh then
                    local mesh = hitComp.SkeletalMesh
                    table.insert(lines, "  SkeletalMesh: " .. mesh:GetFullName())
                end
            end)
        else
            table.insert(lines, "  Component: nil")
        end

        -- Bone name if applicable
        pcall(function()
            if HitResult.BoneName then
                local boneName = nil
                pcall(function() boneName = HitResult.BoneName:ToString() end)
                if boneName and boneName ~= "None" then
                    table.insert(lines, "  BoneName: " .. boneName)
                end
            end
        end)

        -- PhysMaterial
        pcall(function()
            local pm = HitResult.PhysMaterial
            if pm then
                local obj = nil
                pcall(function() obj = pm:Get() end)
                local mat = obj or pm
                table.insert(lines, "  PhysMaterial: " .. mat:GetFullName())
            end
        end)

        -- Print everything to console
        for _, line in ipairs(lines) do
            print(line .. "\n")
        end

        -- ShowHint with condensed info
        if ShowHint then
            local hintLines = {}
            -- Show actor short name
            if hitActor then
                pcall(function()
                    local full = hitActor:GetFullName()
                    local short = full:match("([^%.]+)$") or full
                    table.insert(hintLines, "Actor: " .. short)
                end)
            end
            -- Show mesh short name
            if hitComp then
                pcall(function()
                    if hitComp.StaticMesh then
                        local full = hitComp.StaticMesh:GetFullName()
                        local short = full:match("([^%.]+)$") or full
                        table.insert(hintLines, "Mesh: " .. short)
                    elseif hitComp.SkeletalMesh then
                        local full = hitComp.SkeletalMesh:GetFullName()
                        local short = full:match("([^%.]+)$") or full
                        table.insert(hintLines, "Mesh: " .. short)
                    end
                end)
                pcall(function()
                    local full = hitComp:GetFullName()
                    local short = full:match("([^%.]+)$") or full
                    table.insert(hintLines, "Comp: " .. short)
                end)
            end
            pcall(function()
                if HitResult.Distance then
                    table.insert(hintLines, string.format("Dist: %.1fm", HitResult.Distance / 100))
                end
            end)
            ShowHint(table.concat(hintLines, "\n"), 10)
        end

        table.insert(lines, TAG .. " === END ===")
    end)
end

-- ============================================================
-- F7 Debug: Spline Lead Test
-- Tests pre-computed navmesh path + SplineFollowerForAI for NPC-leads-player.
-- NPC walks 50m in the direction the player is facing.
-- Press F7 to start, F7 again to stop.
-- ============================================================
_G._SplineLeadTest = _G._SplineLeadTest or {
    active = false,
    splineActor = nil,
    monitorHandle = nil,
    npcVoice = nil,
    waypointIdx = nil,
    waypoints = nil,
}

-- Kill stale loop from previous hot-reload
if _G._SplineLeadTest.monitorHandle then
    pcall(CancelDelayedAction, _G._SplineLeadTest.monitorHandle)
    _G._SplineLeadTest.monitorHandle = nil
    _G._SplineLeadTest.active = false
end

local function SplineLeadTest_Cleanup()
    local state = _G._SplineLeadTest
    local TAG = "[LeadTest]"
    -- Kill monitor
    if state.monitorHandle then
        pcall(CancelDelayedAction, state.monitorHandle)
        state.monitorHandle = nil
    end
    -- Release NPC BEFORE destroying the spline actor (SE may reference the PathComponent)
    if state.npcVoice then
        pcall(function()
            local staticData = GetStaticCache()
            if not staticData then return end
            local pm = staticData.populationManager
            if not pm or not SafeIsValid(pm) then return end
            local se = pm:GetScheduledEntityFromName(state.npcVoice)
            if se then
                -- Match follower system cleanup exactly
                se:PerformTask_RemoveActivePerformTask()
                se:EnableScheduling(true, false, true)
                ExecuteInGameThreadWithDelay(500, function()
                    pcall(function() se:EnableScheduling(true, false, true) end)
                end)
            end
        end)
    end
    -- Destroy spawned spline actor AFTER releasing the NPC
    if state.splineActor and SafeIsValid(state.splineActor) then
        pcall(function() state.splineActor:K2_DestroyActor() end)
        print(TAG .. " Destroyed spline actor")
    end
    -- Remove golden trail
    pcall(function()
        local mgr = FindFirstOf("BP_PathNavigationManager_C")
        if mgr and mgr:IsValid() then
            mgr:RemoveGuideSpline()
            mgr:ClearPathTarget()
        end
    end)
    -- Reset state
    state.active = false
    state.splineActor = nil
    state.monitorHandle = nil
    state.npcVoice = nil
    state.waypointIdx = nil
    state.waypoints = nil
end

function DebugF7_SplineLeadTest()
    local state = _G._SplineLeadTest
    local TAG = "[LeadTest]"

    -- == TOGGLE OFF ==
    if state.active then
        ExecuteInGameThread(function()
            SplineLeadTest_Cleanup()
            ShowHint("Lead test stopped", 3)
            print(TAG .. " STOPPED")
        end)
        return
    end

    -- == START ==
    ExecuteInGameThread(function()
        ShowHint("Lead test starting...", 2)

        local staticData = GetStaticCache()
        if not staticData then print(TAG .. " No static cache") return end
        local player = staticData.player
        if not player or not SafeIsValid(player) then print(TAG .. " No player") return end
        local popMgr = staticData.populationManager
        if not popMgr or not SafeIsValid(popMgr) then print(TAG .. " No PopMgr") return end

        -- ── Step 1: Player position + destination ──
        local pLoc = player:K2_GetActorLocation()
        local pRot = player:K2_GetActorRotation()
        local yaw = math.rad(pRot.Yaw)
        local LEAD_DIST = 5000  -- 50m ahead of player
        local dest = {
            X = pLoc.X + math.cos(yaw) * LEAD_DIST,
            Y = pLoc.Y + math.sin(yaw) * LEAD_DIST,
            Z = pLoc.Z,
        }
        print(string.format("%s [1] Dest: (%.0f,%.0f,%.0f) = %.0fm ahead of player",
            TAG, dest.X, dest.Y, dest.Z, LEAD_DIST / 100))

        -- ── Step 2: Find nearest named NPC with ScheduledEntity ──
        -- Skip committed NPCs (already managed by commitment system) and companion
        local bestNpc, bestSe, bestId, bestDist = nil, nil, nil, math.huge

        local allChars = nil
        pcall(function() allChars = FindAllOf("Character") end)
        if allChars then
            local playerFN = player:GetFullName()
            local compFN = ""
            pcall(function()
                local cp = staticData.companionManager:GetPrimaryCompanionPawn()
                if cp and SafeIsValid(cp) then compFN = cp:GetFullName() end
            end)
            for _, actor in pairs(allChars) do
                pcall(function()
                    if not actor:IsValid() then return end
                    local fn = actor:GetFullName()
                    if fn == playerFN or fn == compFN then return end
                    if fn:find("BP_Tier3_Character") then return end

                    local loc = actor:K2_GetActorLocation()
                    local d = math.sqrt((loc.X - pLoc.X)^2 + (loc.Y - pLoc.Y)^2)
                    if d >= bestDist or d > 2000 then return end

                    local se = popMgr:GetScheduledEntityFromActor(actor, false)
                    if not se then return end

                    -- Skip NPCs with active commitments (don't fight the commitment system)
                    local voiceName = nil
                    pcall(function()
                        local mn = se:GetMyName()
                        pcall(function() voiceName = mn:ToString() end)
                    end)
                    if voiceName and _G.ActiveCommitments and _G.ActiveCommitments[voiceName] then return end

                    bestDist, bestNpc, bestSe, bestId = d, actor, se, voiceName
                end)
            end
        end

        if not bestSe then
            print(TAG .. " [2] No NPC found nearby")
            ShowHint("No NPC with ScheduledEntity found nearby.\nStand near a named NPC and try again.", 5)
            return
        end

        local npcLoc = nil
        pcall(function() npcLoc = bestSe:GetLocation() end)
        if not npcLoc then
            print(TAG .. " [2] Could not get NPC location")
            return
        end

        print(string.format("%s [2] NPC: %s (dist=%.0fm)", TAG, tostring(bestId), bestDist / 100))
        state.npcVoice = bestId

        -- Break NPC from current station (don't disable scheduling — it kills movement)
        pcall(function() bestSe:AbandonStations(0) end)

        -- ── Step 3: Compute navmesh path ──
        local pathNavMgr = nil
        pcall(function() pathNavMgr = FindFirstOf("BP_PathNavigationManager_C") end)
        if not pathNavMgr or not SafeIsValid(pathNavMgr) then
            print(TAG .. " [3] No PathNavigationManager")
            return
        end

        -- Try FindPath (TArray<FVector> out param — may or may not work in UE4SS Lua)
        local pathPoints = nil
        local outPath = {}
        local outMissing = {}
        local fpResult = nil
        local fpOk, fpErr = pcall(function()
            fpResult = pathNavMgr:FindPath(npcLoc, dest, outPath, outMissing)
        end)
        print(string.format("%s [3] FindPath: pcall_ok=%s result=%s err=%s outPath_type=%s",
            TAG, tostring(fpOk), tostring(fpResult), tostring(fpErr), type(outPath)))

        -- Check if outPath got populated
        if fpOk then
            if type(outPath) == "table" and #outPath > 0 then
                pathPoints = outPath
                print(string.format("%s [3] FindPath returned %d waypoints (table)", TAG, #pathPoints))
            elseif type(outPath) == "userdata" then
                -- TArray userdata: try indexed access
                print(TAG .. " [3] outPath is userdata, probing...")
                local pts = {}
                pcall(function()
                    -- Try ForEach or indexed access
                    local len = 0
                    pcall(function() len = #outPath end)
                    print(TAG .. " [3] outPath # = " .. tostring(len))
                    if len and len > 0 then
                        for i = 1, math.min(len, 200) do
                            local pt = outPath[i]
                            if pt and pt.X then
                                table.insert(pts, { X = pt.X, Y = pt.Y, Z = pt.Z })
                            end
                        end
                    end
                end)
                if #pts > 0 then
                    pathPoints = pts
                    print(string.format("%s [3] Extracted %d points from userdata", TAG, #pts))
                end
            end
        end

        if not pathPoints or (type(pathPoints) == "table" and #pathPoints == 0) then
            -- Fallback: straight-line path from NPC to dest
            -- Not ideal (ignores navmesh) but lets us test the spline actor/movement
            print(TAG .. " [3] Path query failed — using straight-line fallback (10 segments)")
            pathPoints = {}
            local SEGS = 10
            for i = 0, SEGS do
                local t = i / SEGS
                table.insert(pathPoints, {
                    X = npcLoc.X + (dest.X - npcLoc.X) * t,
                    Y = npcLoc.Y + (dest.Y - npcLoc.Y) * t,
                    Z = npcLoc.Z + (dest.Z - npcLoc.Z) * t,
                })
            end
        end

        state.waypoints = pathPoints
        print(string.format("%s [3] Path: %d points", TAG, #pathPoints))

        -- ── Step 4: Golden trail for the player ──
        pcall(function()
            pathNavMgr:ClearWaypointPathTarget()
            pathNavMgr:RemoveGuideSpline()
            pathNavMgr:AddWaypointPathTarget(dest)
            pathNavMgr:GiveMeHelp()
        end)
        print(TAG .. " [4] Golden trail fired")

        -- ── Step 5: Try to spawn SimpleSplineFollowerForAI ──
        local splineActor = nil
        local spawnMethod = "none"

        pcall(function()
            local classPath = "/Script/Phoenix.SimpleSplineFollowerForAI"
            local splineClass = StaticFindObject(classPath)
            if not splineClass then
                print(TAG .. " [5] Class not found via StaticFindObject: " .. classPath)
                return
            end
            print(TAG .. " [5] Found class: " .. splineClass:GetFullName())

            local gps = StaticFindObject("/Script/Engine.Default__GameplayStatics")
            if not gps then
                print(TAG .. " [5] No GameplayStatics CDO")
                return
            end

            local spawnTransform = {
                Translation = { X = npcLoc.X, Y = npcLoc.Y, Z = npcLoc.Z },
                Rotation = { X = 0, Y = 0, Z = 0, W = 1 },
                Scale3D = { X = 1, Y = 1, Z = 1 },
            }

            local actor = nil
            local ok1, err1 = pcall(function()
                actor = gps:BeginDeferredActorSpawnFromClass(player, splineClass, spawnTransform, 2, player)
            end)
            if not ok1 or not actor then
                print(string.format("%s [5] BeginDeferred FAILED: ok=%s err=%s actor=%s",
                    TAG, tostring(ok1), tostring(err1), tostring(actor)))
                return
            end

            local ok2, err2 = pcall(function()
                gps:FinishSpawningActor(actor, spawnTransform)
            end)
            if not ok2 then
                print(TAG .. " [5] FinishSpawning FAILED: " .. tostring(err2))
            end

            print(TAG .. " [5] Spawned: " .. actor:GetFullName())
            splineActor = actor
            spawnMethod = "spawned"
        end)

        -- If spawn failed, try to find an existing one in the world
        if not splineActor then
            print(TAG .. " [5] Spawn failed, looking for existing SplineFollowerForAI...")
            pcall(function()
                splineActor = FindFirstOf("SplineFollowerForAI")
                if splineActor and SafeIsValid(splineActor) then
                    spawnMethod = "existing_full"
                    print(TAG .. " [5] Found existing: " .. splineActor:GetFullName())
                end
            end)
        end
        if not splineActor then
            pcall(function()
                splineActor = FindFirstOf("SimpleSplineFollowerForAI")
                if splineActor and SafeIsValid(splineActor) then
                    spawnMethod = "existing_simple"
                    print(TAG .. " [5] Found existing simple: " .. splineActor:GetFullName())
                end
            end)
        end

        -- ── Step 6: Configure spline points ──
        -- FVectors in UE4SS Lua are positional arrays: {X, Y, Z}
        -- AddSplineWorldPoint takes a single FVector param (safest signature)
        local splineReady = false
        if splineActor and SafeIsValid(splineActor) then
            state.splineActor = (spawnMethod == "spawned") and splineActor or nil

            local pathComp = nil
            pcall(function() pathComp = splineActor.PathComponent end)

            if pathComp and SafeIsValid(pathComp) then
                print(TAG .. " [6] PathComponent: " .. pathComp:GetFullName())

                -- Inspect first path point format from FindPath output
                local pt1 = pathPoints[1]
                if pt1 then
                    print(string.format("%s [6] Point[1] type=%s X=%s Y=%s Z=%s [1]=%s [2]=%s [3]=%s",
                        TAG, type(pt1), tostring(pt1.X), tostring(pt1.Y), tostring(pt1.Z),
                        tostring(pt1[1]), tostring(pt1[2]), tostring(pt1[3])))
                end

                -- Helper: ensure FVector is positional array format {x, y, z}
                local function toFVec(pt)
                    if pt[1] then
                        return { pt[1], pt[2], pt[3] }
                    else
                        return { pt.X, pt.Y, pt.Z }
                    end
                end

                -- Test single point first
                print(TAG .. " [6a] Calling AddSplineWorldPoint with first point...")
                pathComp:AddSplineWorldPoint(toFVec(pathPoints[1]))
                print(TAG .. " [6a] First point OK")

                -- Add remaining points
                for i = 2, #pathPoints do
                    pathComp:AddSplineWorldPoint(toFVec(pathPoints[i]))
                end
                print(string.format("%s [6b] Added all %d points", TAG, #pathPoints))

                print(TAG .. " [6c] Calling UpdateSpline...")
                pathComp:UpdateSpline()
                print(TAG .. " [6c] UpdateSpline OK")

                local numPts = 0
                pcall(function() numPts = pathComp:GetNumberOfSplinePoints() end)
                print(string.format("%s [6d] Spline reports %d points", TAG, numPts))

                if numPts > 0 then
                    splineReady = true
                end
            else
                print(TAG .. " [6] No PathComponent on actor")
            end

            -- ── Step 7: Drive movement via PerformTask_MoveToLocation with PathComponent ──
            -- StartAIMovementOnSpline alone doesn't drive movement.
            -- Instead, pass the pre-computed PathComponent to the proven PerformTask system.
            if splineReady then
                -- Build dest as positional FVector
                local destVec = { dest.X, dest.Y, dest.Z }

                -- Skip PathComponent for now (may corrupt SE state on cleanup)
                -- Just use destination-only move like the follower system
                print(TAG .. " [7] Calling PerformTask_MoveToLocation (no PathComp)...")
                local startOk, startErr = pcall(function()
                    bestSe:PerformTask_MoveToLocation(destVec, 150, 30, false, 200, nil)
                end)
                print(string.format("%s [7] PerformTask_MoveToLocation: ok=%s err=%s", TAG, tostring(startOk), tostring(startErr)))

                if startOk then
                    state.active = true
                    state.paused = false
                    -- Monitor loop: pause NPC when player falls behind, resume when close
                    local WAIT_DIST = 800    -- 8m: NPC pauses
                    local RESUME_DIST = 600  -- 6m: NPC resumes
                    state.monitorHandle = LoopInGameThreadWithDelay(1499, function()
                        pcall(function()
                            local s = _G._SplineLeadTest
                            if not s.active then
                                pcall(CancelDelayedAction, s.monitorHandle)
                                return
                            end
                            local sd = GetStaticCache()
                            if not sd then return end
                            local p = sd.player
                            if not p or not SafeIsValid(p) then return end
                            local pm = sd.populationManager
                            if not pm or not SafeIsValid(pm) then return end

                            local se = pm:GetScheduledEntityFromName(s.npcVoice)
                            if not se then return end

                            -- Keep scheduler from reclaiming NPC each tick (like followerTick does)
                            pcall(function() se:AbandonStations(0) end)

                            local loc = se:GetLocation()
                            if not loc then return end
                            local pl = p:K2_GetActorLocation()
                            local dDest = math.sqrt((loc.X - dest.X)^2 + (loc.Y - dest.Y)^2)
                            local dPlayer = math.sqrt((pl.X - loc.X)^2 + (pl.Y - loc.Y)^2)

                            -- Arrived?
                            if dDest < 300 then
                                print(TAG .. " NPC ARRIVED at destination!")
                                ShowHint("NPC arrived at destination!", 5)
                                SplineLeadTest_Cleanup()
                                return
                            end

                            -- Player too far: pause NPC
                            if dPlayer > WAIT_DIST and not s.paused then
                                pcall(function()
                                    se:PerformTask_RemoveActivePerformTask()
                                    local npc = se:GetFlesh()
                                    if npc and SafeIsValid(npc) then
                                        npc.CharacterMovement:StopMovementImmediately()
                                    end
                                end)
                                s.paused = true
                                print(string.format("%s [monitor] PAUSED — player %.0fm away", TAG, dPlayer / 100))
                            end

                            -- Player caught up: resume movement
                            if dPlayer < RESUME_DIST and s.paused then
                                pcall(function()
                                    local destVec = { dest.X, dest.Y, dest.Z }
                                    se:PerformTask_MoveToLocation(destVec, 150, 30, false, 200, nil)
                                end)
                                s.paused = false
                                print(string.format("%s [monitor] RESUMED — player %.0fm away", TAG, dPlayer / 100))
                            end

                            local statusStr = s.paused and "WAITING" or "WALKING"
                            ShowHint(string.format("LEAD MODE [%s]\n%s | ToDest=%.0fm | ToPlayer=%.0fm\n[F7 to stop]",
                                statusStr, tostring(s.npcVoice), dDest / 100, dPlayer / 100), 3)
                            print(string.format("%s [monitor] %s %s toDest=%.0f toPlayer=%.0f",
                                TAG, statusStr, tostring(s.npcVoice), dDest / 100, dPlayer / 100))
                        end)
                    end)

                    ShowHint(string.format("SPLINE lead test\n%s -> %.0fm ahead (%d pts)\n[F7 to stop]",
                        tostring(bestId), LEAD_DIST / 100, #pathPoints), 5)
                    print(TAG .. " === SPLINE MODE ACTIVE ===")
                    return  -- success, skip fallback
                end
            end
        else
            print(TAG .. " [5] No spline actor available")
        end

        -- ── FALLBACK: Waypoint chain via PerformTask_MoveToLocation ──
        print(TAG .. " [FALLBACK] Waypoint chain mode")
        state.waypointIdx = 1
        state.active = true

        -- Advance to next waypoint — called from monitor when NPC reaches current WP
        local function advanceWaypoint()
            local s = _G._SplineLeadTest
            if not s.active or not s.waypoints then return end
            local idx = s.waypointIdx
            if idx > #s.waypoints then
                print(TAG .. " All waypoints reached!")
                ShowHint("NPC arrived at destination!", 5)
                SplineLeadTest_Cleanup()
                return
            end
            local wp = s.waypoints[idx]
            -- Normalize: read X/Y/Z from either named keys or positional indices
            local wpX = wp.X or wp[1]
            local wpY = wp.Y or wp[2]
            local wpZ = wp.Z or wp[3]
            pcall(function()
                local sd = GetStaticCache()
                if not sd then return end
                local pm = sd.populationManager
                if not pm or not SafeIsValid(pm) then return end
                local se = pm:GetScheduledEntityFromName(s.npcVoice)
                if se then
                    se:PerformTask_MoveToLocation({ wpX, wpY, wpZ }, 150, 30, false, 200, nil)
                end
            end)
            print(string.format("%s WP %d/%d: (%.0f,%.0f,%.0f)", TAG, idx, #s.waypoints, wpX, wpY, wpZ))
        end

        advanceWaypoint()

        -- Monitor loop: check distance to current WP, pause if player far, advance when close
        state.monitorHandle = LoopInGameThreadWithDelay(1501, function()
            pcall(function()
                local s = _G._SplineLeadTest
                if not s.active then
                    pcall(CancelDelayedAction, s.monitorHandle)
                    return
                end

                local sd = GetStaticCache()
                if not sd then return end
                local p = sd.player
                if not p or not SafeIsValid(p) then return end
                local pm = sd.populationManager
                if not pm or not SafeIsValid(pm) then return end

                local se = pm:GetScheduledEntityFromName(s.npcVoice)
                if not se then return end
                local loc = se:GetLocation()
                if not loc then return end
                local pl = p:K2_GetActorLocation()

                local wp = s.waypoints[s.waypointIdx]
                if not wp then return end

                -- Normalize: handle both {X=,Y=,Z=} and {x,y,z} formats
                local wpX = wp.X or wp[1]
                local wpY = wp.Y or wp[2]
                local wpZ = wp.Z or wp[3]

                local dWp = math.sqrt((loc.X - wpX)^2 + (loc.Y - wpY)^2)
                local dPlayer = math.sqrt((pl.X - loc.X)^2 + (pl.Y - loc.Y)^2)

                -- Pause if player too far (> 15m)
                if dPlayer > 1500 then
                    pcall(function()
                        local npc = se:GetFlesh()
                        if npc and SafeIsValid(npc) then
                            npc.CharacterMovement:StopMovementImmediately()
                        end
                    end)
                    ShowHint(string.format("WAYPOINT MODE — Waiting for player\n%s | %.0fm away\n[F7 to stop]",
                        tostring(s.npcVoice), dPlayer / 100), 3)
                    return
                end

                ShowHint(string.format("WAYPOINT MODE\n%s WP %d/%d | ToWP=%.0fm | ToPlayer=%.0fm\n[F7 to stop]",
                    tostring(s.npcVoice), s.waypointIdx, #s.waypoints, dWp / 100, dPlayer / 100), 3)

                -- Advance when within 3m of current waypoint
                if dWp < 300 then
                    s.waypointIdx = s.waypointIdx + 1
                    advanceWaypoint()
                elseif dWp > 300 then
                    -- Re-issue move (NPC may have paused)
                    pcall(function()
                        se:PerformTask_MoveToLocation({ wpX, wpY, wpZ }, 150, 30, false, 200, nil)
                    end)
                end
            end)
        end)

        ShowHint(string.format("WAYPOINT lead test\n%s -> %.0fm ahead (%d WPs)\n[F7 to stop]",
            tostring(bestId), LEAD_DIST / 100, #pathPoints), 5)
        print(TAG .. " === WAYPOINT MODE ACTIVE ===")
    end)
end

_G._DebugDugbog = _G._DebugDugbog or nil
function DebugF7_DugbogSpawn()
    ExecuteInGameThread(function()
        local TAG = "[DugbogSpawn]"

        -- Toggle: second press destroys
        if _G._DebugDugbog and SafeIsValid(_G._DebugDugbog) then
            print(TAG .. " === DESTROYING DUGBOG ===")
            pcall(function() _G._DebugDugbog:K2_DestroyActor() end)
            _G._DebugDugbog = nil
            print(TAG .. " Destroyed")
            return
        end
        _G._DebugDugbog = nil

        print(TAG .. " === SPAWN DUGBOG (NON-HOSTILE) ===")

        local staticData = GetStaticCache()
        if not staticData then print(TAG .. " No static cache") return end

        local player = staticData.player
        if not player or not SafeIsValid(player) then print(TAG .. " No player") return end

        local playerLoc, playerRot = nil, nil
        pcall(function()
            playerLoc = player:K2_GetActorLocation()
            playerRot = player:K2_GetActorRotation()
        end)
        if not playerLoc or not playerRot then print(TAG .. " No player transform") return end

        -- Spawn ~500 units in front of player (~5m), ~150 units up (~5ft)
        local yawRad = math.rad(playerRot.Yaw)
        local spawnX = playerLoc.X + math.cos(yawRad) * 500
        local spawnY = playerLoc.Y + math.sin(yawRad) * 500
        local spawnZ = playerLoc.Z + 150

        print(string.format("%s Player at (%.0f, %.0f, %.0f) yaw=%.1f", TAG, playerLoc.X, playerLoc.Y, playerLoc.Z, playerRot.Yaw))
        print(string.format("%s Spawn target: (%.0f, %.0f, %.0f)", TAG, spawnX, spawnY, spawnZ))

        -- Find dugbog Blueprint class
        local classPath = "/Game/Pawn/NPC/Enemy/Character/Dugbog/BP_Dugbog.BP_Dugbog_C"
        local dugbogClass = StaticFindObject(classPath)
        if not dugbogClass then
            print(TAG .. " Class not in memory, trying LoadAsset...")
            pcall(function()
                LoadAsset("/Game/Pawn/NPC/Enemy/Character/Dugbog/BP_Dugbog")
            end)
            dugbogClass = StaticFindObject(classPath)
        end
        if not dugbogClass then
            print(TAG .. " FAILED: Could not find or load BP_Dugbog class")
            return
        end
        print(TAG .. " Class: " .. dugbogClass:GetFullName())

        -- GameplayStatics for spawning
        local gps = StaticFindObject("/Script/Engine.Default__GameplayStatics")
        if not gps then
            print(TAG .. " FAILED: No GameplayStatics")
            return
        end

        local spawnTransform = {
            Translation = { X = spawnX, Y = spawnY, Z = spawnZ },
            Rotation = { X = 0, Y = 0, Z = 0, W = 1 },
            Scale3D = { X = 1, Y = 1, Z = 1 }
        }

        -- BeginDeferredActorSpawnFromClass (2 = AdjustIfPossibleButAlwaysSpawn)
        local dugbog = nil
        local ok, err = pcall(function()
            dugbog = gps:BeginDeferredActorSpawnFromClass(player, dugbogClass, spawnTransform, 2, player)
        end)
        if not ok then
            print(TAG .. " BeginDeferredActorSpawnFromClass FAILED: " .. tostring(err))
            return
        end
        if not dugbog then
            print(TAG .. " BeginDeferredActorSpawnFromClass returned nil")
            return
        end

        local ok2, err2 = pcall(function()
            gps:FinishSpawningActor(dugbog, spawnTransform)
        end)
        if not ok2 then
            print(TAG .. " FinishSpawningActor FAILED: " .. tostring(err2))
        end

        print(TAG .. " Dugbog spawned: " .. tostring(dugbog:GetFullName()))
        _G._DebugDugbog = dugbog

        -- === MAKE NON-HOSTILE (delay 200ms for AI components to initialize) ===
        ExecuteInGameThreadWithDelay(200, function()
            if not dugbog or not SafeIsValid(dugbog) then
                print(TAG .. " Dugbog gone before pacify")
                return
            end

            print(TAG .. " --- Pacifying dugbog ---")

            -- EnemyAIComponent: disable attacks + wander mode
            pcall(function()
                local aiClass = StaticFindObject("/Script/Phoenix.EnemyAIComponent")
                if aiClass then
                    local aiComp = dugbog:GetComponentByClass(aiClass)
                    if aiComp then
                        aiComp:SetCanAttack(false)
                        print(TAG .. " EnemyAIComponent:SetCanAttack(false): OK")
                        aiComp:ForceDisengagedState()
                        print(TAG .. " EnemyAIComponent:ForceDisengagedState(): OK")
                        aiComp:SetWanderMode()
                        print(TAG .. " EnemyAIComponent:SetWanderMode(): OK")
                    else
                        print(TAG .. " No EnemyAIComponent on actor")
                    end
                end
            end)

            -- CharacterStateInfo: non-targetable
            pcall(function()
                local csi = dugbog:GetCharacterStateInfo()
                if csi then
                    csi:SetAttackable(false)
                    print(TAG .. " CharacterStateInfo:SetAttackable(false): OK")
                else
                    print(TAG .. " No CharacterStateInfo")
                end
            end)

            print(TAG .. " --- Pacify complete ---")
        end)

        print(TAG .. " Press F7 again to destroy")
    end)
end

-- ============================================================
-- F7 Debug: Portrait Swap
-- Removes Ferdinand from his portrait station, spawns Fig in his place
-- Press F7 again to restore
-- ============================================================
_G._DebugPortraitSwap = _G._DebugPortraitSwap or nil

function DebugF7_PortraitSwap()
    ExecuteInGameThread(function()
        local TAG = "[PortraitSwap]"
        local VICTIM = "FerdinandOctaviusPratt"
        local REPLACEMENT = "AbrahamRonen"

        local staticData = GetStaticCache()
        if not staticData then print(TAG .. " No static cache") return end

        local popManager = staticData.populationManager
        if not popManager or not SafeIsValid(popManager) then
            print(TAG .. " No PopulationManager")
            return
        end

        -- RESTORE PATH
        if _G._DebugPortraitSwap then
            print(TAG .. " === RESTORING ===")
            local state = _G._DebugPortraitSwap

            -- Move Ferdinand back
            if state.victimActor and SafeIsValid(state.victimActor) and state.victimLoc then
                pcall(function()
                    state.victimActor:K2_SetActorLocation(state.victimLoc, false, {}, false)
                end)
                print(TAG .. " Moved " .. VICTIM .. " back")
            end

            -- Re-enable Ferdinand's scheduling
            if state.victimSE then
                pcall(function() state.victimSE:EnableScheduling(true, false, true) end)
                print(TAG .. " Re-enabled " .. VICTIM .. " scheduling")
            end

            -- Move Fig away / end precache / re-enable scheduling
            if state.replacementSE then
                local provider = _G.SonorusState and _G.SonorusState.sonorusModActor or staticData.playerController
                pcall(function() state.replacementSE:EndPrecachingFlesh(3, provider, true, 0, 0) end)
                pcall(function() state.replacementSE:EnableScheduling(true, false, true) end)
                print(TAG .. " Re-enabled " .. REPLACEMENT .. " scheduling")
            end
            if state.replacementActor and SafeIsValid(state.replacementActor) and state.replacementOrigLoc then
                pcall(function()
                    state.replacementActor:K2_SetActorLocation(state.replacementOrigLoc, false, {}, false)
                end)
            end

            _G._DebugPortraitSwap = nil
            print(TAG .. " === RESTORE COMPLETE ===")
            return
        end

        -- SWAP PATH
        print(TAG .. " === SWAPPING " .. VICTIM .. " -> " .. REPLACEMENT .. " ===")

        local state = {}

        -- Step 1: Get Ferdinand's scheduled entity and flesh
        local victimSE = nil
        pcall(function() victimSE = popManager:GetScheduledEntityFromName(VICTIM) end)
        if not victimSE then
            print(TAG .. " Could not find SE for " .. VICTIM)
            return
        end
        state.victimSE = victimSE

        local victimFlesh = nil
        pcall(function() victimFlesh = victimSE:GetFlesh() end)
        if not victimFlesh or not SafeIsValid(victimFlesh) then
            print(TAG .. " " .. VICTIM .. " not in flesh (not loaded)")
            return
        end
        state.victimActor = victimFlesh

        -- Save Ferdinand's position
        local victimLoc = nil
        pcall(function() victimLoc = victimFlesh:K2_GetActorLocation() end)
        if not victimLoc then
            print(TAG .. " Could not get " .. VICTIM .. " location")
            return
        end
        state.victimLoc = { X = victimLoc.X, Y = victimLoc.Y, Z = victimLoc.Z }
        local victimRot = nil
        pcall(function() victimRot = victimFlesh:K2_GetActorRotation() end)
        state.victimRot = victimRot

        print(string.format("%s %s at (%.0f, %.0f, %.0f)", TAG, VICTIM, victimLoc.X, victimLoc.Y, victimLoc.Z))

        -- Step 2: Find Ferdinand's PortraitPaintingActor
        local portraitActor = nil
        pcall(function()
            local allPortraits = FindAllOf("PortraitPaintingActor")
            if allPortraits then
                for _, pa in pairs(allPortraits) do
                    local eName = nil
                    pcall(function() eName = pa.EntityName:ToString() end)
                    print(string.format("%s Found portrait: EntityName=%s", TAG, tostring(eName)))
                    if eName and eName:find("Pratt") then
                        portraitActor = pa
                    end
                end
            else
                print(TAG .. " FindAllOf('PortraitPaintingActor') returned nil")
            end
        end)
        if not portraitActor then
            print(TAG .. " Could not find Ferdinand's PortraitPaintingActor")
            return
        end
        state.portraitActor = portraitActor
        print(TAG .. " Portrait: " .. portraitActor:GetFullName())

        -- Dump portrait properties for research
        pcall(function() print(TAG .. " bReadyForEntities: " .. tostring(portraitActor.bReadyForEntities)) end)
        pcall(function() print(TAG .. " CameraFarPlane: " .. tostring(portraitActor.CameraFarPlane)) end)
        pcall(function() print(TAG .. " PortraitActorRange: " .. tostring(portraitActor.PortraitActorRange)) end)

        -- Step 3: Get replacement NPC and ensure in flesh
        local replSE = nil
        pcall(function() replSE = popManager:GetScheduledEntityFromName(REPLACEMENT) end)
        if not replSE then
            print(TAG .. " Could not find SE for " .. REPLACEMENT)
            return
        end
        state.replacementSE = replSE

        local replFlesh = nil
        pcall(function()
            if replSE:CurrentlyInFlesh() then replFlesh = replSE:GetFlesh() end
        end)
        if not replFlesh or not SafeIsValid(replFlesh) then
            print(TAG .. " Spawning " .. REPLACEMENT .. "...")
            pcall(function()
                popManager:PlaceScheduledEntityBP(REPLACEMENT, {
                    Translation = state.victimLoc,
                    Rotation = { X = 0, Y = 0, Z = 0, W = 1 },
                    Scale3D = { X = 1, Y = 1, Z = 1 }
                })
            end)
        end

        -- Function to hook replacement into portrait
        local function hookIntoPortrait()
            local flesh = nil
            pcall(function() flesh = replSE:GetFlesh() end)
            if not flesh or not SafeIsValid(flesh) then return false end
            state.replacementActor = flesh

            -- Save original loc for restore
            pcall(function()
                local loc = flesh:K2_GetActorLocation()
                if not state.replacementOrigLoc then
                    state.replacementOrigLoc = { X = loc.X, Y = loc.Y, Z = loc.Z }
                end
            end)

            -- Freeze NPC
            pcall(function() replSE:AbandonStations(0) end)
            pcall(function() replSE:EnableScheduling(false, true, true) end)

            -- Move Ferdinand underground
            pcall(function() victimSE:AbandonStations(0) end)
            pcall(function() victimSE:EnableScheduling(false, true, true) end)
            pcall(function()
                victimFlesh:K2_SetActorLocation(
                    { X = victimLoc.X, Y = victimLoc.Y, Z = victimLoc.Z - 5000 },
                    false, {}, false)
            end)

            -- Move replacement to Ferdinand's exact position
            pcall(function() flesh:K2_SetActorLocation(state.victimLoc, false, {}, false) end)
            if state.victimRot then
                pcall(function() flesh:K2_SetActorRotation(state.victimRot, false) end)
            end

            -- Try to hook into portrait rendering system
            local ok1, err1 = pcall(function()
                portraitActor:OnFleshLoaded(flesh, replSE)
            end)
            print(string.format("%s OnFleshLoaded: ok=%s err=%s", TAG, tostring(ok1), tostring(err1)))

            local ok2, err2 = pcall(function()
                portraitActor:OnCharacterLoadComplete(flesh)
            end)
            print(string.format("%s OnCharacterLoadComplete: ok=%s err=%s", TAG, tostring(ok2), tostring(err2)))

            print(string.format("%s %s hooked into portrait!", TAG, REPLACEMENT))
            return true
        end

        if not hookIntoPortrait() then
            local attempts = 0
            local checkHandle
            checkHandle = LoopInGameThreadWithDelay(503, function()
                attempts = attempts + 1
                if hookIntoPortrait() or attempts >= 20 then
                    CancelDelayedAction(checkHandle)
                    if attempts >= 20 then
                        print(TAG .. " Failed after 20 attempts")
                    end
                end
            end)
        end

        _G._DebugPortraitSwap = state
        print(TAG .. " === SWAP COMPLETE === Press F7 to restore")
    end)
end

-- Persistent state for TurnInPlace test toggle
_G._TurnInPlaceState = _G._TurnInPlaceState or nil
function DebugF7_TurnInPlace()
    ExecuteInGameThread(function()
        local TAG = "[TurnInPlace]"

        -- ============================================================
        -- RELEASE PATH (second press)
        -- ============================================================
        if _G._TurnInPlaceState then
            print(TAG .. " === RELEASING ===")
            local st = _G._TurnInPlaceState
            local npcId = st.npcName

            local staticData = GetStaticCache()
            if not staticData then
                print(TAG .. " No static cache - clearing state")
                _G._TurnInPlaceState = nil
                return
            end

            local popManager = staticData.populationManager
            if popManager and SafeIsValid(popManager) then
                local se = nil
                pcall(function() se = popManager:GetScheduledEntityFromName(npcId) end)
                if se then
                    -- Clear AI focus
                    local flesh = nil
                    pcall(function() flesh = se:GetFlesh() end)
                    if flesh and SafeIsValid(flesh) then
                        local ctrl = nil
                        pcall(function() ctrl = flesh.Controller end)
                        if ctrl and SafeIsValid(ctrl) then
                            pcall(function() ctrl:K2_ClearFocus() end)
                            print(TAG .. " K2_ClearFocus OK")
                        end
                    end

                    -- Re-enable scheduling (was disabled to freeze NPC)
                    pcall(function() se:EnableScheduling(true, false, true) end)
                    print(TAG .. " Scheduling re-enabled")
                end
            end

            _G._TurnInPlaceState = nil
            print(TAG .. " === RELEASED (press F7 again to test) ===")
            return
        end

        -- ============================================================
        -- APPLY PATH (first press)
        -- ============================================================
        print(TAG .. " === NPC SetTargetLocationTurnInPlace TEST ===")

        local staticData = GetStaticCache()
        if not staticData then print(TAG .. " No static cache") return end

        local player = staticData.player
        if not player or not SafeIsValid(player) then print(TAG .. " No player") return end

        local playerLoc = nil
        pcall(function() playerLoc = player:K2_GetActorLocation() end)
        if not playerLoc then print(TAG .. " No player location") return end
        print(string.format("%s Player at (%.0f, %.0f, %.0f)", TAG, playerLoc.X, playerLoc.Y, playerLoc.Z))

        -- Find nearest NPC
        local nearestNpc = nil
        local nearestDist = math.huge
        local nearestName = nil
        local playerFullName = staticData.playerFullName or ""

        local npcResult = nil
        pcall(function() npcResult = GetNearbyNPCs(2000, 0.9) end)

        if not npcResult or not npcResult.nearbyList or #npcResult.nearbyList == 0 then
            print(TAG .. " No nearby NPCs found")
            return
        end

        for _, entry in ipairs(npcResult.nearbyList) do
            if entry.actor and SafeIsValid(entry.actor) then
                local entryFullName = entry.actor:GetFullName()
                if entryFullName ~= playerFullName then
                    local npcLoc = entry.actor:K2_GetActorLocation()
                    local dx = npcLoc.X - playerLoc.X
                    local dy = npcLoc.Y - playerLoc.Y
                    local dist = math.sqrt(dx * dx + dy * dy)
                    if dist < nearestDist then
                        nearestDist = dist
                        nearestNpc = entry.actor
                        nearestName = entry.name or "?"
                    end
                end
            end
        end

        if not nearestNpc then
            print(TAG .. " No valid NPC found")
            return
        end

        local npcLoc = nearestNpc:K2_GetActorLocation()
        local npcRot = nearestNpc:K2_GetActorRotation()
        print(string.format("%s Nearest NPC: %s (dist=%.0f, yaw=%.1f)", TAG, nearestName, nearestDist, npcRot.Yaw or 0))

        -- Get ScheduledEntity + PopulationManager
        local popManager = staticData.populationManager
        if not popManager or not SafeIsValid(popManager) then
            print(TAG .. " No PopulationManager")
            return
        end

        local se = nil
        pcall(function() se = popManager:GetScheduledEntityFromActor(nearestNpc, false) end)
        if not se then
            print(TAG .. " No ScheduledEntity for this NPC")
            return
        end

        -- NPCLock-style: brief enable window for animated turn, then freeze

        -- Step 1: Break from station
        pcall(function() se:AbandonStations(0) end)
        print(TAG .. " AbandonStations(0)")

        -- Step 2: Enable scheduling (allows turn animation to play)
        pcall(function() se:EnableScheduling(true, false, true) end)
        print(TAG .. " EnableScheduling(true)")

        -- Step 3: Get NPC_Component and call SetTargetLocationTurnInPlace
        local npcComp = nil
        pcall(function()
            local npcCompClass = staticData.npcComponentClass
            if npcCompClass then
                npcComp = nearestNpc:GetComponentByClass(npcCompClass)
            end
        end)

        if not npcComp then
            print(TAG .. " Could not get NPC_Component")
            return
        end

        local ok2, err2 = pcall(function()
            npcComp:SetTargetLocationTurnInPlace(playerLoc)
        end)
        print(TAG .. " SetTargetLocationTurnInPlace: " .. (ok2 and "OK" or tostring(err2)))

        -- Step 4: After delay, freeze NPC before scheduler reassigns a station
        ExecuteInGameThreadWithDelay(700, function()
            pcall(function()
                if SafeIsValid(nearestNpc) then
                    se:EnableScheduling(false, true, true)
                    print(TAG .. " EnableScheduling(false) - frozen after 700ms")
                end
            end)
        end)

        -- Save state for release toggle
        _G._TurnInPlaceState = {
            npcName = nearestName,
            activityId = nil,
            startYaw = npcRot.Yaw or 0,
        }

        -- Check rotation after delays
        ExecuteInGameThreadWithDelay(1500, function()
            pcall(function()
                if SafeIsValid(nearestNpc) then
                    local newRot = nearestNpc:K2_GetActorRotation()
                    print(string.format("%s After 1.5s: yaw=%.1f (was %.1f, delta=%.1f)",
                        TAG, newRot.Yaw or 0, npcRot.Yaw or 0, (newRot.Yaw or 0) - (npcRot.Yaw or 0)))
                end
            end)
        end)

        ExecuteInGameThreadWithDelay(3000, function()
            pcall(function()
                if SafeIsValid(nearestNpc) then
                    local newRot = nearestNpc:K2_GetActorRotation()
                    print(string.format("%s After 3s: yaw=%.1f (was %.1f, delta=%.1f)",
                        TAG, newRot.Yaw or 0, npcRot.Yaw or 0, (newRot.Yaw or 0) - (npcRot.Yaw or 0)))
                end
            end)
        end)

        print(TAG .. " === TEST FIRED (press F7 again to release) ===")
    end)
end

function DebugF7_ScheduleExplorer()
    ExecuteInGameThread(function()
        print("[DebugF7] === SCHEDULE EXPLORER ===")
        local staticData = GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end

        local popManager = staticData.populationManager
        if not popManager or not SafeIsValid(popManager) then
            print("[DebugF7] No PopulationManager")
            return
        end

        -- Test subjects - mix of nearby and distant NPCs
        local testNames = {
            "PhineasBlack",
            "SebastianSallow",
            "NatasaiOnai",
            "Natsai",
            "NatsaiOnai",
            "PoppySweeting",
            "AbrahamRonen",
            "MatildaWeasley",
        }

        for _, entityName in ipairs(testNames) do
            print(string.format("\n[DebugF7] --- %s ---", entityName))
            local se = nil
            local ok, err = pcall(function()
                se = popManager:GetScheduledEntityFromName(entityName)
            end)
            if not ok then
                print(string.format("[DebugF7]   GetSEFromName FAILED: %s", tostring(err)))
                goto nextEntity
            end
            if not se then
                print("[DebugF7]   ScheduledEntity = nil (not found)")
                goto nextEntity
            end

            -- Check if valid
            local seValid = false
            pcall(function() seValid = se:IsValid() end)
            print(string.format("[DebugF7]   SE valid: %s", tostring(seValid)))
            if not seValid then goto nextEntity end

            -- Identity
            pcall(function()
                local myName = se:GetMyName()
                local nameStr = nil
                pcall(function() nameStr = myName:ToString() end)
                print(string.format("[DebugF7]   GetMyName: %s", tostring(nameStr or myName)))
            end)

            pcall(function()
                local myId = se:GetMyID()
                print(string.format("[DebugF7]   GetMyID: %s", tostring(myId)))
            end)

            -- Is in flesh (streamed in)?
            pcall(function()
                local inFlesh = se:CurrentlyInFlesh()
                print(string.format("[DebugF7]   CurrentlyInFlesh: %s", tostring(inFlesh)))
            end)

            -- Get flesh actor if loaded
            pcall(function()
                local flesh = se:GetFlesh()
                if flesh and SafeIsValid(flesh) then
                    print(string.format("[DebugF7]   Flesh: %s", flesh:GetFullName()))
                else
                    print("[DebugF7]   Flesh: nil/invalid (not streamed in)")
                end
            end)

            -- Location (works even without flesh)
            pcall(function()
                local loc = se:GetLocation()
                if loc then
                    print(string.format("[DebugF7]   Location: (%.0f, %.0f, %.0f)", loc.X or 0, loc.Y or 0, loc.Z or 0))
                else
                    print("[DebugF7]   Location: nil")
                end
            end)

            -- Enabled / state
            pcall(function() print(string.format("[DebugF7]   IsEnabled: %s", tostring(se:IsEnabled()))) end)
            pcall(function() print(string.format("[DebugF7]   IsStudent: %s", tostring(se:IsStudent()))) end)
            pcall(function() print(string.format("[DebugF7]   IsGhost: %s", tostring(se:IsGhost()))) end)
            pcall(function() print(string.format("[DebugF7]   IsHobo: %s", tostring(se:IsHobo()))) end)
            pcall(function() print(string.format("[DebugF7]   IsInTransit: %s", tostring(se:IsInTransit()))) end)

            -- Current activity
            pcall(function()
                local out = {}
                local out2 = {}
                se:GetCurrentActivity(out, out2)
                if out.ActivityIsValid then
                    local activity = nil
                    pcall(function() activity = out.Activity:ToString() end)
                    local actType = nil
                    pcall(function() actType = out.ActivityType:ToString() end)
                    local location = nil
                    pcall(function() location = out.Location:ToString() end)
                    local locKey = nil
                    pcall(function() locKey = out.LocationKey:ToString() end)
                    local stationKey = nil
                    pcall(function() stationKey = out.StationKey:ToString() end)
                    print(string.format("[DebugF7]   CurrentActivity: %s", tostring(activity)))
                    print(string.format("[DebugF7]     Type: %s", tostring(actType)))
                    print(string.format("[DebugF7]     Location: %s", tostring(location)))
                    print(string.format("[DebugF7]     LocationKey: %s", tostring(locKey)))
                    print(string.format("[DebugF7]     StationKey: %s", tostring(stationKey)))
                    print(string.format("[DebugF7]     Time: %s-%s (dur %s min)",
                        tostring(out.StartTime), tostring(out.EndTime), tostring(out.DurationMinutes)))
                    print(string.format("[DebugF7]     DaysMask: %s  Priority: %s", tostring(out.DaysMask), tostring(out.Priority)))
                else
                    print("[DebugF7]   CurrentActivity: NONE (ActivityIsValid=false)")
                end
            end)

            -- Upcoming activity
            pcall(function()
                local out = {}
                local out2 = {}
                se:GetUpcomingActivity(out, out2)
                if out.ActivityIsValid then
                    local activity = nil
                    pcall(function() activity = out.Activity:ToString() end)
                    local location = nil
                    pcall(function() location = out.Location:ToString() end)
                    print(string.format("[DebugF7]   UpcomingActivity: %s @ %s", tostring(activity), tostring(location)))
                    print(string.format("[DebugF7]     Time: %s-%s", tostring(out.StartTime), tostring(out.EndTime)))
                else
                    print("[DebugF7]   UpcomingActivity: NONE")
                end
            end)

            -- Minutes to upcoming
            pcall(function()
                local out = {}
                local out2 = {}
                se:GetMinutesToUpcomingActivity(out, out2)
                if out.ActivityIsValid ~= nil then
                    print(string.format("[DebugF7]   MinutesToUpcoming: %s (valid=%s)",
                        tostring(out.MinutesToUpcomingActivity), tostring(out.ActivityIsValid)))
                end
            end)

            ::nextEntity::
        end

        print("\n[DebugF7] === SCHEDULE EXPLORER DONE ===")
    end)
end

-- F7 Debug Function (Original) - Nearby Station Scanner
function DebugF7_StationScanner()
    ExecuteInGameThread(function()

        print("[DebugF7] === STATION SCANNER ===")

        local staticData = GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end

        local player = staticData.player
        if not player then print("[DebugF7] No player") return end

        local playerLoc = nil
        pcall(function() playerLoc = player:K2_GetActorLocation() end)
        if not playerLoc then print("[DebugF7] No player location") return end

        local KismetSystem = staticData.kismetSystem
        local KismetMath = staticData.kismetMath

        -- Find all stations
        local allStations = FindAllOf("Station")
        if not allStations then
            ShowHint("No stations found", 3)
            return
        end

        local scanRadius = 1500 -- ~15 meters in UE units
        local nearbyStations = {}

        for _, station in pairs(allStations) do
            pcall(function()
                local stationLoc = station:K2_GetActorLocation()
                local dx = stationLoc.X - playerLoc.X
                local dy = stationLoc.Y - playerLoc.Y
                local dz = stationLoc.Z - playerLoc.Z
                local dist = math.sqrt(dx * dx + dy * dy + dz * dz)

                if dist <= scanRadius then
                    local stationComp = nil
                    pcall(function() stationComp = station:GetStationComponent() end)
                    if not stationComp then return end

                    local active = false
                    pcall(function() active = stationComp:IsStationActive() end)
                    if not active then return end

                    local numConns = 0
                    pcall(function() numConns = stationComp:GetNumConnections() end)

                    local isChair = false
                    pcall(function() isChair = stationComp:IsAChair() end)

                    local isBed = false
                    pcall(function() isBed = stationComp:IsABed() end)

                    local propType = -1
                    pcall(function() propType = stationComp:GetPropType() end)

                    local meshName = "?"
                    pcall(function()
                        local mn = stationComp:GetMeshName()
                        if mn then pcall(function() meshName = mn:ToString() end) end
                    end)

                    local stationName = "?"
                    pcall(function() stationName = station:GetFullName():match("([^%.]+)$") end)

                    local stationClass = "?"
                    pcall(function() stationClass = station:GetClass():GetFullName():match("([^%.]+)$") end)

                    -- Check occupancy
                    local numUsers = 0
                    pcall(function()
                        local users = {}
                        stationComp:GetStationUsers(users)
                        for _ in pairs(users) do numUsers = numUsers + 1 end
                    end)

                    table.insert(nearbyStations, {
                        station = station,
                        stationComp = stationComp,
                        name = stationName,
                        class = stationClass,
                        loc = stationLoc,
                        dist = dist,
                        conns = numConns,
                        users = numUsers,
                        isChair = isChair,
                        isBed = isBed,
                        propType = propType,
                        mesh = meshName,
                    })
                end
            end)
        end

        -- Sort by distance
        table.sort(nearbyStations, function(a, b) return a.dist < b.dist end)

        -- Line trace visibility check for each station
        if KismetSystem and KismetMath then
            local playerHalfHeight = 88
            pcall(function()
                local capsule = player.CapsuleComponent
                if capsule and capsule.CapsuleHalfHeight then
                    playerHalfHeight = capsule.CapsuleHalfHeight
                end
            end)

            local traceStart = nil
            pcall(function()
                traceStart = KismetMath:MakeVector(playerLoc.X, playerLoc.Y, playerLoc.Z + playerHalfHeight * 2 + 20)
            end)

            if traceStart then
                local ETraceTypeQuery_Visibility = 0
                local EDrawDebugTrace_None = 0
                local TraceColor = { R = 0, G = 0, B = 0, A = 0 }
                local ActorsToIgnore = { player }

                for _, s in ipairs(nearbyStations) do
                    s.visible = false
                    pcall(function()
                        local EndVector = KismetMath:MakeVector(s.loc.X, s.loc.Y, s.loc.Z + 50)
                        local HitResult = {}
                        local WasHit = KismetSystem:LineTraceSingle(
                            player, traceStart, EndVector,
                            ETraceTypeQuery_Visibility, false, ActorsToIgnore,
                            EDrawDebugTrace_None, HitResult, true,
                            TraceColor, TraceColor, 0.0
                        )
                        s.visible = not WasHit
                    end)
                end
            end
        end

        -- Filter out PROP_TYPE_NONE (area/zone markers) and assign labels
        local filtered = {}
        for _, s in ipairs(nearbyStations) do
            local label = PROP_TYPE_LABELS[s.propType]
            if label then
                s.typeLabel = label
                table.insert(filtered, s)
            end
        end
        nearbyStations = filtered

        -- Print results
        print(string.format("[DebugF7] Found %d stations within %.0fm", #nearbyStations, scanRadius / 100))

        local hintLines = {}
        for i, s in ipairs(nearbyStations) do
            local vis = s.visible and "VIS" or "HID"
            local spots = s.conns - s.users
            local spotsStr = spots .. "/" .. s.conns
            if s.users > 0 then spotsStr = spotsStr .. " (" .. s.users .. " used)" end

            local line = string.format("%s %.0fm %s spots=%s %s",
                vis, s.dist / 100, s.typeLabel, spotsStr, s.mesh)

            print(string.format("[DebugF7] [%d] %s | %s | class=%s", i, line, s.name, s.class))

            if i <= 15 then
                table.insert(hintLines, string.format("%s %.0fm %s %s", vis, s.dist / 100, s.typeLabel, spotsStr))
            end
        end

        local hintText = string.format("STATIONS (%d within %.0fm):\n", #nearbyStations, scanRadius / 100)
            .. table.concat(hintLines, "\n")
        ShowHint(hintText, 15)

        print("[DebugF7] === SCAN COMPLETE ===")
    end)
end

-- F7 Debug Function - Send nearest NPC to nearest available station (toggle)
-- First press: sends nearest NPC to nearest ambient station
-- Second press: releases NPC back to normal schedule
local StationUse = require("Utils.StationUse")
local Utils = require("Utils.Utils")
_G._UseStationState = _G._UseStationState or { active = false, npcId = nil }
_G._UseStationState.teleport = true

function DebugF7BringToMe()
    ExecuteInGameThread(function()
        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end
        local popManager = staticData.populationManager
        if not popManager then print("[DebugF7] No popManager") return end

        local npcId = "EverettClopton"
        local player = staticData.player
        local cam = staticData.cameraManager
        if not player or not cam then print("[DebugF7] Missing refs") return end

        local playerLoc = player:K2_GetActorLocation()
        local camRot = cam:GetCameraRotation()
        local yaw = math.rad(camRot.Yaw)
        local spawnPos = {
            X = playerLoc.X + math.cos(yaw) * 300,
            Y = playerLoc.Y + math.sin(yaw) * 300,
            Z = playerLoc.Z,
        }
        local halfFace = math.rad(camRot.Yaw + 180) * 0.5
        local companionMgr = staticData.companionManager
        if not companionMgr then print("[DebugF7] No companionMgr") return end

        local se = popManager:GetScheduledEntityFromName(npcId)
        if not se or not se:IsValid() then print("[DebugF7] No SE") return end

        -- Step 0: Prime the SE at the target position
        pcall(function()
            popManager:PlaceScheduledEntityBP(npcId, {
                Translation = spawnPos,
                Rotation = { X = 0, Y = 0, Z = math.sin(halfFace), W = math.cos(halfFace) },
                Scale3D = { X = 1, Y = 1, Z = 1 },
            })
        end)

        -- Step 1: Set as companion + force scheduler to bring them in
        print("[DebugF7] Setting companion")
        pcall(function() companionMgr:SetSystemicCompanionBP(npcId, true) end)
        pcall(function() se:AddThinkNowEvent("commitment", 0, 0, true) end)
        pcall(function() popManager:TriggerUpdate(se) end)
        -- Loop for 200ms: keep hiding mesh + forcing WaitForPlayer
        local hideLoopHandle
        hideLoopHandle = LoopInGameThreadWithDelay(29, function()
            pcall(function()
                local flesh = se:GetFlesh()
                if flesh and flesh:IsValid() and flesh.Mesh then
                    flesh.Mesh:SetVisibility(false, true)
                end
            end)
        end)
        -- Stop the loop after 200ms
        ExecuteInGameThreadWithDelay(200, function()
            if hideLoopHandle then CancelDelayedAction(hideLoopHandle) end
        end)

        -- Step 2: 500ms later, clear companion
        ExecuteInGameThreadWithDelay(500, function()
            pcall(function() companionMgr:SetSystemicCompanionBP(npcId, false) end)
            pcall(function() companionMgr:SetCompanionBP(npcId, false) end)
        end)

        -- Step 3: 2s later, find actor, lock, unhide
        ExecuteInGameThreadWithDelay(2000, function()
            local npc = nil
            pcall(function()
                if se:IsValid() and se:CurrentlyInFlesh() then
                    local flesh = se:GetFlesh()
                    if flesh and flesh:IsValid() then npc = flesh end
                end
            end)
            if not npc then
                pcall(function()
                    npc = popManager:GetActorFromEntityNameBP(npcId, true)
                end)
            end

            if not npc then
                print("[DebugF7] No actor found")
                ShowHint("No actor found", 3)
                return
            end

            -- Teleport to target position
            pcall(function()
                npc:K2_TeleportTo(spawnPos, { Pitch = 0, Yaw = camRot.Yaw + 180, Roll = 0 })
            end)
            local NPCLock = _G.NPCLockModule
            local lockId = NPCLock and NPCLock.CreateCommitmentLock(npc, se, npcId)
            -- Unhide + remove wait task after lock
            pcall(function() se:PerformTask_RemoveActivePerformTask() end)
            pcall(function()
                if npc.Mesh then npc.Mesh:SetVisibility(true, true) end
            end)
            print(string.format("[DebugF7] Locked: %s", tostring(lockId)))
            ShowHint(string.format("Placed + locked (%s)", tostring(lockId)), 5)

            -- After 4s: release lock
            ExecuteInGameThreadWithDelay(4000, function()
                if lockId and NPCLock.ReleaseNPC then
                    pcall(NPCLock.ReleaseNPC, lockId)
                    print("[DebugF7] Lock released")
                    ShowHint("Lock released", 3)
                end
            end)
            -- After 6s: destroy
            ExecuteInGameThreadWithDelay(6000, function()
                pcall(function()
                    if npc and npc.IsValid and npc:IsValid() then
                        npc:K2_DestroyActor()
                        print("[DebugF7] Actor destroyed")
                        ShowHint("Actor destroyed", 3)
                    end
                end)
            end)
        end)
    end)
    if true then return end

    -- PlaceScheduledEntityBP test: spawn NPC 300 units in front of player
    ExecuteInGameThread(function()
        local npcId = "EverettClopton"
        local staticData = _G.GetStaticCache and _G.GetStaticCache()
        if not staticData then print("[DebugF7] No static cache") return end
        local player = staticData.player
        local cam = staticData.cameraManager
        local popManager = staticData.populationManager
        if not player or not cam or not popManager then print("[DebugF7] Missing refs") return end

        local schedEnt = popManager:GetScheduledEntityFromName(npcId)
        if schedEnt and schedEnt:IsValid() then
            --schedEnt:SetCurrentActorToAggro(FName("None"), true)
            --schedEnt:PerformTask_WaitForPlayer(0, true)
            popManager:TriggerUpdate(schedEnt)
        end

        local playerLoc = player:K2_GetActorLocation()
        local camRot = cam:GetCameraRotation()
        local yaw = math.rad(camRot.Yaw)
        local spawnPos = {
            X = playerLoc.X + math.cos(yaw) * 300,
            Y = playerLoc.Y + math.sin(yaw) * 300,
            Z = playerLoc.Z,
        }
        local loc = schedEnt:GetLocation()
        for k, v in pairs(loc) do
            print(string.format("  %s = %s", tostring(k), tostring(v)))
        end
        local flesh = schedEnt:GetFlesh()
        print(string.format("[DebugF7] flesh = %s", tostring(flesh)))
        schedEnt:PerformTask_TeleportToTransform({
            Translation = spawnPos,
            Rotation = { X = 0, Y = 0, Z = 0, W = 1 },
            Scale3D = { X = 1, Y = 1, Z = 1 },
        })
        print(string.format("[DebugF7] PlaceScheduledEntityBP result: %s", tostring(result)))
        -- Check where the actor actually is
        local actorLoc = "not found"
        local distToSpawn = "?"
        pcall(function()
            local actor = popManager:GetActorFromEntityNameBP(npcId, true)
            if actor and actor:IsValid() then
                local loc = actor:K2_GetActorLocation()
                if loc then
                    actorLoc = string.format("%.0f,%.0f,%.0f", loc.X, loc.Y, loc.Z)
                    local dx = loc.X - spawnPos.X
                    local dy = loc.Y - spawnPos.Y
                    local dz = loc.Z - spawnPos.Z
                    distToSpawn = string.format("%.0fm", math.sqrt(dx*dx + dy*dy + dz*dz) / 100)
                end
            end
        end)
        print(string.format("[DebugF7] Actor at %s, dist to spawn: %s", actorLoc, distToSpawn))
        --ShowHint(string.format("PlaceSE %s: %s | actor dist: %s", npcId, tostring(result), distToSpawn), 5)
    end)
    if true then return end
    ExecuteInGameThread(function()
        local TAG = "[DebugF7-UseStation]"
        local state = _G._UseStationState

        -- == TOGGLE OFF ==
        if state.active then
            print(string.format("%s === RELEASING %s ===", TAG, tostring(state.npcId)))
            StationUse.Release(state.npcId)
            ShowHint(string.format("Released %s", tostring(state.npcId)), 3)
            state.active = false
            state.npcId = nil
            return
        end

        -- == SEND TO STATION ==
        local staticData = GetStaticCache()
        if not staticData then return end

        local player = staticData.player
        if not player or not SafeIsValid(player) then return end

        local popMgr = staticData.populationManager
        if not popMgr or not SafeIsValid(popMgr) then return end

        local playerLoc = nil
        pcall(function() playerLoc = player:K2_GetActorLocation() end)
        if not playerLoc then return end

        -- Find nearest named NPC (voice ID only, no actor refs stored)
        local bestId, bestDist = nil, math.huge
        local npcLoc = nil
        local allChars = nil
        pcall(function() allChars = FindAllOf("Character") end)
        if allChars then
            local playerFN = player:GetFullName()
            local compFN = ""
            pcall(function()
                local cp = staticData.companionManager:GetPrimaryCompanionPawn()
                if cp and SafeIsValid(cp) then compFN = cp:GetFullName() end
            end)
            for _, actor in pairs(allChars) do
                pcall(function()
                    if not actor:IsValid() then return end
                    local fn = actor:GetFullName()
                    if fn == playerFN or fn == compFN then return end
                    if fn:find("BP_Tier3_Character") then return end

                    local loc = actor:K2_GetActorLocation()
                    local d = math.sqrt((loc.X - playerLoc.X)^2 + (loc.Y - playerLoc.Y)^2 + (loc.Z - playerLoc.Z)^2)
                    if d >= bestDist or d > 2000 then return end

                    local voiceName = Utils.GetActorVoiceId(actor, staticData)
                    if not voiceName then return end

                    -- Verify has a ScheduledEntity
                    local se = popMgr:GetScheduledEntityFromActor(actor, false)
                    if not se then return end

                    bestDist = d
                    bestId = voiceName
                    npcLoc = loc
                end)
            end
        end

        if not bestId then
            ShowHint("No named NPC found within 20m", 3)
            return
        end

        -- Find nearest ambient station
        local station, info = StationUse.FindNearestAmbientStation(playerLoc, npcLoc)
        if not station then
            ShowHint("No available station within 20m", 3)
            return
        end

        -- Send NPC to station
        local teleport = state.teleport or false
        local ok = StationUse.SendToStation(bestId, station, teleport)
        if ok then
            local method = teleport and "Teleport" or "Move"
            ShowHint(string.format("%s -> %s (%s) [%s]\nPress again to release",
                bestId, info.typeLabel, info.name, method), 8)
            state.active = true
            state.npcId = bestId
        else
            ShowHint("FAILED to send " .. bestId .. " to station", 5)
        end
    end)
end
