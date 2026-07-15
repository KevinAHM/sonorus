# OmniVoice (Vulkan) — `omnivoice_cpp` maintainer handoff

This branch adds a third OmniVoice TTS provider that runs through
[omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp) and ggml/Vulkan instead
of torch/CUDA. It supports Vulkan devices from AMD, Intel, and NVIDIA, including selecting
a second GPU for TTS so the game can keep the primary GPU's VRAM. The existing torch
`omnivoice` and hosted `omnivoice_api` providers remain available.

The implementation is based on **Sonorus 1.0.8 pre-release 5**:

```text
8defc25  Import Sonorus 1.0.8 pre-release 5 source
9f9c59f  Reapply omnivoice_cpp provider on pre-release 5
dc421a4  Add OmniVoice Vulkan installer and setup UI
90daf02  Harden OmniVoice Vulkan packaging
```

Pre-release 5 was imported wholesale rather than layering the old pre-release 4 fix stack
over it. Fixes already incorporated upstream were dropped. The two setup bugs in §8 were
still present in pre-release 5 and remain part of this branch.

`docs/OMNIVOICE_CPP_PLAN.md` records the resolved design and acceptance status. This file
describes what is actually present through `90daf02`.

---

## 1. Why this provider exists

The stock local `omnivoice` provider uses torch/CUDA. Its device picker can therefore
select only CUDA devices on Windows. That prevents AMD/Intel-only users from using local
OmniVoice and prevents mixed-GPU systems from moving TTS to a non-NVIDIA secondary GPU.

`omnivoice_cpp` solves both cases with a Vulkan backend and a Vulkan device picker. It also
avoids the roughly 4 GB torch installation. It is not AMD-specific; `Vulkan0`, `Vulkan1`,
and so on may identify devices from any vendor.

---

## 2. Implementation diff against the pre-release 5 import

The following is the code/package delta through `90daf02`, excluding these two handoff
documents:

| Path | Change | Purpose |
|---|---:|---|
| `services/omnivoice_cpp_engine.py` | +898 | ABI-v3 ctypes binding, persistent spawn worker, validated availability checks, model download |
| `services/tts/omnivoice_cpp.py` | +537 | Sonorus provider, sentence streaming, cloning, CFG/EQ settings |
| `utils/vulkan_gpu_info.py` | +256 | Shared Vulkan/ggml device enumeration |
| `routes/config.py` | +444/-2 | GPU lifecycle, installer/status/voice-prep routes, setup-flag bugfix |
| `js/config.js` | +284 | Provider controls, model/voice progress, GPU picker, restart, local-provider bugfix |
| `config.html` | +55 | OmniVoice (Vulkan) setup panel |
| `install_omnivoice_cpp.bat` | +83 | Models-only installer for the embedded Python |
| `services/tts/__init__.py` | +17 | Provider registration, cache clearing, availability |
| `utils/settings.py` | +1 | `tts.omnivoice_cpp` defaults |
| `.gitignore` | +12/-1 | Ignore generated models while admitting the runtime DLLs and notices |
| `omnivoice_cpp/bin/*.dll` | five LFS objects | Bundled native runtime; exact list in §4 |
| `omnivoice_cpp/licenses/*.LICENSE` | two new files | Exact upstream omnivoice.cpp and ggml MIT notices |

Total relative to `8defc25`: **2,644 insertions and 3 deletions** across 17 paths.

`utils/settings.py` and `services/tts/__init__.py` contain mixed CRLF/LF endings upstream.
Their changes were kept byte-minimal (+1 and +17); editors that normalize the files can
turn these small changes into very large diffs.

---

## 3. Runtime architecture

```text
services/tts/__init__.py                 provider == "omnivoice_cpp"
    |
    v
services/tts/omnivoice_cpp.py            Sonorus TTS provider
    |  sentence splitting, voices, CFG, EQ
    v
services/omnivoice_cpp_engine.py         spawn worker + serialized queues
    |
    v
worker process
    |- set GGML_BACKEND before loading any ggml DLL
    |- add omnivoice_cpp/bin to the Windows DLL search path
    |- ctypes.CDLL("omnivoice.dll")
    |- ov_init(...) once; keep the model resident
    |- ov_extract_voice_ref(...) once per voice per worker; cache RVQ codes
    `- ov_synthesize(...) -> float PCM -> 24 kHz int16 -> on_chunk(pcm, None)
