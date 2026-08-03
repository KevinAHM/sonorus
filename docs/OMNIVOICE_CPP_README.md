# OmniVoice (Vulkan) — `omnivoice_cpp` maintainer handoff

For a shorter explanation of the base architecture and the complete branch-level delta,
start with [`BRANCH_ARCHITECTURE_OVERVIEW.md`](BRANCH_ARCHITECTURE_OVERVIEW.md).

This branch adds a third OmniVoice TTS provider that runs through
[omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp) and ggml/Vulkan instead
of torch/CUDA. It supports Vulkan devices from AMD, Intel, and NVIDIA, including selecting
a second GPU for TTS so the game can keep the primary GPU's VRAM. The existing torch
`omnivoice` and hosted `omnivoice_api` providers remain available.

The branch also ports the optional VoxCPM2 AudioVAE to the same native
backend. Sonorus now requires ABI v4 and uses the VAE to turn OmniVoice's native 24 kHz
decode into 48 kHz output before playback. This follow-up has native parity/performance
coverage but has **not** yet been tested in game; the pre-release 5 gameplay results below
remain historical 24 kHz results.

The implementation is based on **Sonorus 1.0.8 pre-release 5**:

```text
8defc25  Import Sonorus 1.0.8 pre-release 5 source
9f9c59f  Reapply omnivoice_cpp provider on pre-release 5
dc421a4  Add OmniVoice Vulkan installer and setup UI
90daf02  Harden OmniVoice Vulkan packaging
fcde0fe  Temporarily restore issue #3 safe ports for testing
9f49b62  Keep Prepare Voices action visible before STT setup
1f72c9d  Revert the temporary issue #3 safe-port workaround before handoff
51856bb  docs: record clean pre5 acceptance results
ccb575d  docs: record final OmniVoice acceptance
2dc4ee1  Fix review findings in OmniVoice (Vulkan) installer/status flow
```

Pre-release 5 was imported wholesale rather than layering the old pre-release 4 fix stack
over it. Most fixes already incorporated upstream were dropped. The two setup bugs in §8
were still present in pre-release 5 and remain part of this branch. The issue #3 safe-port
workaround was used to unblock local acceptance testing, then reverted before handoff so
this branch leaves pre-release 5 networking unchanged.

`docs/OMNIVOICE_CPP_PLAN.md` records the original provider design. This file describes the
provider through `2dc4ee1` plus the 48 kHz follow-up implemented here and in companion
omnivoice.cpp commit `b29e1ee`.

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

The following is the code/package delta through `1f72c9d`, excluding these two handoff
documents:

| Path | Change | Purpose |
|---|---:|---|
| `services/omnivoice_cpp_engine.py` | +898 plus 48 kHz follow-up | ABI-v4 ctypes binding, persistent spawn worker, three-model download, 48 kHz validation |
| `services/tts/omnivoice_cpp.py` | +537 | Sonorus provider, sentence streaming, cloning, CFG/EQ settings |
| `utils/vulkan_gpu_info.py` | +256 | Shared Vulkan/ggml device enumeration |
| `routes/config.py` | +444/-2 | GPU lifecycle, installer/status/voice-prep routes, setup-flag bugfix |
| `js/config.js` | +290 | Provider controls, model/voice progress, GPU picker, restart, local-provider and action-visibility fixes |
| `config.html` | +55 | OmniVoice (Vulkan) setup panel |
| `install_omnivoice_cpp.bat` | +83 plus 48 kHz follow-up | Three-model installer for the embedded Python |
| `services/tts/__init__.py` | +17 | Provider registration, cache clearing, availability |
| `utils/settings.py` | +1 | `tts.omnivoice_cpp` defaults |
| `.gitignore` | +12/-1 | Ignore generated models while admitting the runtime DLLs and notices |
| `omnivoice_cpp/bin/*.dll` | five LFS objects | Bundled native runtime; exact list in §4 |
| `omnivoice_cpp/licenses/*.LICENSE` | two new files | Exact upstream omnivoice.cpp and ggml MIT notices |

The **2,650 insertions and 3 deletions** figure is the historical delta through `1f72c9d`.
The 48 kHz follow-up additionally changes six Sonorus code/UI paths and the companion
omnivoice.cpp runtime; use the final commit diff for the release total.

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
    |- ov_init(..., upscaler_path) once; keep all three models resident
    |- ov_extract_voice_ref(...) once per voice per worker; cache RVQ codes
    `- ov_synthesize(...) -> buffered 48 kHz float PCM -> int16 -> provider playback callback
```

