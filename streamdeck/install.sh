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
echo "QUICK SETUP — MK.2 (15 keys) suggested layout:"
echo ""
echo "  PAGE 1: WALK-UPS"
echo "  ┌─────────┬─────────┬─────────┬─────────┬─────────┐"
echo "  │ Circle 1│ Circle 2│ Circle 3│ Circle 4│ Circle 5│"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │ Circle 6│ Circle 7│ Circle 8│ Circle 9│  STOP   │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │ FADE OUT│  PAUSE  │  TIMER  │ RESET T.│  →SFX   │"
echo "  └─────────┴─────────┴─────────┴─────────┴─────────┘"
echo ""
echo "  PAGE 2: SFX (up to 13 sounds + ← back + → scenes)"
echo ""
echo "  PAGE 3: SCENES & MACROS"
echo "  ┌─────────┬─────────┬─────────┬─────────┬─────────┐"
echo "  │Campfire │Spotlight│  Party  │  Storm  │ Spooky  │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │Blackout │Skit Int.│Big Rev. │ Awards  │End Show │"
echo "  ├─────────┼─────────┼─────────┼─────────┼─────────┤"
echo "  │ Hype Bld│Walk-Up E│         │  ←SFX   │  Home   │"
echo "  └─────────┴─────────┴─────────┴─────────┴─────────┘"
echo ""
echo "Drag actions from the right panel onto buttons."
echo "Click any button to configure it (circle, scene, etc.)"
echo ""
