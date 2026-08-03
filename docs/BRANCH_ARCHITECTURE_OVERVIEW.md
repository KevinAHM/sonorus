# Sonorus native OmniVoice branch: architecture and base changes

## Scope

This branch starts from the wholesale **Sonorus 1.0.8 pre-release 5** import at
`8defc25`. It keeps the existing torch OmniVoice and hosted OmniVoice API providers, and
adds a third provider named `omnivoice_cpp`. The new provider runs OmniVoice through a
small native ggml library, can use Vulkan devices from AMD, Intel, or NVIDIA, and always
returns mono 48 kHz audio through a native VoxCPM2 AudioVAE post-process.

The branch also includes the seven Lua files from the **1.0.8 pre-release 5 hotfix**. No
Python, UI, or OmniVoice integration file was replaced by the hotfix.

This document is an architectural overview of the delta from the base. Detailed build,
installation, model-provenance, and acceptance notes remain in
[`OMNIVOICE_CPP_README.md`](OMNIVOICE_CPP_README.md) and
[`48kHz_UPSCALE_PLAN.md`](48kHz_UPSCALE_PLAN.md).

## Relevant base architecture

Sonorus already separates TTS into three layers:

1. `services/tts/__init__.py` selects a provider from settings.
2. A provider implementing `BaseTTSProvider` resolves voices and generates sentences.
   The base class continues to own buffering, playback, spatial audio, interruption
   epochs, archiving, and viseme coordination.
3. Engine modules own model-specific inference and return PCM to the provider callback.

The game-side UE4SS Lua mod is a separate process boundary. It communicates with the
Python server through the local Lua socket, sends game state and dialogue events, and
receives commands such as facial emotes and NPC-lock operations. The native TTS work does
not move playback or game logic into C++; it only replaces the inference implementation
behind the existing provider contract.

## Added native TTS path

```text
settings / config UI
        |
        v
services/tts/__init__.py              selects "omnivoice_cpp"
        |
        v
services/tts/omnivoice_cpp.py         BaseTTSProvider adapter
        |                              voice lookup, sentence policy, EQ
        v
services/omnivoice_cpp_engine.py      process manager and ctypes ABI
        |                              serialized request/response queues
        v
spawned worker process                owns DLL and resident GGUF models
        |
        +--> OmniVoice LM + codec     reference encode and 24 kHz synthesis
        |
        `--> VoxCPM2 AudioVAE         24 kHz -> 16 kHz conditioning -> 48 kHz
                        |
                        v
