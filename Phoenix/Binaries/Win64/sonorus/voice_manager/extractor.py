"""
Voice extraction wrapper for multi-language support.

Wraps extract_voices.py functions to support extracting voice samples
for any game language.
"""

import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

# Path setup
VOICE_MANAGER_DIR = Path(__file__).parent
SONORUS_DIR = VOICE_MANAGER_DIR.parent
SETUP_DIR = SONORUS_DIR / "setup"
DATA_DIR = SONORUS_DIR / "data"
GAME_DIR = SONORUS_DIR.parent.parent.parent  # Phoenix folder
PAKS_DIR = GAME_DIR / "Content" / "Paks"
EXTRACTED_AUDIO_DIR = SONORUS_DIR / "extracted_audio"

# Tool paths
BIN_DIR = SONORUS_DIR / "bin"
REPAK_EXE = BIN_DIR / "repak.exe"
WWISER = BIN_DIR / "wwiser.pyz"
VGMSTREAM_CLI = BIN_DIR / "vgmstream" / "vgmstream-cli.exe"

# Max samples per voice to prevent excessive extraction
MAX_EXTRACTED_PER_VOICE = 500


def check_tools() -> List[str]:
    """Verify required tools exist. Returns list of error messages."""
    missing = []
    if not REPAK_EXE.exists():
        missing.append("repak.exe not found in bin/ folder")
    if not WWISER.exists():
        missing.append("wwiser.pyz not found in bin/ folder")
    if not VGMSTREAM_CLI.exists():
        missing.append("vgmstream-cli.exe not found in bin/vgmstream/ folder")
    if not PAKS_DIR.exists():
        missing.append("Game Paks directory not found")
    return missing


def get_pak_files() -> List[Path]:
    """Get all pak files in the Paks directory."""
    if not PAKS_DIR.exists():
        return []
    return sorted(PAKS_DIR.glob("*.pak"))


def search_pak_files(pattern: str, lang_path: str = None) -> List[tuple]:
    """
    Search for files matching pattern in pak files.

    Args:
        pattern: Search pattern (case-insensitive)
        lang_path: Optional language path filter (e.g., "de-de")

    Returns:
        List of (pak_file, file_path) tuples
    """
    if not REPAK_EXE.exists():
        print(f"[VoiceExtract] repak.exe not found at {REPAK_EXE}")
        return []

    matches = []
    pattern_lower = pattern.lower()
    pak_files = get_pak_files()
    print(f"[VoiceExtract] Searching {len(pak_files)} pak files for pattern '{pattern}'")

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
                    # Filter by language path if specified
                    if lang_path and f"/{lang_path}/" not in line.lower():
                        continue
                    if line.endswith('.wem') or line.endswith('.bnk'):
                        matches.append((pak_file, line))
                        if len(matches) <= 3:
                            print(f"[VoiceExtract] Found: {line}")

        except Exception as e:
            print(f"[VoiceExtract] Error searching {pak_file}: {e}")

    return matches


def search_wem_by_ids(wem_ids: Set[str], lang_path: str = None) -> List[tuple]:
    """Search pak files for .wem files matching the given IDs."""
    if not wem_ids:
        return []

    matches = []
    pak_files = get_pak_files()
    print(f"[VoiceExtract] Searching {len(pak_files)} pak files for {len(wem_ids)} WEM IDs (lang filter: {lang_path})")

    # Show a few sample WEM IDs we're looking for
    sample_ids = list(wem_ids)[:3]
    print(f"[VoiceExtract] Sample WEM IDs: {sample_ids}")

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

            wem_lines = [l.strip() for l in result.stdout.splitlines() if l.strip().endswith('.wem')]

            # Show sample paths from first pak with wem files
            if wem_lines and not matches:
                print(f"[VoiceExtract] Sample WEM paths from {pak_file.name}:")
                for sample in wem_lines[:3]:
                    print(f"[VoiceExtract]   {sample}")

            for line in wem_lines:
                # Filter by language if specified
                if lang_path and f"/{lang_path}/" not in line.lower():
                    continue
                wem_name = Path(line).stem
                if wem_name in wem_ids:
                    matches.append((pak_file, line))

        except Exception as e:
            print(f"[VoiceExtract] Error searching {pak_file}: {e}")

    return matches


