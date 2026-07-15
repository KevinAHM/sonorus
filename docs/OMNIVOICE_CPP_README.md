# OmniVoice (Vulkan) — `omnivoice_cpp` TTS provider

**Branch overview for the Sonorus maintainer.** This branch adds a third OmniVoice TTS
provider that runs OmniVoice through [omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp)
(ggml/Vulkan) instead of torch/CUDA, so OmniVoice works on **any Vulkan GPU** — AMD, Intel,
or NVIDIA — with **no torch and no CUDA installed at all**.

Base: **1.0.8 pre-release 4** (`Import Sonorus 1.0.8 pre-release 4 source`), with the fix
set from `docs/REBASE_NOTES.md` applied. Design rationale and phase breakdown live in
`docs/OMNIVOICE_CPP_PLAN.md`; this file is the "what shipped and how to build it" summary.

---

## 1. Why

The existing `omnivoice` provider is torch/CUDA-only. Its 1.0.8 GPU picker enumerates
**CUDA devices only** (`utils/gpu_info.py` shells out to `nvidia-smi`), so:

- **AMD/Intel-only players cannot use OmniVoice at all** — it's the highest-quality local
  option, and they're stuck on Pocket.
- **Dual-GPU users can't offload.** Running OmniVoice and the game on one card causes VRAM
  contention (OmniVoice + Hogwarts Legacy exceeds 16 GB), which also forces Pocket.

This provider fixes both with one code path: a Vulkan backend plus a GPU picker that lists
every Vulkan device. Nothing here is AMD-specific — NVIDIA cards enumerate too, so it also
works as a lighter, torch-free OmniVoice for NVIDIA users (no ~4 GB torch install).

The existing `omnivoice` provider is untouched and remains the default local OmniVoice.

---

## 2. What's in the diff

| File | Δ | Purpose |
|---|---|---|
| `services/omnivoice_cpp_engine.py` | **new**, 859 | ctypes binding of the `omnivoice.h` C ABI + persistent spawn worker |
| `services/tts/omnivoice_cpp.py` | **new**, 537 | The provider (`OmniVoiceCppProvider`), adapted from `services/tts/omnivoice.py` |
| `utils/vulkan_gpu_info.py` | **new**, 256 | Shared Vulkan device enumeration (deliberately not TTS-private) |
| `routes/config.py` | +163 | GPU status branch, device-change restart, restart endpoint, + one upstream bugfix (§8) |
| `js/config.js` | +137 | Provider entry, GPU picker, restart button, + one upstream bugfix (§8) |
| `config.html` | +21 | `omnivoiceCppSetup` panel markup |
| `services/tts/__init__.py` | +17 | Provider registration (`get_provider`, `clear_provider_cache`, `is_available`) |
| `utils/settings.py` | +1 | `tts.omnivoice_cpp` defaults |
| `.gitignore` | +8 | Ignore build artifacts / models / local pip installs |

Total ≈ 2,000 added lines, ~1,650 of which are the three new modules. No existing behavior
is modified anywhere except the two bugfixes in §8.

> **Line endings:** `utils/settings.py` and `services/tts/__init__.py` ship with *mixed*
> CRLF/LF endings upstream. Editors normalize them silently, which turned a 1-line change
> into ~800 lines of diff. Both files were rebuilt from the base blobs so the diff is
> byte-minimal (+1 and +17). Worth knowing if you touch them.

---

## 3. Architecture

```
services/tts/__init__.py  get_provider()      provider == "omnivoice_cpp"
    │
    ▼
services/tts/omnivoice_cpp.py                 mirrors services/tts/omnivoice.py
    │   synthesize_stream_sentences(...)      per-sentence streaming, narration
    │                                         voice switching, per-NPC CFG, EQ
    ▼
services/omnivoice_cpp_engine.py              mirrors services/omnivoice_engine.py
    │   mp.get_context('spawn') worker + request/response queues
    │   @_serialized_worker_io + _synthesis_lock (same as pocket_tts_onnx.py)
    ▼
worker process (embedded python)
    ├─ os.environ['GGML_BACKEND'] = device    MUST precede DLL load
    ├─ os.add_dll_directory(bin) + PATH; ctypes.CDLL("omnivoice.dll")
    ├─ ov_init(...)                           model resident in the chosen GPU's VRAM
    ├─ ov_extract_voice_ref(...)              per-voice RVQ encode, cached
    └─ ov_synthesize(...) → float PCM → int16 → on_chunk(pcm, None)
```

`BaseTTSProvider.speak()` handles playback, 3D audio, archive, and visemes unchanged — the
provider only feeds it chunks, exactly like the Pocket and torch OmniVoice paths.

---

## 4. Building the native runtime

**Source:** built from `https://github.com/Jrjy3/omnivoice.cpp` @ `98a5d5f` (a fork of
`ServeurpersoCom/omnivoice.cpp`), submodule `ggml` @ `9e2947f`. **MIT licensed.**

