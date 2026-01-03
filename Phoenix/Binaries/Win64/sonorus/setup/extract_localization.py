"""
Extract localized text from Hogwarts Legacy pak files.
Creates JSON lookup tables for subtitles and main localization.

Usage:
  python extract_localization.py          # Extract subtitles (SUB)
  python extract_localization.py --main   # Extract main localization (MAIN)
  python extract_localization.py --both   # Extract both
  python extract_localization.py --language DE_DE  # Extract German

Supported languages: EN_US, DE_DE, ES_ES, ES_MX, FR_FR, IT_IT, JA_JP, KO_KR, PL_PL, PT_BR, RU_RU, ZH_CN, ZH_TW, AR_AE
"""
import os
import sys
import json
import subprocess
import argparse

# Path setup - this script is in sonorus/setup/, so go up one level to sonorus/
SETUP_DIR = os.path.dirname(os.path.abspath(__file__))
SONORUS_DIR = os.path.dirname(SETUP_DIR)
DATA_DIR = os.path.join(SONORUS_DIR, "data")
GAME_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SONORUS_DIR)))  # Phoenix folder
PAKS_DIR = os.path.join(GAME_DIR, "Content", "Paks")


def get_lang_code(language=None):
    """Convert language code to pak file format (EN_US -> enUS)"""
    lang = language or "EN_US"
    # Convert EN_US -> enUS (lowercase first part, keep second uppercase)
    parts = lang.split("_")
    if len(parts) == 2:
        return parts[0].lower() + parts[1].upper()
    return "enUS"


def get_file_configs(lang_code):
    """Get file configurations for the given language code."""
    return {
        "SUB": {
            "bin_path": f"Phoenix/Content/Localization/WIN64/SUB-{lang_code}.bin",
            "extracted_bin": os.path.join(SONORUS_DIR, f"SUB-{lang_code}.bin"),
            "output_json": os.path.join(DATA_DIR, "subtitles.json"),
            "description": "Subtitles"
        },
        "MAIN": {
            "bin_path": f"Phoenix/Content/Localization/WIN64/MAIN-{lang_code}.bin",
            "extracted_bin": os.path.join(SONORUS_DIR, f"MAIN-{lang_code}.bin"),
            "output_json": os.path.join(DATA_DIR, "main_localization.json"),
            "description": "Main Localization"
        }
    }


# Tools (in sonorus/bin/)
BIN_DIR = os.path.join(SONORUS_DIR, "bin")
REPAK_EXE = os.path.join(BIN_DIR, "repak.exe")
PARSELTONGUE_EXE = os.path.join(BIN_DIR, "parseltongue.exe")

# Target pak file
PAK_FILE = os.path.join(PAKS_DIR, "pakchunk0-WindowsNoEditor.pak")



def check_tools():
    """Verify required tools exist. Returns list of human-readable error messages."""
    missing = []
    if not os.path.exists(REPAK_EXE):
        missing.append("Required tool 'repak.exe' is missing. Ensure the bin/ folder contains all required tools.")
    if not os.path.exists(PARSELTONGUE_EXE):
        missing.append("Required tool 'parseltongue.exe' is missing. Ensure the bin/ folder contains all required tools.")
    if not os.path.exists(PAK_FILE):
        missing.append("Game files not found. Verify Hogwarts Legacy is installed correctly and the pak file exists.")
    return missing


def extract_with_repak(config):
    """Extract .bin file using repak get (single file extraction)"""
    bin_path = config["bin_path"]
    extracted_bin = config["extracted_bin"]

    print(f"[INFO] Extracting single file from pak using repak...")
    print(f"[INFO] Pak: {PAK_FILE}")
    print(f"[INFO] Target: {bin_path}")

    try:
        # repak get <pak> <file_path> outputs to stdout
        # We redirect stdout (binary) to the output file
        with open(extracted_bin, 'wb') as outfile:
            result = subprocess.run(
                [REPAK_EXE, "get", PAK_FILE, bin_path],
                stdout=outfile,
                stderr=subprocess.PIPE,
                cwd=SONORUS_DIR,
                timeout=300
            )

        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8', errors='replace')
            print(f"[ERROR] repak get failed: {error_msg}")
            # Clean up empty/failed file
            if os.path.exists(extracted_bin):
                os.remove(extracted_bin)
            return False, f"Failed to extract from pak: {error_msg}"

        # Verify file was created and has content
        if os.path.exists(extracted_bin) and os.path.getsize(extracted_bin) > 0:
            size_kb = os.path.getsize(extracted_bin) / 1024
            print(f"[SUCCESS] Extracted: {extracted_bin} ({size_kb:.1f} KB)")
            return True, None
        else:
            print(f"[ERROR] Extraction produced empty file")
            return False, "Extraction produced an empty file. The localization file may not exist for this language."

    except subprocess.TimeoutExpired:
        if os.path.exists(extracted_bin):
            os.remove(extracted_bin)
        return False, "Operation timed out. The game files may be too large or the system is busy."
    except PermissionError:
        return False, "Cannot write files. Try running as administrator or check folder permissions."
    except Exception as e:
        print(f"[ERROR] repak extraction failed: {e}")
        return False, f"Extraction failed: {str(e)}"