```

The native DLL never loads into the Flask process. A spawned worker isolates it and owns
the model. Request/response access is serialized so simultaneous player/NPC synthesis
cannot interleave responses. `BaseTTSProvider.speak()` continues to own playback, spatial
audio, archives, interruption epochs, and amplitude-based visemes.

The installation path is deliberately separate from synthesis:

```text
five DLLs bundled in the mod
    +
install_omnivoice_cpp.bat or POST /install-models
    -> huggingface_hub downloads two GGUFs into omnivoice_cpp/models
    -> GET /status reports readiness

optional POST /prepare-voices
    -> configured STT transcribes references missing non-empty .txt files
    -> writes shared transcript sidecars without loading the TTS worker
```

---

## 4. Packaging and installation

### Bundled native runtime

These are the complete production runtime DLLs. They live in
`Phoenix/Binaries/Win64/sonorus/omnivoice_cpp/bin/` and are tracked through Git LFS.

| File | Bytes | MiB |
|---|---:|---:|
| `ggml-vulkan.dll` | 73,998,848 | 70.57 |
| `ggml-cpu.dll` | 833,024 | 0.79 |
| `ggml-base.dll` | 656,896 | 0.63 |
| `omnivoice.dll` | 360,960 | 0.34 |
| `ggml.dll` | 68,608 | 0.07 |
| **Total** | **75,918,336** | **72.40** |

`omnivoice-tts.exe` is useful for native debugging but is ignored and **not shipped**.
The package also includes exact upstream MIT notices at
`omnivoice_cpp/licenses/omnivoice.cpp.LICENSE` and
`omnivoice_cpp/licenses/ggml.LICENSE`.

The end-user native dependencies are the VC++ 2015–2022 redistributable (already required
by Hogwarts Legacy) and a Vulkan-capable display driver. End users do not need Visual
Studio, CMake, or the Vulkan SDK.

### Downloaded models

The installer downloads these files from
[`Serveurperso/OmniVoice-GGUF`](https://huggingface.co/Serveurperso/OmniVoice-GGUF) into
`omnivoice_cpp/models/`:

| File | Bytes | MiB |
|---|---:|---:|
| `omnivoice-base-Q8_0.gguf` | 656,395,008 | 625.99 |
| `omnivoice-tokenizer-F32.gguf` | 734,300,704 | 700.28 |
| **Total** | **1,390,695,712** | **1,326.27** |

Keep the tokenizer at F32; quantizing the RVQ chain reduces cloning quality. The models
are generated/runtime content and remain gitignored.

### Installer behavior

Users may run `Phoenix/Binaries/Win64/sonorus/install_omnivoice_cpp.bat` directly or click
**Download OmniVoice Models** in the config UI. The batch file:

1. checks for the embedded Python and all five bundled DLLs;
2. rejects truncated files and unexpanded Git LFS pointers using file-specific minimum
   sizes and the PE `MZ` signature;
3. checks that the core `huggingface_hub` requirement is installed;
4. calls `omnivoice_cpp_engine.download_models()`;
5. reuses complete files and Hugging Face's cache/partial-download support;
6. records `installing`, `complete`, or `error` in
   `data/.omnivoice_cpp_install_status`.

The UI launches the same batch file in a visible console and polls every three seconds.
The web panel reports file-level progress (0/2, 1/2, 2/2); Hugging Face byte progress is
shown in the installer console. There is no pip, torch, CUDA, or FFmpeg installation step.
If the server finds an `installing` marker without its tracked installer process, it reports
a retryable error instead of leaving the button permanently disabled after a crash or
power loss.

`is_available()` becomes true only when all five runtime DLLs pass the size/PE checks and
both GGUFs exist.
An incomplete DLL bundle is reported as an update/reinstall error rather than starting a
download that could never run.

### Git LFS impact

As of July 2026, GitHub Free, Pro, and Free-for-organizations include **10 GiB of Git LFS
storage and 10 GiB of download bandwidth per month**; Team and Enterprise Cloud include
250 GiB of each. The older remembered 1 GB allowance is no longer current. See GitHub's
official [Git LFS billing documentation](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
and [included-usage table](https://docs.github.com/en/billing/reference/product-usage-included).

One 72.40 MiB runtime revision uses about 0.71% of the free storage allowance. Uploads do
not consume LFS bandwidth, but each changed binary revision stores the complete new file,
and each download is charged to the repository owner's bandwidth. A 10 GiB bandwidth
allowance is roughly 141 complete runtime fetches if no other LFS objects are downloaded.
This is why the DLLs should be rebuilt and committed deliberately and the 1.30 GiB models
must remain outside LFS. GitHub Free/Pro's 2 GB per-file limit also easily accommodates
the largest DLL; see [GitHub's LFS file-size limits](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage).

---

## 5. Model, voice, and status API

| Method and route | Behavior |
|---|---|
| `GET /api/tts/omnivoice-cpp/status` | Returns validated runtime completeness, missing/invalid DLLs, model install progress, missing transcript count, actual STT readiness, and voice-prep progress |
| `POST /api/tts/omnivoice-cpp/install-models` | Starts `install_omnivoice_cpp.bat --no-pause` in a new console; idempotently reports already installed/in progress |
| `POST /api/tts/omnivoice-cpp/prepare-voices` | Starts optional background transcription of missing sidecars; requires an available, fully configured STT provider |
| `POST /api/tts/omnivoice-cpp/restart-worker` | Unloads the worker, clears the provider cache, and warms in the background when available |
| `GET /api/tts/vram-status?provider=omnivoice_cpp` | Returns Vulkan devices, selected device, runtime/model/load state; VRAM values are null |

**Prepare Voice References** scans `voice_references/` and its immediate subdirectories for
supported audio (`wav`, `mp3`, `flac`, `m4a`, `ogg`, `opus`). Standard duration-tagged
references are included only when named `_reference_15s`; plain references and narrator
files are also accepted. Existing non-empty `.txt` sidecars are skipped.

STT is considered ready only when `services.stt.is_available()` succeeds and
`get_provider()` returns a loadable provider; selecting a provider name without its required
credentials is not enough. For each missing transcript, Sonorus converts the reference to
16 kHz mono PCM, sends it through that provider, then atomically writes the `.txt` sidecar.
A run stops early after at least five attempts if more than half fail, which avoids sending
hundreds of files through a broken STT configuration.

This step is optional. Without it, the shared
`ensure_voice_reference_transcript()` path transcribes lazily when a voice is first used.
Preparing ahead only removes that first-conversation delay.

The `.txt` sidecars are shared by both local OmniVoice providers. Torch OmniVoice also
persists `<voice>.tokens.pt`; omnivoice.cpp does **not** use or create those files. Its
audio-only RVQ reference codes are encoded on first use and cached only in the current
native worker.

---

## 6. Building or updating the native runtime

The current runtime was built from `https://github.com/Jrjy3/omnivoice.cpp` at `98a5d5f`
(forked from `ServeurpersoCom/omnivoice.cpp`), with ggml submodule `9e2947f`. Both the fork
and upstream project are MIT licensed.

