# Plan: Run OmniVoice TTS on a second (AMD) GPU in Sonorus via omnivoice.cpp

**Audience:** an engineering agent implementing this from scratch.
**Goal:** Make Sonorus synthesize OmniVoice speech on the machine's **AMD GPU (via
Vulkan)** instead of the NVIDIA card, so the NVIDIA's VRAM is left entirely for the game.
This removes the VRAM contention that currently forces the user onto the lower-quality
Pocket provider (OmniVoice + Hogwarts Legacy together exceed the NVIDIA's 16 GB).

**Environment:** Windows. Two GPUs: NVIDIA RTX 5080 (16 GB, runs the game) and an AMD card
with large VRAM (should run TTS). Sonorus 1.0.7 "Manual Install". Sonorus ships an embedded
CPython 3.13 at `Phoenix\Binaries\Win64\sonorus\python\python.exe`.

---

## 0. Why this is tractable (read first)

Three facts shape the whole design:

1. **omnivoice.cpp has a real C ABI**, not just CLIs. `src/omnivoice.h` is plain C99
   (`extern "C"`) with `ov_init` / `ov_synthesize` / `ov_free` and an opaque `ov_context`.
   Building with `-DOMNIVOICE_SHARED=ON` produces `libomnivoice.dll` exporting only the
   `ov_*` symbols. → We can load the model **once** in a persistent Python worker and call
   it via **ctypes**, avoiding both per-call CLI model reloads and any C++ server work.

2. **Vulkan backend runs on AMD on Windows.** `buildvulkan.cmd` builds the ggml Vulkan
   backend (the ggml fork adds two custom ops `GGML_OP_SNAKE`/`GGML_OP_COL2IM_1D` with
   Vulkan kernels). Models are tiny: `omnivoice-base-Q8_0.gguf` (626 MB) +
   `omnivoice-tokenizer-F32.gguf` (702 MB) ≈ ~1.3 GB — trivial for a large AMD card. GGUFs
   are pre-built at HF `Serveurperso/OmniVoice-GGUF`.

3. **Voice references already match.** Sonorus stores each reference as
   `voice_references\<Name>_reference_15s.wav` **plus a sidecar `<Name>_reference_15s.txt`
   transcript** (see `services/omnivoice_engine.py::_read_reference_transcript` /
   `ensure_voice_reference_transcript`). omnivoice.cpp voice cloning wants exactly a
   `--ref-wav` + `--ref-text` pair. → No reference-format conversion needed.

And Sonorus has a **clean TTS provider abstraction** (`services/tts/`), so this ships as a
**new provider** without touching the conversation/core code.

---

## 1. Target architecture

```
Sonorus server (embedded python)
  services/tts/__init__.py  get_provider()  ── provider_name == "omnivoice_cpp"
      │
      ▼
  services/tts/omnivoice_cpp.py   (NEW provider, mirrors services/tts/omnivoice.py)
      │  synthesize_stream(text, voice_id, on_chunk, ...)
      ▼
  services/omnivoice_cpp_engine.py   (NEW persistent worker manager, mirrors
      │                               services/pocket_tts_onnx.py)
      │  mp.Process(spawn) ── request/response queues
      ▼
  worker process (embedded python)
      ├─ ctypes.CDLL("libomnivoice.dll")   loaded ONCE
      ├─ ov_init({base gguf, codec gguf})  → ov_context (model resident in AMD VRAM)
      └─ per request: ov_synthesize(ctx, params{text, lang, ref_wav, ref_text}) → float PCM
                       → resample 24k→ Sonorus rate if needed
                       → run Sonorus aligner for word timings (lipsync)
                       → return pcm + word_timing over the queue
```

The **base class** (`services/tts/base.py::BaseTTSProvider.speak`) already handles the
PlaybackCoordinator, 3D audio, TTS archive, and turning `on_chunk(pcm, word_timing)` into
lipsync visemes. The new provider only has to feed it chunks — exactly like the Pocket
provider does.

### Abstract methods the new provider must implement

From `services/tts/base.py` (`@abstractmethod`):

- `name() -> str` → `"omnivoice_cpp"`
- `get_config() -> Dict` → reads `settings['tts']['omnivoice_cpp']`
- `get_sample_rate() -> int` → `24000` (omnivoice.cpp output rate)
- `get_voice_cache() -> VoiceCache` → reuse the OmniVoice voice cache almost verbatim
  (it already scans `voice_references/` for `*_reference*.wav`)
- `clone_voice(display_name, reference_wav_path, ...)` → register the ref + ensure a
  sidecar transcript exists (reuse `ensure_voice_reference_transcript`)
- `synthesize_stream(text, voice_id, on_chunk, ...)` → the core: call the worker per
  sentence, align, emit chunks

Everything else (speak, get_or_create_voice, archive, viseme streaming) is inherited.

---

## 2. Work breakdown — hardest / highest-risk first

Do the phases in order. **Phases 0–1 are the real risk;** if they fail or the AMD card is
too slow, stop before touching Sonorus.

### Phase 0 — Prove the native build + AMD GPU + latency (HARDEST, DO FIRST)

Nothing else matters if omnivoice.cpp won't build for Windows/Vulkan, won't run on the AMD
card, or is too slow for conversational TTS.

Tasks:

1. Install toolchain: Visual Studio 2022 (C++ workload), CMake, Git, and the **Vulkan SDK**
   (LunarG). Confirm the AMD driver exposes Vulkan (`vulkaninfo`).
2. Clone with submodules and build the **shared** library + CLIs with Vulkan:
   ```
   git clone --recurse-submodules https://github.com/ServeurpersoCom/omnivoice.cpp.git
   cd omnivoice.cpp
   :: adapt buildvulkan.cmd to also pass -DOMNIVOICE_SHARED=ON so libomnivoice.dll is built
   buildvulkan.cmd
   ```
   Artifacts needed later: `libomnivoice.dll` (+ its ggml/backend DLLs), `omnivoice-tts.exe`.
3. Download GGUFs from `Serveurperso/OmniVoice-GGUF` into `models/`
   (`omnivoice-base-Q8_0.gguf`, `omnivoice-tokenizer-F32.gguf`).
4. **Confirm it runs on the AMD GPU, not the NVIDIA.** The ggml Vulkan backend enumerates
   devices; `src/backend.h` mentions an env override. Verify the exact variable
   (likely `GGML_VK_VISIBLE_DEVICES=<index>`; confirm by reading `backend.h` and by testing
   which index is the AMD card) and prove GPU usage via the AMD card's utilisation in Task
   Manager / an overlay while running:
   ```
   echo "Hello from the AMD card." | omnivoice-tts ^
     --model models\omnivoice-base-Q8_0.gguf ^
     --codec models\omnivoice-tokenizer-F32.gguf ^
     --lang English -o hello.wav
   ```
5. **Benchmark voice cloning latency** (this is what conversations feel):
   ```
   omnivoice-tts --model ... --codec ... --ref-wav ref.wav --ref-text ref.txt ^
     --lang English -o out.wav < one_sentence.txt
   ```
   Measure wall time for a typical 1–3 sentence reply. Note: MaskGIT does **32 full-prefill
   steps with no KV cache** (2× forward per step for CFG), so it is compute-heavy — the AMD
   card's throughput is the deciding factor. Record seconds-per-sentence.

**Exit criteria for Phase 0:** builds cleanly; runs on the AMD GPU with the NVIDIA idle;
voice-cloned single-sentence latency is acceptable for conversation (target: first audio in
≲1.5 s; tune `mg_num_step` down from 32 and `--chunk-duration` if needed).

**Risks / mitigations:** Windows Vulkan build breakage (try `buildall.cmd`; check the repo's
single open issue); AMD too slow (lower `num_step`, use Q8 base already, accept sentence
latency); device-selection env var differs (read `backend.h`).

### Phase 1 — ctypes binding + standalone persistent worker (HARD)

Prove we can drive `libomnivoice.dll` from the **embedded Python** and keep the model
resident.

Tasks:

1. Read `src/omnivoice.h` and transcribe the exact structs to ctypes:
   `ov_init_params`, `ov_tts_params` (fields include at least `text`, `lang`, `instruct`,
   the ref inputs, and seven `mg_*` sampler fields + `seed` + `cancel`/`cancel_user_data`),
   `ov_audio` (`samples: float*`, `n_samples`, `sample_rate`, `channels`), and the
   `ov_status` enum. **Do not guess field order/types — mirror the header exactly.**
   Also call `ov_init_default_params` / `ov_tts_default_params` to populate defaults rather
   than filling every field by hand.
2. Confirm whether the ref inputs are passed as **WAV path + text path/string** through the
   ABI, or whether the ABI expects already-decoded PCM/encoded RVQ. (The CLI takes
   `--ref-wav`/`--ref-text` paths; verify the `ov_tts_params` equivalent in the header. If
   the ABI only accepts paths, we pass the existing `voice_references\*.wav` + its `.txt`
   directly. If it needs PCM, load the WAV in Python first.)
3. Write a standalone script (run with the embedded python) that: `ov_init` once, then
   `ov_synthesize` several sentences in a loop, writing WAVs. Confirm the model loads once
   and stays resident, and that `ov_audio.samples`/`n_samples` decode to correct audio.
   Free with `ov_audio_free` each call and `ov_free` at exit.
4. Decide the **DLL search path** strategy on Windows so the embedded python finds
   `libomnivoice.dll` + its dependent ggml/Vulkan DLLs (use `os.add_dll_directory(...)`
   before `CDLL`, pointing at a bundled `sonorus\omnivoice_cpp\bin\` folder).

**Exit criteria:** a persistent embedded-python process synthesizes N sentences from one
loaded model, on the AMD GPU, with correct audio and no per-call reload.

### Phase 2 — Sonorus worker manager + provider (MEDIUM)

Now wire it into Sonorus, mirroring the Pocket structure.

Tasks:

1. **`services/omnivoice_cpp_engine.py`** — persistent worker manager modeled on
   `services/pocket_tts_onnx.py`:
   - `mp.get_context('spawn').Process` running a worker that loads the DLL (Phase 1) and
     holds `ov_context`.
   - Request/response queues; a `synthesize(text, ref_wav, ref_text, lang, on_chunk)` entry.
   - **Reuse the serialization pattern we already added to the Pocket manager**
     (`_serialize_worker_io` / a single `_io_lock`) so concurrent player+NPC calls can't
     interleave on a shared response queue. (See bug #9 in the project's bug report — do not
     reintroduce that race.)
   - A `warm_up()` that triggers `ov_init` + a tiny synth so first real line isn't cold.
2. **`services/tts/omnivoice_cpp.py`** — provider. Start by **copying
   `services/tts/omnivoice.py`** and swapping the engine calls:
   - `get_sample_rate()` → 24000; `name()` → `"omnivoice_cpp"`; `get_config()` reads
     `settings['tts']['omnivoice_cpp']` (device index, `mg_num_step`, `chunk_duration`,
     `guidance_scale`, `speed`, seed).
   - Reuse the existing OmniVoice `VoiceCache` (scans `voice_references/`) and
     `ensure_voice_reference_transcript` for the `.txt` transcript.
   - `synthesize_stream(...)`: split into sentences (reuse `split_into_sentences_safe` /
     `chunk_text_for_tts`), call the worker per sentence, and for each returned PCM run the
     existing aligner to produce `word_timing`, then `on_chunk(pcm_bytes, word_timing)`.
3. **Register the provider** in `services/tts/__init__.py`: add `elif provider_name ==
   'omnivoice_cpp':` branches everywhere `'omnivoice'`/`'pocket'` are dispatched
   (`get_provider`, `init`, `prepare_tts` fast-path list, `list_voices`, etc. — grep for
   `'omnivoice'` and match each site).
4. **Settings + defaults:** add `tts.omnivoice_cpp` defaults in `utils/settings.py`
   (`DEFAULT_SETTINGS`) and allow `tts.provider == "omnivoice_cpp"`.

**Exit criteria:** selecting `omnivoice_cpp` as the TTS provider produces NPC speech in-game
using the AMD GPU, with the NVIDIA free for rendering.

### Phase 3 — Lipsync alignment + streaming feel (MEDIUM)

omnivoice.cpp returns **no word timings**, but Sonorus drives NPC mouth visemes from word
alignment. The current OmniVoice/Pocket paths already solve this — reuse it.

Tasks:

1. Run Sonorus's existing forced aligner (`align_audio_to_words`, used by the Pocket worker)
   on each sentence's 24 kHz PCM to get `words / wordStartTimeSeconds / wordEndTimeSeconds`,
   and pass that as `word_timing` in `on_chunk` (same dict shape Pocket emits).
2. **Streaming granularity:** the ABI's `ov_synthesize` is one-shot per call, so stream at
   the **sentence** level (call per sentence, emit each as it finishes) to keep first-audio
   latency low. Add inter-sentence silence like the Pocket path does. (Optional later: the
   CLI supports `-o - --stream-by-line` stdout streaming; not needed if per-sentence calls
   are fast enough.)
3. Verify lipsync stays in sync and interruptions/epoch handling work (test barge-in:
   look at speaker mid-line).

**Exit criteria:** NPC mouths animate in sync; interrupts behave; latency acceptable.

### Phase 4 — Packaging + UX (EASY, last)

1. Bundle `libomnivoice.dll` + ggml/Vulkan runtime DLLs under
   `sonorus\omnivoice_cpp\bin\`, and the two GGUFs under `sonorus\omnivoice_cpp\models\`
   (or download-on-first-use via `huggingface_hub`, matching how other models are fetched —
   note the HF **Xet** gotcha from bug #5: use `huggingface_hub`, not a raw URL).
2. Add an **installer step** (`install_omnivoice_cpp.bat` or fold into existing setup) and a
   **config-UI option**: TTS provider dropdown entry + an AMD **device-index** field that
   maps to the Vulkan device env var.
3. Docs: how to pick the AMD device, expected latency, and that it needs the Vulkan runtime.

---

## 3. Exact integration points (reference)

- **Provider base / methods:** `services/tts/base.py` (`BaseTTSProvider`, abstract methods
  listed in §1; `speak()` is inherited and does playback/archive/viseme).
- **Closest template to copy:** `services/tts/omnivoice.py` (voice cache, cloning, sentence
  streaming) and `services/pocket_tts_onnx.py` (persistent worker + queues + the
  `_serialize_worker_io` lock).
- **Provider dispatch:** `services/tts/__init__.py::get_provider()` and the sibling
  functions — add `omnivoice_cpp` next to every `omnivoice`/`pocket` branch.
- **Voice references:** `voice_references\<Name>_reference_15s.wav` + `<Name>_reference_15s.txt`.
  Transcript access: `services/omnivoice_engine.py::ensure_voice_reference_transcript`.
- **Aligner for lipsync:** the same `align_audio_to_words` the Pocket worker uses.
- **Settings:** `utils/settings.py` `DEFAULT_SETTINGS['tts']` (add `omnivoice_cpp` block);
  provider chosen via `settings['tts']['provider']`.

## 4. omnivoice.cpp reference (from its docs/ARCHITECTURE.md)

- **C ABI (`src/omnivoice.h`, extern "C"):** `ov_init_default_params`,
  `ov_init(&iparams)` → `ov_context*`; `ov_tts_default_params`,
  `ov_synthesize(ctx, &params, &audio)` → `ov_status`; `ov_audio_free`, `ov_free`,
  `ov_version()`. `ov_audio.samples` = malloc'd float buffer, `n_samples`,
  `sample_rate` (24000), `channels` (1). Status: `OK 0`, `INVALID_PARAMS -1`,
  `INSTRUCT_INVALID -2`, `GENERATE_FAILED -3`, `OOM -4`, `CANCELLED -5`.
- **Sampler defaults in `ov_tts_params`:** `mg_num_step=32`, `guidance_scale=2.0`,
  `t_shift=0.1`, `layer_penalty_factor=5.0`, `position_temperature=5.0`,
  `class_temperature=0.0`, `seed=42`. Lower `mg_num_step` to trade quality for speed.
- **Shared lib:** configure `-DOMNIVOICE_SHARED=ON` → `libomnivoice.dll`
  (only `ov_*` exported; `-fvisibility=hidden`). Default artifact is the static
  `libomnivoice-core.a`, so the shared flag is required for ctypes.
- **Voice cloning:** reference resampled to 16 kHz, encoded to RVQ, reused as voice prompt;
  reference RMS sets loudness. CLI flags `--ref-wav`, `--ref-text`, `--lang`,
  `--chunk-duration`, `--chunk-threshold`, `--seed`, `--no-preprocess-prompt`.
- **Models (HF `Serveurperso/OmniVoice-GGUF`):** `omnivoice-base-Q8_0.gguf` (626 MB),
  `omnivoice-tokenizer-F32.gguf` (702 MB, keep F32 — the RVQ chain degrades if quantized).

## 5. Open questions to resolve while implementing

1. Exact `ov_tts_params` field list/types and whether ref inputs are **paths vs PCM** — read
   `src/omnivoice.h` (do not assume).
2. Exact **Vulkan device-selection** mechanism/env var and the AMD card's index — read
   `src/backend.h`; confirm empirically.
3. Whether the ABI offers any **progress/chunk callback** (only `cancel` is documented). If
   not, sentence-level streaming is the plan.
4. **Latency** on the AMD card at `mg_num_step` 32 vs reduced — measured in Phase 0; decide
   default.
5. Language handling: Sonorus passes a language per voice/setup; map it to omnivoice.cpp's
   `--lang` label (it supports 646 languages; confirm the label format via `lang-map.h`).
6. Whether to bundle DLLs/GGUFs or download on first run (prefer `huggingface_hub` for GGUFs;
   see bug #5 re: HF Xet).

## 6. Acceptance criteria (end to end)

- TTS provider `omnivoice_cpp` selectable in the config UI.
- NPC replies synthesize on the **AMD GPU**; NVIDIA VRAM/utilisation stays free for the game
  (verify with an overlay while playing).
- Voice-cloned NPC voices match the existing OmniVoice references.
- Lipsync in sync; interruptions/epochs behave; conversation latency acceptable.
- Survives fast travel / long sessions (no regressions to the input-hook or TTS-interleaving
  fixes already in place).

## 7. Fallbacks if Phase 0/1 fail

- If Vulkan build/perf is unworkable: try the ROCm build (`buildall`/HIP) **only if** the AMD
  card + Windows HIP SDK support it (spotty — Vulkan is the safer bet).
- If native integration is too costly: run omnivoice.cpp's `omnivoice-tts.exe` as a
  **persistent subprocess** reading text on stdin and streaming WAV on stdout
  (`-o - --stream-by-line`) instead of ctypes — slower/looser but avoids the shared-lib build
  and ctypes struct work. Keep this as plan B, not plan A.
- If neither pans out: stay on Pocket (CPU) — already working and low-VRAM.
