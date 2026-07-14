# Rebase notes — fixes on top of Sonorus 1.0.8 pre-release 4

Base commit: **Import Sonorus 1.0.8 pre-release 4 source** (source-only; binary assets
left at the 1.0.6 baseline). Fixes below are applied on top.

## Applied (still present in pre-4)
- **#1** `utils/gpu_info.py` — match `CUDA UMD Version:` header (6xx-series drivers).
- **#2** `install_omnivoice.bat` — quote pip specs (cmd ate `>`), per-package repair,
  torchcodec as its own step, auto-copy FFmpeg 7 DLLs from `ffmpeg/`.
- **#3** `routes/config.py::_is_torch_installed` — also require safetensors/transformers/
  accelerate so a partial install is repaired instead of reported "installed".
- **#5** `start_server.bat` + `start_server_debug.bat` — `curl -fL` for GitHub/PyPI
  downloads; the smart-turn model via `huggingface_hub` (HF Xet URLs 403 a plain GET).
- **#6** Configurable ports: HTTP `SONORUS_SERVER_PORT` default 5400, socket
  `SONORUS_SOCKET_PORT` default 8420 (`server.py`, `utils/lua_socket.py`,
  `socket_client.lua`), + overlay/message URLs follow the port. (Default 8173 lands in a
  Windows reserved range on some machines.)
- **#8** Chat keyboard-hook resilience — `input/text.py` (socket sends moved to a
  background sender thread, stuck-held-state auto-recovery, `restart_listener`/
  `restart_capture`), `server.py::check_game_paused` (non-blocking cached
  `get_game_context()` instead of a blocking round-trip inside the hook), and
  `utils/lua_socket.py` player_handshake rebuilds the hook after each loading screen.

## Already fixed upstream in 1.0.8 (NOT re-applied)
- **#7** `NameError: current_llm_provider` — fixed (1.0.8 pre-2).
- **#9** Pocket TTS voice interleaving — fixed (1.0.8 pre-3, `_serialized_worker_io`
  decorator; same approach as our fix).
- **#4** torchaudio→torchcodec chain — pre-4 already installs torchcodec; the remaining
  quoting/FFmpeg pieces are folded into #2.

See `docs/OMNIVOICE_CPP_PLAN.md` for the AMD-GPU OmniVoice plan.