BaseTTSProvider playback callback     mono PCM16 at 48 kHz
```

The DLL is deliberately loaded only in a spawned worker, never in the Flask process. This
isolates native crashes and GPU state, and allows the selected `GGML_BACKEND` to be set
before any ggml DLL is loaded. The worker keeps the language model, codec, AudioVAE, and
encoded voice references resident so normal sentence generation does not reload them.

Manager operations are serialized because the worker uses one request and one response
queue. A timed-out or dead worker is fully invalidated—process, queues, and pending
responses—before another generation can start. This prevents a late response from one
sentence being consumed by the next request. Shutdown also participates in the same
synthesis lock, so it cannot close queues underneath active inference.

## Changes to the Sonorus package

### Provider and settings

- `services/tts/omnivoice_cpp.py` adapts native inference to `BaseTTSProvider`. It reuses
  the existing voice-reference naming, hashing, transcript sidecars, sentence streaming,
  smoothing EQ, and playback callback behavior.
- `services/tts/__init__.py` registers the provider and its cache/lifecycle hooks.
- `utils/settings.py` adds the `tts.omnivoice_cpp` settings block. Important settings are
  Vulkan/CPU device, MaskGIT steps, first-sentence steps, guidance scale, seed, and EQ.
- The provider reports 48 kHz unconditionally because the AudioVAE is part of the required
  runtime rather than a user-facing quality toggle.

### Configuration and installation

- `config.html`, `js/config.js`, and `routes/config.py` add provider selection, Vulkan
  device discovery, model/runtime status, model installation, Test Voice, worker restart,
  and voice-preparation progress.
- `utils/vulkan_gpu_info.py` enumerates the same ggml Vulkan device order used by the
  native runtime, allowing a secondary GPU to be selected without assuming CUDA.
- `install_omnivoice_cpp.bat` uses Sonorus's embedded Python to install the native runtime
  and models. The web UI and batch entry point call the same engine download logic.
- The branch retains two setup fixes: a config save no longer erases TTS/LLM test flags
  written during the same session, and Prepare Voices remains visible before STT is
  configured so the UI can explain its prerequisite.

### Runtime and model packaging

Five mutually compatible DLLs are published as one versioned GitHub Release archive:
`omnivoice.dll`, `ggml.dll`, `ggml-base.dll`, `ggml-cpu.dll`, and `ggml-vulkan.dll`.
Sonorus tracks only a small manifest containing the release URL, exact archive hash, and
per-DLL hashes. This keeps native revisions out of Git LFS while preserving a self-service
installation path. The archive also carries the omnivoice.cpp and ggml license notices.

The installer downloads and verifies the runtime archive before downloading the OmniVoice
base model, tokenizer/codec model, and AudioVAE. Extraction accepts only the expected flat
file list, validates every DLL before replacing the installed runtime, and rejects path
traversal or extra entries. Runtime checks then reject missing, truncated, or non-PE DLLs.
A subprocess ABI probe calls both required ABI-v4 default initializers before the UI reports
the runtime ready. The AudioVAE has exact byte-size and SHA-256 validation, including after
a resumed fallback download. The upstream models and AudioVAE are pinned to immutable
Hugging Face revisions; a checksum-identical GitHub Release mirrors the AudioVAE. A
complete `.incomplete` fallback file returned with HTTP 416 is validated and promoted
rather than discarded.

## Native library changes

The companion `omnivoice.cpp` branch implements the C ABI and AudioVAE graph used above.
The main compatibility decision is to preserve the ABI-v3 binary initializers while adding
explicit v4 initializers for new callers. A program compiled against the old structure
therefore receives only the old prefix; Sonorus explicitly binds the v4 symbols and may
safely supply the new `upscaler_path` tail field.

Additional native hardening includes:

- rejecting a non-null but empty upscaler path before backend/model loading;
- returning the public OOM status without allowing `std::bad_alloc` across the C ABI;
- validating GGUF scalar and array types before calling typed accessors;
- propagating cancellation before AudioVAE work and between bounded chunks;
- keeping output empty on cancellation or synthesis failure;
- strict converter validation for the fixed VoxCPM2 AudioVAE V2 topology.

The AudioVAE receives the native float output after OmniVoice post-processing. It performs
the same 24-to-16 kHz resampling and 48 kHz sample-rate conditioning as the authoritative
torch path. Longer audio is processed in bounded overlapping chunks with history discard,
which limits peak graph memory while preserving exact output length and seams. Native
`on_chunk` streaming is intentionally unavailable when the VAE is active; Sonorus already
buffers one completed sentence before invoking playback, so this does not change the
provider-level sentence boundary.

## Integrated 1.0.8 hotfix

The supplied hotfix differed substantively from the pre-release 5 base in seven UE4SS Lua
files. Those files were imported verbatim. The hotfix:

- discovers the live Python socket port from `lua_socket.port` instead of assuming 8173;
- synchronizes scheduler voice IDs and aliases and adds presence-validation messaging;
- uses stable actor IDs and the Blueprint bridge for NPC snap rotation instead of retaining
  and dereferencing stale UObject wrappers;
- returns a language-independent location ID alongside the display name, preventing
  localized names or “Hogwarts Valley” from being mistaken for Hogwarts Castle;
- refreshes UE4SS actor wrappers when continuing an emote on the same stable actor;
- gates experimental presence-ledger modules and replaces temporary F7 diagnostics with
  the presence-validation workflow.

The Python socket server and packaged Blueprint/native content in this branch already
contained the matching interfaces, so no OmniVoice merge adaptation was required.

## Verification and known boundary

The branch has focused coverage for worker timeout/death invalidation, ABI probing,
concurrent model validation, HTTP 416 resume behavior, ABI-v3/v4 compatibility, GGUF
metadata rejection, converter configuration, and AudioVAE cancellation. The packaged
public path has produced non-empty mono 48 kHz output on CPU and `Vulkan2` (AMD Radeon AI
PRO R9700).

One backend-specific issue remains: `Vulkan0` on the tested RTX 5080 asserts inside ggml's
MaskGIT `GET_ROWS` operation before AudioVAE processing. CPU and the R9700 Vulkan path pass,
so this is tracked as a device/backend limitation rather than a 48 kHz integration failure.
Gameplay-level device switching, interruption/barge-in, and audible comparisons remain
release acceptance work.