def parse_bnk_for_wem_ids(bnk_path: Path) -> Set[str]:
    """Parse a .bnk file with wwiser to extract referenced .wem IDs."""
    if not bnk_path.exists():
        print(f"[VoiceExtract] BNK file does not exist: {bnk_path}")
        return set()

    print(f"[VoiceExtract] Parsing BNK: {bnk_path.name}")

    try:
        # Use wwiser with -d txt flag to generate text dump
        txt_path = bnk_path.with_suffix('.bnk.txt')

        result = subprocess.run(
            [sys.executable, str(WWISER), str(bnk_path), "-d", "txt"],
            capture_output=True,
            text=True,
            cwd=str(bnk_path.parent),
            timeout=120
        )

        if result.returncode != 0:
            print(f"[VoiceExtract] wwiser failed (code {result.returncode})")
            if result.stderr:
                print(f"[VoiceExtract] stderr: {result.stderr[:500]}")
            return set()

        # Check for generated text dump
        if not txt_path.exists():
            print(f"[VoiceExtract] No text dump generated at {txt_path.name}")
            return set()

        print(f"[VoiceExtract] Found dump: {txt_path.name}")

        # Parse sourceID values from text dump
        import re
        content = txt_path.read_text(encoding='utf-8', errors='ignore')

        # Extract WEM IDs using the pattern from extract_voice_simple.py
        wem_ids = set(re.findall(r'sourceID = (\d+)', content))

        print(f"[VoiceExtract] Extracted {len(wem_ids)} WEM IDs from {txt_path.name}")

        # Cleanup text dump
        txt_path.unlink()

        return wem_ids

    except subprocess.TimeoutExpired:
        print(f"[ERROR] Parsing bnk timed out")
        return set()
    except Exception as e:
        print(f"[ERROR] Failed to parse bnk: {e}")
        return set()


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


