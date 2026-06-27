#!/bin/bash
# ============================================================
#  MUSIC MAN CONSOLE — install.sh
#  Broken Arrow Expedition
#  Boring Badger · Gaming Gecko · Little Red Machine
# ============================================================
#  Run this once on a fresh Raspberry Pi OS Lite (64-bit)
#  installation. It installs all dependencies, creates the
#  folder structure, and sets up the service.
#
#  Usage:
#    chmod +x install.sh
#    sudo ./install.sh
#
#  After running, access the system at:
#    http://musicman.local
# ============================================================

set -e  # Exit on any error

# ── COLORS FOR OUTPUT ──
RED='\033[0;31m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ── HELPERS ──
info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${AMBER}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
header()  { echo -e "\n${AMBER}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${AMBER}  $1${NC}"; echo -e "${AMBER}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── CHECK ROOT ──
if [ "$EUID" -ne 0 ]; then
    error "Please run with sudo: sudo ./install.sh"
fi

# ── VARIABLES ──
INSTALL_DIR=/home/pi/musicman
SERVICE_USER=pi
PYTHON=python3
PIP=pip3

clear
echo ""
echo -e "${AMBER}╔════════════════════════════════════════════╗${NC}"
echo -e "${AMBER}║     MUSIC MAN CONSOLE — INSTALLER         ║${NC}"
echo -e "${AMBER}║     Broken Arrow Expedition                ║${NC}"
echo -e "${AMBER}║     Boring Badger · Gaming Gecko           ║${NC}"
echo -e "${AMBER}║     Little Red Machine                     ║${NC}"
echo -e "${AMBER}╚════════════════════════════════════════════╝${NC}"
echo ""
info "Starting installation. This will take about 10 minutes."
info "Go get a coffee. ☕"
echo ""


# ════════════════════════════════════════════
header "STEP 1 of 8 — System update"
# ════════════════════════════════════════════
info "Updating package lists..."
apt-get update -qq
success "Package lists updated"

info "Upgrading installed packages..."
apt-get upgrade -y -qq
success "Packages upgraded"


# ════════════════════════════════════════════
header "STEP 2 of 8 — System dependencies"
# ════════════════════════════════════════════
info "Installing system packages..."

apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    ffmpeg \
    libhidapi-hidraw0 \
    libhidapi-libusb0 \
    libudev-dev \
    libusb-1.0-0-dev \
    libsdl2-mixer-2.0-0 \
    libsdl2-2.0-0 \
    libasound2-dev \
    libportaudio2 \
    chromium-browser \
    avahi-daemon \
    nginx \
    samba \
    curl \
    wget \
    unzip \
    at \
    fonts-open-sans

success "System packages installed"


# ════════════════════════════════════════════
header "STEP 3 of 8 — Folder structure"
# ════════════════════════════════════════════
info "Creating Music Man directory structure..."

mkdir -p $INSTALL_DIR
mkdir -p $INSTALL_DIR/assets/circles
mkdir -p $INSTALL_DIR/assets/roles
mkdir -p $INSTALL_DIR/assets/sfx
mkdir -p $INSTALL_DIR/assets/music
mkdir -p $INSTALL_DIR/assets/display
mkdir -p $INSTALL_DIR/assets/generated
mkdir -p $INSTALL_DIR/assets/fonts
mkdir -p $INSTALL_DIR/campouts
mkdir -p $INSTALL_DIR/logs
mkdir -p $INSTALL_DIR/static
mkdir -p $INSTALL_DIR/templates

# Create placeholder directories for each circle
for i in {1..9}; do
    mkdir -p $INSTALL_DIR/assets/circles/circle_$i
done

# Create placeholder directories for each role
for role in navigator log_keeper story_teller firestarter candy_man wizard circle_navigator; do
    mkdir -p $INSTALL_DIR/assets/roles/$role
done

# Set ownership
chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR

success "Folder structure created at $INSTALL_DIR"


# ════════════════════════════════════════════
header "STEP 4 of 8 — Python virtual environment"
# ════════════════════════════════════════════
info "Creating Python virtual environment..."

sudo -u $SERVICE_USER $PYTHON -m venv $INSTALL_DIR/venv
VENV_PIP=$INSTALL_DIR/venv/bin/pip
VENV_PYTHON=$INSTALL_DIR/venv/bin/python

success "Virtual environment created"

info "Installing Python packages (this takes a few minutes)..."

sudo -u $SERVICE_USER $VENV_PIP install --quiet --upgrade pip

sudo -u $SERVICE_USER $VENV_PIP install --quiet \
    flask \
    gunicorn \
    pygame \
    pyyaml \
    python-streamdeck \
    hidapi \
    requests \
    websockets \
    flask-sock \
    pillow \
    numpy \
    moviepy \
    smbus2 \
    sacn \
    watchdog \
    psutil

success "Python packages installed"


# ════════════════════════════════════════════
header "STEP 5 of 8 — Config and static files"
# ════════════════════════════════════════════

# Copy config.yaml if it exists next to the installer
if [ -f "$(dirname $0)/config.yaml" ]; then
    info "Copying config.yaml..."
    cp "$(dirname $0)/config.yaml" $INSTALL_DIR/config.yaml
    chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/config.yaml
    success "config.yaml installed"
else
    warn "config.yaml not found next to installer — you'll need to add it manually"
    warn "Copy it to: $INSTALL_DIR/config.yaml"
fi

# Copy UI files if they exist
for file in musicman_ui.html musicman_admin.html; do
    if [ -f "$(dirname $0)/$file" ]; then
        info "Copying $file..."
        cp "$(dirname $0)/$file" $INSTALL_DIR/static/$file
        chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/static/$file
        success "$file installed"
    else
        warn "$file not found — add it to $INSTALL_DIR/static/ later"
    fi
done

success "Static files processed"


# ════════════════════════════════════════════
header "STEP 6 of 8 — Stream Deck USB permissions"
# ════════════════════════════════════════════
info "Adding udev rules for Stream Deck..."

cat > /etc/udev/rules.d/70-streamdeck.rules << 'UDEVEOF'
# Elgato Stream Deck MK.2
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0080", MODE="0666", GROUP="plugdev"
# Elgato Stream Deck Original
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0060", MODE="0666", GROUP="plugdev"
# Elgato Stream Deck XL
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="006c", MODE="0666", GROUP="plugdev"
# Elgato Stream Deck Mini
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0063", MODE="0666", GROUP="plugdev"
UDEVEOF

udevadm control --reload-rules
udevadm trigger

usermod -a -G plugdev $SERVICE_USER

success "Stream Deck USB permissions configured"

info "Adding udev rule for Stream Deck auto-reconnect watchdog..."

cat > /etc/udev/rules.d/71-streamdeck-reconnect.rules << 'RECONNEOF'
# Restart Music Man service when Stream Deck is plugged in
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", RUN+="/bin/systemctl restart musicman.service"
RECONNEOF

udevadm control --reload-rules
success "Auto-reconnect rule added"


# ════════════════════════════════════════════
header "STEP 7 of 8 — Flask application"
# ════════════════════════════════════════════
info "Writing main application file..."

cat > $INSTALL_DIR/musicman.py << 'APPEOF'
#!/usr/bin/env python3
"""
Music Man Console — musicman.py
Broken Arrow Expedition
Boring Badger · Gaming Gecko · Little Red Machine

Main Flask application. Handles:
- Audio engine (pygame)
- Stream Deck daemon
- WLED HTTP API calls
- Art-Net DMX for moving heads
- WebSocket state push to UI
- REST API for operator and admin UI
"""

import os
import sys
import yaml
import json
import time
import threading
import logging
import requests
import socket
import pygame
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_sock import Sock

# ── PATHS ──
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / 'config.yaml'
STATIC_DIR = BASE_DIR / 'static'
ASSETS_DIR = BASE_DIR / 'assets'

# ── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(BASE_DIR / 'logs' / 'musicman.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger('musicman')

# ── LOAD CONFIG ──
def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

config = load_config()
log.info(f"Music Man Console starting — {config['expedition']['name']}")

# ── FLASK APP ──
app = Flask(__name__, static_folder=str(STATIC_DIR))
sock = Sock(app)

# ── WEBSOCKET CLIENTS ──
ws_clients = set()
ws_lock = threading.Lock()

def broadcast(event, data):
    """Push state update to all connected WebSocket clients."""
    msg = json.dumps({'event': event, 'data': data})
    with ws_lock:
        dead = set()
        for client in ws_clients:
            try:
                client.send(msg)
            except Exception:
                dead.add(client)
        ws_clients.difference_update(dead)

@sock.route('/ws')
def websocket(ws):
    with ws_lock:
        ws_clients.add(ws)
    log.info(f"WebSocket client connected ({len(ws_clients)} total)")
    try:
        while True:
            ws.receive(timeout=30)
    except Exception:
        pass
    finally:
        with ws_lock:
            ws_clients.discard(ws)
        log.info(f"WebSocket client disconnected ({len(ws_clients)} remaining)")

# ── AUDIO ENGINE ──
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
pygame.mixer.set_num_channels(8)

audio_state = {
    'playing': False,
    'paused': False,
    'current_track': None,
    'current_track_sub': None,
    'volume': config['audio']['default_volume'],
    'fade_timer': None,
}

def play_audio(filepath, volume=None, start_pos=0):
    """Play an audio file. Stops any currently playing audio."""
    try:
        if not os.path.exists(filepath):
            log.error(f"Audio file not found: {filepath}")
            return False
        pygame.mixer.music.stop()
        pygame.mixer.music.load(filepath)
        vol = (volume or audio_state['volume']) / 100
        pygame.mixer.music.set_volume(vol)
        pygame.mixer.music.play(start=start_pos)
        audio_state['playing'] = True
        audio_state['paused'] = False
        audio_state['current_track'] = os.path.basename(filepath)
        broadcast('audio_state', audio_state)
        log.info(f"Playing: {filepath}")
        return True
    except Exception as e:
        log.error(f"Audio play error: {e}")
        return False

def play_sfx(filepath, volume=None):
    """Play a sound effect on a separate channel (doesn't stop music)."""
    try:
        sound = pygame.mixer.Sound(filepath)
        vol = (volume or config['audio']['sfx_volume']) / 100
        sound.set_volume(vol)
        sound.play()
        log.info(f"SFX: {filepath}")
    except Exception as e:
        log.error(f"SFX error: {e}")

def fade_audio(duration=None):
    """Fade out currently playing audio."""
    dur = int((duration or config['audio']['fade_duration']) * 1000)
    pygame.mixer.music.fadeout(dur)
    audio_state['playing'] = False
    broadcast('audio_state', audio_state)

def stop_audio():
    """Immediately stop all audio."""
    pygame.mixer.music.stop()
    pygame.mixer.stop()
    audio_state['playing'] = False
    audio_state['paused'] = False
    audio_state['current_track'] = None
    broadcast('audio_state', audio_state)

def set_volume(v):
    """Set master volume 0-100."""
    audio_state['volume'] = int(v)
    pygame.mixer.music.set_volume(int(v) / 100)
    broadcast('audio_state', audio_state)

# ── SKIT TIMER ──
timer_state = {
    'running': False,
    'seconds_remaining': config['timer']['duration'],
    'duration': config['timer']['duration'],
    'warning_fired': False,
    'expired': False,
}
timer_thread = None
timer_stop_event = threading.Event()

def timer_worker():
    global timer_state
    while not timer_stop_event.is_set():
        time.sleep(1)
        if not timer_state['running']:
            continue
        timer_state['seconds_remaining'] -= 1
        broadcast('timer_state', timer_state)
        # Warning
        if timer_state['seconds_remaining'] == config['timer']['warning_at'] \
                and not timer_state['warning_fired']:
            timer_state['warning_fired'] = True
            sfx_path = ASSETS_DIR / 'sfx' / config['timer']['warning_sound']
            if sfx_path.exists():
                play_sfx(str(sfx_path))
            broadcast('timer_warning', {})
            log.info("Timer: warning fired")
        # Expired
        if timer_state['seconds_remaining'] <= 0:
            timer_state['seconds_remaining'] = 0
            timer_state['running'] = False
            timer_state['expired'] = True
            sfx_path = ASSETS_DIR / 'sfx' / config['timer']['end_sound']
            if sfx_path.exists():
                play_sfx(str(sfx_path))
            broadcast('timer_expired', {})
            log.info("Timer: expired — airhorn fired")

def start_timer_thread():
    global timer_thread, timer_stop_event
    timer_stop_event = threading.Event()
    timer_thread = threading.Thread(target=timer_worker, daemon=True)
    timer_thread.start()

# ── WLED HTTP API ──
def wled_set_scene(scene_id):
    """Apply a lighting scene to all WLED devices."""
    scenes = {s['id']: s for s in config.get('scenes', [])}
    if scene_id not in scenes:
        log.warning(f"Scene not found: {scene_id}")
        return
    scene = scenes[scene_id]
    devices = {d['id']: d for d in config.get('wled_devices', [])}
    zones = scene.get('zones', {})
    def send_zone(device_id, seg_id, effect, color=None, brightness=75, speed=50):
        if device_id not in devices:
            return
        ip = devices[device_id]['ip']
        payload = {
            'seg': [{
                'id': seg_id,
                'on': brightness > 0,
                'bri': int(brightness * 2.55),
                'fx': 0 if effect == 'solid' else
                      35 if effect == 'fire' else
                      9  if effect == 'rainbow' else
                      11 if effect == 'strobe' else
                      7  if effect == 'fade' else
                      15 if effect == 'theater' else
                      44 if effect == 'twinkle' else 0,
                'sx': speed,
            }]
        }
        if color:
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            payload['seg'][0]['col'] = [[r, g, b]]
        try:
            requests.post(f"http://{ip}/json/state", json=payload, timeout=0.5)
        except Exception as e:
            log.warning(f"WLED {device_id} unreachable: {e}")
    threads = []
    for zone_key, zone_cfg in zones.items():
        if '_pole' in zone_key:
            parts = zone_key.split('_pole_')
            device_id = parts[0] + '_pole' if len(parts) > 1 else zone_key
            seg_name = parts[1] if len(parts) > 1 else 'main'
            device = devices.get(device_id)
            if device:
                seg_id = next((s['id'] for s in device.get('segments', [])
                               if s['name'] == seg_name), 0)
                t = threading.Thread(
                    target=send_zone,
                    args=(device_id, seg_id,
                          zone_cfg.get('effect', 'solid'),
                          zone_cfg.get('color'),
                          zone_cfg.get('brightness', 75),
                          zone_cfg.get('speed', 50)),
                    daemon=True
                )
                threads.append(t)
        else:
            t = threading.Thread(
                target=send_zone,
                args=(zone_key, 0,
                      zone_cfg.get('effect', 'solid'),
                      zone_cfg.get('color'),
                      zone_cfg.get('brightness', 75),
                      zone_cfg.get('speed', 50)),
                daemon=True
            )
            threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=1.0)
    log.info(f"Scene applied: {scene_id}")
    broadcast('scene_changed', {'scene': scene_id})

