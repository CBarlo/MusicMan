#!/bin/bash
# Music Man Stream Deck Plugin Installer (macOS)
set -e

PLUGIN_SRC="$(cd "$(dirname "$0")" && pwd)/com.musicman.streamdeck.sdPlugin"
PLUGIN_DST="$HOME/Library/Application Support/com.elgato.StreamDeck/Plugins/com.musicman.streamdeck.sdPlugin"

echo ""
echo "═══════════════════════════════════════════"
echo "   MUSIC MAN  —  Stream Deck Plugin Setup  "
echo "═══════════════════════════════════════════"
echo ""

# Check Node.js
if ! command -v node &>/dev/null; then
  echo "✕ Node.js not found. Install it first:"
  echo "  https://nodejs.org  (LTS version)"
  exit 1
fi
echo "✓ Node.js $(node --version)"

# Install ws dependency
echo "→ Installing dependencies…"
cd "$PLUGIN_SRC"
npm install --omit=dev --silent
echo "✓ Dependencies installed"

# Stop Stream Deck app if running
echo "→ Stopping Stream Deck app…"
killall "Stream Deck" 2>/dev/null || true
sleep 2

# Copy plugin
echo "→ Installing plugin…"
rm -rf "$PLUGIN_DST"
cp -r "$PLUGIN_SRC" "$PLUGIN_DST"
echo "✓ Plugin installed to:"
echo "  $PLUGIN_DST"

# Reopen Stream Deck
echo "→ Restarting Stream Deck…"
open -a "Stream Deck" 2>/dev/null || echo "  (open Stream Deck app manually)"

echo ""
echo "✓ Done! Open the Stream Deck app and look for"
echo "  'Music Man' in the action list on the right."
echo ""
echo "SUGGESTED LAYOUT — MK.2 (15 keys)"
echo ""
echo "  MAIN PAGE (always visible)"
echo "  ┌─────────┬─────────┬─────────┬─────────┬─────────┐"
echo "  │  STOP   │  PAUSE  │ →SHOW   │→WALKUPS │  →SFX   │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │  TIMER  │ RESET T.│ →ROLES  │ →MACROS │→DISPLAY │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │ FADE OUT│ MUS VOL-│ MUS VOL+│ SFX VOL-│ SFX VOL+│"
echo "  └─────────┴─────────┴─────────┴─────────┴─────────┘"
echo "  Timer button shows live countdown while running."
echo "  Use Stream Deck 'Folder' buttons for →SHOW etc."
echo ""
echo "  SHOW FOLDER (show flow steps, up to 14 + back)"
echo "  ┌─────────┬─────────┬─────────┬─────────┬─────────┐"
echo "  │  ←BACK  │ Step 1  │ Step 2  │ Step 3  │ Step 4  │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │ Step 5  │ Step 6  │ Step 7  │ Step 8  │ Step 9  │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │ Step 10 │ Step 11 │ Step 12 │ Step 13 │ Step 14 │"
echo "  └─────────┴─────────┴─────────┴─────────┴─────────┘"
echo "  Active step glows. Completed steps dim with a ✓."
echo ""
echo "  WALKUPS / ROLES / SFX / MACROS folders:"
echo "  Same pattern — ←BACK in slot 1, items fill the rest."
echo "  Circle and role buttons show their logo from Admin."
echo ""
echo "  DISPLAY folder:"
echo "  ┌─────────┬─────────┬─────────┬─────────┬─────────┐"
echo "  │  ←BACK  │ BAE LOGO│BLACKOUT │ STANDBY │ Slide 1 │"
echo "  └─────────┴─────────┴─────────┴─────────┴─────────┘"
echo "  Use Transport/Timer → stop for BLACKOUT,"
echo "  Macro buttons for STANDBY and display files."
echo ""
echo "Drag actions from the right panel onto buttons."
echo "Click any button to configure it in the right panel."
echo ""