def convert_wem_to_wav(wem_path: Path, wav_path: Path) -> bool:
    """Convert a .wem file to .wav using vgmstream-cli."""
    if not VGMSTREAM_CLI.exists():
        return False

    try:
        result = subprocess.run(
            [str(VGMSTREAM_CLI), "-o", str(wav_path), str(wem_path)],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


def _extract_and_convert_one_wem(wem_id: str, lang_path: str, wem_dir: Path, wav_dir: Path, pak_files: List[Path]) -> dict:
    """
    Extract and convert a single WEM file (worker function for ThreadPoolExecutor).

    Returns:
        {"success": bool, "wem_id": str, "cached": bool, "error": str or None}
    """
    wem_filename = f"{wem_id}.wem"
    wem_pak_path = f"Phoenix/Content/WwiseAudio/windows/{lang_path}/{wem_filename}"
    local_wem = wem_dir / wem_filename
    local_wav = wav_dir / f"{wem_id}.wav"

    # Skip if already converted
    if local_wav.exists():
        return {"success": True, "wem_id": wem_id, "cached": True}

    # Try to extract WEM from paks
    extracted = False
    for pak_file in pak_files:
        try:
            with open(local_wem, 'wb') as outfile:
                result = subprocess.run(
                    [str(REPAK_EXE), "get", str(pak_file), wem_pak_path],
                    stdout=outfile,
                    stderr=subprocess.PIPE,
                    cwd=str(SONORUS_DIR),
                    timeout=30
                )

            if local_wem.exists() and local_wem.stat().st_size > 0:
                extracted = True
                break
            else:
                if local_wem.exists():
                    local_wem.unlink()
        except:
            pass

    if not extracted:
        return {"success": False, "wem_id": wem_id, "error": "Not found in paks"}

    # Convert to WAV
    if convert_wem_to_wav(local_wem, local_wav):
        return {"success": True, "wem_id": wem_id, "cached": False}
    else:
        return {"success": False, "wem_id": wem_id, "error": "Conversion failed"}


def extract_voice_batch(voice_name: str, lang_path: str, offset: int = 0, batch_size: int = 200, progress_callback=None) -> dict:
    """
    Extract and convert a batch of audio samples for a voice.

    Args:
        voice_name: Voice ID (e.g., "SebastianSallow")
        lang_path: Language path (e.g., "de-de", "fr-fr")
        offset: Starting index in the WEM ID list
        batch_size: Number of samples to extract in this batch
        progress_callback: Optional callback(current, total, message) for progress updates

    Returns:
        {
            "success": bool,
            "sample_count": int,
            "total_available": int,
            "has_more": bool,
            "error": str or None
        }
    """
    print(f"[VoiceExtract] Starting batch extraction for {voice_name} ({lang_path}) - offset={offset}, batch_size={batch_size}")

    # Check tools
    missing = check_tools()
    if missing:
        print(f"[VoiceExtract] Missing tools: {missing}")
        return {"success": False, "sample_count": 0, "total_available": 0, "has_more": False, "error": "; ".join(missing)}

    # Setup output directories
    voice_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name
    bnk_dir = voice_dir / "bnk"
    wem_dir = voice_dir / "wem"
    wav_dir = voice_dir / "wav"

    bnk_dir.mkdir(parents=True, exist_ok=True)
    wem_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)

    # Search for language-specific soundbank
    bnk_name = f"dialogue_{voice_name.lower()}.bnk"
    bnk_pak_path = f"Phoenix/Content/WwiseAudio/windows/{lang_path}/{bnk_name}"
    local_bnk = bnk_dir / bnk_name

    # Extract or reuse BNK if already exists
    if not local_bnk.exists():
        print(f"[VoiceExtract] Looking for BNK: {bnk_pak_path}")
        bnk_found = False
        pak_files = get_pak_files()

        for pak_file in pak_files:
            try:
                with open(local_bnk, 'wb') as outfile:
                    result = subprocess.run(
                        [str(REPAK_EXE), "get", str(pak_file), bnk_pak_path],
                        stdout=outfile,
                        stderr=subprocess.PIPE,
                        cwd=str(SONORUS_DIR),
                        timeout=60
                    )

                if local_bnk.exists() and local_bnk.stat().st_size > 0:
                    print(f"[VoiceExtract] Found BNK in {pak_file.name} ({local_bnk.stat().st_size / 1024:.1f} KB)")
                    bnk_found = True
                    break
                else:
                    if local_bnk.exists():
                        local_bnk.unlink()
            except Exception as e:
                print(f"[VoiceExtract] Error extracting from {pak_file.name}: {e}")

        if not bnk_found:
            return {"success": False, "sample_count": 0, "total_available": 0, "has_more": False, "error": f"No soundbank found for {voice_name} in {lang_path}"}

    # Parse BNK to get WEM IDs (reuse if already parsed)
    wem_ids = parse_bnk_for_wem_ids(local_bnk)
    print(f"[VoiceExtract] Found {len(wem_ids)} total WEM IDs from soundbank")

    if not wem_ids:
        _cleanup_dir(bnk_dir)
        return {"success": False, "sample_count": 0, "total_available": 0, "has_more": False, "error": "No WEM IDs found in soundbank"}

    # Get batch of WEM IDs
    all_wem_ids = list(wem_ids)
    total_available = len(all_wem_ids)
    batch_wem_ids = all_wem_ids[offset:offset + batch_size]
    has_more = (offset + batch_size) < total_available

    if not batch_wem_ids:
        return {"success": True, "sample_count": 0, "total_available": total_available, "has_more": False, "error": None}

    print(f"[VoiceExtract] Extracting batch: {len(batch_wem_ids)} samples (offset {offset}, total {total_available})")

    # Extract and convert WEMs in parallel
    pak_files = get_pak_files()
    converted = 0
    total_wems = len(batch_wem_ids)
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        future_map = {}

        for wem_id in batch_wem_ids:
            future = executor.submit(
                _extract_and_convert_one_wem,
                wem_id,
                lang_path,
                wem_dir,
                wav_dir,
                pak_files
            )
            futures.append(future)
            future_map[future] = wem_id

        completed = 0
        for future in as_completed(futures):
            result = future.result()
            wem_id = future_map[future]

            with progress_lock:
                completed += 1
                if result.get("success"):
                    converted += 1

                if progress_callback:
                    wem_filename = f"{wem_id}.wem"
                    status = "cached" if result.get("cached") else "converted"
                    progress_callback(completed, total_wems, f"{status.capitalize()}: {wem_filename}")

    # Final progress update
    if progress_callback:
        progress_callback(total_wems, total_wems, f"Batch complete: {converted} samples")

    # Cleanup intermediate files
    _cleanup_dir(wem_dir)

    print(f"[VoiceExtract] Batch done! Converted {converted} samples")
    return {
        "success": True,
        "sample_count": converted,
        "total_available": total_available,
        "has_more": has_more,
        "error": None
    }