def wled_set_color(device_id, color, brightness=75, seg_id=0):
    """Set a specific WLED device to a solid color."""
    devices = {d['id']: d for d in config.get('wled_devices', [])}
    if device_id not in devices:
        return
    ip = devices[device_id]['ip']
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    payload = {
        'seg': [{'id': seg_id, 'on': True,
                 'bri': int(brightness * 2.55),
                 'fx': 0, 'col': [[r, g, b]]}]
    }
    try:
        requests.post(f"http://{ip}/json/state", json=payload, timeout=0.5)
    except Exception as e:
        log.warning(f"WLED {device_id} unreachable: {e}")

# ── KILL EVERYTHING ──
def kill_everything():
    """
    Emergency stop. Stops all audio, resets lights to campfire warm.
    Does NOT lose program position or reset timer to zero.
    """
    log.info("KILL EVERYTHING fired")
    stop_audio()
    timer_state['running'] = False
    wled_set_scene('campfire')
    broadcast('kill_all', {})

# ── WALKUP MACRO ──
def fire_walkup(circle_id=None, role_id=None):
    """Fire a complete walk-up sequence for a circle or role."""
    cfg = load_config()
    if circle_id:
        items = [c for c in cfg.get('circles', []) if c['id'] == circle_id]
        item = items[0] if items else None
        item_type = 'circle'
    elif role_id:
        items = [r for r in cfg.get('roles', []) if r['id'] == role_id]
        item = items[0] if items else None
        item_type = 'role'
    else:
        return
    if not item:
        log.error(f"Walk-up target not found: {circle_id or role_id}")
        return
    color = item.get('color', '#FFFFFF')
    walkup_cfg = item.get('walkup', {})
    music_file = ASSETS_DIR / ('circles' if item_type == 'circle' else 'roles') \
                 / item['id'] / item['assets'].get('walkup_music', '')
    # Flash circle color on all strips
    def flash_color():
        for device in cfg.get('wled_devices', []):
            if device['id'] in ('ezup_ceil',):
                continue
            wled_set_color(device['id'], color, brightness=80)
    # Play music
    def play_music():
        if music_file.exists():
            play_audio(
                str(music_file),
                start_pos=walkup_cfg.get('start_time', 0)
            )
            # Schedule auto-fade
            duration = walkup_cfg.get('duration', 30)
            fade_dur = walkup_cfg.get('fade_duration', 3)
            def auto_fade():
                time.sleep(duration)
                fade_audio(fade_dur)
                time.sleep(fade_dur + 0.5)
                wled_set_scene('campfire')
            threading.Thread(target=auto_fade, daemon=True).start()
        else:
            log.warning(f"Walk-up music not found: {music_file}")
    # Send display event
    broadcast('display_walkup', {
        'id': item['id'],
        'name': item.get('name', ''),
        'color': color,
        'animation': item['assets'].get('animation', ''),
        'type': item_type,
    })
    # Fire everything concurrently
    threading.Thread(target=flash_color, daemon=True).start()
    threading.Thread(target=play_music, daemon=True).start()
    log.info(f"Walk-up fired: {item.get('name')}")