Prerequisites: Visual Studio 2022 with Desktop development with C++, CMake, Git, and the
LunarG Vulkan SDK.

```bat
git clone --recurse-submodules https://github.com/Jrjy3/omnivoice.cpp
cd omnivoice.cpp
call "<VS install>\VC\Auxiliary\Build\vcvars64.bat"
cmake -B build -DGGML_VULKAN=ON -DOMNIVOICE_SHARED=ON
cmake --build build --config Release -j %NUMBER_OF_PROCESSORS%
```

`-DOMNIVOICE_SHARED=ON` is mandatory for ctypes. Copy only the five DLLs listed in §4 from
the Release output, verify them together, and commit them once through LFS. Do not commit
the CLI or either GGUF. Avoid normalizing unrelated source-file line endings when updating
the Python integration.

---

## 7. Settings and GPU selection

```python
"omnivoice_cpp": {
    "device": "auto",
    "num_steps": 32,
    "first_sentence_steps": 24,
    "guidance_scale": 2.0,
    "apply_smoothing_eq": True,
    "seed": 42,
},
```

`device` is a ggml device name: `auto`, `Vulkan0`, `Vulkan1`, and so on. The worker sets
`GGML_BACKEND` before loading the DLL; this is not `GGML_VK_VISIBLE_DEVICES`. Since ggml
reads the setting during backend initialization, changing devices requires a full worker
restart. Saving a changed device performs unload -> provider-cache clear -> background
warm-up. The panel also exposes a manual **Restart TTS Worker** button.

`utils/vulkan_gpu_info.py` enumerates devices in a throwaway subprocess. It prefers the
bundled ggml DLLs, whose names exactly match `GGML_BACKEND`. Before the runtime is present,
it can fall back to `vulkaninfo --summary`; that fallback may list duplicate ICD entries,
so ggml enumeration is authoritative once the bundle exists.

---

## 8. Behavior and two retained bugfixes

