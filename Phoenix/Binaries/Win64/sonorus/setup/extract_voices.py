"""
Voice Sample Extraction & Combination Script
=============================================
Extracts and combines voice samples for TTS voice cloning.

Workflow:
1. repak extracts .bnk (soundbank) file for the voice
2. wwiser parses .bnk to find referenced .wem file IDs
3. repak extracts those .wem files
4. vgmstream-cli converts .wem -> .wav
5. This script combines samples to reach target duration

Prerequisites:
- repak.exe (in bin folder) - For extracting from .pak files
- wwiser.pyz (in bin folder) - For parsing Wwise soundbanks
- vgmstream-cli.exe (in bin/vgmstream folder) - For .wem to .wav conversion
- voice_manifest.json - Shipped with mod, contains pre-selected voice IDs

Usage:
    python extract_voices.py --full <voice>      # Extract + convert + combine (all-in-one)
    python extract_voices.py --extract <voice>   # Extract samples for a voice
    python extract_voices.py --combine <voice>   # Combine to reach target duration
    python extract_voices.py --search <pattern>  # Search for audio files in pak
    python extract_voices.py --explore           # Process ALL voices from manifest
    python extract_voices.py --from-manifest     # Rebuild using only manifest files
"""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set
import wave

# Path setup - this script is in sonorus/setup/, go up one level to sonorus/
SETUP_DIR = Path(__file__).parent
SONORUS_DIR = SETUP_DIR.parent
DATA_DIR = SONORUS_DIR / "data"
GAME_DIR = SONORUS_DIR.parent.parent.parent  # Phoenix folder
PAKS_DIR = GAME_DIR / "Content" / "Paks"

EXTRACTED_AUDIO_DIR = SONORUS_DIR / "extracted_audio"
COMBINED_AUDIO_DIR = SONORUS_DIR / "voice_references"
MANIFEST_FILE = DATA_DIR / "voice_manifest.json"
TARGET_DURATIONS = [10.0, 15.0, 60.0]  # seconds - generate multiple reference lengths
MAX_EXTRACTED_PER_VOICE = 500  # limit extraction - increased for Player which has many short clips

# Tool paths (in sonorus/bin/)
BIN_DIR = SONORUS_DIR / "bin"
REPAK_EXE = BIN_DIR / "repak.exe"
WWISER = BIN_DIR / "wwiser.pyz"
VGMSTREAM_CLI = BIN_DIR / "vgmstream" / "vgmstream-cli.exe"


# Pak files to search - find all pak files dynamically
def get_pak_files():
    """Get all pak files in the Paks directory."""
    if not PAKS_DIR.exists():
        return []
    return sorted(PAKS_DIR.glob("*.pak"))


def check_tools() -> List[str]:
    """Verify required tools exist. Returns list of human-readable error messages."""
    missing = []
    if not REPAK_EXE.exists():
        missing.append("Required tool 'repak.exe' is missing. Ensure the bin/ folder contains all required tools.")
    if not WWISER.exists():
        missing.append("Required tool 'wwiser.pyz' is missing. Ensure the bin/ folder contains all required tools.")
    if not VGMSTREAM_CLI.exists():
        missing.append("Required tool 'vgmstream-cli.exe' is missing. Download from https://github.com/vgmstream/vgmstream/releases")

    pak_files = get_pak_files()
    if not pak_files:
        missing.append("Game files not found. Verify Hogwarts Legacy is installed correctly.")

    return missing


def parse_bnk_for_wem_ids(bnk_path: Path) -> tuple:
    """Parse a .bnk file with wwiser to extract referenced .wem IDs.

    Returns: (wem_ids: Set[str], wem_to_name: Dict[str, str])
    Note: wem_to_name is always empty - bnk doesn't contain lineID mappings.
    """
    if not bnk_path.exists():
        return set(), {}

    try:
        result = subprocess.run(
            [sys.executable, str(WWISER), str(bnk_path)],
            capture_output=True,
            text=True,
            cwd=str(bnk_path.parent),
            timeout=120
        )

        wem_ids = set()

        # Check for generated XML
        xml_path = None
        for ext in ['.bnk.xml', '.xml']:
            check_path = bnk_path.parent / f"{bnk_path.stem}{ext}"
            if check_path.exists():
                xml_path = check_path
                break

        if xml_path:
            content = xml_path.read_text(encoding='utf-8', errors='ignore')

            # Extract wem IDs - simple patterns only, no DOTALL
            for match in re.findall(r'sourceID["\s=:>va]+(\d{6,})', content):
                wem_ids.add(match)
            for match in re.findall(r'mediaID["\s=:>va]+(\d{6,})', content):
                wem_ids.add(match)

            xml_path.unlink()

        return wem_ids, {}

    except subprocess.TimeoutExpired:
        print(f"[ERROR] Parsing bnk timed out")
        return set(), {}
    except Exception as e:
        print(f"[ERROR] Failed to parse bnk: {e}")
        return set(), {}