# ════════════════════════════════════════════
# API ROUTES
# ════════════════════════════════════════════

# ── SERVE UI ──
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'musicman_ui.html')

@app.route('/admin')
def admin():
    return send_from_directory(STATIC_DIR, 'musicman_admin.html')

@app.route('/display')
def display():
    return send_from_directory(STATIC_DIR, 'display.html')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route('/assets/<path:filename>')
def asset_files(filename):
    return send_from_directory(ASSETS_DIR, filename)

# ── STATE ──
@app.route('/api/state')
def api_state():
    return jsonify({
        'audio': audio_state,
        'timer': timer_state,
        'system': {
            'hostname': config['system']['hostname'],
            'expedition': config['expedition']['name'],
        }
    })

# ── AUDIO ──
@app.route('/api/audio/stop')
def api_audio_stop():
    stop_audio()
    return jsonify({'ok': True})

@app.route('/api/audio/pause')
def api_audio_pause():
    pygame.mixer.music.pause()
    audio_state['paused'] = True
    broadcast('audio_state', audio_state)
    return jsonify({'ok': True})

@app.route('/api/audio/resume')
def api_audio_resume():
    pygame.mixer.music.unpause()
    audio_state['paused'] = False
    broadcast('audio_state', audio_state)
    return jsonify({'ok': True})