The native DLL never loads into the Flask process. A spawned worker isolates it and owns
the model. Request/response access is serialized so simultaneous player/NPC synthesis
cannot interleave responses. `BaseTTSProvider.speak()` continues to own playback, spatial
audio, archives, interruption epochs, and amplitude-based visemes.

Sonorus binds `ov_init_default_params_v4` explicitly. The DLL also retains the legacy
`ov_init_default_params` export, which writes only the ABI-v3 struct prefix, so older
dynamically linked consumers cannot have a new tail field written past their allocation.
The VAE receives native float PCM directly after OmniVoice's existing post-processing,
avoiding the PCM16 file round trip used by the torch integration.

The installation path is deliberately separate from synthesis:

```text
five DLLs bundled in the mod
    +
install_omnivoice_cpp.bat or POST /install-models
    -> downloads three GGUFs into omnivoice_cpp/models
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
| `omnivoice.dll` | 394,752 | 0.38 |
| `ggml.dll` | 68,608 | 0.07 |
| **Total** | **75,952,128** | **72.43** |

The table records the packaged ABI-v4 build from companion commit `b29e1ee`; its
`omnivoice.dll` SHA-256 is
`3147a51e0b84da400d0926989222c1fdaa82290a3fb4745791372b4ed07722a7`.
`omnivoice-tts.exe`
and `omnivoice-upscale.exe` are useful for native debugging but are ignored and **not shipped**.
The package also includes exact upstream MIT notices at
`omnivoice_cpp/licenses/omnivoice.cpp.LICENSE` and
`omnivoice_cpp/licenses/ggml.LICENSE`.

The end-user native dependencies are the VC++ 2015–2022 redistributable (already required
by Hogwarts Legacy) and a Vulkan-capable display driver. End users do not need Visual
Studio, CMake, or the Vulkan SDK.

### Downloaded models

The installer downloads the first two files from
[`Serveurperso/OmniVoice-GGUF`](https://huggingface.co/Serveurperso/OmniVoice-GGUF) and the
VAE from the release source described below into `omnivoice_cpp/models/`:

| File | Bytes | MiB |
|---|---:|---:|
| `omnivoice-base-Q8_0.gguf` | 656,395,008 | 625.99 |
| `omnivoice-tokenizer-F32.gguf` | 734,300,704 | 700.28 |
| `voxcpm2-audiovae-f16.gguf` | 187,868,032 | 179.17 |
| **Total** | **1,578,563,744** | **1,505.44** |

Keep the tokenizer at F32; quantizing the RVQ chain reduces cloning quality. The AudioVAE
uses mixed F16/F32 weights and records source SHA-256
`2f3ab19e167a9a31985194fb9843d0460b7424ef127e8559e2aedc5e45e9c2f6`. The converted
asset SHA-256 is
`a5fb091c0a95172bdee2ee7230335dac7d3dc318d77ca100f095d023cabd5d97`. The three models
are generated/runtime content and remain gitignored.

The VAE is derived from [OpenBMB/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2),
Apache-2.0. Its VAE-only converter and graph mapping were informed by
[CrispASR](https://github.com/CrispStrobe/CrispASR), MIT; CrispASR is not bundled or loaded
at runtime. The converter and exact mapping contract live in the companion omnivoice.cpp
repository under `tools/convert-voxcpm2-audiovae.py` and
`docs/VOXCPM2_AUDIOVAE_TOOLING.md`.

The VAE is hosted as the
[`voxcpm2-audiovae-v1` prerelease asset](https://github.com/Jrjy3/omnivoice.cpp/releases/tag/voxcpm2-audiovae-v1).
The direct downloader supports resuming a partial `.incomplete` file and accepts the final
asset only when both its exact size and SHA-256 match. The release URL was exercised from
a fresh temporary directory after upload.

### Installer behavior

Users may run `Phoenix/Binaries/Win64/sonorus/install_omnivoice_cpp.bat` directly or click
**Download OmniVoice Models** in the config UI. The batch file:

1. checks for the embedded Python and all five bundled DLLs;
2. rejects truncated files and unexpanded Git LFS pointers using file-specific minimum
   sizes and the PE `MZ` signature;
3. checks that the core `huggingface_hub` requirement is installed;
4. calls `omnivoice_cpp_engine.download_models()`;
5. reuses complete files, Hugging Face's cache, and the VAE release asset's resumable
   `.incomplete` download;
6. records `installing`, `complete`, or `error` in
   `data/.omnivoice_cpp_install_status`.

The UI launches the same batch file in a visible console and polls every three seconds.
The web panel reports file-level progress (0/3 through 3/3); byte progress is
shown in the installer console. There is no pip, torch, CUDA, or FFmpeg installation step.
If the server finds an `installing` marker without its tracked installer process, it reports
a retryable error instead of leaving the button permanently disabled after a crash or
power loss.

`is_available()` becomes true only when all five runtime DLLs pass the size/PE checks,
both upstream OmniVoice GGUFs exist, and the VAE passes its exact size/SHA-256 check.
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
This is why the DLLs should be rebuilt and committed deliberately and the roughly 1.47 GiB models
must remain outside LFS. GitHub Free/Pro's 2 GB per-file limit also easily accommodates
the largest DLL; see [GitHub's LFS file-size limits](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage).

Release archives must contain the hydrated LFS objects, not Git's small pointer files.
Enable GitHub's **Include Git LFS objects in archives** repository setting, or build the
release zip from a checkout after `git lfs pull`. The runtime and installer reject pointer
or truncated DLLs through minimum-size and `MZ` PE-signature checks, but such an archive
would still be unusable until repackaged correctly.

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

The current runtime was built from `https://github.com/Jrjy3/omnivoice.cpp` branch
`voxcpm2-upscaler` at `b29e1ee` (based on fork commit `98a5d5f`, with ggml submodule
`9e2947f`). Both the fork and upstream project are MIT licensed.