def search_wem_by_ids(wem_ids: Set[str]) -> List[tuple]:
    """Search pak files for .wem files matching the given IDs."""
    if not wem_ids:
        return []

    matches = []
    pak_files = get_pak_files()

    print(f"[INFO] Searching for {len(wem_ids)} .wem files across {len(pak_files)} paks...")

    for pak_file in pak_files:
        try:
            result = subprocess.run(
                [str(REPAK_EXE), "list", str(pak_file)],
                capture_output=True,
                text=True,
                cwd=str(SONORUS_DIR),
                timeout=60
            )

            if result.returncode != 0:
                continue

            for line in result.stdout.splitlines():
                line = line.strip()
                if line.endswith('.wem'):
                    # Extract the numeric ID from the filename
                    wem_name = Path(line).stem
                    if wem_name in wem_ids:
                        matches.append((pak_file, line))

        except Exception as e:
            print(f"[ERROR] Failed to search pak: {e}")

    return matches


def search_pak_files(pattern: str, quiet: bool = True) -> List[tuple]:
    """Search for files matching pattern in pak files using repak list."""
    if not REPAK_EXE.exists():
        print(f"[ERROR] repak.exe not found at: {REPAK_EXE}")
        return []

    matches = []
    pattern_lower = pattern.lower()
    pak_files = get_pak_files()

    for pak_file in pak_files:
        try:
            result = subprocess.run(
                [str(REPAK_EXE), "list", str(pak_file)],
                capture_output=True,
                text=True,
                cwd=str(SONORUS_DIR),
                timeout=60
            )

            if result.returncode != 0:
                continue

            for line in result.stdout.splitlines():
                line = line.strip()
                if pattern_lower in line.lower():
                    if line.endswith('.wem') or line.endswith('.bnk'):
                        matches.append((pak_file, line))

        except Exception:
            pass

    return matches