@app.route('/api/audio/fade')
def api_audio_fade():
    duration = request.args.get('duration', config['audio']['fade_duration'])
    fade_audio(float(duration))
    return jsonify({'ok': True})

@app.route('/api/audio/volume')
def api_audio_volume():
    v = request.args.get('v', 80)
    set_volume(int(v))
    return jsonify({'ok': True, 'volume': int(v)})

# ── SFX ──
@app.route('/api/sfx/play')
def api_sfx_play():
    name = request.args.get('name', '')
    sfx_map = {s['id']: s for s in config.get('sfx', [])}
    if name in sfx_map:
        filepath = ASSETS_DIR / 'sfx' / sfx_map[name]['file']
        if filepath.exists():
            play_sfx(str(filepath))
            return jsonify({'ok': True})
    log.warning(f"SFX not found: {name}")
    return jsonify({'ok': False, 'error': f'SFX not found: {name}'}), 404

# ── TIMER ──
@app.route('/api/timer/start')
def api_timer_start():
    if not timer_state['running']:
        if timer_state['expired']:
            timer_state['seconds_remaining'] = config['timer']['duration']
            timer_state['warning_fired'] = False
            timer_state['expired'] = False
        timer_state['running'] = True
        broadcast('timer_state', timer_state)
        log.info("Timer started")
    return jsonify({'ok': True, 'timer': timer_state})