Prerequisites: Visual Studio 2022 with Desktop development with C++, CMake, Git, and the
LunarG Vulkan SDK.

```bat
git clone --recurse-submodules https://github.com/Jrjy3/omnivoice.cpp
cd omnivoice.cpp
git checkout voxcpm2-upscaler
call "<VS install>\VC\Auxiliary\Build\vcvars64.bat"
cmake -B build -DGGML_VULKAN=ON -DOMNIVOICE_SHARED=ON
cmake --build build --config Release -j %NUMBER_OF_PROCESSORS%
```

`-DOMNIVOICE_SHARED=ON` is mandatory for ctypes. The 48 kHz integration also requires the
ABI-v4 branch (`OV_ABI_VERSION == 4` and `ov_init_params.upscaler_path`). Copy
only the five DLLs listed in §4 from
the Release output, verify them together, and commit them once through LFS. Do not commit
the CLI tools or any GGUF. Avoid normalizing unrelated source-file line endings when updating
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
| Native output | 48 kHz through torch AudioVAE upscaler | 24 kHz codec -> native VoxCPM2 AudioVAE -> 48 kHz |
| Smoothing EQ | yes | yes, reuses `_wrap_omnivoice_eq` |
| Word timings | none; amplitude visemes | same |
| Voice input | reference audio + `.txt`; persistent `.tokens.pt` | same audio/`.txt`; worker-memory RVQ cache |
| Runtime | torch/CUDA | ctypes/ggml/Vulkan, CPU fallback possible |
| Language | torch path's language handling | ABI supports empty/`en`/`zh`; provider currently passes auto |

The C++ VAE follows the same learned 24 -> 16 -> 48 kHz reconstruction path as the torch
provider and runs on the already selected GGML backend. Inputs are bounded to 6.4-second
payloads with 1.6 seconds of causal history, then stitched by discarding historical output.
Sonorus uses buffered `ov_synthesize` once per sentence. ABI-v4 native `on_chunk` callbacks
remain unsupported while an upscaler is loaded; this does not affect Sonorus's existing
sentence queue, which forwards the completed 48 kHz sentence to playback.

Two setup fixes are included because both bugs remain in the pre-release 5 base:

1. `isCurrentTtsConfigured()` treated any provider outside a hardcoded local allowlist as
   a cloud provider requiring an API key. Adding `omnivoice_cpp` lets a successful Test
   Voice complete setup.
2. The config page posted a stale `setup` snapshot on later saves, overwriting backend test
   flags. `routes/config.py` now treats the five test flags as backend-owned. Legitimate
   resets after provider/model changes still run later and take precedence.

The optional voice-preparation action now remains visible before STT setup. It is disabled
and labeled **Configure STT to Prepare Voices** until a loadable provider is selected,
rather than disappearing while the warning tells the user to configure STT.

### Pre-release 5 networking finding (not included in this branch)

Pre-release 5 creates an automatic Python socket port and publishes `lua_socket.port`, but
the shipped `socket_client.lua` never reads that file and still connects to hardcoded port
8173. Its HTTP server also still defaults to commonly occupied port 5000. A temporary local
workaround using HTTP 5400 and a matched Python/Lua socket on 8420 unblocked acceptance
testing, but commit `1f72c9d` reverted it before handoff. The maintainer should complete the
dynamic port-file handoff on the Lua side and decide how to handle HTTP port-5000 collisions.

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

