# 48 kHz VoxCPM2 AudioVAE implementation record

## Status

The approved plan is implemented on the companion `omnivoice.cpp` branch
`voxcpm2-upscaler` (commit `b29e1ee`) and wired
into Sonorus's `omnivoice_cpp` provider. The original 24 kHz codec output is now optionally
passed through a VAE-only VoxCPM2 model on the same GGML backend:

```text
OmniVoice codec
    -> post-processed 24 kHz mono PCM
    -> Hann-windowed sinc resample to 16 kHz (torchaudio defaults)
    -> VoxCPM2 causal AudioVAE encoder (deterministic mu latent)
    -> sample-rate-conditioned decoder (48 kHz bucket)
    -> 48 kHz mono PCM
```

For the Sonorus integration the VAE is required: the worker supplies
`ov_init_params.upscaler_path`, requires ABI v4, and rejects output that is not 48 kHz.
The native library itself keeps backward-compatible 24 kHz behavior when
`upscaler_path == NULL`.

## Implemented design

The AudioVAE was ported directly into `omnivoice.cpp`. CrispASR was used as an MIT-licensed
mapping/graph reference, but is not bundled, linked, or launched as a second runtime.
This keeps OmniVoice and the upscaler on the same selected CPU/CUDA/Vulkan backend and avoids
ONNX Runtime, a second device picker, and model/backend skew.

Relevant native files:

- `src/pipeline-upscaler.{h,cpp}`: GGML encoder/decoder graphs, sample-rate conditioning,
  24 -> 16 kHz resampling, and bounded long-audio processing.
- `src/omnivoice.{h,cpp}`: ABI v4 optional `upscaler_path`, eager loading, 48 kHz buffered
  return path, and explicit rejection of native callback streaming with the VAE loaded.
- `tools/convert-voxcpm2-audiovae.py`: strict VAE-only GGUF converter.
- `tools/dump-voxcpm2-audiovae-reference.py`: authoritative OpenBMB PyTorch stage/full
  reference harness.
- `tools/omnivoice-upscale.cpp`: standalone 24 -> 16 -> 48 kHz parity/benchmark CLI.
- `docs/VOXCPM2_AUDIOVAE_TOOLING.md`: converter, provenance, and validation instructions.

Relevant Sonorus files:

- `services/omnivoice_cpp_engine.py`: ABI-v4 ctypes layout, third-model download and status,
  upscaler path at worker initialization, and 48 kHz output validation.
- `services/tts/omnivoice_cpp.py`: reports/forwards 48 kHz PCM to the existing sentence
  playback queue and EQ.
- `routes/config.py`, `js/config.js`, `config.html`, and
  `install_omnivoice_cpp.bat`: three-model install state and user-facing copy.

## GGUF conversion and model inventory

The official OpenBMB checkpoint is loaded from `audiovae.pth`. The converter:

- folds all 75 PyTorch weight-normalisation pairs using the default `dim=0` convention;
- excludes `encoder.fc_logvar` because inference uses the deterministic `mu`;
- stores large convolution weights as F16 and keeps biases, Snake alpha, sample-rate
  embeddings, and small weights as F32;
- rejects unmapped, orphaned, duplicate, or unconsumed tensors;
- embeds configuration, source SHA-256, Apache-2.0 provenance, and CrispASR MIT attribution.

Current conversion:

| Item | Value |
|---|---:|
| Source tensors | 312 |
| GGUF tensors | 233 |
| Folded weight-norm pairs | 75 |
| Output file | `voxcpm2-audiovae-f16.gguf` |
| Output bytes | **187,868,032** |
| Output MiB | **179.17** |
| Source SHA-256 | `2f3ab19e167a9a31985194fb9843d0460b7424ef127e8559e2aedc5e45e9c2f6` |
| Output SHA-256 | `a5fb091c0a95172bdee2ee7230335dac7d3dc318d77ca100f095d023cabd5d97` |

Sonorus now expects three downloaded models:

| File | Bytes | MiB |
|---|---:|---:|
| `omnivoice-base-Q8_0.gguf` | 656,395,008 | 625.99 |
| `omnivoice-tokenizer-F32.gguf` | 734,300,704 | 700.28 |
| `voxcpm2-audiovae-f16.gguf` | 187,868,032 | 179.17 |
| **Total** | **1,578,563,744** | **1,505.44** |

## Long-audio behavior

The AudioVAE is causal but a single full graph can exceed practical backend graph/dispatch
limits. The implementation therefore uses 102,400-sample payloads at 16 kHz (6.4 seconds)
with 25,600 samples (1.6 seconds) of causal history. Each chunk re-evaluates its history,
then discards that historical output. Interior right-padding is discarded; only the final
chunk retains the official 640-sample encoder-hop padding.

This is bounded-memory whole-buffer processing, not true incremental output. Native
`ov_tts_params.on_chunk` is rejected when the VAE is loaded. Sonorus already calls
buffered `ov_synthesize` once per sentence and then forwards that completed sentence to
its playback callback, so this limitation does not alter the provider's existing sentence
boundary.

## Validation results

All comparisons used identical float32 16 kHz samples from the authoritative OpenBMB
PyTorch implementation:

| Test | Result |
|---|---:|
| CPU, one-second output cosine | **0.9999996** |
| Vulkan, one-second output cosine | **0.9999625** |
| R9700 Vulkan, 17.28 s output (warmed) | **1.023-1.040 s**, RTF **0.059-0.060** |
| Full bounded-chunk output vs whole PyTorch | cosine **0.9999720** |
| Full native cloned-TTS smoke test | **5.88 s** at 48 kHz; TTS **1.869 s** + VAE **0.388 s** |

Output lengths matched the PyTorch hop-padding contract exactly. The full comparison
exercised multiple 6.4-second payloads and their history/discard boundaries, providing
numerical seam validation.

These are standalone native and Python-integration results. The earlier pre-release 5
in-game conversations were generated by the 24 kHz implementation. The 48 kHz build has
**not yet been tested in game**, so no in-game quality, interruption, or latency claim
should be made for it yet.

## Packaging and remaining acceptance work

The exact VAE GGUF is published as the
[`voxcpm2-audiovae-v1` prerelease asset](https://github.com/Jrjy3/omnivoice.cpp/releases/tag/voxcpm2-audiovae-v1).
The Sonorus installer resumes direct downloads, then validates the exact 187,868,032-byte
size and SHA-256 before reporting the models ready. Environment overrides
`SONORUS_OMNIVOICE_UPSCALER_REPO` and `SONORUS_OMNIVOICE_UPSCALER_URL` are available for
local testing or mirrors. A real download from the published URL passed the same integrity
check.

Remaining release acceptance:

1. Run a clean three-model installation through the packaged Sonorus UI/batch flow.
2. Test in-game 48 kHz conversations, device switching, interruption/barge-in, and an
   audible A/B against both the old 24 kHz C++ path and torch OmniVoice.
3. Test vendors/configurations beyond the R9700 Vulkan and CPU validation paths.

## Provenance and licenses

- [OpenBMB/VoxCPM2 model](https://huggingface.co/openbmb/VoxCPM2), Apache-2.0.
- [Official AudioVAE V2 source](https://github.com/OpenBMB/VoxCPM/blob/main/src/voxcpm/modules/audiovae/audio_vae_v2.py),
  Apache-2.0.
- [CrispASR runtime and mapping reference](https://github.com/CrispStrobe/CrispASR),
  MIT.
- [omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp) and its ggml fork,
  MIT.

The previous ONNX/external-CrispASR alternatives and 4–7 day estimate were planning
history. Direct same-backend GGML integration was selected and is now implemented.