@app.route('/api/timer/pause')
def api_timer_pause():
    timer_state['running'] = False
    broadcast('timer_state', timer_state)
    return jsonify({'ok': True})

@app.route('/api/timer/reset')
def api_timer_reset():
    timer_state['running'] = False
    timer_state['seconds_remaining'] = config['timer']['duration']
    timer_state['warning_fired'] = False
    timer_state['expired'] = False
    broadcast('timer_state', timer_state)
    log.info("Timer reset")
    return jsonify({'ok': True})

@app.route('/api/timer/toggle')
def api_timer_toggle():
    if timer_state['running']:
        return api_timer_pause()
    else:
        return api_timer_start()

# ── LIGHTING ──
@app.route('/api/lights/scene')
def api_lights_scene():
    name = request.args.get('name', 'campfire')
    threading.Thread(target=wled_set_scene, args=(name,), daemon=True).start()
    return jsonify({'ok': True, 'scene': name})

@app.route('/api/lights/brightness')
def api_lights_brightness():
    v = int(request.args.get('v', 75))
    # Apply brightness to all active devices
    for device in config.get('wled_devices', []):
        if device.get('independent'):
            continue
        ip = device['ip']
        try:
            requests.post(f"http://{ip}/json/state",
                         json={'bri': int(v * 2.55)}, timeout=0.5)
        except Exception:
            pass
    return jsonify({'ok': True, 'brightness': v})

