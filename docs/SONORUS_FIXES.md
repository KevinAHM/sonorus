# Sonorus 1.0.7 — several bugs found during a fresh OmniVoice setup (with fixes)

> **Note on format:** This is intentionally **one combined issue** covering several
> distinct bugs, rather than nine separate issues, so I don't spam the tracker.
> Happy to split any/all of these into individual issues if you'd prefer.
>
> **Attribution:** The investigation, root-cause analysis, and the fixes below were
> done by **Claude (Anthropic's AI agent)** while helping me get Sonorus running on a
> new machine. Each fix was applied to a local copy and syntax-checked (`py_compile`);
> the input-hook (#8) and Pocket-interleaving (#9) fixes were also verified in-game.
> Please review before merging.
>
> **PRs:** I'm happy to open a **separate PR for each** of these if that's useful —
> just let me know which ones you'd want.
>
> **Already reported:** Issue #1 below is a repeat of #2 — it's included only for
> completeness, since it was part of the same setup run.

## Environment

- **GPU:** NVIDIA GeForce RTX 5080 (16 GB)
- **Driver:** 610.62 (Game Ready), reports `CUDA UMD Version: 13.3`
- **OS:** Windows (Build 26200, 25H2)
- **Sonorus:** 1.0.7 (Manual Install)
- **Embedded Python:** 3.13
- **TTS provider:** OmniVoice (then Pocket)

## Summary

| # | Area | Symptom | File |
|---|------|---------|------|
| 1 | GPU detection | "No compatible NVIDIA GPU. CUDA >= 12.6 driver required." on a CUDA 13.3 driver | `utils/gpu_info.py` |
| 2 | OmniVoice install | Version pins dropped by cmd; partial install never repairs | `install_omnivoice.bat` |
| 3 | OmniVoice install | UI reports deps installed / won't repair when only torch is present | `routes/config.py` |
| 4 | OmniVoice deps | Latest torchaudio forces torchcodec + FFmpeg on Windows | `install_omnivoice.bat` |
| 5 | Model download | `AccessDenied` fetching smart-turn model from HF Xet CDN | `start_server.bat` |
| 6 | Networking | Hardcoded ports; socket port 8173 lands in a Windows reserved range → bind fails | `server.py`, `utils/lua_socket.py`, `socket_client.lua` |
| 7 | Setup API | `NameError` 500s `/api/setup/status` | `routes/setup.py` |
| 8 | Input | Chat keyboard hook gets culled by Windows (typing load + fast-travel); dies until server restart | `input/text.py`, `server.py`, `utils/lua_socket.py` |
| 9 | TTS (Pocket) | Player and NPC voices interleave mid-sentence (shared response queue) | `services/pocket_tts_onnx.py` |

---

## 1. GPU check regex fails on 6xx-series drivers (`CUDA UMD Version:`)

> **Already reported** in #2 — included here only for completeness/reiteration
> alongside the rest.

**Symptom:** OmniVoice refused to start with *"No compatible NVIDIA GPU. CUDA >= 12.6
driver required."* and the log showed `CUDA compatible: False`, despite a fully
supported RTX 5080 on a CUDA 13.3 driver.

**Cause:** `utils/gpu_info.py` parses the CUDA version from `nvidia-smi` with:

```python
match = re.search(r"CUDA Version:\s*(\d+\.\d+)", result.stdout)
```

Newer drivers (6xx series) changed the header from `CUDA Version: 13.3` to a
KMD/UMD split:

```
| NVIDIA-SMI 610.62   KMD Version: 610.62   CUDA UMD Version: 13.3 |
```

The literal string `CUDA Version:` no longer appears (the word `UMD` sits in the
middle), so the regex returns `None`, which is treated as incompatible.

**Fix:** make the `UMD` token optional so both old and new headers match:

```python
match = re.search(r"CUDA (?:UMD )?Version:\s*(\d+\.\d+)", result.stdout)
```

Verified against both `CUDA Version: 12.8` → `12.8` and `CUDA UMD Version: 13.3` → `13.3`.

---

## 2. `install_omnivoice.bat`: unquoted pip specs + a guard that can't repair partial installs

**Symptom:** After OmniVoice "install", the worker crashed with
`ModuleNotFoundError: No module named 'safetensors'`, and re-running the installer
just said *"PyTorch is already installed"* and exited without fixing it.

**Cause A — unquoted version specifiers:**

```bat
... pip install torch>=2.4.0 torchaudio>=2.4.0 --index-url ...
```

In `cmd`, `>` is a redirection operator, so `torch>=2.4.0` is parsed as `torch`
plus a redirect to a file named `=2.4.0`. The version constraints are silently
dropped and pip's output is redirected to a junk file.

**Cause B — torch-only guard:** the script checks only for `torch` to decide it's
"already installed", so if Step 1 (torch) succeeds but Step 2
(transformers/accelerate/safetensors) fails or is interrupted, re-running can never
repair it.

**Fix:** quote the specs, and give each step its own presence check so a partial
install self-repairs:

```bat
:: Step 1
"%PYTHON%" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('torch') else 1)" >nul 2>&1
if errorlevel 1 (
    ... pip install "torch>=2.4.0" "torchaudio>=2.4.0" --index-url https://download.pytorch.org/whl/cu128 ...
)

:: Step 2 (independent check)
"%PYTHON%" -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ['safetensors','transformers','accelerate']) else 1)" >nul 2>&1
if errorlevel 1 (
    ... pip install "transformers>=4.45.0" accelerate safetensors ...
)
```

---

## 3. Dep detection / `/install-deps` only check for torch

**Symptom:** With torch present but Step-2 packages missing, the in-app OmniVoice
status showed deps as installed, the worker started and crashed on
`import safetensors`, and the **Install** button returned `already_installed` — so
the UI could never fix the partial state.

**Cause:** `routes/config.py` decides everything from `_is_torch_installed()`, which
only verifies torch:

```python
def _is_torch_installed() -> bool:
    spec = importlib.util.find_spec("torch")
    ...
    return os.path.exists(os.path.join(torch_dir, "lib", "torch_cpu.dll"))
```

Both `get_omnivoice_status` and the `install_omnivoice_deps` guard rely on this.

**Fix:** require the packages the worker actually imports at startup, so a partial
install is correctly reported as *not* installed and the installer re-runs:

```python
    # after confirming torch finished (torch_cpu.dll exists):
    for _mod in ("safetensors", "transformers", "accelerate"):
        if importlib.util.find_spec(_mod) is None:
            return False
    return True
```

---

## 4. Latest torchaudio forces torchcodec + FFmpeg 7 on Windows

**Symptom:** After deps installed, playback failed with
`ModuleNotFoundError: No module named 'torchcodec'`, and after installing torchcodec,
a `TorchCodec / FFmpeg` error.

**Cause:** Because #2 drops the version pins, pip installs the newest torch/torchaudio
(2.11 here). Newer `torchaudio.load()` routes through
`torchaudio/_torchcodec.py::load_with_torchcodec`, which requires **torchcodec**, and
torchcodec requires **FFmpeg** shared libraries. On Windows, torchcodec supports
FFmpeg **4–7 only** (FFmpeg 8 is Mac/Linux) — so a "latest FFmpeg" build (v8) is
silently not detected.

**Suggested fixes (any of):**

- Pin `torchaudio` to a version whose `load()` still uses the soundfile backend
  (`soundfile` is already a dependency), avoiding torchcodec entirely; **or**
- Keep torchcodec but install it as its **own** step (so its failure can't block
  safetensors/transformers), and document/bundle **FFmpeg 7** shared DLLs
  (`avutil-59`, `avcodec-61`, `avformat-61`, `avdevice-61`, `avfilter-10`,
  `swscale-8`, `swresample-5`) next to the embedded `python.exe`. FFmpeg 8
  (`avutil-60`/`avcodec-62`) will not work on Windows.

---

## 5. `start_server.bat` can't download the smart-turn model (HF Xet → `AccessDenied`)

**Symptom:** `Downloading turn detection model...` failed with an S3-style
`AccessDenied` XML error. The same happened for direct browser downloads.

**Cause:** the script fetches the model with a plain request:

```bat
Invoke-WebRequest -Uri 'https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx' -OutFile 'models\smart-turn-v3.2-cpu.onnx'
```

Hugging Face now serves large files from its **Xet** backend
(`cas-bridge.xethub.hf.co`), and a plain GET to the redirected signed URL returns
`AccessDenied` for any non-Xet client. (The repo is public; `huggingface_hub` fetches
it fine — which is what the OmniVoice model download already uses.)

**Fix:** download via `huggingface_hub` (already in `requirements.txt`, installed
before this step):

```bat
"%PYTHON%" -c "from huggingface_hub import hf_hub_download; hf_hub_download('pipecat-ai/smart-turn-v3','smart-turn-v3.2-cpu.onnx', local_dir='models')"
```

(Separately, the GitHub/PyPI downloads in this script are more robust with
`curl.exe -fL --retry 3` than `Invoke-WebRequest` — `-f` avoids leaving a 0-byte file
that the `if not exist` guard then treats as "already downloaded".)

---

## 6. Hardcoded ports; socket port 8173 lands in a Windows reserved range

**Symptom:** The socket server failed to bind with
`PermissionError: [WinError 10013] An attempt was made to access a socket in a way
forbidden by its access permissions`, and the HTTP server hit the same 10013 on 5000
(taken by another app). `netsh interface ipv4 show excludedportrange protocol=tcp`
showed **8173 inside the reserved `8081–8180` range** (Hyper-V/WinNAT auto-reservation
— common on machines with Hyper-V/WSL2/Docker).

**Cause:** ports are effectively hardcoded:

- HTTP: `server.py` (`SONORUS_SERVER_PORT` exists, but the **owlpost/grimoire overlay
  URLs and a couple of message strings hardcode `localhost:5000`**).
- Socket: `utils/lua_socket.py` (`def __init__(self, port=8173)`) and the game side
  **`socket_client.lua` (`local SERVER_PORT = 8173`)** — two places that must agree,
  neither configurable.

**Fix:** make both ports configurable and keep the two socket-port declarations in
sync:

- Read the socket port from an env var in `server.py`
  (`LuaSocketServer(port=int(os.getenv("SONORUS_SOCKET_PORT", ...)))`).
- Parametrize the hardcoded `localhost:5000` overlay/message URLs to follow the
  configured HTTP port.
- Note in docs that if the socket port is changed, the Lua `SERVER_PORT` must match
  (or have Python write the chosen port to a file the Lua reads).

---

## 7. `NameError` 500s `/api/setup/status`

**Symptom:** `/api/setup/status` returned HTTP 500 repeatedly.

**Cause:** `routes/setup.py` line ~552 references a variable that isn't defined in
that function:

```python
if memory_settings.get('enabled') and current_llm_provider in ('openai', 'openrouter'):
```

`current_llm_provider` is defined in a *different* function; everywhere else the code
calls the helper `_get_current_llm_provider(settings)`.

**Fix:**

```python
if memory_settings.get('enabled') and _get_current_llm_provider(settings) in ('openai', 'openrouter'):
```

---

## 8. Text-input keyboard hook gets culled by Windows and never recovers

**Symptom:** The chat hotkey (Enter) stopped being detected, while all other hotkeys
kept working, and only a **server restart** revived it. Two reliable triggers: (a) long
typing/conversation sessions, and (b) **fast travel** — after a few loading screens,
Enter would go dead.

**Cause:** `input/text.py` uses a single low-level keyboard hook
(`keyboard.Listener(win32_event_filter=...)`). Unlike the other hotkeys — which use
`on_press` callbacks that pynput dispatches on a separate thread — the `win32_event_filter`
runs **in the raw Windows hook procedure**, which must return fast. That callback does
variable-latency work: a **socket send on every keystroke** (`_send_message` →
`self.send` → `lua_socket.send`), and a **blocking socket round-trip**
(`can_activate_hotkey` → `check_game_paused` → `request_state_only`) when opening chat.
Windows removes any low-level hook whose callback exceeds `LowLevelHooksTimeout`
(~300 ms). Under conversation load the per-keystroke send trips it; during **fast travel**
the pause round-trip blocks (socket mid-reconnect + heavy disk/CPU load) and trips it.
Once culled, pynput never reinstalls the hook and there's no watchdog, so chat input is
dead until the server restarts. The `on_press`-based hotkeys survive because a blocking
callback there doesn't stall the raw hook — hence the asymmetry.

A secondary stuck-state also existed: `_hotkey_down_time` is set on key-down and cleared
on key-**up**; if a key-up is ever missed, every later press hits
`if self._hotkey_down_time is not None: return` and is silently swallowed.

**Fix (four parts):**

1. Move per-keystroke socket sends **off the hook thread** — queue them and drain on a
   background worker so the callback returns immediately:

```python
# __init__
self._send_queue = queue.Queue()
self._sender_thread = None

# start(): launch worker before the hook
if self._sender_thread is None:
    self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
    self._sender_thread.start()

def _sender_loop(self):
    while True:
        msg = self._send_queue.get()
        if msg is None:
            break
        self.send(msg)

# _send_message(): enqueue instead of blocking
self._send_queue.put_nowait(msg)
```

2. Replace the blocking pause round-trip with a **non-blocking cached read** in the
   check that runs inside the hook (`server.py::check_game_paused`). The socket receive
   thread already maintains a live game-state cache:

```python
# was: context = lua_socket.request_state_only(timeout=0.2)  # blocking round-trip
context = lua_socket.get_game_context()  # instant, thread-safe cache
```

3. **Deterministically rebuild the hook after each loading screen.** The game sends a
   `player_handshake` on load-complete (fast travel / zone change) and `lua_socket`
   already holds the input-capture module, so add a `restart_capture()` (stop + start the
   listener, keeping the sender thread) and call it from the `player_handshake` handler.
   This guarantees a healthy hook right when the fast-travel trigger occurs, regardless of
   whether it was culled.

4. Auto-recover the stuck held-state instead of swallowing forever:

```python
if self._hotkey_down_time is not None:
    if time.time() - self._hotkey_down_time < 2.0:
        self.listener.suppress_event()
        return
    # stale (missed KEYUP) — reset and treat as a fresh press
    self._hotkey_held = False
    if self._hold_timer is not None:
        self._hold_timer.cancel(); self._hold_timer = None
    self._hotkey_down_time = None
```

Parts 1–2 make culling far less likely; part 3 makes it self-correcting at the exact
moment (loading screens) it was failing; part 4 handles the missed-keyup edge case.
Verified fixed in testing (many consecutive fast travels, long conversations).

---

## 9. Pocket TTS interleaves player and NPC voices (shared response queue)

**Symptom:** With the Pocket provider, sending a long message plays the player's line but
**random snippets of the NPC's reply cut in mid-sentence**, then it flips back to the
player's voice to finish. Word alignment/lipsync for both turns is scrambled too.

**Cause:** The Pocket worker (`services/pocket_tts_onnx.py`) is a single process with **one
shared `_response_queue`**, and each audio chunk it emits is tagged only as
`{"type": "chunk"}` — **no request/turn/speaker ID**. `synthesize()` and
`synthesize_sentence()` send a request and then loop on `self._response_queue.get()` to
pull chunks, **without holding any lock during the read**. The player line ("early player
TTS") and the NPC line run on **separate threads concurrently**, so both sit in their own
`get()` loops against the *same* queue. Whichever thread's `get()` fires next grabs the
next chunk **regardless of which request produced it** — so the player thread plays some
of the NPC's chunks and vice-versa. The queue was implicitly designed as a single-consumer
channel (startup/warmup/synthesis all drain it assuming exclusive access), but the
concurrent early-player-TTS path broke that assumption.

**Fix:** restore the single-consumer invariant by serializing every worker-queue I/O call
with a dedicated lock, so one call fully drains its chunks before the next begins. Since
the worker is single-threaded, this costs no real throughput.

```python
def _serialize_worker_io(fn):
    @functools.wraps(fn)
    def _wrapper(self, *args, **kwargs):
        with self._io_lock:          # dedicated lock, separate from the lifecycle lock
            return fn(self, *args, **kwargs)
    return _wrapper

# self._io_lock = threading.Lock() in __init__; apply @_serialize_worker_io to
# synthesize / synthesize_sentence / warm_up / clear_embedding
```

(A per-request ID + demux would also work and preserve concurrency, but the queue also has
several direct-read control paths — `ready`/`warmup_done`/etc. — that assume exclusive
access, so enforcing single-consumer via the lock is the cleaner, complete fix. If Pocket
ever runs more than one worker process, revisit.) Verified fixed in testing.

---

*Thanks for building Sonorus — it's a fantastic mod, and all of the above were hit
while getting it running on new (RTX 50-series / very recent driver) hardware.*
