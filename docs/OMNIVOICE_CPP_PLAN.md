# OmniVoice (Vulkan) implementation record and maintenance plan

**Status:** implemented and clean-install tested through `9f49b62` on Sonorus **1.0.8 pre-release 5**.

**Audience:** the Sonorus maintainer or a future contributor changing the native runtime,
installer, or provider integration. This document started as an implementation plan; it now
records the resolved decisions, completed phases, and remaining acceptance work. For the
full shipped-file inventory and operational detail, see `OMNIVOICE_CPP_README.md`.

---

## 1. Goal and final result

Add `omnivoice_cpp` as a third OmniVoice provider that:

1. runs on any Vulkan-capable AMD, Intel, or NVIDIA device without torch/CUDA;
2. lets mixed-GPU users reserve the primary GPU for Hogwarts Legacy;
3. retains Sonorus voice cloning, narration, spatial playback, archives, lipsync, and
   interruption behavior;
4. ships a ready native runtime while downloading the large GGUFs only when requested;
5. provides setup, status, GPU, restart, and optional voice-preparation controls in the
   existing config UI.

The final branch history is:

```text
8defc25  Import Sonorus 1.0.8 pre-release 5 source
9f9c59f  Reapply omnivoice_cpp provider on pre-release 5
dc421a4  Add OmniVoice Vulkan installer and setup UI
90daf02  Harden OmniVoice Vulkan packaging
fcde0fe  Temporarily restore issue #3 safe ports for testing
9f49b62  Keep Prepare Voices action visible before STT setup
```

The pre-release 5 tree replaced the pre-release 4 package wholesale. Most local fixes that
the maintainer had already incorporated upstream were not carried forward. The provider
and two setup bugfixes were reapplied because pre-release 5 still needed them. The issue
#3 port workaround was restored later when testing showed that pre-release 5 publishes a
dynamic Python socket port but leaves the Lua client hardcoded to 8173.

---

## 2. Resolved design decisions

| Question | Final decision |
|---|---|
| Integration | Direct C ABI through ctypes, not the CLI |
| Process model | One spawned worker owns the DLL/context; serialized request/response queues |
| Backend selection | Set `GGML_BACKEND=<ggml device name>` before DLL load; `auto` leaves it unset |
| Devices | Enumerate bundled ggml Vulkan devices first; fall back to `vulkaninfo` before runtime availability |
| Streaming | One native synthesis call per sentence; emit `on_chunk(pcm, None)` |
| Lipsync | Use existing amplitude visemes; forced alignment deferred |
| Native distribution | Bundle five validated DLLs through Git LFS plus exact upstream MIT notices |
| Model distribution | Download two GGUFs through `huggingface_hub`; keep them out of Git/LFS |
| User installer | Models-only `install_omnivoice_cpp.bat`; no pip/torch/CUDA step |
| Voice preparation | Optional batch STT creates missing shared `.txt` sidecars only |
| Voice-code cache | Audio-only RVQ codes live in worker memory; no C++ `.tokens.pt` format |
| Language | Pass auto; ABI accepts only empty/`en`/`zh`, not the previously assumed 646-label map |
| Output parity | Native 24 kHz plus reusable smoothing EQ; no torch AudioVAE upscaler |

---

## 3. Final architecture

```text
Sonorus Flask process
  services/tts/__init__.py
      -> services/tts/omnivoice_cpp.py
      -> services/omnivoice_cpp_engine.py
             |
             | multiprocessing spawn + serialized queues
             v
         native worker
           GGML_BACKEND set before load
           omnivoice.dll + ggml DLLs loaded once
           OV ABI v3 context/model kept resident
           reference audio -> cached RVQ codes
           sentence -> 24 kHz mono PCM

BaseTTSProvider
  playback + 3D audio + archives + epochs + amplitude visemes
```

The Flask process never loads the native library. Device changes unload the worker, clear
the cached provider, and create/warm a new worker so ggml sees the new backend at init.

The installation/control plane is independent:

```text
bundled bin/*.dll
  + install_omnivoice_cpp.bat
      -> embedded python
      -> services.omnivoice_cpp_engine.download_models()
      -> Hugging Face GGUFs in ignored models/

config UI
  -> status polling every 3 seconds
  -> model installer console
  -> optional STT transcript preparation
  -> GPU selection and manual worker restart
```

---

## 4. Completed phases

### Phase 0 — Native feasibility and performance: complete on pre-release 4

- Built `omnivoice.cpp` fork `98a5d5f` with ggml `9e2947f`,
  `GGML_VULKAN=ON`, and `OMNIVOICE_SHARED=ON`.
- Verified `GGML_BACKEND=VulkanN` selects the Radeon AI PRO R9700 while the RTX 5080 stays
  idle.
- Measured CLI RTF 0.62 and persistent-worker RTF about 0.4 at 32 steps.

These native results remain the basis for the unchanged engine core, but have not yet been
rerun after the pre-release 5 packaging rebase.

### Phase 1 — ABI binding and persistent worker: complete

`services/omnivoice_cpp_engine.py` mirrors OV ABI version 3 exactly, fills defaults through
the native default-parameter functions, loads the model once, owns audio buffers correctly,
serializes synthesis/cache messages, and provides availability, lifecycle, and download
helpers.

### Phase 2 — Sonorus provider integration: complete

- `services/tts/omnivoice_cpp.py` supplies the provider, voice cache, sentence streaming,
  narration behavior, per-voice CFG, seed/step settings, and smoothing EQ.
- `services/tts/__init__.py` registers provider creation, named/all cache clearing, and
  availability.
- `utils/settings.py` supplies defaults.
- `routes/config.py` handles provider switching and GPU-change restarts.

### Phase 3 — Playback, lipsync, and interruptions: complete; verified on pre-release 4

The provider emits sentence PCM with `word_timing=None`. The existing base class generates
amplitude visemes and applies interruption epochs. Long conversations and third-character
barge-in worked in the earlier in-game test. Forced alignment remains a possible later
enhancement, not a requirement for lipsync.

### Phase 4 — Packaging and setup UX: implemented; clean-user model install verified

- Five native DLLs (72.40 MiB total) are bundled through LFS.
- Two GGUFs (1.30 GiB total) are downloaded through the embedded Python.
- `install_omnivoice_cpp.bat` validates Python, PE signatures/minimum sizes, and
  `huggingface_hub`, then downloads/reuses models and records status.
- The UI shows runtime errors, starts the model installer, polls status, selects a Vulkan
  device, explains/restarts the worker, and optionally prepares missing transcript files.

Runtime checks reject truncated DLLs and unexpanded LFS pointer stubs. Orphaned
`installing` markers become retryable errors, and voice preparation requires an actually
available/loadable STT provider rather than only a non-`none` setting. The action remains
visible but disabled before STT setup so the required next step is discoverable. The clean
package and empty-model-directory UI workflow were exercised successfully through
`9f49b62`; optional batch sidecar preparation remains to be tested.

---

## 5. Shipped and downloaded files

The production DLL bundle is exactly:

```text
omnivoice_cpp/bin/omnivoice.dll       360,960 bytes
omnivoice_cpp/bin/ggml.dll             68,608 bytes
omnivoice_cpp/bin/ggml-base.dll       656,896 bytes
omnivoice_cpp/bin/ggml-cpu.dll        833,024 bytes
omnivoice_cpp/bin/ggml-vulkan.dll  73,998,848 bytes
```

Total: 75,918,336 bytes (72.40 MiB). `omnivoice-tts.exe` is debug-only and ignored.

Required notices shipped alongside the runtime:

```text
omnivoice_cpp/licenses/omnivoice.cpp.LICENSE
omnivoice_cpp/licenses/ggml.LICENSE
```

Both are exact upstream MIT license texts.

Downloaded to `omnivoice_cpp/models/`:

```text
omnivoice-base-Q8_0.gguf        656,395,008 bytes
omnivoice-tokenizer-F32.gguf    734,300,704 bytes
```

Total: 1,390,695,712 bytes (1,326.27 MiB). Models are ignored; only the five DLLs and two
notices are admitted through `.gitignore`. The DLLs are tracked by the repository's
`*.dll` LFS rule; the notices are ordinary Git text.

As of July 2026, GitHub Free/Pro/free-organization accounts include 10 GiB LFS storage and
10 GiB monthly download bandwidth, not the older 1 GB allowance. See GitHub's official
[Git LFS billing page](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
and [plan allowance table](https://docs.github.com/en/billing/reference/product-usage-included).
Each changed DLL revision adds the full new object to storage, so native rebuild commits
should be deliberate.

For releases, enable GitHub's **Include Git LFS objects in archives** setting or create the
zip from a checkout after `git lfs pull`. Otherwise an archive can contain LFS pointer
files instead of runnable DLLs; the runtime validation rejects those pointers by design.

---

## 6. Endpoint and setup contract

### `GET /api/tts/omnivoice-cpp/status`

Returns:

- `dll_present`, `runtime_present`, and `missing_runtime_files` after minimum-size and PE
  `MZ` validation;
- `models_present` and `install_progress` (`status`, completed/total files, current file,
  message);
- `voices_needing_transcripts` and `stt_configured`, where readiness requires both
  `services.stt.is_available()` and a loadable `get_provider()`;
- `voice_progress` (`status`, total/completed/succeeded/failed, current, error).

### `POST /api/tts/omnivoice-cpp/install-models`

Rejects a missing, truncated, or unexpanded-LFS runtime, returns `already_installed` when
both models exist, returns `installing` for a live installer, or launches
`install_omnivoice_cpp.bat --no-pause` in a new console and returns HTTP 202.
An `installing` marker with no tracked live process is reported as a retryable error.

### `POST /api/tts/omnivoice-cpp/prepare-voices`

Requires an available, fully configured, and loadable STT provider. It scans supported
references missing a non-empty `.txt`, transcribes in a background thread, writes sidecars
atomically, reports progress, and stops after five or more attempts when the failure ratio
exceeds 50%.

This route intentionally does **not** create torch `.tokens.pt` files or load the native
worker. C++ RVQ reference codes are audio-only and cached by the worker on first use.

### `POST /api/tts/omnivoice-cpp/restart-worker`

Unloads the process, clears the provider cache, and starts background warm-up if all
runtime/model files are available.

### `GET /api/tts/vram-status?provider=omnivoice_cpp`

Returns ggml/Vulkan devices and runtime/model/load state. VRAM telemetry fields remain null
because there is no reliable cross-vendor equivalent to the CUDA/nvidia-smi path.

---

## 7. Voice-reference lifecycle

Both local OmniVoice providers consume the same reference audio and `.txt` transcript:

```text
voice_references/<Name>_reference_15s.wav
voice_references/<Name>_reference_15s.txt
```

The optional **Prepare Voice References** action creates missing transcripts through the
configured STT provider before gameplay. If skipped, the shared
`ensure_voice_reference_transcript()` helper performs the same work lazily on first use.

Torch OmniVoice separately persists `<Name>_reference_15s.tokens.pt`. The C++ provider
neither reads nor writes that format. It uses `ov_extract_voice_ref()` to encode reference
audio and holds the resulting RVQ data only in the current worker's cache.

---

## 8. Acceptance status

| Criterion | Status | Evidence / next step |
|---|---|---|
| Native Vulkan build | Verified on pre-4 | VS 2022 + Vulkan SDK 1.4.350 |
| Selected AMD GPU with NVIDIA idle | Verified on pre-4 | R9700=`Vulkan2`, RTX 5080 idle |
| Persistent conversational latency | Verified on pre-4 | RTF about 0.4 after cold start |
| Provider/GPU picker/Test Voice | Verified on pre-4 | Live config page |
| Long game session and barge-in | Verified on pre-4 | Third-character interruption worked |
| Reapply against pre-release 5 | Verified statically | Clean feature diff from `8defc25` |
| Python and JavaScript syntax | Verified on pre-5 | `py_compile`; `node --check` |
| LFS/runtime bundle integrity | Verified on pre-5 | `git lfs fsck`; five valid PE DLLs; two exact MIT notices |
| Fresh batch model install | Verified on pre-5 | Clean package began without GGUFs and downloaded both through the UI-launched batch |
| UI model install/status | Verified on pre-5 | Installer launched and final ready state was reached |
| Prepare Voices through STT | Pending | Remove one `.txt`; test ready and misconfigured STT; inspect atomic sidecar |
| Prepare action discoverability | Fixed; retest pending | Disabled action remains visible until STT is configured (`9f49b62`) |
| Lazy first-use transcript | Verified on pre-5 | Canary created a missing transcript during gameplay in 533 ms |
| Long in-game pre-release 5 dialogue | Verified | `Vulkan2` / R9700 conversations completed successfully |
| Pre-release 5 device change / barge-in | Pending | Repeat device switching and third-character interruption |
| Issue #3 safe-port workaround | Verified for testing | HTTP 5400 and matched socket 8420 bypassed occupied/reserved ports |
| Other vendors / CPU fallback | Pending | Test Intel/NVIDIA Vulkan and CPU behavior |

Do not promote a pending item to verified merely because the equivalent core path passed on
pre-release 4. Record the exact branch, hardware, and setup state when completing the clean
acceptance run.

---

## 9. Remaining limitations and follow-up work

1. **Language:** the native ABI accepts only empty/`en`/`zh`; the provider currently uses
   auto. There is no 646-language label mapping to add against this ABI.
2. **Speed:** `speed` exists for config-shape parity but is not applied.
3. **Output:** native output is 24 kHz. The torch-only 48 kHz AudioVAE upscaler is not
   included, so timbre can differ.
4. **Timing:** no word timings are returned. Existing amplitude visemes work, but word-level
   subtitle timing and interrupt trimming remain unavailable.
5. **Progress:** the web UI reports model files completed, not downloaded bytes. The visible
   installer console carries byte progress.
6. **Model pinning:** filenames are fixed but `hf_hub_download` does not pin a repository
   revision. Pin a revision if upstream model replacement becomes a compatibility risk.
7. **Worker cache:** RVQ voice encodings are in memory and must be rebuilt after restart.
8. **Device telemetry:** names/order are available, but VRAM totals/free memory are null.
9. **Testing:** optional batch STT preparation, pre-release 5 device-change/barge-in, broader
   GPU vendors, and CPU fallback remain unverified.
10. **Networking:** `fcde0fe` is intentionally a fixed-port test workaround. The durable
   upstream fix should make Lua consume the dynamically published socket port and should
   handle HTTP port-5000 collisions without requiring users to set environment variables.

Optional later enhancements:

- forced alignment for word timings (not required for lipsync);
- byte-level download progress streamed into the web UI;
- a pinned model revision/checksum contract;
- persistent native RVQ cache if restart-time re-encoding becomes noticeable.

---

## 10. Native rebuild procedure

Source: `https://github.com/Jrjy3/omnivoice.cpp` at `98a5d5f`, ggml at `9e2947f`.

```bat
git clone --recurse-submodules https://github.com/Jrjy3/omnivoice.cpp
cd omnivoice.cpp
call "<VS install>\VC\Auxiliary\Build\vcvars64.bat"
cmake -B build -DGGML_VULKAN=ON -DOMNIVOICE_SHARED=ON
cmake --build build --config Release -j %NUMBER_OF_PROCESSORS%
```

Copy only the five DLLs in §5, run the standalone CLI/ABI test, run `git lfs fsck`, and
repeat the full clean-user acceptance matrix. Do not commit the CLI, models, build tree, or
partial Hugging Face files.