def extract_voice_for_language(voice_name: str, lang_path: str, progress_callback=None) -> dict:
    """
    Extract and convert audio samples for a voice in a specific language.
    Uses parallel processing (max 4 workers) for extraction and conversion.

    Args:
        voice_name: Voice ID (e.g., "SebastianSallow")
        lang_path: Language path (e.g., "de-de", "fr-fr")
        progress_callback: Optional callback(current, total, message) for progress updates

    Returns:
        {"success": bool, "sample_count": int, "error": str or None}
    """
    print(f"[VoiceExtract] Starting extraction for {voice_name} ({lang_path})")

    # Check tools
    missing = check_tools()
    if missing:
        print(f"[VoiceExtract] Missing tools: {missing}")
        return {"success": False, "sample_count": 0, "error": "; ".join(missing)}

    print(f"[VoiceExtract] Tools OK. PAKS_DIR: {PAKS_DIR}")

    # Setup output directories
    voice_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name
    bnk_dir = voice_dir / "bnk"
    wem_dir = voice_dir / "wem"
    wav_dir = voice_dir / "wav"

    bnk_dir.mkdir(parents=True, exist_ok=True)
    wem_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)

    # Search for language-specific soundbank
    # Pattern: Phoenix/Content/WwiseAudio/windows/{lang}/dialogue_{voice}.bnk
    bnk_name = f"dialogue_{voice_name.lower()}.bnk"
    bnk_pak_path = f"Phoenix/Content/WwiseAudio/windows/{lang_path}/{bnk_name}"

    print(f"[VoiceExtract] Looking for BNK: {bnk_pak_path}")

    # Try to find and extract the BNK
    local_bnk = bnk_dir / bnk_name
    bnk_found = False

    pak_files = get_pak_files()
    for pak_file in pak_files:
        try:
            with open(local_bnk, 'wb') as outfile:
                result = subprocess.run(
                    [str(REPAK_EXE), "get", str(pak_file), bnk_pak_path],
                    stdout=outfile,
                    stderr=subprocess.PIPE,
                    cwd=str(SONORUS_DIR),
                    timeout=60
                )

            if local_bnk.exists() and local_bnk.stat().st_size > 0:
                print(f"[VoiceExtract] Found BNK in {pak_file.name} ({local_bnk.stat().st_size / 1024:.1f} KB)")
                bnk_found = True
                break
            else:
                if local_bnk.exists():
                    local_bnk.unlink()
        except Exception as e:
            print(f"[VoiceExtract] Error extracting from {pak_file.name}: {e}")

    if not bnk_found:
        return {"success": False, "sample_count": 0, "error": f"No soundbank found for {voice_name} in {lang_path}"}

    # Parse BNK to get WEM IDs
    wem_ids = parse_bnk_for_wem_ids(local_bnk)

    print(f"[VoiceExtract] Found {len(wem_ids)} WEM IDs from soundbank")

    if not wem_ids:
        _cleanup_dir(bnk_dir)
        return {"success": False, "sample_count": 0, "error": "No WEM IDs found in soundbank"}

    # Limit extraction count
    wem_ids_to_extract = list(wem_ids)
    if len(wem_ids_to_extract) > MAX_EXTRACTED_PER_VOICE:
        wem_ids_to_extract = wem_ids_to_extract[:MAX_EXTRACTED_PER_VOICE]

    # Extract and convert WEMs in parallel using language-specific paths
    print(f"[VoiceExtract] Extracting and converting {len(wem_ids_to_extract)} files from {lang_path} (parallel, max 4 workers)...")

    converted = 0
    total_wems = len(wem_ids_to_extract)
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=8) as executor:
        # Submit all jobs
        futures = []
        for wem_id in wem_ids_to_extract:
            future = executor.submit(
                _extract_and_convert_one_wem,
                wem_id,
                lang_path,
                wem_dir,
                wav_dir,
                pak_files
            )
            futures.append((future, wem_id))

        # Process results as they complete
        completed = 0
        for future, wem_id in futures:
            result = future.result()

            # Thread-safe progress update
            with progress_lock:
                completed += 1
                if result.get("success"):
                    converted += 1

                # Report progress
                if progress_callback:
                    wem_filename = f"{wem_id}.wem"
                    status = "cached" if result.get("cached") else "converted"
                    progress_callback(completed, total_wems, f"{status.capitalize()}: {wem_filename}")

    # Final progress update
    if progress_callback:
        progress_callback(total_wems, total_wems, "Conversion complete")

    # Cleanup intermediate files
    _cleanup_dir(bnk_dir)
    _cleanup_dir(wem_dir)

    print(f"[VoiceExtract] Done! Converted {converted} samples for {voice_name}")
    return {"success": True, "sample_count": converted, "error": None}


def _cleanup_dir(dir_path: Path):
    """Remove all files in a directory and the directory itself."""
    if not dir_path.exists():
        return
    for f in dir_path.glob("*"):
        try:
            f.unlink()
        except:
            pass
    try:
        dir_path.rmdir()
    except:
        pass
