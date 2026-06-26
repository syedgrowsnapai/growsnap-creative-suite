#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if [ ! -d ".venv" ]; then
    echo "[First Run] Initializing Python virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    python3 -m pip install --upgrade pip
    pip install pyqt6 patchright requests yt-dlp
else
    source .venv/bin/activate
fi

export PYTHONPATH="$DIR/grow_snap_dola:$PYTHONPATH"
python3 grow_snap_dola/main.py "$@"