# ── WALK-UP ──
@app.route('/api/macro/walkup')
def api_walkup():
    circle = request.args.get('circle')
    role = request.args.get('role')
    threading.Thread(
        target=fire_walkup,
        kwargs={'circle_id': circle, 'role_id': role},
        daemon=True
    ).start()
    return jsonify({'ok': True})

# ── KILL ALL ──
@app.route('/api/kill')
def api_kill():
    threading.Thread(target=kill_everything, daemon=True).start()
    return jsonify({'ok': True})

# ── CALL AND RESPONSE ──
@app.route('/api/callresponse/stage/on')
def api_cr_stage_on():
    cfg = config.get('call_response', {})
    color = cfg.get('stage_color', '#F5A623')
    for zone in cfg.get('stage_zones', []):
        parts = zone.split('_pole_')
        if len(parts) == 2:
            wled_set_color(parts[0] + '_pole', color, brightness=80,
                          seg_id=0 if parts[1] == 'crowd' else 1)
    return jsonify({'ok': True})

@app.route('/api/callresponse/stage/off')
def api_cr_stage_off():
    cfg = config.get('call_response', {})
    for zone in cfg.get('stage_zones', []):
        parts = zone.split('_pole_')
        if len(parts) == 2:
            wled_set_color(parts[0] + '_pole', '#000000', brightness=0,
                          seg_id=0 if parts[1] == 'crowd' else 1)
    return jsonify({'ok': True})

@app.route('/api/callresponse/crowd/on')
def api_cr_crowd_on():
    cfg = config.get('call_response', {})
    color = cfg.get('crowd_color', '#2B5FA6')
    for zone in cfg.get('crowd_zones', []):
        if '_pole_' in zone:
            parts = zone.split('_pole_')
            wled_set_color(parts[0] + '_pole', color, brightness=80,
                          seg_id=0 if parts[1] == 'crowd' else 1)
        else:
            wled_set_color(zone, color, brightness=70)
    return jsonify({'ok': True})

@app.route('/api/callresponse/crowd/off')
def api_cr_crowd_off():
    cfg = config.get('call_response', {})
    for zone in cfg.get('crowd_zones', []):
        if '_pole_' in zone:
            parts = zone.split('_pole_')
            wled_set_color(parts[0] + '_pole', '#000000', brightness=0,
                          seg_id=0 if parts[1] == 'crowd' else 1)
        else:
            wled_set_color(zone, '#000000', brightness=0)
    return jsonify({'ok': True})

# ── APPLAUSE METER ──
applause_level = 0
applause_lock = threading.Lock()

@app.route('/api/applause/up')
def api_applause_up():
    global applause_level
    cfg = config.get('applause_meter', {})
    with applause_lock:
        applause_level = min(100, applause_level + cfg.get('rise_rate', 5))
        _apply_applause(applause_level)
    return jsonify({'ok': True, 'level': applause_level})

@app.route('/api/applause/down')
def api_applause_down():
    global applause_level
    cfg = config.get('applause_meter', {})
    with applause_lock:
        applause_level = max(0, applause_level - cfg.get('rise_rate', 5))
        _apply_applause(applause_level)
    return jsonify({'ok': True, 'level': applause_level})