**Prerequisites**
- Visual Studio 2022+ with the **Desktop development with C++** workload (Build Tools is enough)
- **Vulkan SDK** (LunarG) — needed for `glslc` to compile the ggml Vulkan shaders
- CMake, Git
- A Vulkan-capable GPU driver (end users need only the driver, not the SDK)

**Build**
```bat
git clone --recurse-submodules https://github.com/Jrjy3/omnivoice.cpp
cd omnivoice.cpp
call "<VS install>\VC\Auxiliary\Build\vcvars64.bat"
cmake -B build -DGGML_VULKAN=ON -DOMNIVOICE_SHARED=ON
cmake --build build --config Release -j %NUMBER_OF_PROCESSORS%
```

`-DOMNIVOICE_SHARED=ON` is required — the default target is a static archive, and ctypes
needs the DLL. The repo's `buildvulkan.cmd` hardcodes a VS2022 **BuildTools** path and
doesn't pass the shared flag, so invoke cmake directly as above.

**Artifacts** → copy from `build\Release\` into `Phoenix\Binaries\Win64\sonorus\omnivoice_cpp\bin\`:

```
omnivoice.dll      ggml.dll      ggml-base.dll
ggml-cpu.dll       ggml-vulkan.dll  (~71 MB — contains the compiled shaders)
omnivoice-tts.exe  (optional; CLI, handy for debugging outside the server)
```

**Models** → `Phoenix\Binaries\Win64\sonorus\omnivoice_cpp\models\`, from HF
[`Serveurperso/OmniVoice-GGUF`](https://huggingface.co/Serveurperso/OmniVoice-GGUF):

```
omnivoice-base-Q8_0.gguf        626 MB
omnivoice-tokenizer-F32.gguf    700 MB   (keep F32 — the RVQ chain degrades if quantized)
```

**Runtime dependencies for end users:** the DLLs link `MSVCP140` / `VCRUNTIME140(_1)` — the
VC++ 2015–2022 redistributable, which Hogwarts Legacy already requires — and `vulkan-1.dll`,
the Vulkan loader installed by any Vulkan-capable GPU driver. Neither the Vulkan SDK nor the
VS toolchain is needed to *run* this.

≈1.3 GB resident. Both paths are resolved by `omnivoice_cpp_engine.BIN_DIR` / `MODEL_DIR`.
`is_available()` returns False until the DLL and both GGUFs are present, and the config UI
shows a "runtime not installed" hint instead of failing at synthesis time.

`omnivoice_cpp/` is gitignored on this branch — see §9 for the bundle-vs-download decision.

---

## 5. Settings

```python
"omnivoice_cpp": {"device": "auto", "num_steps": 32, "first_sentence_steps": 24,
                  "guidance_scale": 2.0, "apply_smoothing_eq": True, "seed": 42},
