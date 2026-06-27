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
import io
import base64
import yaml
import json
import time
import shutil
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
import requests
import socket
import pygame
from pathlib import Path
from mutagen.mp3  import MP3  as MutagenMP3
from mutagen.wave import WAVE as MutagenWAV
from flask import Flask, request, jsonify, send_from_directory
from flask_sock import Sock

# ── PATHS ──
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / 'config.yaml'
STATIC_DIR  = BASE_DIR / 'static'
ASSETS_DIR  = BASE_DIR / 'assets'
NORM_STAMP  = BASE_DIR / 'logs' / 'normalize_last_run'
GAMES_FILE  = BASE_DIR / 'games.json'

# ── LOGGING ──
_log_file = BASE_DIR / 'logs' / 'musicman.log'
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(_log_file, maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(sys.stdout),
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
            except Exception as e:
                log.debug(f"WebSocket send failed ({event}): {e}")
                dead.add(client)
        ws_clients.difference_update(dead)

@sock.route('/ws')
def websocket(ws):
    with ws_lock:
        ws_clients.add(ws)
    log.info(f"WebSocket client connected ({len(ws_clients)} total)")
    # Each connection lives at most 10 minutes, then the client auto-reconnects.
    # Every 25s we attempt a send; if it fails the connection is dead and we exit,
    # freeing the Gunicorn thread.  Without this, dead connections (browser tab
    # closed without a clean WebSocket close frame) hold threads indefinitely.
    deadline = time.time() + 600
    try:
        while time.time() < deadline:
            try:
                msg = ws.receive(timeout=25)
            except Exception:
                break
            if msg is None:
                try:
                    ws.send(json.dumps({'event': 'ping', 'data': {}}))
                except Exception:
                    break
    finally:
        with ws_lock:
            ws_clients.discard(ws)
        log.info(f"WebSocket client disconnected ({len(ws_clients)} remaining)")

# ── AUDIO ENGINE ──
def _init_mixer(retries=5, delay=2):
    for attempt in range(1, retries + 1):
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            pygame.mixer.set_num_channels(8)
            log.info("pygame mixer initialised")
            return
        except Exception as e:
            log.warning(f"pygame mixer init attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(delay)
    log.error("pygame mixer could not initialise — audio will be unavailable")

_init_mixer()

audio_state = {
    'playing': False,
    'paused': False,
    'current_track': None,
    'current_track_sub': None,
    'volume': config['audio']['default_volume'],
    'fade_timer': None,
    'duration': 0,
}
sfx_state = {
    'playing':  False,
    'paused':   False,
    'name':     '',
    'duration': 0,
    'volume':   config.get('audio', {}).get('sfx_volume', 80),
}
_sfx_channel   = None   # active pygame Channel for the current SFX
_sfx_cache     = {}    # filepath → pygame.mixer.Sound (load once, reuse)
_SFX_CACHE_MAX = 50
current_scene  = None   # id of last activated scene
current_circle = None   # id of last activated circle walk-up
current_slide  = {}     # live custom slide currently shown on projector
_walkup_fade_cancel = threading.Event()  # set to cancel the in-flight auto-fade
_audio_session      = 0                  # increments on every play_audio call

# ── PLAYLIST ENGINE ──
AUDIO_EXTS = {'.mp3', '.flac', '.ogg', '.wav', '.m4a', '.aac', '.opus'}

playlist_state = {
    'active':     False,
    'path':       '',
    'index':      0,
    'total':      0,
    'shuffle':    False,
    'track_name': '',
}
_playlist_tracks     = []
_playlist_advance_id = 0

def _find_usb_mount():
    candidates = []
    for root in ['/media/usb0', '/media/usb1', '/media/usb']:
        p = Path(root)
        if p.is_dir():
            try:
                next(p.iterdir())
                candidates.append(str(p))
            except StopIteration:
                pass
    try:
        for child in sorted(Path('/media/pi').iterdir()):
            if child.is_dir():
                try:
                    next(child.iterdir())
                    candidates.append(str(child))
                except StopIteration:
                    pass
    except FileNotFoundError:
        pass
    return candidates[0] if candidates else None

def scan_playlist(path):
    p = Path(path)
    if p.suffix.lower() == '.m3u':
        tracks = []
        try:
            with open(p, encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        tp = Path(line) if Path(line).is_absolute() else p.parent / line
                        if tp.exists():
                            tracks.append(str(tp))
        except Exception as e:
            log.error(f'M3U parse error: {e}')
        return tracks
    elif p.is_dir():
        return sorted(str(f) for f in p.rglob('*') if f.suffix.lower() in AUDIO_EXTS and f.is_file())
    return []

def _advance_playlist_to(idx):
    global _playlist_advance_id
    _playlist_advance_id += 1
    my_id = _playlist_advance_id
    if idx >= len(_playlist_tracks):
        playlist_state['active']     = False
        playlist_state['track_name'] = ''
        broadcast('playlist_state', playlist_state)
        return
    playlist_state['index']      = idx
    playlist_state['track_name'] = Path(_playlist_tracks[idx]).stem
    play_audio(_playlist_tracks[idx])
    my_session = _audio_session   # capture after play_audio increments it
    broadcast('playlist_state', playlist_state)
    def _watch():
        while True:
            time.sleep(1)
            if _playlist_advance_id != my_id or not playlist_state.get('active'):
                return
            if _audio_session != my_session:
                # A walkup or macro replaced the audio — stop trying to advance
                return
            if not pygame.mixer.music.get_busy() and not audio_state.get('paused'):
                _advance_playlist_to(idx + 1)
                return
    threading.Thread(target=_watch, daemon=True).start()

def play_playlist(path, shuffle=False):
    global _playlist_tracks
    tracks = scan_playlist(path)
    if not tracks:
        log.warning(f'Playlist: no audio found at {path}')
        return
    if shuffle:
        import random
        random.shuffle(tracks)
    _playlist_tracks = tracks
    playlist_state.update({
        'active':     True,
        'path':       path,
        'index':      0,
        'total':      len(tracks),
        'shuffle':    shuffle,
        'track_name': Path(tracks[0]).stem,
    })
    broadcast('playlist_state', playlist_state)
    _advance_playlist_to(0)

def play_audio(filepath, volume=None, start_pos=0, loops=0, crossfade_ms=3000):
    """Play an audio file, fading out any currently playing track first."""
    global _audio_session
    _audio_session += 1
    try:
        if not os.path.exists(filepath):
            log.error(f"Audio file not found: {filepath}")
            return False
        if crossfade_ms > 0 and pygame.mixer.music.get_busy():
            orig = pygame.mixer.music.get_volume()
            steps = max(10, int(crossfade_ms / 1000 * 20))
            interval = crossfade_ms / 1000 / steps
            for i in range(steps - 1, -1, -1):
                if not pygame.mixer.music.get_busy():
                    break
                pygame.mixer.music.set_volume(orig * i / steps)
                time.sleep(interval)
        pygame.mixer.music.stop()
        pygame.mixer.music.load(filepath)
        try:
            ext = Path(filepath).suffix.lower()
            if ext == '.mp3':
                audio_state['duration'] = round(MutagenMP3(filepath).info.length, 2)
            elif ext in ('.wav', '.wave'):
                audio_state['duration'] = round(MutagenWAV(filepath).info.length, 2)
            else:
                audio_state['duration'] = 0
        except Exception:
            audio_state['duration'] = 0
        vol = (volume or audio_state['volume']) / 100
        pygame.mixer.music.set_volume(vol)
        pygame.mixer.music.play(loops=loops, start=start_pos)
        audio_state['playing'] = True
        audio_state['paused'] = False
        audio_state['current_track'] = os.path.basename(filepath)
        broadcast('audio_state', audio_state)
        log.info(f"Playing: {filepath}")
        return True
    except Exception as e:
        log.error(f"Audio play error: {e}")
        return False

def play_sfx(filepath, volume=None, fade_ms=0):
    """Play a sound effect on a separate channel (doesn't stop music)."""
    global _sfx_channel
    try:
        if filepath not in _sfx_cache:
            if len(_sfx_cache) >= _SFX_CACHE_MAX:
                _sfx_cache.pop(next(iter(_sfx_cache)))
            _sfx_cache[filepath] = pygame.mixer.Sound(filepath)
        sound = _sfx_cache[filepath]
        vol = (volume or sfx_state['volume']) / 100
        sound.set_volume(vol)
        channel = sound.play(fade_ms=int(fade_ms))
        _sfx_channel = channel
        sfx_name = Path(filepath).stem
        duration = round(sound.get_length(), 2)
        sfx_state['playing']  = True
        sfx_state['paused']   = False
        sfx_state['name']     = sfx_name
        sfx_state['duration'] = duration
        broadcast('sfx_state', sfx_state)
        log.info(f"SFX: {filepath}")
        deadline = time.time() + duration + 5
        def _sfx_done(name=sfx_name, ch=channel):
            while ch and ch.get_busy() and time.time() < deadline:
                time.sleep(0.05)
            if sfx_state.get('name') == name and sfx_state.get('playing'):
                sfx_state['playing'] = False
                sfx_state['paused']  = False
                broadcast('sfx_state', sfx_state)
        threading.Thread(target=_sfx_done, daemon=True).start()
    except Exception as e:
        log.error(f"SFX error: {e}")

def fade_audio(duration=None):
    """Fade out currently playing audio (music + active SFX channel)."""
    global _playlist_advance_id
    dur_sec = float(duration or config['audio']['fade_duration'])
    _playlist_advance_id += 1
    playlist_state['active']     = False
    playlist_state['track_name'] = ''
    broadcast('playlist_state', playlist_state)
    my_advance_id = _playlist_advance_id

    def _do_fade():
        # Manual volume ramp — more reliable than pygame fadeout() on SDL/Pi
        if pygame.mixer.music.get_busy():
            orig = pygame.mixer.music.get_volume()
            steps = max(10, int(dur_sec * 20))
            interval = dur_sec / steps
            for i in range(steps - 1, -1, -1):
                if _playlist_advance_id != my_advance_id:
                    # A new fade or play_audio took over — stop meddling
                    return
                if not pygame.mixer.music.get_busy():
                    break
                pygame.mixer.music.set_volume(orig * i / steps)
                time.sleep(interval)
            pygame.mixer.music.stop()
            pygame.mixer.music.set_volume(orig)
        if _sfx_channel and sfx_state.get('playing'):
            _sfx_channel.fadeout(int(dur_sec * 1000))
        audio_state['playing'] = False
        audio_state['paused']  = False
        broadcast('audio_state', audio_state)

    threading.Thread(target=_do_fade, daemon=True).start()

def stop_audio():
    """Immediately stop all audio."""
    global _sfx_channel, _playlist_advance_id
    _playlist_advance_id += 1   # cancel any advance thread
    playlist_state['active']     = False
    playlist_state['track_name'] = ''
    pygame.mixer.music.stop()
    pygame.mixer.stop()
    _sfx_channel = None
    audio_state['playing'] = False
    audio_state['paused'] = False
    audio_state['current_track'] = None
    sfx_state['playing'] = False
    sfx_state['paused']  = False
    broadcast('audio_state', audio_state)
    broadcast('sfx_state', sfx_state)
    broadcast('playlist_state', playlist_state)

def set_volume(v):
    """Set music volume 0-100."""
    audio_state['volume'] = int(v)
    pygame.mixer.music.set_volume(int(v) / 100)
    broadcast('audio_state', audio_state)

def set_sfx_volume(v):
    """Set SFX volume 0-100."""
    sfx_state['volume'] = int(v)
    broadcast('sfx_state', sfx_state)

# ── SKIT TIMER ──
def _timer_state_from_cfg(cfg):
    t = cfg.get('timer', {})
    return {
        'running': False,
        'paused':  False,
        'seconds_remaining': t.get('duration', 180),
        'duration':          t.get('duration', 180),
        'warning_fired': False,
        'expired': False,
        'preset_name':        t.get('active_preset', ''),
        'show_on_display':    t.get('show_on_display', False),
        'warning_at':         t.get('warning_at', 15),
        'start_sound':        t.get('start_sound', ''),
        'start_sound_start':  t.get('start_sound_start', 0),
        'start_sound_duration': t.get('start_sound_duration', 0),
        'start_sound_fade':   t.get('start_sound_fade', 0),
        'start_sound_loop':   t.get('start_sound_loop', True),
        'warning_sound':      t.get('warning_sound', ''),
        'warning_scene':      t.get('warning_scene', ''),
        'end_sound':          t.get('end_sound', ''),
        'end_scene':          t.get('end_scene', ''),
    }

timer_state = _timer_state_from_cfg(config)
timer_thread = None
timer_stop_event = threading.Event()

stopwatch_state = {
    'running': False,
    'start_ms': None,
    'elapsed_ms': 0,
    'show_on_display': False,
}

# Games-tab countdown — fully independent from the skit timer
countdown_state = {
    'running': False,
    'paused':  False,
    'seconds_remaining': 180,
    'duration': 180,
    'expired':  False,
    'show_on_display': False,
    'show_end_display': True,
    'start_sound': '',
    'start_sound_fade': 3.0,
    'end_sound': '',
}
countdown_thread     = None
countdown_stop_event = threading.Event()
_cd_sound_stop       = threading.Event()
_cd_sound_looping    = False  # True only while a looping countdown sound is active
_timer_sound_stop    = threading.Event()
_timer_sound_looping = False  # True only while a looping skit timer sound is active

def timer_worker():
    global timer_state
    while not timer_stop_event.is_set():
        time.sleep(1)
        if not timer_state['running']:
            continue
        timer_state['seconds_remaining'] -= 1
        broadcast('timer_state', timer_state)
        # Warning
        tcfg = load_config().get('timer', {})
        if timer_state['seconds_remaining'] <= tcfg.get('warning_at', 15) \
                and not timer_state['warning_fired']:
            timer_state['warning_fired'] = True
            sfx_name = tcfg.get('warning_sound', '')
            if sfx_name:
                sfx_path = _resolve_audio_file(sfx_name)
                if sfx_path:
                    play_sfx(str(sfx_path))
            warn_scene = tcfg.get('warning_scene', '')
            if warn_scene:
                wled_set_scene(warn_scene)
            disp_file = tcfg.get('warning_display', '')
            if disp_file:
                _broadcast_timer_display(disp_file)
            broadcast('timer_warning', {'seconds_remaining': timer_state['seconds_remaining'],
                                        'warning_fired': True, 'running': True})
            log.info("Timer: warning fired")
        # Expired
        if timer_state['seconds_remaining'] <= 0:
            timer_state['seconds_remaining'] = 0
            timer_state['running'] = False
            timer_state['expired'] = True
            if _timer_sound_looping:
                _timer_sound_stop.set()
                fade_audio(0.6)
                time.sleep(0.7)
            tcfg = load_config().get('timer', {})
            sfx_name = tcfg.get('end_sound', '')
            if sfx_name:
                sfx_path = _resolve_audio_file(sfx_name)
                if sfx_path:
                    play_sfx(str(sfx_path))
            end_scene = tcfg.get('end_scene', '')
            if end_scene:
                wled_set_scene(end_scene)
            disp_file = tcfg.get('end_display', '')
            if disp_file and tcfg.get('show_end_display', True):
                _broadcast_timer_display(disp_file)
            broadcast('timer_expired', {})
            log.info("Timer: expired")

def start_timer_thread():
    global timer_thread, timer_stop_event
    timer_stop_event = threading.Event()
    timer_thread = threading.Thread(target=timer_worker, daemon=True)
    timer_thread.start()

def countdown_worker():
    while not countdown_stop_event.is_set():
        time.sleep(1)
        if not countdown_state['running']:
            continue
        countdown_state['seconds_remaining'] -= 1
        broadcast('countdown_state', countdown_state)
        if countdown_state['seconds_remaining'] <= 0:
            countdown_state['seconds_remaining'] = 0
            countdown_state['running'] = False
            countdown_state['expired'] = True
            if _cd_sound_looping:
                _cd_sound_stop.set()
                fade_audio(0.6)
                time.sleep(0.7)
            sfx_name = countdown_state.get('end_sound', '')
            if sfx_name:
                sfx_path = _resolve_audio_file(sfx_name)
                if sfx_path:
                    play_sfx(str(sfx_path))
            broadcast('countdown_expired', {})
            log.info("Countdown: expired")

def start_countdown_thread():
    global countdown_thread, countdown_stop_event
    countdown_stop_event = threading.Event()
    countdown_thread = threading.Thread(target=countdown_worker, daemon=True)
    countdown_thread.start()

def _resolve_audio_file(name: str, prefer_music: bool = False):
    """Return Path for audio filename, checking sfx/ and music/ dirs."""
    if not name:
        return None
    dirs = ['music', 'sfx'] if prefer_music else ['sfx', 'music']
    for d in dirs:
        p = ASSETS_DIR / d / name
        if p.exists():
            return p
    return None

def load_games() -> dict:
    try:
        with open(GAMES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_games(data: dict):
    try:
        with open(GAMES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"save_games: {e}")

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

    # Apply pole flashlight modes
    from SolixBLE.states import LightStatus
    _ls_map = {'off': LightStatus.OFF, 'low': LightStatus.LOW, 'high': LightStatus.HIGH, 'sos': LightStatus.SOS}
    for pole_label, mode in scene.get('poles', {}).items():
        ls = _ls_map.get((mode or '').lower())
        if ls is None:
            continue
        addr = next((u['address'] for u in config.get('battery_units', []) if u['label'] == pole_label), None)
        if addr:
            ok, err = _batt_cmd(addr, lambda d, _ls=ls: d.set_light_mode(_ls))
            if not ok:
                log.warning(f'Scene pole light [{pole_label}]: {err}')

    global current_scene
    current_scene = scene_id
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
    global _walkup_fade_cancel
    log.info("KILL EVERYTHING fired")
    _walkup_fade_cancel.set()
    _walkup_fade_cancel = threading.Event()
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
    global _walkup_fade_cancel
    _walkup_fade_cancel.set()           # cancel any previous auto-fade
    _walkup_fade_cancel = threading.Event()
    cancel = _walkup_fade_cancel

    color        = item.get('color',  '#FFFFFF')
    color2       = item.get('color2', '#000000')
    walkup_cfg   = item.get('walkup', {})
    assets_cfg   = item.get('assets', {})
    # Resolve walkup music: library ref takes priority over direct upload
    music_lib    = assets_cfg.get('walkup_music_lib', '')
    if music_lib:
        music_file = ASSETS_DIR / 'music' / music_lib
    else:
        music_file = ASSETS_DIR / ('circles' if item_type == 'circle' else 'roles') \
                     / item['id'] / assets_cfg.get('walkup_music', '')
    # Resolve logo URL for display broadcast
    logo_lib     = assets_cfg.get('logo_lib', '')
    logo_url     = f'/assets/display/{logo_lib}' if logo_lib else ''
    walkup_scene = walkup_cfg.get('walkup_scene') or None
    stage_scene  = walkup_cfg.get('stage_scene')  or 'campfire'

    # Lighting at walk-up start: configured scene, or color-flash fallback
    def fire_walkup_lighting():
        if walkup_scene:
            wled_set_scene(walkup_scene)
        else:
            for device in cfg.get('wled_devices', []):
                if device['id'] in ('ezup_ceil',):
                    continue
                wled_set_color(device['id'], color, brightness=80)

    display_payload = {
        'id':              item['id'],
        'name':            item.get('name', ''),
        'color':           color,
        'color2':          color2,
        'type':            item_type,
        'number':          item.get('number', ''),
        'music':           item.get('assets', {}).get('walkup_music', ''),
        'logo':            item.get('assets', {}).get('logo', ''),
        'logo_url':        logo_url,
        'duration':        walkup_cfg.get('duration', 30),
        'animation':       item.get('assets', {}).get('animation',       ''),
        'animation_intro': item.get('assets', {}).get('animation_intro', ''),
        'hide_name':       walkup_cfg.get('hide_name', False),
    }

    # Play music with auto-fade → stage scene transition.
    # display_payload broadcast is deferred until after the crossfade so the
    # video/graphic doesn't appear while the previous music is still fading out.
    def play_music():
        if music_file.exists() and music_file.is_file():
            vol_trim = walkup_cfg.get('volume_trim', 100)
            base_vol = audio_state['volume']
            trimmed  = max(0, min(100, int(base_vol * vol_trim / 100)))
            play_audio(
                str(music_file),
                volume=trimmed,
                start_pos=walkup_cfg.get('start_time', 0)
            )
            # play_audio blocks for the full crossfade — display fires as music starts
            broadcast('display_walkup', display_payload)
            my_session = _audio_session
            duration = walkup_cfg.get('duration', 30)
            fade_dur = walkup_cfg.get('fade_duration', 3)
            def auto_fade(ev=cancel, sess=my_session):
                if ev.wait(timeout=duration):
                    return  # cancelled by new walkup or kill
                if _audio_session != sess:
                    return  # audio changed (playlist, macro, etc.) — don't fade
                fade_audio(fade_dur)
                if ev.wait(timeout=fade_dur + 0.5):
                    return
                if _audio_session == sess:
                    wled_set_scene(stage_scene)
            threading.Thread(target=auto_fade, daemon=True).start()
        else:
            if music_file.name and not music_file.exists():
                log.warning(f"Walk-up music not found: {music_file}")
            if pygame.mixer.music.get_busy():
                fade_audio(3)
                # Wait for fade to finish before showing display
                deadline = time.time() + 4.0
                while pygame.mixer.music.get_busy() and time.time() < deadline:
                    time.sleep(0.1)
            broadcast('display_walkup', display_payload)

    # Track current circle
    global current_circle
    current_circle = item['id']
    # Fire everything concurrently
    threading.Thread(target=fire_walkup_lighting, daemon=True).start()
    threading.Thread(target=play_music, daemon=True).start()
    log.info(f"Walk-up fired: {item.get('name')}")

# ════════════════════════════════════════════
# AUDIO NORMALIZATION
# ════════════════════════════════════════════

_NORM_EXTS = {'.mp3', '.wav', '.wave', '.ogg', '.flac', '.aac', '.m4a'}
_normalize_state = {
    'running':  False,
    'done':     0,
    'total':    0,
    'current':  '',
    'errors':   [],
    'last_run': int(NORM_STAMP.stat().st_mtime) if NORM_STAMP.exists() else 0,
}

def _normalize_file_2pass(fp: Path):
    """Two-pass EBU R128 normalization to -14 LUFS. Overwrites file in-place."""
    ext = fp.suffix.lower()

    # Pass 1 — measure loudness
    r1 = subprocess.run(
        ['ffmpeg', '-nostdin', '-i', str(fp),
         '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json',
         '-f', 'null', '-'],
        capture_output=True, text=True
    )
    idx_start = r1.stderr.rfind('{')
    idx_end   = r1.stderr.rfind('}')
    if idx_start == -1 or idx_end == -1 or idx_end < idx_start:
        raise RuntimeError(f"loudnorm pass 1 produced no JSON for {fp.name}")
    stats = json.loads(r1.stderr[idx_start:idx_end + 1])

    # Pass 2 — apply linear normalization
    af2 = (
        f"loudnorm=I=-14:TP=-1.5:LRA=11:linear=true:"
        f"measured_I={stats['input_i']}:"
        f"measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:"
        f"measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}"
    )
    codec = {
        '.mp3':  ['-c:a', 'libmp3lame', '-q:a', '2'],
        '.wav':  ['-c:a', 'pcm_s16le'],
        '.wave': ['-c:a', 'pcm_s16le'],
    }.get(ext, [])

    tmp = fp.with_suffix('.normalizing' + ext)
    try:
        subprocess.run(
            ['ffmpeg', '-nostdin', '-y', '-i', str(fp),
             '-af', af2, '-ar', '44100'] + codec + [str(tmp)],
            capture_output=True, text=True, check=True
        )
        tmp.replace(fp)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

def _collect_norm_files(mode: str) -> list:
    cutoff = NORM_STAMP.stat().st_mtime if (mode == 'new' and NORM_STAMP.exists()) else 0
    seen   = set()
    files  = []

    def _add(f: Path):
        key = str(f)
        if key in seen or f.name.startswith('._'):
            return
        seen.add(key)
        if cutoff == 0 or f.stat().st_mtime > cutoff:
            files.append(f)

    # Local assets: music library + per-circle + per-role (sfx intentionally excluded)
    local_dirs = [ASSETS_DIR / 'music']
    for base in (ASSETS_DIR / 'circles', ASSETS_DIR / 'roles'):
        if base.exists():
            local_dirs.extend(d for d in base.iterdir() if d.is_dir())
    for d in local_dirs:
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in _NORM_EXTS:
                    _add(f)

    # Entire USB drive, recursively
    mount = _find_usb_mount()
    if mount:
        for f in sorted(Path(mount).rglob('*')):
            if f.is_file() and f.suffix.lower() in _NORM_EXTS:
                _add(f)

    return files

def _run_normalization(mode: str):
    if not shutil.which('ffmpeg'):
        _normalize_state.update({'running': False, 'errors': ['ffmpeg not found on PATH']})
        return
    files = _collect_norm_files(mode)
    _normalize_state.update({'total': len(files), 'done': 0, 'errors': [], 'current': ''})
    for fp in files:
        _normalize_state['current'] = fp.name
        try:
            _normalize_file_2pass(fp)
            log.info(f"Normalized: {fp.name}")
        except Exception as e:
            log.error(f"Normalize error {fp.name}: {e}")
            _normalize_state['errors'].append(fp.name)
        _normalize_state['done'] += 1
    NORM_STAMP.parent.mkdir(parents=True, exist_ok=True)
    NORM_STAMP.touch()
    _normalize_state.update({
        'running':  False,
        'current':  '',
        'last_run': int(NORM_STAMP.stat().st_mtime),
    })
    log.info(f"Normalization done: {len(files)} files, {len(_normalize_state['errors'])} errors")

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

@app.route('/manual')
def manual():
    return send_from_directory(STATIC_DIR, 'MusicMan_Manual.html')

@app.route('/display')
def display():
    resp = send_from_directory(STATIC_DIR, 'display.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/api/display/reload')
def api_display_reload():
    broadcast('display_reload', {})
    return jsonify({'ok': True})

@app.route('/api/display/clear')
def api_display_clear():
    global current_slide
    current_slide = {}
    broadcast('display_clear', {})
    return jsonify({'ok': True})

def _make_qr_data_url(text):
    try:
        import qrcode as _qr
        img = _qr.make(text, border=2)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.error(f"QR generation failed: {e}")
        return ''

@app.route('/api/display/slide', methods=['POST'])
def api_display_slide():
    global current_slide
    data = request.json or {}
    if data.get('template') == 'qr' and data.get('qr_url'):
        data['qr_data_url'] = _make_qr_data_url(data['qr_url'])
    current_slide = data
    broadcast('display_slide', data)
    return jsonify({'ok': True})

@app.route('/api/display/slide/clear')
def api_display_slide_clear():
    global current_slide
    current_slide = {}
    broadcast('display_slide_clear', {})
    return jsonify({'ok': True})

@app.route('/api/slides')
def api_slides_list():
    return jsonify(load_config().get('slides', []))

@app.route('/api/slides', methods=['POST'])
def api_slides_save():
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    cfg = load_config()
    slides = cfg.get('slides', [])
    sid = data.get('id') or f'slide_{int(time.time()*1000)}'
    slide = {k: v for k, v in data.items()}
    slide['id'] = sid
    idx = next((i for i, s in enumerate(slides) if s['id'] == sid), None)
    if idx is not None:
        slides[idx] = slide
    else:
        slides.append(slide)
    cfg['slides'] = slides
    save_config(cfg)
    return jsonify({'ok': True, 'id': sid})

@app.route('/api/slides/<sid>', methods=['DELETE'])
def api_slides_delete(sid):
    cfg = load_config()
    cfg['slides'] = [s for s in cfg.get('slides', []) if s['id'] != sid]
    save_config(cfg)
    return jsonify({'ok': True})

@app.route('/api/slides/<sid>/push')
def api_slides_push(sid):
    global current_slide
    cfg = load_config()
    slide = next((s for s in cfg.get('slides', []) if s['id'] == sid), None)
    if not slide:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    slide_data = dict(slide)
    if slide_data.get('template') == 'qr' and slide_data.get('qr_url'):
        slide_data['qr_data_url'] = _make_qr_data_url(slide_data['qr_url'])
    current_slide = slide_data
    broadcast('display_slide', slide_data)
    return jsonify({'ok': True})

@app.route('/api/display/images')
def api_display_images():
    IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    results = []
    for subdir in ('display', 'circles', 'roles'):
        d = ASSETS_DIR / subdir
        if not d.exists():
            continue
        for f in sorted(d.rglob('*')):
            if f.suffix.lower() in IMAGE_EXTS and f.is_file():
                rel = str(f.relative_to(ASSETS_DIR))
                parts = f.relative_to(d).parts
                label = ' / '.join(p.replace('_', ' ').title() for p in parts)
                results.append({'path': rel, 'label': label, 'dir': subdir})
    return jsonify(results)

@app.route('/api/display/list')
def api_display_list():
    d = ASSETS_DIR / 'display'
    files = sorted(f.name for f in d.iterdir() if f.is_file() and not f.name.startswith('.')) if d.exists() else []
    return jsonify(files)

@app.route('/api/display/show')
def api_display_show():
    circle_id = request.args.get('circle')
    role_id   = request.args.get('role')
    cfg = load_config()
    if circle_id:
        item = next((c for c in cfg.get('circles', []) if c['id'] == circle_id), None)
        if item:
            _broadcast_display_only('circle', item)
            return jsonify({'ok': True})
    elif role_id:
        item = next((r for r in cfg.get('roles', []) if r['id'] == role_id), None)
        if item:
            _broadcast_display_only('role', item)
            return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'not found'}), 404

@app.route('/api/display/show_file')
def api_display_show_file():
    """Show an image or video from assets/display/ on the projector."""
    filename = request.args.get('file', '')
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    ext = Path(filename).suffix.lower()
    video_exts = {'.mp4', '.webm', '.mov', '.ogv'}
    if ext in video_exts:
        broadcast('display_animation', {'video': f'/assets/display/{filename}'})
    else:
        broadcast('display_step', {'image': f'/assets/display/{filename}'})
    return jsonify({'ok': True})

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
        'audio':      audio_state,
        'sfx':        sfx_state,
        'timer':      timer_state,
        'countdown':  countdown_state,
        'stopwatch':  stopwatch_state,
        'playlist':   playlist_state,
        'scene':      current_scene,
        'circle':     current_circle,
        'slide':      current_slide,
        'system': {
            'hostname':   config['system']['hostname'],
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
    try:
        pygame.mixer.music.pause()
        audio_state['paused'] = True
        broadcast('audio_state', audio_state)
        if _sfx_channel and sfx_state.get('playing'):
            _sfx_channel.pause()
            sfx_state['paused'] = True
            broadcast('sfx_state', sfx_state)
    except Exception as e:
        log.warning(f"Audio pause error: {e}")
    return jsonify({'ok': True})

@app.route('/api/audio/resume')
def api_audio_resume():
    try:
        pygame.mixer.music.unpause()
        audio_state['paused'] = False
        broadcast('audio_state', audio_state)
        if _sfx_channel and sfx_state.get('paused'):
            _sfx_channel.unpause()
            sfx_state['paused'] = False
            broadcast('sfx_state', sfx_state)
    except Exception as e:
        log.warning(f"Audio resume error: {e}")
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

@app.route('/api/sfx/volume')
def api_sfx_volume():
    v = request.args.get('v', 80)
    set_sfx_volume(int(v))
    return jsonify({'ok': True, 'volume': int(v)})

# ── SFX ──
@app.route('/api/sfx/play')
def api_sfx_play():
    name = request.args.get('name', '')
    # Try config map first
    sfx_map = {s['id']: s for s in config.get('sfx', [])}
    if name in sfx_map:
        filepath = ASSETS_DIR / 'sfx' / sfx_map[name]['file']
        if filepath.exists():
            play_sfx(str(filepath))
            return jsonify({'ok': True})
    # Fallback: scan sfx directory for matching filename
    sfx_dir = ASSETS_DIR / 'sfx'
    for ext in ('.mp3', '.wav', '.ogg', '.flac', '.aac'):
        filepath = sfx_dir / (name + ext)
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
        was_paused = timer_state.get('paused', False)
        timer_state['paused'] = False
        broadcast('timer_state', timer_state)
        if not was_paused and timer_state.get('show_on_display'):
            broadcast('timer_show', {'seconds': timer_state['seconds_remaining']})
        if not was_paused:
            sfx_name = timer_state.get('start_sound', '')
            if sfx_name:
                audio_path = _resolve_audio_file(sfx_name, prefer_music=True)
                if audio_path:
                    start_pos  = float(timer_state.get('start_sound_start', 0))
                    fade_sec   = float(timer_state.get('start_sound_fade', 0))
                    sound_dur  = float(timer_state.get('start_sound_duration', 0))
                    timer_dur  = timer_state['seconds_remaining']
                    # sound_dur > 0 means play only that many seconds; otherwise full timer length
                    play_dur   = sound_dur if sound_dur > 0 else timer_dur
                    should_loop = timer_state.get('start_sound_loop', True)
                    _timer_sound_stop.clear()
                    global _timer_sound_looping
                    _timer_sound_looping = True
                    def _timer_sound(sp=start_pos, fade=fade_sec, pdur=play_dur,
                                     ap=str(audio_path), ev=_timer_sound_stop,
                                     loop=should_loop):
                        global _timer_sound_looping
                        stem = Path(ap).stem.replace('_', ' ')
                        loops_arg = -1 if loop else 0
                        play_audio(ap, start_pos=sp, loops=loops_arg, crossfade_ms=0)
                        broadcast('music_track', {'name': stem, 'file': Path(ap).name, 'sub': 'SKIT TIMER'})
                        if fade <= 0:
                            ev.wait(timeout=pdur)
                            if not ev.is_set():
                                pygame.mixer.music.stop()
                        else:
                            ev.wait(timeout=max(0.0, pdur - fade))
                            if not ev.is_set():
                                # Fade inline — fade_audio() can be interrupted by _playlist_advance_id
                                # changes from macros or other audio actions, leaving sound looping forever.
                                steps = max(10, int(fade * 20))
                                interval = fade / steps
                                vol = pygame.mixer.music.get_volume()
                                for i in range(steps - 1, -1, -1):
                                    if ev.is_set():
                                        break
                                    pygame.mixer.music.set_volume(vol * i / steps)
                                    time.sleep(interval)
                                pygame.mixer.music.stop()
                                pygame.mixer.music.set_volume(vol)
                        _timer_sound_looping = False
                    threading.Thread(target=_timer_sound, daemon=True).start()
        log.info("Timer started")
    return jsonify({'ok': True, 'timer': timer_state})

@app.route('/api/timer/pause')
def api_timer_pause():
    timer_state['running'] = False
    timer_state['paused']  = True
    broadcast('timer_state', timer_state)
    return jsonify({'ok': True})

@app.route('/api/timer/reset')
def api_timer_reset():
    timer_state['running'] = False
    timer_state['paused']  = False
    timer_state['seconds_remaining'] = config['timer']['duration']
    timer_state['warning_fired'] = False
    timer_state['expired'] = False
    _timer_sound_stop.set()
    if _timer_sound_looping:
        fade_audio(1.0)
    broadcast('timer_state', timer_state)
    broadcast('timer_expired', {})
    log.info("Timer reset")
    return jsonify({'ok': True})

@app.route('/api/timer/toggle')
def api_timer_toggle():
    if timer_state['running']:
        return api_timer_pause()
    else:
        return api_timer_start()

@app.route('/api/timer/config')
def api_timer_get_config():
    return jsonify(load_config().get('timer', {}))

@app.route('/api/timer/show_on_display', methods=['POST'])
def api_timer_set_show_on_display():
    data = request.json or {}
    val  = bool(data.get('show', False))
    timer_state['show_on_display'] = val
    broadcast('timer_state', timer_state)
    return jsonify({'ok': True, 'show_on_display': val})

@app.route('/api/timer/set', methods=['POST'])
def api_timer_console_set():
    """Configure timer duration/audio from the console GAMES tab."""
    data = request.json or {}
    cfg  = load_config()
    t    = cfg.setdefault('timer', {})
    changed = False
    if 'duration' in data:
        dur = max(1, int(data['duration']))
        t['duration'] = dur
        timer_state['duration'] = dur
        if not timer_state['running']:
            timer_state['seconds_remaining'] = dur
        changed = True
    for key in ('start_sound', 'end_sound', 'warning_sound'):
        if key in data:
            t[key] = data[key]
            changed = True
    for key in ('start_sound_fade', 'start_sound_duration', 'start_sound_start'):
        if key in data:
            t[key] = float(data[key])
            changed = True
    if 'start_sound_loop' in data:
        t['start_sound_loop'] = bool(data['start_sound_loop'])
        changed = True
    if 'show_on_display' in data:
        t['show_on_display'] = bool(data['show_on_display'])
        changed = True
    if 'show_end_display' in data:
        t['show_end_display'] = bool(data['show_end_display'])
        changed = True
    if changed:
        save_config(cfg)
        broadcast('timer_state', timer_state)
    return jsonify({'ok': True, 'timer': timer_state})

# ── STOPWATCH ──
@app.route('/api/stopwatch/start')
def api_stopwatch_start():
    if not stopwatch_state['running']:
        stopwatch_state['running'] = True
        stopwatch_state['start_ms'] = int(time.time() * 1000) - stopwatch_state['elapsed_ms']
        stopwatch_state['show_on_display'] = request.args.get('display', '0') == '1'
        broadcast('stopwatch_state', stopwatch_state)
        log.info("Stopwatch started")
    return jsonify({'ok': True, 'stopwatch': stopwatch_state})

@app.route('/api/stopwatch/stop')
def api_stopwatch_stop():
    if stopwatch_state['running']:
        stopwatch_state['elapsed_ms'] = int(time.time() * 1000) - stopwatch_state['start_ms']
        stopwatch_state['running'] = False
        broadcast('stopwatch_state', stopwatch_state)
        log.info(f"Stopwatch stopped at {stopwatch_state['elapsed_ms']}ms")
    return jsonify({'ok': True, 'stopwatch': stopwatch_state})

@app.route('/api/stopwatch/reset')
def api_stopwatch_reset():
    stopwatch_state['running'] = False
    stopwatch_state['start_ms'] = None
    stopwatch_state['elapsed_ms'] = 0
    broadcast('stopwatch_state', stopwatch_state)
    log.info("Stopwatch reset")
    return jsonify({'ok': True, 'stopwatch': stopwatch_state})

# ── COUNTDOWN (games tab) ──
@app.route('/api/countdown/config')
def api_countdown_get_config():
    return jsonify(countdown_state)

@app.route('/api/countdown/set', methods=['POST'])
def api_countdown_set():
    data = request.json or {}
    changed = False
    if 'duration' in data:
        dur = max(1, int(data['duration']))
        countdown_state['duration'] = dur
        if not countdown_state['running']:
            countdown_state['seconds_remaining'] = dur
        changed = True
    for key in ('start_sound', 'end_sound'):
        if key in data:
            countdown_state[key] = data[key]
            changed = True
    if 'start_sound_fade' in data:
        countdown_state['start_sound_fade'] = float(data['start_sound_fade'])
        changed = True
    if 'show_end_display' in data:
        countdown_state['show_end_display'] = bool(data['show_end_display'])
        changed = True
    if changed:
        broadcast('countdown_state', countdown_state)
    return jsonify({'ok': True, 'countdown': countdown_state})

@app.route('/api/countdown/start')
def api_countdown_start():
    if not countdown_state['running']:
        if countdown_state['expired']:
            countdown_state['seconds_remaining'] = countdown_state['duration']
            countdown_state['expired'] = False
        was_paused = countdown_state.get('paused', False)
        countdown_state['running'] = True
        countdown_state['paused'] = False
        broadcast('countdown_show', {'seconds': countdown_state['seconds_remaining']})
        broadcast('countdown_state', countdown_state)
        if not was_paused:
            sfx_name = countdown_state.get('start_sound', '')
            if sfx_name:
                audio_path = _resolve_audio_file(sfx_name, prefer_music=True)
                if audio_path:
                    fade_sec  = float(countdown_state.get('start_sound_fade', 3.0))
                    timer_dur = countdown_state['seconds_remaining']
                    _cd_sound_stop.clear()
                    global _cd_sound_looping
                    _cd_sound_looping = (fade_sec > 0)
                    def _cd_sound(tdur=timer_dur, fade=fade_sec, ap=str(audio_path), ev=_cd_sound_stop):
                        global _cd_sound_looping
                        stem = Path(ap).stem.replace('_',' ')
                        if fade <= 0:
                            play_audio(ap, loops=0, crossfade_ms=0)
                            broadcast('music_track', {'name': stem, 'file': Path(ap).name, 'sub': 'COUNTDOWN'})
                            ev.wait(timeout=tdur)
                            if not ev.is_set():
                                pygame.mixer.music.stop()
                        else:
                            play_audio(ap, loops=-1, crossfade_ms=0)
                            broadcast('music_track', {'name': stem, 'file': Path(ap).name, 'sub': 'COUNTDOWN'})
                            ev.wait(timeout=max(0.0, tdur - fade))
                            if not ev.is_set():
                                # Fade inline — fade_audio() can be interrupted by _playlist_advance_id
                                # changes from macros or other audio actions, leaving sound looping forever.
                                steps = max(10, int(fade * 20))
                                interval = fade / steps
                                vol = pygame.mixer.music.get_volume()
                                for i in range(steps - 1, -1, -1):
                                    if ev.is_set():
                                        break
                                    pygame.mixer.music.set_volume(vol * i / steps)
                                    time.sleep(interval)
                                pygame.mixer.music.stop()
                                pygame.mixer.music.set_volume(vol)
                        _cd_sound_looping = False
                    threading.Thread(target=_cd_sound, daemon=True).start()
                else:
                    log.warning(f"Countdown start_sound not found: {sfx_name}")
        log.info("Countdown started")
    return jsonify({'ok': True, 'countdown': countdown_state})

@app.route('/api/countdown/pause')
def api_countdown_pause():
    countdown_state['running'] = False
    countdown_state['paused'] = True
    _cd_sound_stop.set()
    if _cd_sound_looping:
        fade_audio(1.5)
    broadcast('countdown_state', countdown_state)
    return jsonify({'ok': True})

@app.route('/api/countdown/reset')
def api_countdown_reset():
    countdown_state['running'] = False
    countdown_state['paused'] = False
    countdown_state['seconds_remaining'] = countdown_state['duration']
    countdown_state['expired'] = False
    _cd_sound_stop.set()
    if _cd_sound_looping:
        fade_audio(1.0)
    broadcast('countdown_state', countdown_state)
    return jsonify({'ok': True, 'countdown': countdown_state})

# ── GAME TIMES ──
@app.route('/api/games')
def api_games_list():
    return jsonify(load_games())

@app.route('/api/games/record', methods=['POST'])
def api_games_record():
    data  = request.json or {}
    game  = data.get('game', '').strip()
    label = data.get('label', '').strip() or 'Runner'
    ms    = int(data.get('ms', 0))
    if not game or ms <= 0:
        return jsonify({'ok': False, 'error': 'missing game or ms'}), 400
    games = load_games()
    if game not in games:
        games[game] = []
    games[game].append({'label': label, 'ms': ms, 'at': int(time.time())})
    games[game].sort(key=lambda x: x['ms'])
    save_games(games)
    broadcast('games_state', games)
    return jsonify({'ok': True, 'games': games})

@app.route('/api/games/new', methods=['POST'])
def api_games_new():
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'missing name'}), 400
    games = load_games()
    if name not in games:
        games[name] = []
        save_games(games)
        broadcast('games_state', games)
    return jsonify({'ok': True, 'games': games})

@app.route('/api/games/clear', methods=['POST'])
def api_games_clear():
    data = request.json or {}
    name = data.get('name', '').strip()
    games = load_games()
    if name in games:
        games[name] = []
        save_games(games)
        broadcast('games_state', games)
    return jsonify({'ok': True})

@app.route('/api/games/delete', methods=['POST'])
def api_games_delete():
    data = request.json or {}
    name = data.get('name', '').strip()
    games = load_games()
    if name in games:
        del games[name]
        save_games(games)
        broadcast('games_state', games)
    return jsonify({'ok': True})

@app.route('/api/games/display', methods=['POST'])
def api_games_display():
    data  = request.json or {}
    game  = data.get('game', '').strip()
    sort  = data.get('sort', 'asc')
    games = load_games()
    times = sorted(games.get(game, []), key=lambda t: t.get('ms', 0), reverse=(sort == 'desc'))
    broadcast('display_leaderboard', {'game': game, 'times': times})
    return jsonify({'ok': True})

# ── LIGHTING ──
@app.route('/api/lights/scene')
def api_lights_scene():
    name = request.args.get('name', 'campfire')
    threading.Thread(target=wled_set_scene, args=(name,), daemon=True).start()
    return jsonify({'ok': True, 'scene': name})

@app.route('/api/lights/brightness')
def api_lights_brightness():
    v = int(request.args.get('v', 75))
    bri = int(v * 2.55)
    devices = [d for d in config.get('wled_devices', []) if not d.get('independent')]
    def _apply():
        for device in devices:
            try:
                requests.post(f"http://{device['ip']}/json/state",
                              json={'bri': bri}, timeout=0.5)
            except Exception as e:
                log.warning(f"WLED {device['id']} brightness error: {e}")
    threading.Thread(target=_apply, daemon=True).start()
    return jsonify({'ok': True, 'brightness': v})

# ── WALK-UP ──
@app.route('/api/scenes')
def api_get_scenes():
    cfg = load_config()
    return jsonify(cfg.get('scenes', []))

@app.route('/api/macros')
def api_get_macros():
    cfg = load_config()
    return jsonify(cfg.get('macros', []))

@app.route('/api/admin/scene', methods=['POST'])
def api_save_scene():
    global config
    data = request.get_json()
    cfg  = load_config()
    scenes = cfg.get('scenes', [])
    existing = next((s for s in scenes if s['id'] == data.get('id')), None)
    if existing:
        existing.update(data)
    else:
        scenes.append(data)
    cfg['scenes'] = scenes
    save_config(cfg)
    config = cfg
    return jsonify({'ok': True})

@app.route('/api/admin/scene/delete', methods=['POST'])
def api_delete_scene():
    global config
    data = request.get_json()
    cfg  = load_config()
    cfg['scenes'] = [s for s in cfg.get('scenes', []) if s['id'] != data.get('id')]
    save_config(cfg)
    config = cfg
    return jsonify({'ok': True})

@app.route('/api/admin/macro', methods=['POST'])
def api_save_macro():
    global config
    data = request.get_json()
    cfg  = load_config()
    macros = cfg.get('macros', [])
    existing = next((m for m in macros if m['id'] == data.get('id')), None)
    if existing:
        existing.update(data)
    else:
        macros.append(data)
    cfg['macros'] = macros
    save_config(cfg)
    config = cfg
    return jsonify({'ok': True})

@app.route('/api/admin/macro/delete', methods=['POST'])
def api_delete_macro():
    global config
    data = request.get_json()
    cfg  = load_config()
    cfg['macros'] = [m for m in cfg.get('macros', []) if m['id'] != data.get('id')]
    save_config(cfg)
    config = cfg
    return jsonify({'ok': True})

@app.route('/api/macro/run')
def api_macro_run():
    name   = request.args.get('name', '')
    cfg    = load_config()
    macros = {m['id']: m for m in cfg.get('macros', [])}
    if name not in macros:
        log.warning(f"Macro not found: {name}")
        return jsonify({'ok': False, 'error': f'Macro not found: {name}'}), 404
    # Check if this macro is part of the show flow and broadcast which step fired
    macro_obj = macros[name]
    step_name  = macro_obj.get('name', name)
    step_color = '#F5A623'
    for idx, entry in enumerate(cfg.get('show_flow', [])):
        if entry.get('macro_id') == name:
            step_name  = entry.get('name') or step_name
            step_color = entry.get('color', step_color)
            broadcast('show_step', {'index': idx, 'macro_id': name, 'name': step_name, 'desc': entry.get('desc', '')})
            break
    # Always update projector display when a macro fires
    disp = {'name': step_name, 'color': step_color}
    img  = macro_obj.get('display_image', '')
    if img:
        disp['image'] = f'/assets/macros/{name}/{img}'
    broadcast('display_step', disp)
    threading.Thread(target=execute_macro, args=(macro_obj,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/show/fire')
def api_show_fire():
    """Fire a show-flow step by index. Delegates to macro/run logic."""
    try:
        idx = int(request.args.get('index', -1))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid index'}), 400
    cfg  = load_config()
    flow = cfg.get('show_flow', [])
    if idx < 0 or idx >= len(flow):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404

    entry     = flow[idx]
    step_type = entry.get('type', 'macro')
    step_desc = entry.get('desc', '')
    if step_type == 'circle':
        target_id = entry.get('target_id', '')
        broadcast('show_step', {'index': idx, 'name': entry.get('name', target_id), 'desc': step_desc})
        threading.Thread(target=fire_walkup, kwargs={'circle_id': target_id}, daemon=True).start()
    elif step_type == 'role':
        target_id = entry.get('target_id', '')
        broadcast('show_step', {'index': idx, 'name': entry.get('name', target_id), 'desc': step_desc})
        threading.Thread(target=fire_walkup, kwargs={'role_id': target_id}, daemon=True).start()
    else:
        macro_id  = entry.get('macro_id', '')
        macros    = {m['id']: m for m in cfg.get('macros', [])}
        macro_obj = macros.get(macro_id)
        if not macro_obj:
            log.warning(f'show/fire: macro not found: {macro_id!r}')
            return jsonify({'ok': False, 'error': f'macro not found: {macro_id}'}), 404
        step_name  = entry.get('name') or macro_obj.get('name', macro_id)
        step_color = entry.get('color', '#F5A623')
        broadcast('show_step', {'index': idx, 'macro_id': macro_id, 'name': step_name, 'desc': step_desc})
        disp = {'name': step_name, 'color': step_color}
        img  = macro_obj.get('display_image', '')
        if img:
            disp['image'] = f'/assets/macros/{macro_id}/{img}'
        broadcast('display_step', disp)
        threading.Thread(target=execute_macro, args=(macro_obj,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/show/reset')
def api_show_reset():
    broadcast('show_step', {'index': None})
    return jsonify({'ok': True})

def _broadcast_display_only(item_type, item):
    """Show a circle/role's display without triggering audio or lighting."""
    assets = item.get('assets', {})
    broadcast('display_walkup', {
        'id':        item.get('id', ''),
        'name':      item.get('name', ''),
        'color':     item.get('color', '#F5A623'),
        'type':      item_type,
        'animation': assets.get('animation', ''),
        'logo':      assets.get('logo', ''),
    })

_VIDEO_EXTS_PY = {'.mp4', '.webm', '.mov', '.ogv'}

def _broadcast_timer_display(filename):
    ext = Path(filename).suffix.lower()
    if ext in _VIDEO_EXTS_PY:
        broadcast('display_animation', {'video': f'/assets/display/{filename}'})
    else:
        broadcast('display_step', {'image': f'/assets/display/{filename}'})

_AUDIO_ACTIONS = {'music', 'play_playlist', 'audio_stop', 'audio_fade', 'walkup_circle', 'walkup_role'}

def execute_macro(macro):
    global config, current_slide
    log.info(f"Executing macro: {macro.get('id')}")
    steps = macro.get('steps', [])
    if not any(s.get('action') in _AUDIO_ACTIONS for s in steps):
        if pygame.mixer.music.get_busy():
            fade_audio(3)
    for step in steps:
        action = step.get('action', '')
        try:
            if action == 'scene':
                wled_set_scene(step.get('scene', ''))
            elif action == 'sfx':
                sfx_name = step.get('name', '')
                sfx_dir  = ASSETS_DIR / 'sfx'
                for ext in ('.mp3', '.wav', '.ogg', '.flac'):
                    fp = sfx_dir / (sfx_name + ext)
                    if fp.exists():
                        play_sfx(str(fp))
                        break
                else:
                    log.warning(f"Macro SFX not found: {sfx_name}")
            elif action == 'audio_stop':
                kill_everything()
            elif action == 'audio_fade':
                fade_audio(step.get('duration', 3))
            elif action == 'timer_start':
                if not timer_state['running']:
                    timer_state['running'] = True
                    timer_state['start_time'] = time.time()
                    broadcast('timer_state', timer_state)
            elif action == 'timer_reset':
                timer_state['running'] = False
                timer_state['seconds_remaining'] = config.get('timer', {}).get('duration', 180)
                timer_state['warning_fired'] = False
                timer_state['expired'] = False
                broadcast('timer_state', timer_state)
            elif action == 'setup_skit_timer':
                preset_name = step.get('preset', '')
                _cfg = load_config()
                presets = _cfg.get('skit_timer_presets', {})
                preset = presets.get(preset_name)
                if preset:
                    t = _cfg.setdefault('timer', {})
                    t.update(preset)
                    t['active_preset'] = preset_name
                    save_config(_cfg)
                    config = _cfg
                    new_ts = _timer_state_from_cfg(_cfg)
                    was_running = timer_state['running']
                    secs_remaining = timer_state['seconds_remaining'] if was_running else new_ts['duration']
                    show_on_display = timer_state.get('show_on_display', False)  # preserve console toggle
                    timer_state.update(new_ts)
                    timer_state['running'] = was_running
                    timer_state['seconds_remaining'] = secs_remaining
                    timer_state['preset_name'] = preset_name
                    timer_state['show_on_display'] = show_on_display
                    broadcast('timer_state', timer_state)
                    broadcast('timer_preset_loaded', {'name': preset_name, 'duration': timer_state['duration']})
                    log.info(f"Skit timer configured from preset: {preset_name!r}")
                else:
                    log.warning(f"Skit timer preset not found: {preset_name!r}")
            elif action == 'music':
                fname = step.get('file', '')
                do_loop = -1 if step.get('loop') else 0
                music_dir = ASSETS_DIR / 'music'
                candidates = [music_dir / fname] + \
                    [music_dir / (fname + ext) for ext in ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a')]
                for fp in candidates:
                    if fp.exists():
                        play_audio(str(fp), loops=do_loop)
                        break
                else:
                    log.warning(f"Macro music not found: {fname}")
            elif action == 'play_playlist':
                path    = step.get('path', '')
                shuffle = bool(step.get('shuffle', False))
                if path:
                    if not Path(path).is_absolute():
                        mount = _find_usb_mount()
                        if mount:
                            path = str(Path(mount) / path)
                    play_playlist(path, shuffle=shuffle)
                else:
                    log.warning('play_playlist action: no path')
            elif action == 'walkup_circle':
                circle_id = step.get('circle_id', '')
                if circle_id:
                    threading.Thread(target=fire_walkup, kwargs={'circle_id': circle_id}, daemon=True).start()
            elif action == 'walkup_role':
                role_id = step.get('role_id', '')
                if role_id:
                    threading.Thread(target=fire_walkup, kwargs={'role_id': role_id}, daemon=True).start()
            elif action == 'wait':
                time.sleep(float(step.get('seconds', 1)))
            elif action == 'display_image':
                file = step.get('file', '')
                if file:
                    broadcast('display_step', {'image': f'/assets/display/{file}'})
                dur = float(step.get('duration', 3))
                if dur > 0:
                    time.sleep(dur)
            elif action == 'display_anim':
                file = step.get('file', '')
                dur  = float(step.get('duration', 5))
                loop = bool(step.get('loop', False))
                hold = not loop and (dur == 0)
                if file:
                    broadcast('display_animation', {'video': f'/assets/display/{file}', 'hold': hold, 'loop': loop})
                if dur > 0 and not loop:
                    time.sleep(dur)
            elif action == 'display_circle':
                cid  = step.get('circle_id', '')
                _cfg = load_config()
                item = next((c for c in _cfg.get('circles', []) if c['id'] == cid), None)
                if item:
                    _broadcast_display_only('circle', item)
            elif action == 'display_role':
                rid  = step.get('role_id', '')
                _cfg = load_config()
                item = next((r for r in _cfg.get('roles', []) if r['id'] == rid), None)
                if item:
                    _broadcast_display_only('role', item)
            elif action == 'slide':
                slide_data = {k: v for k, v in step.items() if k != 'action'}
                if slide_data.get('template') == 'qr' and slide_data.get('qr_url'):
                    slide_data['qr_data_url'] = _make_qr_data_url(slide_data['qr_url'])
                current_slide = slide_data
                broadcast('display_slide', slide_data)
            elif action == 'slide_clear':
                current_slide = {}
                broadcast('display_slide_clear', {})
            elif action == 'saved_slide':
                sid  = step.get('slide_id', '')
                _cfg = load_config()
                _sl  = next((s for s in _cfg.get('slides', []) if s['id'] == sid), None)
                if _sl:
                    slide_data = dict(_sl)
                    if slide_data.get('template') == 'qr' and slide_data.get('qr_url'):
                        slide_data['qr_data_url'] = _make_qr_data_url(slide_data['qr_url'])
                    current_slide = slide_data
                    broadcast('display_slide', slide_data)
            log.info(f"Macro step done: {action}")
        except Exception as e:
            log.error(f"Macro step error ({action}): {e}")

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
    global config
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
               ('walkup.mp3'       if asset_type == 'walkup_music'    else
                'logo.png'         if asset_type == 'logo'            else
                'walkup.mp4'       if asset_type == 'animation'       else
                'walkup_intro.mp4' if asset_type == 'animation_intro' else f.filename)
    elif target_type == 'role' and target_id:
        dest = ASSETS_DIR / 'roles' / target_id / \
               ('walkup.mp3'       if asset_type == 'walkup_music'    else
                'logo.png'         if asset_type == 'logo'            else
                'walkup.mp4'       if asset_type == 'animation'       else
                'walkup_intro.mp4' if asset_type == 'animation_intro' else f.filename)
    elif target_type == 'macro' and target_id:
        ext  = Path(f.filename).suffix.lower() or '.png'
        dest = ASSETS_DIR / 'macros' / target_id / ('display_image' + ext)
    elif target_type == 'display':
        dest = ASSETS_DIR / 'display' / f.filename
    else:
        return jsonify({'ok': False, 'error': 'Unknown target type'}), 400
    dest.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(dest))
    stored_name = dest.name  # use the actual saved filename (logo.png, walkup.mp3, etc.)
    log.info(f"File uploaded: {dest}")

    # Update config.yaml with the filename so UI reflects saved state
    cfg = load_config()
    if target_type == 'circle' and target_id:
        circles = cfg.get('circles', [])
        circle = next((c for c in circles if c['id'] == target_id), None)
        if circle:
            if 'assets' not in circle:
                circle['assets'] = {}
            if asset_type == 'logo':
                circle['assets']['logo'] = stored_name
                circle['assets'].pop('logo_lib', None)
            elif asset_type == 'walkup_music':
                circle['assets']['walkup_music'] = stored_name
                circle['assets'].pop('walkup_music_lib', None)
            elif asset_type in ('animation', 'animation_intro'):
                circle['assets'][asset_type] = stored_name
            save_config(cfg)
            config = cfg
            log.info(f"Config updated: {target_id}.assets.{asset_type} = {stored_name}")
    elif target_type == 'role' and target_id:
        roles = cfg.get('roles', [])
        role = next((r for r in roles if r['id'] == target_id), None)
        if role:
            if 'assets' not in role:
                role['assets'] = {}
            if asset_type == 'logo':
                role['assets']['logo'] = stored_name
                role['assets'].pop('logo_lib', None)
            elif asset_type == 'walkup_music':
                role['assets']['walkup_music'] = stored_name
                role['assets'].pop('walkup_music_lib', None)
            elif asset_type in ('animation', 'animation_intro'):
                role['assets'][asset_type] = stored_name
            save_config(cfg)
            config = cfg
            log.info(f"Config updated: {target_id}.assets.{asset_type} = {stored_name}")
    elif target_type == 'macro' and target_id:
        macros_list = cfg.get('macros', [])
        macro = next((m for m in macros_list if m['id'] == target_id), None)
        if macro:
            macro['display_image'] = stored_name
            save_config(cfg)
            log.info(f"Config updated: macro {target_id}.display_image = {stored_name}")
    return jsonify({'ok': True, 'path': str(dest), 'filename': stored_name})

# ── ADMIN — CLEAR ASSET ──

@app.route('/api/admin/asset/clear', methods=['POST'])
def api_asset_clear():
    data        = request.get_json(force=True)
    target_type = data.get('type')
    target_id   = data.get('id')
    asset_type  = data.get('asset')
    if not all([target_type, target_id, asset_type]):
        return jsonify({'ok': False, 'error': 'Missing fields'})
    cfg = load_config()
    collection = cfg.get(target_type + 's', [])
    item = next((x for x in collection if x['id'] == target_id), None)
    if not item:
        return jsonify({'ok': False, 'error': 'Not found'})
    assets = item.setdefault('assets', {})
    for key in [asset_type, asset_type + '_lib']:
        assets.pop(key, None)
    save_config(cfg)
    log.info(f"Cleared asset {asset_type} from {target_type} {target_id}")
    return jsonify({'ok': True})

# ── ADMIN — GENERATE ANIMATION ──

@app.route('/api/admin/upload_display', methods=['POST'])
def api_upload_display():
    """Upload default standby image for the projector display."""
    import shutil
    file = request.files.get('file')
    if not file:
        return jsonify({'ok': False, 'error': 'No file'})
    dest_dir = ASSETS_DIR / 'display'
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext  = Path(file.filename).suffix.lower() or '.png'
    dest = dest_dir / ('default' + ext)
    file.save(str(dest))
    if ext != '.png':
        shutil.copy2(str(dest), str(dest_dir / 'default.png'))
    log.info(f'Display default image uploaded: {dest}')
    return jsonify({'ok': True})

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
    os.system('echo BrokenArrow | sudo -S systemctl restart musicman.service')
    return jsonify({'ok': True})

@app.route('/api/system/shutdown', methods=['POST'])
def api_system_shutdown():
    log.info("Shutdown requested via API")
    threading.Thread(target=lambda: (time.sleep(1), os.system('echo BrokenArrow | sudo -S shutdown -h now')), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/usb/list')
def api_usb_list():
    import shutil
    mount = _find_usb_mount()
    if not mount:
        return jsonify({'mounted': None, 'items': [], 'disk': None})
    items = []
    try:
        for entry in sorted(Path(mount).iterdir()):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir():
                count = sum(1 for f in entry.rglob('*') if f.suffix.lower() in AUDIO_EXTS and f.is_file())
                if count:
                    items.append({'name': entry.name, 'type': 'dir', 'path': str(entry), 'count': count})
            elif entry.suffix.lower() == '.m3u':
                items.append({'name': entry.name, 'type': 'm3u', 'path': str(entry), 'count': 0})
        total = sum(1 for f in Path(mount).rglob('*') if f.suffix.lower() in AUDIO_EXTS and f.is_file())
        if total:
            items.insert(0, {'name': 'All Files', 'type': 'dir', 'path': mount, 'count': total})
    except Exception as e:
        log.error(f'USB list error: {e}')
    disk = None
    try:
        du = shutil.disk_usage(mount)
        disk = {
            'used_gb':  round(du.used  / 1e9, 1),
            'free_gb':  round(du.free  / 1e9, 1),
            'total_gb': round(du.total / 1e9, 1),
            'pct':      round(du.used  / du.total * 100, 1),
        }
    except Exception:
        pass
    return jsonify({'mounted': mount, 'items': items, 'disk': disk})

@app.route('/api/playlist/play')
def api_playlist_play():
    path    = request.args.get('path', '')
    shuffle = request.args.get('shuffle', '0') == '1'
    if not path:
        return jsonify({'ok': False, 'error': 'no path'}), 400
    if not Path(path).is_absolute():
        mount = _find_usb_mount()
        path  = str(Path(mount) / path) if mount else path
    threading.Thread(target=play_playlist, args=(path, shuffle), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/playlist/next')
def api_playlist_next():
    if playlist_state.get('active'):
        idx = playlist_state['index'] + 1
        threading.Thread(target=_advance_playlist_to, args=(idx,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/playlist/prev')
def api_playlist_prev():
    if playlist_state.get('active'):
        idx = max(0, playlist_state['index'] - 1)
        threading.Thread(target=_advance_playlist_to, args=(idx,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/audio/reset-mixer', methods=['POST'])
def api_audio_reset_mixer():
    global _sfx_cache, _sfx_channel
    stop_audio()
    _sfx_cache.clear()
    _sfx_channel = None
    pygame.mixer.quit()
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
    pygame.mixer.set_num_channels(8)
    log.info("Mixer reset")
    return jsonify({'ok': True})

@app.route('/api/audio/normalize', methods=['POST'])
def api_audio_normalize():
    if _normalize_state['running']:
        return jsonify({'ok': False, 'error': 'Already running'}), 400
    mode = (request.get_json(silent=True) or {}).get('mode', 'all')
    _normalize_state['running'] = True
    threading.Thread(target=_run_normalization, args=(mode,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/audio/normalize/status')
def api_audio_normalize_status():
    s = dict(_normalize_state)
    # Count new files when idle
    if not s['running']:
        cutoff = NORM_STAMP.stat().st_mtime if NORM_STAMP.exists() else 0
        cnt = 0
        for base in [ASSETS_DIR / 'music']:
            if base.exists():
                for f in base.iterdir():
                    if f.is_file() and f.suffix.lower() in _NORM_EXTS and f.stat().st_mtime > cutoff:
                        cnt += 1
        s['new_count'] = cnt
    else:
        s['new_count'] = 0
    return jsonify(s)

# ── CIRCLES AND ROLES API ──
@app.route('/api/circles')
def api_get_circles():
    cfg = load_config()
    return jsonify(cfg.get('circles', []))

@app.route('/api/roles')
def api_get_roles():
    cfg = load_config()
    return jsonify(cfg.get('roles', []))

@app.route('/api/show_flow')
def api_get_show_flow():
    cfg = load_config()
    flow = cfg.get('show_flow', [])
    macros = {m['id']: m for m in cfg.get('macros', [])}
    result = []
    for entry in flow:
        macro_id = entry.get('macro_id', '')
        macro = macros.get(macro_id, {})
        result.append({
            'id':       entry.get('id', macro_id),
            'name':     entry.get('name') or macro.get('name', macro_id),
            'color':    entry.get('color', '#F5A623'),
            'macro_id': macro_id,
        })
    return jsonify(result)

@app.route('/api/admin/show_flow', methods=['POST'])
def api_save_show_flow():
    cfg = load_config()
    cfg['show_flow'] = request.json or []
    save_config(cfg)
    return jsonify({'ok': True})

@app.route('/api/admin/timer', methods=['POST'])
def api_save_timer():
    cfg  = load_config()
    data = request.json or {}
    t    = cfg.setdefault('timer', {})
    if 'duration'      in data: t['duration']      = int(data['duration'])
    if 'warning_at'    in data: t['warning_at']    = int(data['warning_at'])
    if 'start_sound'          in data: t['start_sound']          = data['start_sound']
    if 'start_sound_start'    in data: t['start_sound_start']    = float(data['start_sound_start'])
    if 'start_sound_duration' in data: t['start_sound_duration'] = float(data['start_sound_duration'])
    if 'start_sound_fade'     in data: t['start_sound_fade']     = float(data['start_sound_fade'])
    if 'warning_sound'    in data: t['warning_sound']    = data['warning_sound']
    if 'warning_scene'    in data: t['warning_scene']    = data['warning_scene']
    if 'warning_display'  in data: t['warning_display']  = data['warning_display']
    if 'end_sound'        in data: t['end_sound']        = data['end_sound']
    if 'end_scene'        in data: t['end_scene']        = data['end_scene']
    if 'end_display'      in data: t['end_display']      = data['end_display']
    save_config(cfg)
    # Sync all params into live timer_state
    new_ts = _timer_state_from_cfg(cfg)
    was_running = timer_state['running']
    secs = timer_state['seconds_remaining'] if was_running else new_ts['duration']
    timer_state.update(new_ts)
    timer_state['running'] = was_running
    timer_state['seconds_remaining'] = secs
    broadcast('timer_state', timer_state)
    return jsonify({'ok': True})

@app.route('/api/admin/timer/presets')
def api_get_timer_presets():
    cfg = load_config()
    return jsonify(cfg.get('skit_timer_presets', {}))

@app.route('/api/admin/timer/preset/save', methods=['POST'])
def api_save_timer_preset():
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    cfg = load_config()
    presets = cfg.setdefault('skit_timer_presets', {})
    # Start from existing timer config, then overlay any values passed directly
    snapshot = {k: v for k, v in cfg.get('timer', {}).items()}
    for key in ('duration', 'warning_at', 'start_sound', 'start_sound_start',
                'start_sound_duration', 'start_sound_fade', 'start_sound_loop',
                'warning_sound', 'warning_scene', 'warning_display',
                'end_sound', 'end_scene', 'end_display', 'show_on_display'):
        if key in data:
            snapshot[key] = data[key]
    if 'duration' in snapshot:
        snapshot['duration'] = int(snapshot['duration'])
    presets[name] = snapshot
    save_config(cfg)
    return jsonify({'ok': True, 'presets': presets})

@app.route('/api/admin/timer/preset/delete', methods=['POST'])
def api_delete_timer_preset():
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    cfg = load_config()
    presets = cfg.get('skit_timer_presets', {})
    presets.pop(name, None)
    cfg['skit_timer_presets'] = presets
    save_config(cfg)
    return jsonify({'ok': True, 'presets': presets})

@app.route('/api/timer/preset/load', methods=['POST'])
def api_load_timer_preset():
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    cfg = load_config()
    presets = cfg.get('skit_timer_presets', {})
    preset = presets.get(name)
    if not preset:
        return jsonify({'ok': False, 'error': 'preset not found'}), 404
    t = cfg.setdefault('timer', {})
    t.update(preset)
    t['active_preset'] = name
    save_config(cfg)
    global config
    config = cfg
    new_ts = _timer_state_from_cfg(cfg)
    was_running = timer_state['running']
    secs_remaining = timer_state['seconds_remaining'] if was_running else new_ts['duration']
    show_on_display = timer_state.get('show_on_display', False)
    timer_state.update(new_ts)
    timer_state['running'] = was_running
    timer_state['seconds_remaining'] = secs_remaining
    timer_state['preset_name'] = name
    timer_state['show_on_display'] = show_on_display
    broadcast('timer_state', timer_state)
    broadcast('timer_preset_loaded', {'name': name, 'duration': timer_state['duration']})
    log.info(f"Timer preset loaded: {name!r}")
    return jsonify({'ok': True, 'timer': timer_state})

@app.route('/api/music/list')
def api_get_music():
    music_dir = ASSETS_DIR / 'music'
    if music_dir.exists():
        exts = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a'}
        files = sorted(f.name for f in music_dir.iterdir() if f.suffix.lower() in exts)
        return jsonify(files)
    return jsonify([])

@app.route('/api/music/play')
def api_music_play():
    """Play a single track from assets/music/ (no playlist auto-advance)."""
    global _playlist_advance_id
    filename = request.args.get('file', '')
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    music_dir = ASSETS_DIR / 'music'
    filepath  = (music_dir / filename).resolve()
    try:
        filepath.relative_to(music_dir.resolve())
    except ValueError:
        return jsonify({'ok': False, 'error': 'invalid path'}), 400
    if not filepath.exists():
        return jsonify({'ok': False, 'error': 'file not found'}), 404
    # Cancel any active playlist
    _playlist_advance_id += 1
    playlist_state['active']     = False
    playlist_state['track_name'] = ''
    broadcast('playlist_state', playlist_state)
    stem = Path(filename).stem
    def _play():
        play_audio(str(filepath))
        broadcast('music_track', {'name': stem, 'file': filename})
    threading.Thread(target=_play, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/sfx/list')
def api_get_sfx():
    # Return actual files from sfx directory so the console shows what's available
    sfx_dir = ASSETS_DIR / 'sfx'
    if sfx_dir.exists():
        exts = {'.mp3', '.wav', '.ogg', '.flac', '.aac'}
        files = sorted(f.name for f in sfx_dir.iterdir() if f.suffix.lower() in exts)
        if files:
            return jsonify(files)
    # Fall back to config list
    cfg = load_config()
    return jsonify(cfg.get('sfx', []))

# ── FILE MANAGEMENT ──
@app.route('/api/admin/file/delete', methods=['POST'])
def api_file_delete():
    data      = request.get_json()
    file_type = data.get('type')   # 'sfx' or 'music'
    name      = data.get('name', '')
    if file_type == 'sfx':
        target_dir = ASSETS_DIR / 'sfx'
    elif file_type == 'music':
        target_dir = ASSETS_DIR / 'music'
    elif file_type == 'display':
        target_dir = ASSETS_DIR / 'display'
    else:
        return jsonify({'ok': False, 'error': 'Unknown type'}), 400
    filepath = target_dir / name
    # Safety: must be inside the expected directory
    if not filepath.resolve().is_relative_to(target_dir.resolve()):
        return jsonify({'ok': False, 'error': 'Invalid path'}), 400
    if not filepath.exists():
        return jsonify({'ok': False, 'error': 'File not found'}), 404
    filepath.unlink()
    log.info(f"Deleted {file_type} file: {name}")
    return jsonify({'ok': True})

@app.route('/api/admin/asset/link', methods=['POST'])
def api_asset_link():
    """Store a reference to an existing library file on a circle/role asset (no copy)."""
    global config
    import shutil as _shutil
    data      = request.get_json()
    dest_type = data.get('dest_type', '')  # 'circle' or 'role'
    dest_id   = data.get('dest_id',   '')
    asset     = data.get('asset',     '')  # 'walkup_music' or 'logo'
    src_type  = data.get('src_type',  '')  # 'music' or 'display'
    src_file  = data.get('src_file',  '')
    if not all([dest_type, dest_id, asset, src_type]):
        return jsonify({'ok': False, 'error': 'missing fields'}), 400
    if src_type == 'music':
        ref_key  = 'walkup_music_lib'
        src_path = ASSETS_DIR / 'music' / src_file if src_file else None
    elif src_type == 'display':
        ref_key  = 'logo_lib'
        src_path = ASSETS_DIR / 'display' / src_file if src_file else None
    else:
        return jsonify({'ok': False, 'error': 'invalid src_type'}), 400
    if src_file and (src_path is None or not src_path.exists()):
        return jsonify({'ok': False, 'error': 'source file not found'}), 404
    cfg   = load_config()
    items = cfg.get('circles' if dest_type == 'circle' else 'roles', [])
    item  = next((x for x in items if x['id'] == dest_id), None)
    if not item:
        return jsonify({'ok': False, 'error': 'item not found'}), 404
    if 'assets' not in item:
        item['assets'] = {}
    if src_file:
        item['assets'][ref_key] = src_file
    else:
        item['assets'].pop(ref_key, None)
    save_config(cfg)
    config = cfg
    log.info(f"Asset link: {dest_type} {dest_id} {ref_key}={src_file!r}")
    return jsonify({'ok': True})

@app.route('/api/admin/file/rename', methods=['POST'])
def api_file_rename():
    data      = request.get_json()
    file_type = data.get('type')
    old_name  = data.get('old', '')
    new_name  = data.get('new', '').strip()
    if file_type == 'sfx':
        target_dir = ASSETS_DIR / 'sfx'
    elif file_type == 'music':
        target_dir = ASSETS_DIR / 'music'
    elif file_type == 'display':
        target_dir = ASSETS_DIR / 'display'
    else:
        return jsonify({'ok': False, 'error': 'Unknown type'}), 400
    if not new_name:
        return jsonify({'ok': False, 'error': 'New name is empty'}), 400
    old_path = target_dir / old_name
    new_path = target_dir / new_name
    if not old_path.resolve().is_relative_to(target_dir.resolve()):
        return jsonify({'ok': False, 'error': 'Invalid path'}), 400
    if not old_path.exists():
        return jsonify({'ok': False, 'error': 'File not found'}), 404
    if new_path.exists():
        return jsonify({'ok': False, 'error': 'A file with that name already exists'}), 409
    old_path.rename(new_path)
    log.info(f"Renamed {file_type} file: {old_name} → {new_name}")
    return jsonify({'ok': True, 'name': new_name})

# ── SYSTEM FILE UPDATER ──
@app.route('/update')
def updater():
    return send_from_directory(STATIC_DIR, 'updater.html')

@app.route('/api/system/update-file', methods=['POST'])
def api_update_file():
    """Receive an updated HTML/config file and save it in place."""
    import shutil
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'No file provided'}), 400
    f = request.files['file']
    allowed = {
        'musicman_ui.html':    STATIC_DIR / 'musicman_ui.html',
        'musicman_admin.html': STATIC_DIR / 'musicman_admin.html',
        'updater.html':        STATIC_DIR / 'updater.html',
        'display.html':        STATIC_DIR / 'display.html',
        'config.yaml':         BASE_DIR / 'config.yaml',
        'musicman.py':         BASE_DIR / 'musicman.py',
    }
    if f.filename not in allowed:
        return jsonify({'ok': False, 'error': f'File not allowed: {f.filename}'}), 400
    dest = allowed[f.filename]
    if dest.exists():
        backup = dest.with_suffix(dest.suffix + '.bak')
        shutil.copy2(str(dest), str(backup))
    f.save(str(dest))
    log.info(f"System file updated: {f.filename}")
    if f.filename == 'config.yaml':
        global config
        config = load_config()
    return jsonify({'ok': True, 'message': f'{f.filename} updated successfully'})

# ════════════════════════════════════════════
# WIFI MANAGEMENT
# ════════════════════════════════════════════
import re as _re_wifi

def _nmcli(*args, timeout=20):
    cmd = ['sudo', 'nmcli'] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode

def _nmcli_parse(line, n=None):
    tmp = line.replace('\\:', '\x00')
    parts = tmp.split(':') if n is None else tmp.split(':', n - 1)
    return [p.replace('\x00', ':') for p in parts]

@app.route('/api/wifi/status')
def api_wifi_status():
    out, _ = _nmcli('-t', '-f', 'ACTIVE,SSID,SIGNAL', 'device', 'wifi', 'list')
    current = None
    for line in out.splitlines():
        p = _nmcli_parse(line, 3)
        if len(p) >= 3 and p[0] == 'yes' and p[1]:
            current = {'ssid': p[1], 'signal': int(p[2]) if p[2].isdigit() else 0}
            break
    out2, _ = _nmcli('-t', '-f', 'IP4.ADDRESS', 'device', 'show', 'wlan0')
    ip = None
    for line in out2.splitlines():
        if 'IP4.ADDRESS' in line and ':' in line:
            val = line.split(':', 1)[1]
            ip = val.split('/')[0] if val else None
            break
    return jsonify({'current': current, 'ip': ip})

@app.route('/api/wifi/scan')
def api_wifi_scan():
    out, _ = _nmcli('-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list', '--rescan', 'yes', timeout=25)
    seen = {}
    for line in out.splitlines():
        p = _nmcli_parse(line, 4)
        if len(p) < 4 or not p[1]:
            continue
        ssid, signal, is_active = p[1], int(p[2]) if p[2].isdigit() else 0, p[0] == '*'
        if ssid not in seen or signal > seen[ssid]['signal']:
            seen[ssid] = {'ssid': ssid, 'signal': signal, 'security': p[3], 'connected': is_active}
        elif is_active:
            seen[ssid]['connected'] = True
    return jsonify({'networks': sorted(seen.values(), key=lambda x: -x['signal'])})

@app.route('/api/wifi/saved')
def api_wifi_saved():
    out, _ = _nmcli('-t', '-f', 'NAME,TYPE', 'connection', 'show')
    saved = []
    for line in out.splitlines():
        p = _nmcli_parse(line, 2)
        if len(p) >= 2 and p[1] == '802-11-wireless' and p[0]:
            name = p[0]
            ssid_out, _ = _nmcli('-g', '802-11-wireless.ssid', 'connection', 'show', name)
            saved.append({'name': name, 'ssid': ssid_out.strip() or name})
    return jsonify({'saved': saved})

@app.route('/api/wifi/connect', methods=['POST'])
def api_wifi_connect():
    data = request.get_json() or {}
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    if not ssid:
        return jsonify({'ok': False, 'error': 'No SSID'}), 400
    args = ['device', 'wifi', 'connect', ssid, 'ifname', 'wlan0']
    if password:
        args += ['password', password]
    out, rc = _nmcli(*args, timeout=30)
    return jsonify({'ok': rc == 0, 'error': out if rc != 0 else None})

@app.route('/api/wifi/forget', methods=['POST'])
def api_wifi_forget():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'No name'}), 400
    out, rc = _nmcli('connection', 'delete', name)
    return jsonify({'ok': rc == 0, 'error': out if rc != 0 else None})

# ════════════════════════════════════════════
# ACCESS POINT MANAGEMENT
# ════════════════════════════════════════════
_AP_SSID     = 'MusicMan'
_AP_PASSWORD = 'BrokenArrow'
_AP_IP       = '192.168.4.1'
_AP_LEASE    = '/var/lib/misc/dnsmasq.leases'

def _ap_associated_macs():
    """Return set of MAC addresses currently 802.11-associated with uap0."""
    r = subprocess.run(['/sbin/iw', 'dev', 'uap0', 'station', 'dump'],
                       capture_output=True, text=True)
    macs = set()
    for line in r.stdout.splitlines():
        if line.strip().startswith('Station '):
            macs.add(line.strip().split()[1].lower())
    return macs

def _ap_lease_lookup():
    """Return dict of mac → {ip, hostname} from the dnsmasq leases file."""
    leases = {}
    try:
        with open(_AP_LEASE) as f:
            for ln in f:
                parts = ln.strip().split()
                if len(parts) >= 4:
                    leases[parts[1].lower()] = {
                        'ip':       parts[2],
                        'hostname': parts[3] if parts[3] != '*' else 'Unknown',
                    }
    except FileNotFoundError:
        pass
    return leases

@app.route('/api/ap/status')
def api_ap_status():
    r = subprocess.run(['systemctl', 'is-active', 'hostapd'], capture_output=True, text=True)
    online = r.stdout.strip() == 'active'
    clients = len(_ap_associated_macs())
    return jsonify({'online': online, 'ssid': _AP_SSID, 'password': _AP_PASSWORD,
                    'ip': _AP_IP, 'clients': clients})

@app.route('/api/ap/clients')
def api_ap_clients():
    macs   = _ap_associated_macs()
    leases = _ap_lease_lookup()
    clients = []
    for mac in macs:
        info = leases.get(mac, {})
        clients.append({
            'mac':      mac,
            'ip':       info.get('ip', '—'),
            'hostname': info.get('hostname', 'Unknown'),
        })
    clients.sort(key=lambda c: c['hostname'])
    return jsonify({'clients': clients})

@app.route('/api/ap/restart', methods=['POST'])
def api_ap_restart():
    r = subprocess.run(['sudo', '/usr/bin/systemctl', 'restart', 'hostapd'],
                       capture_output=True, text=True)
    return jsonify({'ok': r.returncode == 0, 'error': r.stderr.strip() if r.returncode else None})

# ════════════════════════════════════════════
# BATTERY MONITOR — Anker Solix C300 via BLE
# ════════════════════════════════════════════

import asyncio as _asyncio
from functools import partial as _partial

_BATT_TELE_UUID = '8c850003-0302-41c5-b46e-cf057c562025'
_BATT_POLL_SEC  = 10

_battery_lock      = threading.Lock()
_battery_cache     = {}   # address → { status, label, battery, power_in, ... }
_battery_devices   = {}   # address → live C300 device object (when connected)
_battery_loop      = None  # asyncio event loop running in the daemon thread
_batt_wake_events  = {}   # address → asyncio.Event to interrupt retry sleep


def _batt_update(addr, patch):
    with _battery_lock:
        _battery_cache[addr] = {**_battery_cache.get(addr, {}), **patch}


def _batt_all():
    with _battery_lock:
        return list(_battery_cache.values())


def _batt_cmd(addr, coro_fn):
    """Run a C300 command coroutine from Flask (thread-safe). Returns (ok, err)."""
    with _battery_lock:
        device = _battery_devices.get(addr)
    if device is None:
        return False, 'not connected'
    if _battery_loop is None or not _battery_loop.is_running():
        return False, 'battery loop not running'
    try:
        fut = _asyncio.run_coroutine_threadsafe(coro_fn(device), _battery_loop)
        fut.result(timeout=5)
        return True, None
    except Exception as e:
        return False, str(e)


async def _reset_bt_adapter():
    """Restart BlueZ to recover from adapter I/O errors."""
    log.warning('Battery: resetting Bluetooth adapter')
    loop = _asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: subprocess.run(
        ['sudo', 'systemctl', 'restart', 'bluetooth'],
        capture_output=True, timeout=20
    ))
    await _asyncio.sleep(5)
    log.info('Battery: Bluetooth reset complete')


_batt_scan_sem = None  # set to Semaphore(1) inside the event loop


async def _scan_until_seen(address, label, timeout=30):
    """Scan until BlueZ sees the target address. Returns BLEDevice or None."""
    found_device = None
    found_evt = _asyncio.Event()

    def cb(dev, _adv):
        nonlocal found_device
        if dev.address.upper() == address.upper() and found_device is None:
            found_device = dev
            found_evt.set()

    from bleak import BleakScanner
    log.info(f'Battery [Pole {label}]: scanning for {address}')
    async with _batt_scan_sem:
        try:
            async with BleakScanner(cb):
                await _asyncio.wait_for(found_evt.wait(), timeout)
            log.info(f'Battery [Pole {label}]: found, connecting')
            return found_device
        except _asyncio.TimeoutError:
            log.warning(f'Battery [Pole {label}]: not found in {timeout}s scan')
            return None
        except Exception as e:
            log.warning(f'Battery [Pole {label}]: scan error: {e}')
            return None


async def _monitor_unit(address, label):
    """Connect to one C300 forever. Scan to warm BlueZ cache, then connect."""
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
    from bleak.exc import BleakDeviceNotFoundError
    from SolixBLE import C300

    wake_evt = _asyncio.Event()
    with _battery_lock:
        _batt_wake_events[address] = wake_evt

    fail_count = 0
    ble_dev = None  # real BLEDevice from scan
    _batt_update(address, {'address': address, 'label': label, 'status': 'connecting', 'error': None})

    while True:
        try:
            connect_target = ble_dev if ble_dev is not None else address
            async with BleakClient(connect_target, timeout=10.0) as client:
                fail_count = 0
                c300_dev = ble_dev or BLEDevice(address=address, name='Anker SOLIX C300', details={}, rssi=0)
                device = C300(c300_dev)
                device._reset_session(reset_data=False)
                device._client = client

                await client.start_notify(
                    _BATT_TELE_UUID,
                    _partial(device._process_notification, client)
                )
                await device._initiate_negotiations()

                for _ in range(20):
                    await _asyncio.sleep(1)
                    if device.negotiated:
                        break
                else:
                    raise RuntimeError('negotiate timeout')

                log.info(f'Battery [Pole {label}]: connected and negotiated')
                with _battery_lock:
                    _battery_devices[address] = device
                _batt_update(address, {'status': 'connected', 'error': None})

                last_data_time = time.time()
                while client.is_connected:
                    try:
                        # get_status_update() returns a params dict from c840 response;
                        # inject it into device._data so properties work
                        params = await _asyncio.wait_for(device.get_status_update(), timeout=15)
                        if params:
                            device._data = params
                    except _asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning(f'Battery [Pole {label}]: poll error: {type(e).__name__}: {e}')

                    if device._data is not None:
                        last_data_time = time.time()
                        _batt_update(address, {
                            'status':         'ok',
                            'error':          None,
                            'battery':        device.battery_percentage,
                            'charging':       device.charging_status.name,
                            'temp':           device.temperature,
                            'time_remaining': device.time_remaining,
                            'power_in':       device.power_in,
                            'power_out':      device.power_out,
                            'ac_power_in':    device.ac_power_in,
                            'ac_power_out':   device.ac_power_out,
                            'solar_power_in': device.solar_power_in,
                            'dc_power_out':   device.dc_power_out,
                            'usb_c1_power':   device.usb_c1_power,
                            'usb_c2_power':   device.usb_c2_power,
                            'usb_c3_power':   device.usb_c3_power,
                            'usb_a1_power':   device.usb_a1_power,
                            'ac_output':      device.ac_output.name,
                            'dc_output':      device.dc_output.name,
                            'usb_port_c1':    device.usb_port_c1.name,
                            'usb_port_c2':    device.usb_port_c2.name,
                            'usb_port_c3':    device.usb_port_c3.name,
                            'usb_port_a1':    device.usb_port_a1.name,
                            'light':          device.light.name,
                            'serial':         device.serial_number,
                            'firmware':       device.software_version,
                            'updated':        time.time(),
                        })
                    elif time.time() - last_data_time > 45:
                        log.warning(f'Battery [Pole {label}]: no data for 45s, reconnecting')
                        break

                    wake_evt.clear()
                    try:
                        await _asyncio.wait_for(wake_evt.wait(), _BATT_POLL_SEC)
                    except _asyncio.TimeoutError:
                        pass

                log.info(f'Battery [Pole {label}]: disconnected, retrying')

        except BleakDeviceNotFoundError:
            log.info(f'Battery [Pole {label}]: not in BlueZ cache, scanning...')
            found = await _scan_until_seen(address, label, timeout=30)
            if found is not None:
                ble_dev = found
            else:
                ble_dev = None
                fail_count += 1
                if fail_count >= 3:
                    await _reset_bt_adapter()
                    fail_count = 0
                wake_evt.clear()
                try:
                    await _asyncio.wait_for(wake_evt.wait(), 10)
                except _asyncio.TimeoutError:
                    pass
            continue

        except _asyncio.CancelledError:
            raise

        except Exception as exc:
            err = f'{type(exc).__name__}: {exc}' if str(exc) else type(exc).__name__
            log.warning(f'Battery [Pole {label}]: {err}')
            fail_count += 1
            if fail_count >= 3:
                await _reset_bt_adapter()
                fail_count = 0

        finally:
            with _battery_lock:
                _battery_devices.pop(address, None)
            if _battery_cache.get(address, {}).get('status') not in ('connecting',):
                _batt_update(address, {'status': 'offline'})

        _batt_update(address, {'status': 'connecting', 'error': None})
        wake_evt.clear()
        try:
            await _asyncio.wait_for(wake_evt.wait(), 10)
        except _asyncio.TimeoutError:
            pass


def _start_battery_monitor():
    units = config.get('battery_units', [])
    if not units:
        log.info('Battery: no battery_units in config, skipping')
        return

    def _run():
        global _battery_loop, _batt_scan_sem
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        _battery_loop = loop

        async def _main():
            global _batt_scan_sem
            _batt_scan_sem = _asyncio.Semaphore(1)
            await _asyncio.gather(*[
                _monitor_unit(u['address'], u['label']) for u in units
            ])

        try:
            loop.run_until_complete(_main())
        except Exception as e:
            log.error(f'Battery monitor crashed: {e}')

    threading.Thread(target=_run, daemon=True, name='battery-monitor').start()


try:
    _start_battery_monitor()
    log.info('Battery monitor started')
except Exception as exc:
    log.warning(f'Battery monitor failed to start: {exc}')


@app.route('/api/battery')
def api_battery():
    return jsonify({'units': _batt_all()})


@app.route('/api/battery/scan', methods=['POST'])
def api_battery_scan():
    if _battery_loop and _battery_loop.is_running():
        with _battery_lock:
            events = list(_batt_wake_events.values())
        for evt in events:
            _battery_loop.call_soon_threadsafe(evt.set)
    return jsonify({'ok': True})


@app.route('/api/battery/<path:addr>/ac/<state>', methods=['POST'])
def api_battery_ac(addr, state):
    addr = addr.replace('-', ':')
    ok, err = _batt_cmd(addr, lambda d: d.turn_ac_on() if state == 'on' else d.turn_ac_off())
    return (jsonify({'ok': True}), 200) if ok else (jsonify({'error': err}), 503)


@app.route('/api/battery/<path:addr>/dc/<state>', methods=['POST'])
def api_battery_dc(addr, state):
    addr = addr.replace('-', ':')
    ok, err = _batt_cmd(addr, lambda d: d.turn_dc_on() if state == 'on' else d.turn_dc_off())
    return (jsonify({'ok': True}), 200) if ok else (jsonify({'error': err}), 503)


@app.route('/api/battery/<path:addr>/light/<mode>', methods=['POST'])
def api_battery_light(addr, mode):
    from SolixBLE.states import LightStatus
    addr = addr.replace('-', ':')
    modes = {'off': LightStatus.OFF, 'low': LightStatus.LOW, 'high': LightStatus.HIGH, 'sos': LightStatus.SOS}
    ls = modes.get(mode)
    if ls is None:
        return jsonify({'error': 'unknown mode'}), 400
    ok, err = _batt_cmd(addr, lambda d: d.set_light_mode(ls))
    return (jsonify({'ok': True}), 200) if ok else (jsonify({'error': err}), 503)


# ════════════════════════════════════════════
# STARTUP — runs under both gunicorn (import) and direct execution
# ════════════════════════════════════════════
start_timer_thread()
start_countdown_thread()

if __name__ == '__main__':
    log.info(f"Serving at http://{config['system']['hostname']}.local")
    app.run(
        host='0.0.0.0',
        port=config['system']['port'],
        debug=config['system']['debug'],
        threaded=True,
        use_reloader=False
    )