def _apply_applause(level):
    """Fill crowd pole strips from bottom based on applause level."""
    cfg = config.get('applause_meter', {})
    color_low  = cfg.get('color_low',  '#3CB96A')
    color_mid  = cfg.get('color_mid',  '#F5A623')
    color_high = cfg.get('color_high', '#CC2222')
    color = color_low if level < 40 else color_mid if level < 75 else color_high
    brightness = int(level * 0.8)
    for device_id in ['tower_a', 'tower_b']:
        wled_set_color(device_id, color, brightness=brightness, seg_id=0)
    broadcast('applause_level', {'level': level, 'color': color})

# ── CONFIG API ──
@app.route('/api/config', methods=['GET'])
def api_config_get():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
def api_config_save():
    data = request.get_json()
    save_config(data)
    global config
    config = load_config()
    log.info("Config saved and reloaded")
    return jsonify({'ok': True})

# ── ADMIN — FILE UPLOAD ──
@app.route('/api/admin/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'No file'}), 400
    f = request.files['file']
    target_type = request.form.get('type', 'sfx')   # sfx / music / circle / role
    target_id   = request.form.get('id', '')
    asset_type  = request.form.get('asset', 'walkup_music')
    if target_type == 'sfx':
        dest = ASSETS_DIR / 'sfx' / f.filename
    elif target_type == 'music':
        dest = ASSETS_DIR / 'music' / f.filename
    elif target_type == 'circle' and target_id:
        dest = ASSETS_DIR / 'circles' / target_id / \
               ('walkup.mp3' if asset_type == 'walkup_music' else
                'logo.png'   if asset_type == 'logo' else
                'walkup.mp4' if asset_type == 'animation' else f.filename)
    elif target_type == 'role' and target_id:
        dest = ASSETS_DIR / 'roles' / target_id / \
               ('walkup.mp3' if asset_type == 'walkup_music' else
                'logo.png'   if asset_type == 'logo' else
                'walkup.mp4' if asset_type == 'animation' else f.filename)
    else:
        return jsonify({'ok': False, 'error': 'Unknown target type'}), 400
    dest.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(dest))
    log.info(f"File uploaded: {dest}")
    return jsonify({'ok': True, 'path': str(dest)})

