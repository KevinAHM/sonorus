#!/bin/bash
# ============================================
# Sonorus Mod - Linux/Proton Server Launcher
# ============================================
# Called automatically by start_server.bat under Wine/Proton.
# Can also be run manually: bash start_server.sh
# ============================================

set -e
cd "$(dirname "$(readlink -f "$0")")"

VENV_DIR="./venv"

# --- Create/activate virtual environment ---

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
PYTHON="$VENV_DIR/bin/python"

# --- Download dependencies if missing ---

if command -v curl &>/dev/null; then
    dl() { curl -fSL -o "$2" "$1"; }
elif command -v wget &>/dev/null; then
    dl() { wget -q -O "$2" "$1"; }
else
    echo "ERROR: curl or wget required."
    exit 1
fi

mkdir -p bin bin/vgmstream models

[ ! -f "bin/parseltongue.exe" ] && {
    echo "Downloading parseltongue..."
    dl "https://github.com/insomnious/parseltongue/releases/download/v0.2.3/parseltongue-0.2.3.zip" "bin/parseltongue.zip"
    unzip -qo "bin/parseltongue.zip" -d "bin/parseltongue_temp"
    mv "bin/parseltongue_temp/parseltongue.exe" "bin/parseltongue.exe"
    rm -rf "bin/parseltongue_temp" "bin/parseltongue.zip"
}

[ ! -f "bin/wwiser.pyz" ] && {
    echo "Downloading wwiser..."
    dl "https://github.com/bnnm/wwiser/releases/download/v20250928/wwiser.pyz" "bin/wwiser.pyz"
}

[ ! -f "bin/wwnames.db3" ] && {
    echo "Downloading wwnames.db3..."
    dl "https://github.com/bnnm/wwiser/releases/download/v20250928/wwnames.db3" "bin/wwnames.db3"
}

[ ! -f "bin/repak.exe" ] && {
    echo "Downloading repak..."
    dl "https://github.com/trumank/repak/releases/download/v0.2.3/repak_cli-x86_64-pc-windows-msvc.zip" "bin/repak.zip"
    unzip -qo "bin/repak.zip" -d "bin/repak_temp"
    mv "bin/repak_temp/repak.exe" "bin/repak.exe"
    rm -rf "bin/repak_temp" "bin/repak.zip"
}

[ ! -f "bin/oo2core_9_win64.dll" ] && {
    echo "Downloading oo2core..."
    dl "https://raw.githubusercontent.com/WorkingRobot/OodleUE/refs/heads/main/Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/win/redist/oo2core_9_win64.dll" "bin/oo2core_9_win64.dll"
}

[ ! -f "bin/vgmstream/vgmstream-cli.exe" ] && {
    echo "Downloading vgmstream..."
    dl "https://github.com/vgmstream/vgmstream/releases/download/r2055/vgmstream-win64.zip" "bin/vgmstream.zip"
    unzip -qo "bin/vgmstream.zip" -d "bin/vgmstream"
    rm -f "bin/vgmstream.zip"
}

[ ! -f "models/smart-turn-v3.2-cpu.onnx" ] && {
    echo "Downloading turn detection model..."
    dl "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx" "models/smart-turn-v3.2-cpu.onnx"
}

# --- System dependencies ---

if ! ldconfig -p 2>/dev/null | grep -q libopenal; then
    echo "Installing OpenAL system library (needed for 3D audio)..."
    if command -v apt &>/dev/null; then
        sudo apt install -y libopenal-dev
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y openal-soft openal-soft-devel
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm openal
    else
        echo "WARNING: Could not install libopenal - install it manually for 3D audio."
    fi
fi

# --- Bootstrap pip and install Python deps ---

if ! $PYTHON -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in ['soundfile', 'sentencepiece', 'onnx_asr', 'kaldi_native_fbank']) else 1)" 2>/dev/null; then
    echo "Installing Python dependencies..."
    $PYTHON -m pip install setuptools wheel --no-warn-script-location -q
    grep -v libaudioverse requirements.txt | $PYTHON -m pip install -r /dev/stdin --no-warn-script-location
    $PYTHON -m pip install pyopenal --no-warn-script-location -q
fi

# --- Start server ---

date +%T > server.lock
rm -f server.lock.stop server.heartbeat

$PYTHON heartbeat.py &
HEARTBEAT_PID=$!

echo "============================================"
echo "  Sonorus Mod - Starting Server"
echo "============================================"
echo
echo "Starting Sonorus server..."
echo "The web interface will open in your browser shortly."
echo
$PYTHON server.py

# Cleanup
echo > server.lock.stop
rm -f server.lock
kill $HEARTBEAT_PID 2>/dev/null
