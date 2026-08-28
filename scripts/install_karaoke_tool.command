#!/bin/bash
# Karaoke Tool -- installer + launcher, double-click to run.
#
# First run: checks/installs what's missing (Homebrew, Python 3.11, ffmpeg),
# sets up a local Python environment, and downloads the speech/audio models
# (a few GB, one time, 10-20 min on a normal connection).
#
# Every run after that: just launches the tool and opens it in your browser.
# Safe to run again any time -- every step here skips itself if already done.
#
# This file needs to sit in the same folder as karaoke_tool.py and
# karaoke_process.py -- all three travel together.

set -e
cd "$(dirname "$0")"

echo "=== MusicMan Karaoke Tool ==="
echo

if ! command -v brew >/dev/null 2>&1; then
  echo "This needs Homebrew first (a standard package installer for Mac -- one-time, safe)."
  echo "Install it by pasting this into Terminal, then double-click this file again:"
  echo
  echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  echo
  read -p "Press Enter to close this window..."
  exit 1
fi

if ! command -v python3.11 >/dev/null 2>&1; then
  echo "Installing Python 3.11 (one time)..."
  brew install python@3.11
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Installing ffmpeg (one time)..."
  brew install ffmpeg
fi

FIRST_SETUP=0
if [ ! -d "karaoke_venv" ]; then
  FIRST_SETUP=1
  echo
  echo "Setting this up for the first time. This downloads the speech and"
  echo "audio-separation models (a few GB) -- only happens once, please be"
  echo "patient, this can take 10-20 minutes."
  echo
  python3.11 -m venv karaoke_venv
fi

source karaoke_venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet flask openai-whisper demucs

if [ "$FIRST_SETUP" = "1" ]; then
  echo
  echo "Setup done."
fi

echo
echo "Starting the Karaoke Tool -- opening in your browser at"
echo "http://localhost:5151"
echo
echo "Leave this window open while you use the tool. Close it (or press"
echo "Ctrl+C) when you're done."
echo
( sleep 2 && open "http://localhost:5151" ) &
python3 karaoke_tool.py