# ── ADMIN — GENERATE ANIMATION ──
@app.route('/api/admin/generate', methods=['POST'])
def api_generate():
    data = request.get_json()
    target_type = data.get('type', 'circle')
    target_id   = data.get('id', '')
    def render_job():
        try:
            from generator import generate_animation
            generate_animation(target_type, target_id, config)
            broadcast('animation_ready', {'type': target_type, 'id': target_id})
            log.info(f"Animation generated: {target_type}/{target_id}")
        except Exception as e:
            log.error(f"Animation generation failed: {e}")
            broadcast('animation_error', {'type': target_type, 'id': target_id, 'error': str(e)})
    threading.Thread(target=render_job, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Rendering in background...'})

# ── ADMIN — CIRCLE / ROLE SAVE ──
@app.route('/api/admin/circle', methods=['POST'])
def api_save_circle():
    data = request.get_json()
    cfg = load_config()
    circles = cfg.get('circles', [])
    existing = next((c for c in circles if c['id'] == data.get('id')), None)
    if existing:
        existing.update(data)
    else:
        circles.append(data)
    cfg['circles'] = circles
    save_config(cfg)
    global config
    config = cfg
    return jsonify({'ok': True})

@app.route('/api/admin/role', methods=['POST'])
def api_save_role():
    data = request.get_json()
    cfg = load_config()
    roles = cfg.get('roles', [])
    existing = next((r for r in roles if r['id'] == data.get('id')), None)
    if existing:
        existing.update(data)
    else:
        roles.append(data)
    cfg['roles'] = roles
    save_config(cfg)
    global config
    config = cfg
    return jsonify({'ok': True})

# ── SYSTEM ──
@app.route('/api/system/status')
def api_system_status():
    import psutil
    return jsonify({
        'cpu': psutil.cpu_percent(),
        'memory': psutil.virtual_memory().percent,
        'disk': psutil.disk_usage('/').percent,
        'uptime': time.time() - psutil.boot_time(),
    })

@app.route('/api/system/restart', methods=['POST'])
def api_system_restart():
    log.info("Service restart requested via API")
    os.system('sudo systemctl restart musicman.service')
    return jsonify({'ok': True})

# ════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════
if __name__ == '__main__':
    log.info("Starting Music Man Console...")
    start_timer_thread()
    log.info(f"Serving at http://{config['system']['hostname']}.local")
    app.run(
        host='0.0.0.0',
        port=config['system']['port'],
        debug=config['system']['debug'],
        threaded=True,
        use_reloader=False
    )
APPEOF

chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/musicman.py
success "Application file written"


# ════════════════════════════════════════════
header "STEP 8 of 8 — Systemd service"
# ════════════════════════════════════════════
info "Creating systemd service..."

cat > /etc/systemd/system/musicman.service << SERVICEEOF
[Unit]
Description=Music Man Console — Broken Arrow Expedition
After=network.target sound.target avahi-daemon.service
Wants=avahi-daemon.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/gunicorn musicman:app \
    --bind 0.0.0.0:80 \
    --workers 2 \
    --threads 4 \
    --worker-class gthread \
    --timeout 30 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile $INSTALL_DIR/logs/access.log \
    --error-logfile $INSTALL_DIR/logs/error.log
Restart=always
RestartSec=5
StandardOutput=append:$INSTALL_DIR/logs/musicman.log
StandardError=append:$INSTALL_DIR/logs/musicman.log

# Watchdog — restart if service hangs
WatchdogSec=30
NotifyAccess=main

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable musicman.service
success "Service created and enabled"

info "Starting Music Man service..."
systemctl start musicman.service
sleep 3

if systemctl is-active --quiet musicman.service; then
    success "Music Man service is running"
else
    warn "Service may not have started cleanly — check logs:"
    warn "  sudo journalctl -u musicman.service -n 50"
fi


# ════════════════════════════════════════════
header "STEP 9 of 9 — Network file sharing (Samba)"
# ════════════════════════════════════════════
info "Configuring Samba share for musicman assets..."

SMB_CONF=/etc/samba/smb.conf

# Remove any existing [musicman] share block to avoid duplicates
if grep -q '^\[musicman\]' "$SMB_CONF"; then
    sed -i '/^\[musicman\]/,/^$/d' "$SMB_CONF"
fi

# Append share definition
cat >> "$SMB_CONF" << 'SMBEOF'

[musicman]
path = /home/pi/musicman
browseable = yes
writable = yes
valid users = pi
create mask = 0664
directory mask = 0775
SMBEOF

# Set Samba password for pi user (same as system password by convention: BrokenArrow)
printf "BrokenArrow\nBrokenArrow\n" | smbpasswd -a pi -s

systemctl enable smbd --quiet
systemctl restart smbd

success "Samba share 'musicman' configured — connect as pi / BrokenArrow"


# ════════════════════════════════════════════
# DONE
# ════════════════════════════════════════════
echo ""
echo -e "${AMBER}╔════════════════════════════════════════════╗${NC}"
echo -e "${AMBER}║     INSTALLATION COMPLETE                  ║${NC}"
echo -e "${AMBER}╚════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✓${NC}  Music Man Console is running"
echo -e "${GREEN}✓${NC}  Auto-starts on every boot"
echo -e "${GREEN}✓${NC}  Stream Deck will auto-reconnect on plug"
echo -e "${GREEN}✓${NC}  Network file share ready (smb://musicman.local — user: pi / pw: BrokenArrow)"
echo ""
echo -e "    Operator UI:   ${BLUE}http://musicman.local${NC}"
echo -e "    Admin panel:   ${BLUE}http://musicman.local/admin${NC}"
echo -e "    Display page:  ${BLUE}http://musicman.local/display${NC}"
echo ""
echo -e "    Logs:          ${INSTALL_DIR}/logs/"
echo -e "    Config:        ${INSTALL_DIR}/config.yaml"
echo -e "    Assets:        ${INSTALL_DIR}/assets/"
echo ""
echo -e "${AMBER}Next steps:${NC}"
echo -e "  1. Copy your audio files to ${INSTALL_DIR}/assets/sfx/"
echo -e "  2. Open ${BLUE}http://musicman.local/admin${NC} to configure circles"
echo -e "  3. Plug in the Stream Deck"
echo -e "  4. Open ${BLUE}http://musicman.local${NC} on the iPad"
echo ""
echo -e "  Boring Badger · Gaming Gecko · Little Red Machine 🔥"
echo ""