def run_parseltongue(config, file_type, lang_code):
    """Run parseltongue.exe to convert bin to JSON"""
    extracted_bin = config["extracted_bin"]
    output_json = config["output_json"]

    if not os.path.exists(extracted_bin):
        return False, f"BIN file not found: {extracted_bin}"

    print(f"[INFO] Running parseltongue on {extracted_bin}...")

    try:
        # parseltongue auto-detects .bin and converts to JSON
        # Output is <filename>-modified.json in same directory
        result = subprocess.run(
            [PARSELTONGUE_EXE, extracted_bin],
            capture_output=True,
            text=True,
            cwd=SONORUS_DIR,
            timeout=120
        )

        if result.returncode != 0:
            return False, f"parseltongue conversion failed: {result.stderr}"

        # parseltongue outputs {TYPE}-{lang}-modified.json
        modified_json = os.path.join(SONORUS_DIR, f"{file_type}-{lang_code}-modified.json")
        if os.path.exists(modified_json):
            if os.path.exists(output_json):
                os.remove(output_json)
            os.rename(modified_json, output_json)
            print(f"[SUCCESS] Created: {output_json}")
            return True, None
        else:
            return False, f"Expected output not found: {modified_json}"

    except subprocess.TimeoutExpired:
        return False, "Conversion timed out. The file may be too large."
    except Exception as e:
        return False, f"Failed to run parseltongue: {str(e)}"


def verify_output(config, search_term=None):
    """Verify and show sample of the output JSON"""
    output_json = config["output_json"]

    if not os.path.exists(output_json):
        return False

    try:
        with open(output_json, 'r', encoding='utf-8') as f:
            data = json.load(f)

        print(f"\n[INFO] Loaded {len(data) if isinstance(data, (list, dict)) else 'unknown'} entries")

        # Show sample
        print("\n[SAMPLE] First few entries:")
        if isinstance(data, dict):
            for i, (key, value) in enumerate(data.items()):
                if i >= 5:
                    break
                print(f"  {key}: {str(value)[:80]}...")
        elif isinstance(data, list):
            for i, entry in enumerate(data[:5]):
                print(f"  {entry}")

        # Search for specific term if provided
        if search_term:
            print(f"\n[CHECK] Looking for '{search_term}' entries...")
            found = 0
            if isinstance(data, dict):
                for key, value in data.items():
                    if search_term.lower() in str(key).lower() or search_term.lower() in str(value).lower():
                        print(f"  {key}: {str(value)[:100]}")
                        found += 1
                        if found >= 10:
                            print(f"  ... and more")
                            break
            if found == 0:
                print(f"  No entries found containing '{search_term}'")

        return True

    except Exception as e:
        print(f"[ERROR] Failed to parse JSON: {e}")
        return False


def extract_localization(file_type, lang_code, file_configs, search_term=None):
    """Extract a specific localization file (SUB or MAIN).

    Returns: (success: bool, error_message: str or None)
    """
    config = file_configs[file_type]
    extracted_bin = config["extracted_bin"]
    output_json = config["output_json"]

    print(f"\n{'='*60}")
    print(f"Extracting {config['description']} ({file_type})")
    print(f"{'='*60}")

    # Check if bin already extracted
    if os.path.exists(extracted_bin):
        print(f"[OK] BIN file already exists: {extracted_bin}")
    else:
        # Extract with repak
        success, error = extract_with_repak(config)
        if not success:
            return False, error

    # Run parseltongue
    success, error = run_parseltongue(config, file_type, lang_code)
    if not success:
        return False, error

    # Clean up intermediate .bin file
    if os.path.exists(extracted_bin):
        os.remove(extracted_bin)
        print(f"[CLEANUP] Removed intermediate file: {extracted_bin}")

    # Verify output
    verify_output(config, search_term)

    print(f"\n[DONE] {config['description']} extraction complete!")
    print(f"Output: {output_json}")
    return True, None


def run_extraction(language=None, extract_sub=True, extract_main=True, search_term=None):
    """Run the extraction process. Returns (success, error_message)."""
    lang_code = get_lang_code(language)
    file_configs = get_file_configs(lang_code)

    print("=" * 60)
    print("Hogwarts Legacy Localization Extractor")
    print("=" * 60)
    print(f"[LANG] Language: {lang_code}")

    # Check tools
    missing = check_tools()
    if missing:
        for m in missing:
            print(f"[ERROR] {m}")
        return False, missing[0]  # Return first error

    print(f"[OK] repak.exe found")
    print(f"[OK] parseltongue.exe found")
    print(f"[OK] Pak file found: {PAK_FILE}")

    errors = []

    if extract_sub:
        success, error = extract_localization("SUB", lang_code, file_configs, search_term)
        if not success:
            errors.append(f"Subtitles: {error}")

    if extract_main:
        success, error = extract_localization("MAIN", lang_code, file_configs, search_term)
        if not success:
            errors.append(f"Main: {error}")

    if errors:
        return False, "; ".join(errors)

    return True, None


def main():
    parser = argparse.ArgumentParser(description="Extract localization data from Hogwarts Legacy")
    parser.add_argument("--main", action="store_true", help="Extract MAIN localization (locations, UI, etc.)")
    parser.add_argument("--sub", action="store_true", help="Extract SUB subtitles (default)")
    parser.add_argument("--both", action="store_true", help="Extract both MAIN and SUB")
    parser.add_argument("--language", type=str, help="Language code (e.g., EN_US, DE_DE)")
    parser.add_argument("--search", type=str, help="Search for entries containing this term")
    args = parser.parse_args()

    # Determine what to extract
    extract_sub = args.sub or args.both or (not args.main and not args.both)  # Default to SUB
    extract_main = args.main or args.both

    success, error = run_extraction(
        language=args.language,
        extract_sub=extract_sub,
        extract_main=extract_main,
        search_term=args.search
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