### Verified on the final pre-release 5 branch through `1f72c9d`

- The provider was reapplied against the wholesale pre-release 5 tree with a clean diff.
- Python compilation succeeds for the engine, provider, Vulkan utility, and config routes.
- `node --check` succeeds for `js/config.js`.
- `git diff --check` and `git lfs fsck` pass.
- All five required DLLs are present, valid PE files, and LFS-tracked; models are not
  tracked. Runtime checks reject LFS pointer stubs and undersized/truncated files.
- Exact upstream MIT notices for omnivoice.cpp and ggml are included.
- A clean package with no GGUFs installed all five DLLs and both notices successfully.
- The config UI launched the models-only installer, downloaded both GGUFs, and reached the
  ready state on the clean installation.
- A temporary local HTTP 5400/socket 8420 workaround allowed acceptance testing on a system
  where port 5000 was occupied and 8173 was Windows-reserved. That workaround is not part
  of the handoff branch.
- `Vulkan2` selected the Radeon AI PRO R9700 and completed long in-game conversations on
  pre-release 5 successfully.
- Canary STT exercised the lazy first-use path for a missing sidecar in gameplay and
  produced a transcript successfully (logged inference time: 533 ms).
- The visible Prepare Voices action enabled after Canary STT configuration. Its batch run
  atomically created 173 transcript sidecars and continued past two empty STT results. A
  retry correctly attempted only `Bragbor_reference_15s.wav` and
  `Bully1_reference_15s.wav`; both still returned no transcript.

### Verified for the 48 kHz native follow-up

- The VAE-only converter strictly mapped 312 source tensors to 233 GGUF tensors, folded 75
  weight-normalisation pairs, omitted the unused `fc_logvar`, and produced an exact
  187,868,032-byte mixed F16/F32 model.
- CPU output versus authoritative PyTorch float32: cosine **0.9999996**.
- Vulkan output versus the same reference: cosine **0.9999625**.
- Radeon AI PRO R9700 / `Vulkan2` (warmed): 17.28 seconds of VAE output in
  **1.023-1.040 seconds**, **RTF 0.059-0.060**.
- Full bounded-chunk/seam output versus whole-utterance PyTorch: cosine **0.9999720**, with
  exact output length and no numerical seam failure.
- Full native cloned-TTS smoke test produced 5.88 seconds of mono 48 kHz audio; TTS took
  1.869 seconds and the VAE post-step took 0.388 seconds on `Vulkan2`.
- ABI v4 rejects an incompatible DLL, eagerly loads the VAE on the selected backend, and
  requires 48 kHz output in the Sonorus worker.
- The published VAE asset downloaded successfully and passed the packaged exact-size and
  SHA-256 validation.

These are standalone/native runtime and Python integration tests. They do **not** replace
the historical pre-release 5 gameplay tests and must not be described as an in-game 48 kHz
validation.

### Still requires acceptance testing

- Investigate why Canary returns no text for the Bragbor and Bully1 reference clips, or
  confirm that the clips contain no usable speech and should remain without sidecars.
- Repeat device switching and third-character barge-in on the pre-release 5 branch.
- Run the complete three-model UI/batch installation from a clean game installation, then
  test in-game 48 kHz conversations, interruption,
  barge-in, device switching, and an audible A/B against both 24 kHz C++ and torch OmniVoice.
- Test vendors/configurations beyond the R9700/RTX 5080 system, including CPU fallback.

---

## 10. Remaining limitations

- The ABI exposes only auto, English, and Chinese language hints; Sonorus currently passes
  auto rather than mapping its language codes.
- `speed` is read for shape parity with torch OmniVoice but is not applied.
- The AudioVAE is mandatory for Sonorus's `omnivoice_cpp` availability and output is 48 kHz;
  the underlying codec remains 24 kHz. The native public ABI still supports 24 kHz when
  `upscaler_path` is NULL, but this provider does not use that fallback.
- ABI-v4 `on_chunk` streaming cannot be used while the VAE is loaded; Sonorus buffers one
  sentence natively and then forwards it through the existing playback callback.
- There are no word timings; lipsync uses amplitude visemes and interrupt trimming cannot
  use word-level boundaries from this provider.
- Vulkan enumeration has no cross-vendor VRAM telemetry, so the UI reports device names
  and load state but null VRAM values.
- UI download progress is file-level; byte-level progress is visible only in the installer
  console.
- GGUF filenames are fixed but the Hugging Face download does not pin a repository
  revision, so future model replacement could create native/model skew.
- RVQ voice codes are cached only for the worker lifetime and are recomputed after restart.