def extract_wem_from_pak(pak_file: Path, wem_path: str, output_path: Path) -> bool:
    """Extract a single .wem file from pak using repak get."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'wb') as outfile:
            result = subprocess.run(
                [str(REPAK_EXE), "get", str(pak_file), wem_path],
                stdout=outfile,
                stderr=subprocess.PIPE,
                cwd=str(SONORUS_DIR),
                timeout=60
            )

        if result.returncode != 0:
            print(f"[ERROR] repak get failed: {result.stderr.decode()}")
            if output_path.exists():
                output_path.unlink()
            return False

        if output_path.exists() and output_path.stat().st_size > 0:
            return True
        else:
            if output_path.exists():
                output_path.unlink()
            return False

    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}")
        return False


def get_wav_duration(wav_path: Path) -> float:
    """Get duration of a WAV file in seconds."""
    try:
        with wave.open(str(wav_path), 'rb') as w:
            frames = w.getnframes()
            rate = w.getframerate()
            return frames / float(rate)
    except Exception as e:
        print(f"Error reading {wav_path}: {e}")
        return 0.0


def convert_wem_to_wav(wem_path: Path, wav_path: Path) -> bool:
    """Convert a .wem file to .wav using vgmstream-cli."""
    if not VGMSTREAM_CLI.exists():
        print(f"vgmstream-cli.exe not found at {VGMSTREAM_CLI}")
        print("Download from: https://github.com/vgmstream/vgmstream/releases")
        return False

    try:
        result = subprocess.run(
            [str(VGMSTREAM_CLI), "-o", str(wav_path), str(wem_path)],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Error converting {wem_path}: {e}")
        return False


def combine_wav_files(wav_files: List[Path], output_path: Path, target_duration: float,
                      gap_seconds: float = 1.0) -> tuple:
    """Combine WAV files until target duration is reached, with silence gaps between clips.

    Returns: (success: bool, selected_files: List[Path])
    """
    if not wav_files:
        return False, []

    # Get durations and filter by minimum length
    all_with_duration = [(f, get_wav_duration(f)) for f in wav_files]

    # Filter short clips, try progressively lower thresholds
    long_enough = []
    for min_dur in [3.0, 2.0, 1.0, 0.5]:
        long_enough = [(f, d) for f, d in all_with_duration if d >= min_dur]
        if long_enough:
            break

    if not long_enough:
        print(f"  [WARN] No usable audio files")
        return False, []

    # Sort by duration (longest first) for better references
    long_enough.sort(key=lambda x: x[1], reverse=True)

    # Select files up to target duration
    selected = []
    total_duration = 0.0

    for wav_file, duration in long_enough:
        gap_to_add = gap_seconds if selected else 0
        new_duration = total_duration + gap_to_add + duration

        if new_duration > target_duration and selected:
            break

        selected.append((wav_file, duration))
        total_duration = new_duration

    print(f"  {int(target_duration)}s reference: {len(selected)} clips, {total_duration:.1f}s")

    # Read and combine WAV data
    combined_data = []
    params = None
    silence = None

    for i, (wav_file, _) in enumerate(selected):
        try:
            with wave.open(str(wav_file), 'rb') as w:
                if params is None:
                    params = w.getparams()
                    bytes_per_sample = params.sampwidth
                    silence_samples = int(params.framerate * gap_seconds)
                    silence = b'\x00' * (silence_samples * params.nchannels * bytes_per_sample)
                data = w.readframes(w.getnframes())
                combined_data.append(data)
                if i < len(selected) - 1:
                    combined_data.append(silence)
        except Exception as e:
            print(f"Error reading {wav_file}: {e}")
            continue

    if not combined_data or params is None:
        print("No audio data to combine")
        return False, []

    # Write combined WAV
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), 'wb') as w:
            w.setparams(params)
            for data in combined_data:
                w.writeframes(data)
        print(f"  Saved: {output_path.name}")
        return True, [f for f, _ in selected]
    except Exception as e:
        print(f"Error writing combined audio: {e}")
        return False, []


def extract_voice_by_bnk(bnk_pattern: str, output_name: str, wem_filter: Optional[Set[str]] = None) -> tuple:
    """Extract audio by searching for a specific bnk pattern.

    Args:
        bnk_pattern: Pattern to search for in bnk filenames (e.g., "playermale")
        output_name: Name to use for output folders (e.g., "PlayerMale")
        wem_filter: If provided, only extract these specific wem IDs

    Returns: (wem_locations: Dict[wem_id -> pak_path], wem_to_name: Dict[wem_id -> dialog_name])
    """
    wem_locations: Dict[str, str] = {}
    wem_to_name: Dict[str, str] = {}

    voice_dir = EXTRACTED_AUDIO_DIR / output_name
    voice_bnk_dir = voice_dir / "bnk"
    voice_wem_dir = voice_dir / "wem"
    voice_wav_dir = voice_dir / "wav"
    voice_bnk_dir.mkdir(parents=True, exist_ok=True)
    voice_wem_dir.mkdir(parents=True, exist_ok=True)
    voice_wav_dir.mkdir(parents=True, exist_ok=True)

    wem_matches = []

    if wem_filter:
        wem_matches = search_wem_by_ids(wem_filter)
    else:
        matches = search_pak_files(bnk_pattern)
        bnk_matches = [(p, f) for p, f in matches if f.endswith('.bnk')]

        if not bnk_matches:
            return {}, {}

        print(f"  Found {len(bnk_matches)} soundbank(s)")

        all_wem_ids = set()
        for i, (pak_file, bnk_path) in enumerate(bnk_matches):
            bnk_name = Path(bnk_path).name
            print(f"  Parsing bnk {i+1}/{len(bnk_matches)}: {bnk_name}", end="", flush=True)
            local_bnk = voice_bnk_dir / bnk_name

            if not local_bnk.exists():
                if not extract_wem_from_pak(pak_file, bnk_path, local_bnk):
                    print(" [FAIL]")
                    continue

            wem_ids, name_map = parse_bnk_for_wem_ids(local_bnk)
            print(f" -> {len(wem_ids)} IDs")
            all_wem_ids.update(wem_ids)
            wem_to_name.update(name_map)

        if all_wem_ids:
            wem_matches = search_wem_by_ids(all_wem_ids)
            print(f"  Found {len(wem_matches)} audio files, {len(wem_to_name)} with names")

    if not wem_matches:
        return {}, {}

    if len(wem_matches) > MAX_EXTRACTED_PER_VOICE:
        wem_matches = wem_matches[:MAX_EXTRACTED_PER_VOICE]

    for pak_file, wem_path in wem_matches:
        wem_id = Path(wem_path).stem
        wem_locations[wem_id] = wem_path

    # Extract and convert
    converted = 0
    for pak_file, wem_path in wem_matches:
        wem_name = Path(wem_path).name
        local_wem = voice_wem_dir / wem_name
        local_wav = voice_wav_dir / f"{Path(wem_name).stem}.wav"

        if not local_wem.exists():
            if not extract_wem_from_pak(pak_file, wem_path, local_wem):
                continue

        if not local_wav.exists() and local_wem.exists():
            if convert_wem_to_wav(local_wem, local_wav):
                converted += 1

    print(f"  Converted {converted} audio files")

    # Cleanup bnk and wem
    for f in voice_bnk_dir.glob("*"):
        f.unlink()
    for f in voice_wem_dir.glob("*"):
        f.unlink()
    try:
        voice_bnk_dir.rmdir()
        voice_wem_dir.rmdir()
    except:
        pass

    return wem_locations, wem_to_name


def extract_voice(voice_name: str, wem_filter: Optional[Set[str]] = None) -> tuple:
    """Extract and convert audio for a specific voice using repak + wwiser.

    Args:
        voice_name: Name of the voice to extract (searches pak files for matching pattern)
        wem_filter: If provided, only extract these specific wem IDs

    Returns: (wem_locations: Dict[wem_id -> pak_path], wem_to_name: Dict[wem_id -> dialog_name])
    """
    print(f"\n=== {voice_name} ===")

    wem_locations: Dict[str, str] = {}
    wem_to_name: Dict[str, str] = {}

    voice_dir = EXTRACTED_AUDIO_DIR / voice_name
    voice_bnk_dir = voice_dir / "bnk"
    voice_wem_dir = voice_dir / "wem"
    voice_wav_dir = voice_dir / "wav"
    voice_bnk_dir.mkdir(parents=True, exist_ok=True)
    voice_wem_dir.mkdir(parents=True, exist_ok=True)
    voice_wav_dir.mkdir(parents=True, exist_ok=True)

    wem_matches = []

    if wem_filter:
        wem_matches = search_wem_by_ids(wem_filter)
    else:
        matches = search_pak_files(voice_name)
        bnk_matches = [(p, f) for p, f in matches if f.endswith('.bnk')]
        wem_matches = [(p, f) for p, f in matches if f.endswith('.wem')]

        if not bnk_matches and not wem_matches:
            return {}, {}

        all_wem_ids = set()
        if bnk_matches:
            print(f"  Found {len(bnk_matches)} soundbank(s)")

            for pak_file, bnk_path in bnk_matches:
                bnk_name = Path(bnk_path).name
                local_bnk = voice_bnk_dir / bnk_name

                if not local_bnk.exists():
                    if not extract_wem_from_pak(pak_file, bnk_path, local_bnk):
                        continue

                wem_ids, name_map = parse_bnk_for_wem_ids(local_bnk)
                all_wem_ids.update(wem_ids)
                wem_to_name.update(name_map)

        if all_wem_ids:
            wem_matches = search_wem_by_ids(all_wem_ids)
            print(f"  Found {len(wem_matches)} audio files, {len(wem_to_name)} with names")

    if not wem_matches:
        return {}, {}

    if len(wem_matches) > MAX_EXTRACTED_PER_VOICE:
        wem_matches = wem_matches[:MAX_EXTRACTED_PER_VOICE]

    for pak_file, wem_path in wem_matches:
        wem_id = Path(wem_path).stem
        wem_locations[wem_id] = wem_path

    # Extract and convert
    converted = 0
    for pak_file, wem_path in wem_matches:
        wem_name = Path(wem_path).name
        local_wem = voice_wem_dir / wem_name
        local_wav = voice_wav_dir / f"{Path(wem_name).stem}.wav"

        if not local_wem.exists():
            if not extract_wem_from_pak(pak_file, wem_path, local_wem):
                continue

        if not local_wav.exists() and local_wem.exists():
            if convert_wem_to_wav(local_wem, local_wav):
                converted += 1

    print(f"  Converted {converted} audio files")

    # Cleanup
    for f in voice_bnk_dir.glob("*"):
        f.unlink()
    for f in voice_wem_dir.glob("*"):
        f.unlink()
    try:
        voice_bnk_dir.rmdir()
        voice_wem_dir.rmdir()
    except:
        pass

    return wem_locations, wem_to_name


def combine_voice(voice_name: str, target_durations: List[float] = None, cleanup: bool = True) -> List[str]:
    """Combine extracted samples for a voice at multiple target durations.

    Returns: List of selected WAV filenames (stems, which correspond to wem IDs) from longest version
    """
    if target_durations is None:
        target_durations = TARGET_DURATIONS

    voice_wav_dir = EXTRACTED_AUDIO_DIR / voice_name / "wav"
    voice_dir = EXTRACTED_AUDIO_DIR / voice_name

    if not voice_wav_dir.exists():
        voice_wav_dir = voice_dir

    if not voice_wav_dir.exists():
        print(f"No extracted audio found for '{voice_name}'")
        return []

    wav_files = list(voice_wav_dir.glob("*.wav"))

    if not wav_files:
        print(f"No .wav files found in {voice_wav_dir}")
        return []

    all_selected_ids = []

    # Generate a reference file for each target duration
    for target_duration in sorted(target_durations):
        suffix = f"_{int(target_duration)}s"
        output_file = COMBINED_AUDIO_DIR / f"{voice_name}_reference{suffix}.wav"
        success, selected_files = combine_wav_files(wav_files, output_file, target_duration)

        if success:
            selected_ids = [f.stem for f in selected_files]
            if len(selected_ids) > len(all_selected_ids):
                all_selected_ids = selected_ids

    # Clean up source files after successful combination
    if all_selected_ids and cleanup:
        for wav_file in wav_files:
            wav_file.unlink()
        try:
            if voice_wav_dir != voice_dir:
                voice_wav_dir.rmdir()
            voice_dir.rmdir()
        except OSError:
            pass

    return all_selected_ids


def explore_all():
    """Process ALL voices: extract, combine, and create manifest of selected files.

    Gets voice names from existing manifest file.
    """
    if not MANIFEST_FILE.exists():
        print("No voice_manifest.json found. Cannot run explore without manifest.")
        print("The manifest file should be shipped with the mod.")
        return

    with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
        manifest_data = json.load(f)

    voice_names = set(manifest_data.get("voices", {}).keys())
    # Remove player variants - handle separately
    voice_names.discard("PlayerMale")
    voice_names.discard("PlayerFemale")

    print(f"Found {len(voice_names)} voices in manifest")

    print(f"\n{'='*60}")
    print(f"EXPLORE MODE: Processing {len(voice_names)} voices + Player (M/F)")
    print(f"{'='*60}")

    # Load existing manifest to preserve already-processed voices
    manifest = {"target_durations": TARGET_DURATIONS, "voices": {}}
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
                existing = json.load(f)
                manifest["voices"] = existing.get("voices", {})
        except:
            pass

    successful = 0
    failed = 0

    # Process Player as two separate voices (Male/Female)
    for player_variant, bnk_pattern in [("PlayerMale", "playermale"), ("PlayerFemale", "playerfemale")]:
        # Skip if references already exist AND in manifest
        ref_file = COMBINED_AUDIO_DIR / f"{player_variant}_reference_60s.wav"
        if ref_file.exists() and player_variant in manifest["voices"]:
            print(f"\n=== {player_variant} === [SKIP - already exists]")
            successful += 1
            continue

        print(f"\n=== {player_variant} ===")

        wem_locations, wem_to_name = extract_voice_by_bnk(bnk_pattern, player_variant)

        if not wem_locations:
            print(f"  [SKIP] No audio found")
            failed += 1
            continue

        selected_ids = combine_voice(player_variant, cleanup=True)

        if selected_ids:
            manifest["voices"][player_variant] = {
                "selected_wem_ids": selected_ids,
                "wem_paths": {wid: wem_locations.get(wid, "") for wid in selected_ids}
            }
            successful += 1
        else:
            failed += 1

    # Process all other voices
    for voice_name in sorted(voice_names):
        # Skip if references already exist AND in manifest
        ref_file = COMBINED_AUDIO_DIR / f"{voice_name}_reference_60s.wav"
        if ref_file.exists() and voice_name in manifest["voices"]:
            print(f"\n=== {voice_name} === [SKIP - already exists]")
            successful += 1
            continue

        print(f"\n=== {voice_name} ===")
        wem_locations, wem_to_name = extract_voice_by_bnk(voice_name.lower(), voice_name)

        if not wem_locations:
            failed += 1
            continue

        selected_ids = combine_voice(voice_name, cleanup=True)

        if selected_ids:
            # Record in manifest: only the files that were actually used
            manifest["voices"][voice_name] = {
                "selected_wem_ids": selected_ids,
                "wem_paths": {wid: wem_locations.get(wid, "") for wid in selected_ids}
            }
            successful += 1
        else:
            failed += 1

    # Write manifest
    print(f"\n{'='*60}")
    print(f"EXPLORE COMPLETE")
    print(f"{'='*60}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")

    if manifest["voices"]:
        with open(MANIFEST_FILE, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2)
        print(f"Manifest saved to: {MANIFEST_FILE}")
        print(f"\nTo rebuild from manifest later:")
        print(f"  python extract_voices.py --from-manifest")


def from_manifest():
    """Extract and combine voices using only the files specified in the manifest.

    Returns: (success: bool, error_message: str or None)
    """
    if not MANIFEST_FILE.exists():
        return False, f"Voice manifest not found. Ensure voice_manifest.json exists in the sonorus folder."

    with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    voices = manifest.get("voices", {})
    if not voices:
        return False, "Voice manifest contains no voices."

    print(f"\n{'='*60}")
    print(f"FROM MANIFEST: Processing {len(voices)} voices")
    print(f"Target durations: {TARGET_DURATIONS}")
    print(f"{'='*60}")

    errors = []
    successful = 0
    skipped = 0
    audio_pak_cache = None  # Cache which pak contains audio files

    for voice_name, voice_data in voices.items():
        # Check if reference already exists
        ref_file = COMBINED_AUDIO_DIR / f"{voice_name}_reference_60s.wav"
        if ref_file.exists():
            print(f"\n[SKIP] {voice_name} - reference already exists")
            skipped += 1
            continue

        print(f"\n{'='*60}")
        print(f"Processing: {voice_name}")
        print(f"{'='*60}")

        wem_paths = voice_data.get("wem_paths", {})
        if not wem_paths:
            print(f"[SKIP] No wem paths in manifest for {voice_name}")
            continue

        # Setup directories
        voice_dir = EXTRACTED_AUDIO_DIR / voice_name
        voice_wem_dir = voice_dir / "wem"
        voice_wav_dir = voice_dir / "wav"
        voice_wem_dir.mkdir(parents=True, exist_ok=True)
        voice_wav_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Extract wem files (sequential for pak caching)
        print(f"  Extracting {len(wem_paths)} audio files...")
        wem_files_to_convert = []

        for wem_id, wem_pak_path in wem_paths.items():
            if not wem_pak_path:
                continue

            local_wem = voice_wem_dir / f"{wem_id}.wem"
            local_wav = voice_wav_dir / f"{wem_id}.wav"

            # Skip if already converted
            if local_wav.exists():
                continue

            # Extract wem file (use cached pak if available)
            if not local_wem.exists():
                paks_to_try = [audio_pak_cache] if audio_pak_cache else []
                paks_to_try.extend([p for p in get_pak_files() if p != audio_pak_cache])

                for pak_file in paks_to_try:
                    if extract_wem_from_pak(pak_file, wem_pak_path, local_wem):
                        audio_pak_cache = pak_file
                        break

            if local_wem.exists():
                wem_files_to_convert.append((local_wem, local_wav))

        # Step 2: Convert wem to wav (parallel)
        converted = 0
        if wem_files_to_convert:
            print(f"  Converting {len(wem_files_to_convert)} files (parallel)...")
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(convert_wem_to_wav, wem, wav): (wem, wav)
                    for wem, wav in wem_files_to_convert
                }
                for future in as_completed(futures):
                    if future.result():
                        converted += 1

        print(f"  Converted {converted} audio files")

        # Cleanup wem files first (before combine, so parent can be removed)
        for f in voice_wem_dir.glob("*"):
            f.unlink()
        try:
            voice_wem_dir.rmdir()
        except:
            pass

        # Combine (uses all target durations) - this cleans wav files and voice_dir
        result = combine_voice(voice_name, cleanup=True)

        # Final cleanup - ensure voice_dir is removed if still exists
        try:
            voice_dir.rmdir()
        except:
            pass

        if result:
            successful += 1
        else:
            errors.append(f"Failed to combine voice: {voice_name}")

    print(f"\n{'='*60}")
    print("FROM MANIFEST COMPLETE")
    print(f"{'='*60}")
    print(f"Successful: {successful}")
    print(f"Skipped (existing): {skipped}")
    print(f"Errors: {len(errors)}")

    # Cleanup extracted_audio folder if empty
    try:
        if EXTRACTED_AUDIO_DIR.exists():
            # Remove any remaining empty subdirectories
            for subdir in list(EXTRACTED_AUDIO_DIR.iterdir()):
                if subdir.is_dir():
                    try:
                        subdir.rmdir()
                    except:
                        pass
            # Remove the main folder if empty
            EXTRACTED_AUDIO_DIR.rmdir()
            print(f"[CLEANUP] Removed extracted_audio folder")
    except:
        pass  # Folder not empty or other error

    if errors:
        return False, "; ".join(errors)
    return True, None


def search_audio(pattern: str):
    """Search for audio files in pak matching a pattern."""
    print(f"\n=== Searching for '{pattern}' ===\n")

    matches = search_pak_files(pattern)

    if not matches:
        print("No matching audio files found.")
        return

    print(f"Found {len(matches)} matching files:\n")

    # Group by pak file
    by_pak = {}
    for pak, path in matches:
        if pak not in by_pak:
            by_pak[pak] = []
        by_pak[pak].append(path)

    for pak, paths in by_pak.items():
        print(f"In {pak.name}:")
        for path in paths[:20]:  # Limit output
            print(f"  {path}")
        if len(paths) > 20:
            print(f"  ... and {len(paths) - 20} more")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Voice Sample Extraction Tool")
    parser.add_argument("--full", metavar="VOICE", help="Extract + convert + combine (all-in-one)")
    parser.add_argument("--extract", metavar="VOICE", help="Extract samples for a voice")
    parser.add_argument("--combine", metavar="VOICE", help="Combine samples for a voice")
    parser.add_argument("--search", metavar="PATTERN", help="Search for audio files in pak")
    parser.add_argument("--explore", action="store_true",
                        help="Process ALL voices, combine, and create manifest")
    parser.add_argument("--from-manifest", action="store_true",
                        help="Rebuild voices using only files from manifest")
    parser.add_argument("--keep-sources", action="store_true",
                        help="Keep source WAV files after combining (default: delete them)")

    args = parser.parse_args()

    # Check tools
    missing = check_tools()
    needs_tools = args.extract or args.search or args.full or args.explore or getattr(args, 'from_manifest', False)
    if missing and needs_tools:
        for m in missing:
            print(f"[ERROR] {m}")
        return 1

    cleanup = not args.keep_sources

    if args.search:
        search_audio(args.search)
    elif args.explore:
        explore_all()
    elif getattr(args, 'from_manifest', False):
        success, error = from_manifest()
        if not success:
            print(f"[ERROR] {error}")
            return 1
    elif args.full:
        extract_voice(args.full)
        combine_voice(args.full, cleanup=cleanup)
    elif args.extract:
        extract_voice(args.extract)
    elif args.combine:
        combine_voice(args.combine, cleanup=cleanup)
    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    sys.exit(main())