```

`device` holds a **ggml device name** — `"auto"` (let ggml pick best), or `"Vulkan0"`,
`"Vulkan1"`, … The other keys mirror the torch provider's meanings.

---

## 6. GPU selection

Device choice is the **`GGML_BACKEND` environment variable** (`src/backend.h`), set in the
worker process before the DLL loads. Values are ggml device names (`Vulkan0`, `CUDA0`,
`CPU`); unset = auto-best. Note this is *not* `GGML_VK_VISIBLE_DEVICES`.

Because ggml reads it at backend init, **changing the GPU requires a worker restart.** That's
wired up in `routes/config.py` mirroring the existing `omnivoice_device_changed` logic:
on save with a changed device, unload → `clear_provider_cache('omnivoice_cpp')` → background
`warm_up()`. The UI shows a restart notice when the selection differs from the saved value,
plus an always-available **Restart TTS Worker** button.

**Enumeration** (`utils/vulkan_gpu_info.py`, `detect_vulkan_gpus()`):

- **Primary: the bundled ggml DLLs**, via ctypes in a throwaway subprocess (so the server
  never holds a lock on the DLLs). This is authoritative — the names it returns are exactly
  what the worker will resolve for `GGML_BACKEND`.
- **Fallback: `vulkaninfo --summary`**, used only when the runtime isn't installed yet.
  Caveat: vulkaninfo lists **one entry per ICD**, so a GPU with two drivers registered shows
  up twice and indices can drift from ggml's. The ggml path deduplicates correctly, which is
  why it wins whenever available.

Built as a shared utility rather than TTS-private, so a future CUDA-free STT path can reuse it.

**Endpoints**
- `GET /api/tts/vram-status?provider=omnivoice_cpp` → `gpus[]` (`device`, `name`, `index`),
  `selected_device`, `dll_present`, `models_present`, `model_loaded`. VRAM fields are null
  (Vulkan doesn't expose them the way nvidia-smi does).
- `POST /api/tts/omnivoice-cpp/restart-worker` → unload + cache clear + background warm-up.

---

## 7. Behavior vs. the torch OmniVoice provider

| | `omnivoice` (torch) | `omnivoice_cpp` (this) |
|---|---|---|
| Output rate | 48 kHz (always-on AudioVAE upscaler) | **24 kHz native** — no upscaler |
| Smoothing EQ | yes | yes — reuses `_wrap_omnivoice_eq` from `omnivoice.py` |
| Word timings | none → amplitude visemes | same |
| Voice refs | `<Name>_reference_15s.wav` + sidecar `.txt` | identical, no conversion |
| Audio tags | `omnivoice_text.preprocess_text` | same |
| Language | — | ABI takes `""`/`en`/`zh`; left NULL = auto |

**Expect a slight timbre difference from stock OmniVoice** — the torch path's 48 kHz
upscaler is part of the torch stack and isn't carried over. That's inherent, not a bug.

Lipsync needs no forced aligner: `base.py` already generates amplitude visemes whenever
`on_chunk` receives `word_timing=None`, which is what Pocket (`do_align = False`) and torch
OmniVoice both do today. Wiring `services/alignment.py` in per sentence is a possible later
enhancement — its value would be interrupt trimming and word-level subtitles, not lipsync.

---

## 8. Two upstream bugs fixed along the way

1. **Setup wizard never accepts a successful TTS test** (`js/config.js`,
   `isCurrentTtsConfigured`). It has a hardcoded allowlist of local providers that need no
   API key; anything absent is assumed cloud and checked for a key. `omnivoice_cpp` added.
   *(Specific to this provider.)*

2. **Config saves wipe TTS/LLM test flags** (`routes/config.py`, `save_config`). **This is
   provider-agnostic and affects everyone today.** The test endpoints write `tts_tested` etc.
   straight to settings.json, but the config page keeps the `setup` snapshot it loaded at
   page-open and POSTs it back on every save. The existing preservation only restored keys
   *missing* from the payload, not stale ones — so testing and then saving any setting in the
   same browser session reverted setup to "Not Started". Fix: treat those five keys as
   backend-owned and always prefer the stored values. The legitimate resets
   (`active_tts_changed` / `llm_client_changed`) still run afterward and take precedence.

---

## 9. Known gaps / decisions for you

- **No installer yet.** DLLs and GGUFs must be placed manually today. An
  `install_omnivoice_cpp.bat` plus a config-UI download button (mirroring
  `install_omnivoice.bat` and `/api/tts/omnivoice/install-deps`) is the obvious next step.
  `omnivoice_cpp_engine.download_models()` already implements the fetch via `huggingface_hub`
  — which is in `requirements.txt` and therefore present at runtime, and which sidesteps the
  HF Xet 403 from the rebase notes — it simply has no UI button yet. No pip bootstrap and no
  new dependency are required; unlike the torch provider there is nothing to `pip install`.
- **Bundle vs. download.** `omnivoice_cpp/` is gitignored here so as not to dump ~1.3 GB of
  models + ~72 MB of DLLs into the repo. Also note `.gitattributes` routes `*.dll` and
  `*.exe` through **LFS**, so bundling binaries has LFS implications. Your call.
- **Language is auto-only.** The ABI accepts only `""`/`en`/`zh`, so there's no Sonorus
  language-code mapping to do (this corrects open question #5 in the plan doc).
- **`speed`** is read into `get_config()` for shape-parity with the torch provider but is not
  applied in this path.
- **Testing status** — see §10.

---

## 10. Verified

Test hardware: RTX 5080 (game) + **Radeon AI PRO R9700 32 GB** (TTS), Ryzen 9 9950X3D,
Windows 11. Enumeration on this box: `Vulkan0` = RTX 5080, `Vulkan1` = AMD iGPU,
`Vulkan2` = R9700.

- Build: clean on first try with VS 2022 + Vulkan SDK 1.4.350.
- CLI voice-clone on the R9700: 5.36 s of audio in **3.34 s** (RTF 0.62), MaskGIT 32 steps
  @ 78.6 ms/step.
- Persistent worker (the real path) on `Vulkan2`: cold start 3.5 s; then **~3.5 s of audio
  per sentence in ~1.2 s wall (RTF ≈ 0.4)** — comfortably inside conversational latency,
  with the NVIDIA idle.
- Config UI: provider switch, GPU picker, and Test Voice all working in the live server.
- **In-game, full play sessions:** long conversations run flawlessly, including interrupts /
  barge-in from a third character mid-line. Output quality is a clear step up from Pocket.
  With TTS pinned to the second GPU the pacing feels real-time with no noticeable delay —
  at least with the player-voice option on, since speaking the typed input gives the NPC's
  line time to generate. The NVIDIA stayed free for the game throughout.

**Not yet verified:** first-time voice cloning through the STT transcript path. The sidecar
`<Name>_reference_15s.txt` transcripts are shared with the torch provider (same
`voice_references/`, same `ensure_voice_reference_transcript`), so where they already existed
from prior OmniVoice use, this provider never exercised the generation path. That path is
unchanged from the torch provider either way.