| Capability | Torch `omnivoice` | `omnivoice_cpp` |
|---|---|---|
| Native output | 48 kHz through torch AudioVAE upscaler | 24 kHz native |
| Smoothing EQ | yes | yes, reuses `_wrap_omnivoice_eq` |
| Word timings | none; amplitude visemes | same |
| Voice input | reference audio + `.txt`; persistent `.tokens.pt` | same audio/`.txt`; worker-memory RVQ cache |
| Runtime | torch/CUDA | ctypes/ggml/Vulkan, CPU fallback possible |
| Language | torch path's language handling | ABI supports empty/`en`/`zh`; provider currently passes auto |

The missing torch upscaler can produce a slight timbre difference. It is expected, not a
bug. Sentence-level one-shot synthesis is the streaming boundary; the ABI does not expose
incremental audio chunks.

Two setup fixes are included because both bugs remain in the pre-release 5 base:

1. `isCurrentTtsConfigured()` treated any provider outside a hardcoded local allowlist as
   a cloud provider requiring an API key. Adding `omnivoice_cpp` lets a successful Test
   Voice complete setup.
2. The config page posted a stale `setup` snapshot on later saves, overwriting backend test
   flags. `routes/config.py` now treats the five test flags as backend-owned. Legitimate
   resets after provider/model changes still run later and take precedence.

---

## 9. Verification status

### Verified on the earlier pre-release 4 integration

These results are strong evidence for the unchanged native/provider core but have **not**
yet been repeated end-to-end on the final pre-release 5 packaging branch:

- Clean native build with VS 2022 and Vulkan SDK 1.4.350.
- CLI voice clone on Radeon AI PRO R9700: 5.36 seconds of audio in 3.34 seconds
  (RTF 0.62; 32 MaskGIT steps at 78.6 ms/step).
- Persistent worker on `Vulkan2`: about 3.5-second cold start, then roughly 3.5 seconds of
  audio in 1.2 seconds wall time (RTF about 0.4), with an RTX 5080 idle.
- Live config UI provider switching, GPU selection, and Test Voice.
- Long in-game conversations, including interruption/barge-in from a third character.
  Quality was materially better than Pocket, and the second-GPU configuration felt
  real-time when player-message voicing provided generation overlap.

Test system: RTX 5080 for the game, Radeon AI PRO R9700 32 GB for TTS, Ryzen 9 9950X3D,
Windows 11. Device enumeration was `Vulkan0` RTX 5080, `Vulkan1` AMD iGPU, `Vulkan2` R9700.

### Verified on the final pre-release 5 branch through `90daf02`

- The provider was reapplied against the wholesale pre-release 5 tree with a clean diff.
- Python compilation succeeds for the engine, provider, Vulkan utility, and config routes.
- `node --check` succeeds for `js/config.js`.
- `git diff --check` and `git lfs fsck` pass.
- All five required DLLs are present, valid PE files, and LFS-tracked; models are not
  tracked. Runtime checks reject LFS pointer stubs and undersized/truncated files.
- Exact upstream MIT notices for omnivoice.cpp and ggml are included.

### Still requires a clean-user acceptance test

- Run the batch installer from an install with no GGUFs, including retry/resume behavior.
- Run the UI installer and confirm console launch, 3-second status polling, and transition
  to 2/2 ready.
- Prepare at least one reference with no `.txt` through a configured and loadable STT
  provider; confirm the unavailable/misconfigured-STT warning and early-failure guard.
- Exercise the lazy first-use transcript path with no pre-existing sidecar.
- Repeat Test Voice, a long in-game conversation, device switching, and third-character
  barge-in on the pre-release 5 branch.
- Test vendors/configurations beyond the R9700/RTX 5080 system, including CPU fallback.

---

## 10. Remaining limitations

- The ABI exposes only auto, English, and Chinese language hints; Sonorus currently passes
  auto rather than mapping its language codes.
- `speed` is read for shape parity with torch OmniVoice but is not applied.
- Output is native 24 kHz with no torch AudioVAE upscaler.
- There are no word timings; lipsync uses amplitude visemes and interrupt trimming cannot
  use word-level boundaries from this provider.
- Vulkan enumeration has no cross-vendor VRAM telemetry, so the UI reports device names
  and load state but null VRAM values.
- UI download progress is file-level; byte-level progress is visible only in the installer
  console.
- GGUF filenames are fixed but the Hugging Face download does not pin a repository
  revision, so future model replacement could create native/model skew.
- RVQ voice codes are cached only for the worker lifetime and are recomputed after restart.
