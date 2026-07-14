"""Toggle SonorusMod enabled/disabled in ue4ss Mods.txt and Mods.json."""

import argparse
import json
import os
import sys

MOD_NAME = "SonorusMod"


def toggle_mods_txt(path, enable):
    """Set 'SonorusMod : 1' or 'SonorusMod : 0' in Mods.txt."""
    value = "1" if enable else "0"
    lines = []
    found = False

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n\r")
                # Match "SonorusMod : 0" or "SonorusMod : 1" (flexible whitespace)
                parts = stripped.split(":")
                if len(parts) == 2 and parts[0].strip() == MOD_NAME:
                    lines.append(f"{MOD_NAME} : {value}")
                    found = True
                else:
                    lines.append(stripped)

    if not found:
        if enable:
            lines.append(f"{MOD_NAME} : {value}")
        else:
            # Not present and disabling — nothing to do
            return

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def toggle_mods_json(path, enable):
    """Set mod_enabled for SonorusMod in Mods.json."""
    mods = []

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            mods = json.load(f)

    found = False
    for mod in mods:
        if mod.get("mod_name") == MOD_NAME:
            mod["mod_enabled"] = enable
            found = True
            break

    if not found:
        if enable:
            mods.append({"mod_name": MOD_NAME, "mod_enabled": True})
        else:
            # Not present and disabling — nothing to do
            return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(mods, f, indent=4)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Toggle SonorusMod on/off")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    parser.add_argument("--mods-dir", required=True, help="Path to ue4ss/Mods directory")
    args = parser.parse_args()

    enable = args.enable
    mods_dir = args.mods_dir

    if not os.path.isdir(mods_dir):
        print(f"ERROR: Mods directory not found: {mods_dir}", file=sys.stderr)
        sys.exit(1)

    txt_path = os.path.join(mods_dir, "mods.txt")
    json_path = os.path.join(mods_dir, "mods.json")

    toggle_mods_txt(txt_path, enable)
    toggle_mods_json(json_path, enable)

    state = "enabled" if enable else "disabled"
    print(f"SonorusMod {state}.")


if __name__ == "__main__":
    main()
