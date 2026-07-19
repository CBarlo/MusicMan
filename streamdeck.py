#!/usr/bin/env python3
"""
Music Man — Stream Deck Controller
Runs on the Pi, drives a USB-connected Stream Deck (15 keys, 5×3).

Pages:
  0  MAIN      — transport, timer (live), volume, nav to all sub-pages
  1  SHOW      — show rundown steps 1–12
  2  SHOW 2    — show rundown steps 13–24 (overflow)
  3  WALKUPS   — circle walk-up buttons (with logos)
  4  ROLES     — role walk-up buttons (with logos)
  5  SFX       — sound effect buttons
  6  MACROS    — macro buttons
  7  DISPLAY   — display/projector controls
"""

import threading, time, json, os, sys, signal, subprocess
import requests
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
import websocket

# ── CONFIG ───────────────────────────────────────────────────────────────────
BASE_URL   = 'http://localhost'
ASSETS_DIR = '/home/pi/musicman/assets'
FONT_BOLD  = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
BRIGHTNESS = 80   # 0–100

KEYS_TOTAL = 15

PAGE_MAIN    = 0
PAGE_SHOW    = 1
PAGE_SHOW2   = 2
PAGE_WALKUPS = 3
PAGE_ROLES   = 4
PAGE_SFX     = 5
PAGE_MACROS  = 6
PAGE_DISPLAY = 7

VOL_STEP = 10   # percent per button press

# ── LIVE STATE ───────────────────────────────────────────────────────────────
state = {
    'page':            PAGE_MAIN,
    'show_step':       None,     # index of last-fired show segment (None = reset)
    'active_circle':   None,
    'active_role':     None,
    'active_scene':    None,
    'audio_playing':   False,
    'audio_paused':    False,
    'timer_running':   False,
    'timer_paused':    False,
    'timer_end':       0.0,      # monotonic() when countdown hits 0 (while running)
    'timer_remaining': 0.0,      # snapshot while paused/stopped
    'music_volume':    80,
    'sfx_volume':      80,
}

_timer_tick_id = 0

# Cached Pi data
pi = {
    'show_flow': [],
    'circles':   [],
    'roles':     [],
    'scenes':    [],
    'macros':    [],
    'sfx':       [],
    'display':   [],   # display file names (without path)
}

# Logo image cache: (type, id) → PIL Image (RGBA, key-sized) or None
_logo_cache  = {}
_key_wh      = None   # (width, height) — set after deck opens

deck      = None
deck_lock = threading.Lock()

# ── FONT HELPERS ─────────────────────────────────────────────────────────────
_font_cache = {}

def font(size):
    if size not in _font_cache:
        try:
            _font_cache[size] = ImageFont.truetype(FONT_BOLD, size)
        except Exception:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]

# ── COLOR HELPERS ────────────────────────────────────────────────────────────
def hex_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def dim(rgb, factor=0.15):
    return tuple(max(0, int(c * factor)) for c in rgb)

def brighten(rgb, factor=1.4):
    return tuple(min(255, int(c * factor)) for c in rgb)

# ── LOGO LOADER ───────────────────────────────────────────────────────────────
def _load_logo(entity_type, entity_id):
    """Load, resize, and cache a circle/role logo. Returns RGBA Image or None."""
    cache_key = (entity_type, entity_id)
    if cache_key in _logo_cache:
        return _logo_cache[cache_key]
    path = os.path.join(ASSETS_DIR, f'{entity_type}s', entity_id, 'logo.png')
    img = None
    try:
        raw = Image.open(path).convert('RGBA')
        if _key_wh:
            w, h = _key_wh
            # Cover-fill: scale so the image fills the key, cropping excess
            ratio = max(w / raw.width, h / raw.height)
            nw, nh = int(raw.width * ratio), int(raw.height * ratio)
            raw = raw.resize((nw, nh), Image.LANCZOS)
            x0 = (nw - w) // 2
            y0 = (nh - h) // 2
            raw = raw.crop((x0, y0, x0 + w, y0 + h))
        img = raw
    except Exception:
        pass
    _logo_cache[cache_key] = img
    return img

def _preload_logos():
    _logo_cache.clear()
    for c in pi['circles']:
        _load_logo('circle', c['id'])
    for r in pi['roles']:
        _load_logo('role', r['id'])

# ── IMAGE BUILDERS ────────────────────────────────────────────────────────────
def blank_image():
    img = PILHelper.create_image(deck)
    return PILHelper.to_native_format(deck, img)

def label_image(top, bottom='', color='#F5A623', active=False, dot=False):
    """Generic colored label button."""
    rgb  = hex_rgb(color)
    bg   = dim(rgb, 0.22 if active else 0.10)
    edge = brighten(rgb, 1.3) if active else rgb
    bw   = 2 if active else 1

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=bw)

    if bottom:
        draw.text((w//2, h//2 - 9), top[:11],    font=font(10), fill=edge,         anchor='mm')
        draw.text((w//2, h//2 + 9), bottom[:13], font=font(8),  fill=(*edge, 180), anchor='mm')
    else:
        words = top.split()
        lines, cur = [], ''
        for wd in words:
            test = (cur + ' ' + wd).strip()
            if len(test) <= 10: cur = test
            else:
                if cur: lines.append(cur)
                cur = wd
        if cur: lines.append(cur)
        lines = lines[:3]
        total = len(lines) * 13
        fsz   = 10 if any(len(l) > 8 for l in lines) else 12
        y0    = h // 2 - total // 2 + 6
        for i, line in enumerate(lines):
            draw.text((w//2, y0 + i*13), line, font=font(fsz), fill=edge, anchor='mm')

    if dot:
        draw.ellipse([w-13, 3, w-3, 13], fill=edge)

    return PILHelper.to_native_format(deck, img)

def nav_image(direction, label=''):
    arrow = '→' if direction == 'next' else '←'
    text  = f'{arrow} {label}' if direction == 'next' else f'{label} {arrow}'
    return label_image(text.strip(), color='#334455')

def home_image():
    return label_image('HOME ←', color='#334455')

def volume_image(label, vol_pct, color):
    """Volume step button showing current level."""
    rgb  = hex_rgb(color)
    bg   = dim(rgb, 0.12)
    edge = rgb

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=1)

    draw.text((w//2, h//2 - 9), label,          font=font(9),  fill=edge,         anchor='mm')
    draw.text((w//2, h//2 + 9), f'{vol_pct}%', font=font(11), fill=(*edge, 220), anchor='mm')

    return PILHelper.to_native_format(deck, img)

def timer_image(remaining_sec, paused=False):
    """Live countdown key — MM:SS big."""
    secs  = max(0, int(remaining_sec))
    text  = f'{secs // 60:02d}:{secs % 60:02d}'
    color = '#778899' if paused else '#F5A623'
    rgb   = hex_rgb(color)
    bg    = dim(rgb, 0.12)

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=rgb, width=2 if not paused else 1)
    draw.text((w//2, h//2 - 6), text,                        font=font(20), fill=rgb,          anchor='mm')
    draw.text((w//2, h//2 + 13), 'PAUSED' if paused else 'RESET', font=font(8),  fill=(*rgb, 160), anchor='mm')
    return PILHelper.to_native_format(deck, img)

def _timer_button_image():
    """Current timer image for key 5 (MAIN) and key 12 (SHOW pages)."""
    if state['timer_running']:
        return timer_image(state['timer_end'] - time.monotonic())
    elif state['timer_paused']:
        return timer_image(state['timer_remaining'], paused=True)
    return label_image('START', 'TIMER', color='#3CB96A')

def show_step_image(idx, entry, active=False, done=False):
    """Show flow segment. Active = currently firing. Done = already ran."""
    color = entry.get('color', '#F5A623')
    rgb   = hex_rgb(color)
    bg    = dim(rgb, 0.25 if active else 0.12 if done else 0.08)
    edge  = brighten(rgb, 1.4) if active else dim(rgb, 0.5) if done else rgb
    bw    = 3 if active else 1

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=bw)

    num_color = brighten(rgb, 1.2) if active else dim(rgb, 0.6) if done else rgb
    draw.text((8, 8), str(idx + 1), font=font(9), fill=num_color, anchor='lt')
    if done and not active:
        draw.text((w - 8, 8), '✓', font=font(9), fill=num_color, anchor='rt')

    name  = entry.get('name', '').upper()
    words = name.split()
    lines, cur = [], ''
    for wd in words:
        test = (cur + ' ' + wd).strip()
        if len(test) <= 9:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = wd
    if cur: lines.append(cur)
    lines  = lines[:3]
    total  = len(lines) * 13
    y0     = h // 2 - total // 2 + 6
    for i, line in enumerate(lines):
        draw.text((w//2, y0 + i * 13), line, font=font(9), fill=edge, anchor='mm')

    return PILHelper.to_native_format(deck, img)

def _entity_image(entity, entity_type, active=False):
    """
    Circle or role button. Uses logo PNG from assets if available,
    falls back to color + text.
    """
    color = entity.get('color', '#888888')
    rgb   = hex_rgb(color)
    edge  = brighten(rgb, 1.3) if active else rgb
    bw    = 3 if active else 1
    name  = entity.get('name', '').upper()

    logo = _load_logo(entity_type, entity['id'])

    if logo:
        # Composite logo onto a dark bg, add border + text overlay
        bg_color = dim(rgb, 0.30 if active else 0.15)
        base = PILHelper.create_image(deck)
        base.paste(Image.new('RGB', base.size, bg_color))
        # Dim logo slightly when inactive
        alpha_factor = 1.0 if active else 0.78
        lo = logo.copy()
        r2, g2, b2, a2 = lo.split()
        a2 = a2.point(lambda x: int(x * alpha_factor))
        lo = Image.merge('RGBA', (r2, g2, b2, a2))
        base.paste(lo, (0, 0), lo)
        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle([1, 1, base.width-2, base.height-2], radius=7, outline=edge, width=bw)
        if active:
            draw.ellipse([base.width-13, 3, base.width-3, 13], fill=edge)
        return PILHelper.to_native_format(deck, base)

    # Text-only fallback
    bg = dim(rgb, 0.18 if active else 0.10)
    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=bw)

    words, lines, cur = name.split(), [], ''
    for wd in words:
        test = (cur + ' ' + wd).strip()
        if len(test) <= 9: cur = test
        else:
            if cur: lines.append(cur)
            cur = wd
    if cur: lines.append(cur)
    lines  = lines[:3]
    total  = len(lines) * 13
    y0     = h // 2 - total // 2 + 6
    fsz    = 10 if any(len(l) > 7 for l in lines) else 11
    for i, line in enumerate(lines):
        draw.text((w//2, y0 + i*13), line, font=font(fsz), fill=edge, anchor='mm')

    if active:
        draw.ellipse([w-13, 3, w-3, 13], fill=edge)

    return PILHelper.to_native_format(deck, img)

# ── TIMER TICK ────────────────────────────────────────────────────────────────
def _start_timer_tick():
    global _timer_tick_id
    _timer_tick_id += 1
    my_id = _timer_tick_id
    def _tick():
        while True:
            time.sleep(1)
            if _timer_tick_id != my_id or not state['timer_running']:
                break
            if not deck:
                continue
            p = state['page']
            with deck_lock:
                if p == PAGE_MAIN:
                    deck.set_key_image(5, _timer_button_image())
                elif p in (PAGE_SHOW, PAGE_SHOW2):
                    deck.set_key_image(12, _timer_button_image())
    threading.Thread(target=_tick, daemon=True).start()

def _stop_timer_tick():
    global _timer_tick_id
    _timer_tick_id += 1

# ── PAGE RENDERERS ────────────────────────────────────────────────────────────
def render_current_page():
    if not deck:
        return
    with deck_lock:
        p = state['page']
        if   p == PAGE_MAIN:    _render_main()
        elif p == PAGE_SHOW:    _render_show()
        elif p == PAGE_SHOW2:   _render_show2()
        elif p == PAGE_WALKUPS: _render_walkups()
        elif p == PAGE_ROLES:   _render_roles()
        elif p == PAGE_SFX:     _render_sfx()
        elif p == PAGE_MACROS:  _render_macros()
        elif p == PAGE_DISPLAY: _render_display()

def _render_main():
    """
    Main page layout (3×5):
      Row 0: STOP    | PAUSE      | →SHOW    | →WALKUPS | →SFX
      Row 1: TIMER▶  | TIMER RST  | →ROLES   | →MACROS  | →DISPLAY
      Row 2: FADE    | MUS VOL-   | MUS VOL+ | SFX VOL- | SFX VOL+
    """
    paused  = state['audio_paused']
    running = state['timer_running']
    mvol    = state['music_volume']
    svol    = state['sfx_volume']

    deck.set_key_image(0,  label_image('STOP',   'AUDIO',                            color='#CC2222'))
    deck.set_key_image(1,  label_image('RESUME' if paused else 'PAUSE', 'AUDIO',
                                       color='#66AAFF' if paused else '#8B44CC', active=paused))
    deck.set_key_image(2,  nav_image('next', 'SHOW'))
    deck.set_key_image(3,  nav_image('next', 'WALKUPS'))
    deck.set_key_image(4,  nav_image('next', 'SFX'))

    deck.set_key_image(5,  _timer_button_image())
    deck.set_key_image(6,  label_image('RESET', 'TIMER',  color='#445566'))
    deck.set_key_image(7,  nav_image('next', 'ROLES'))
    deck.set_key_image(8,  nav_image('next', 'MACROS'))
    deck.set_key_image(9,  nav_image('next', 'DISPLAY'))

    deck.set_key_image(10, label_image('FADE',   'OUT',    color='#C4610A'))
    deck.set_key_image(11, volume_image('MUS VOL▼', mvol,  color='#1a7a3a'))
    deck.set_key_image(12, volume_image('MUS VOL▲', mvol,  color='#1a7a3a'))
    deck.set_key_image(13, volume_image('SFX VOL▼', svol,  color='#1a3d7a'))
    deck.set_key_image(14, volume_image('SFX VOL▲', svol,  color='#1a3d7a'))

def _render_show():
    """Show flow steps 1–12 + quick stop + home + overflow nav."""
    flow     = pi['show_flow']
    cur_step = state['show_step']

    for i in range(12):
        if i < len(flow):
            active = (i == cur_step)
            done   = (cur_step is not None) and (i < cur_step)
            deck.set_key_image(i, show_step_image(i, flow[i], active=active, done=done))
        else:
            deck.set_key_image(i, blank_image())

    deck.set_key_image(12, _timer_button_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, nav_image('next', 'SHOW 2') if len(flow) > 12 else blank_image())

def _render_show2():
    """Show flow steps 13–24."""
    flow     = pi['show_flow']
    cur_step = state['show_step']
    OFFSET   = 12

    for i in range(12):
        idx = i + OFFSET
        if idx < len(flow):
            active = (idx == cur_step)
            done   = (cur_step is not None) and (idx < cur_step)
            deck.set_key_image(i, show_step_image(idx, flow[idx], active=active, done=done))
        else:
            deck.set_key_image(i, blank_image())

    deck.set_key_image(12, _timer_button_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, nav_image('prev', 'SHOW'))

def _render_walkups():
    circles = pi['circles']
    for i in range(13):
        if i < len(circles):
            c = circles[i]
            deck.set_key_image(i, _entity_image(c, 'circle', active=(c['id'] == state['active_circle'])))
        else:
            deck.set_key_image(i, blank_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, blank_image())

def _render_roles():
    roles = pi['roles']
    for i in range(13):
        if i < len(roles):
            r = roles[i]
            deck.set_key_image(i, _entity_image(r, 'role', active=(r['id'] == state['active_role'])))
        else:
            deck.set_key_image(i, blank_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, blank_image())

def _render_sfx():
    COLORS = ['#C4610A','#F5A623','#CC2222','#3CB96A','#2B5FA6',
              '#8B44CC','#C4610A','#F5A623','#CC2222','#3CB96A',
              '#2B5FA6','#8B44CC','#CC2222']
    sfx = pi['sfx']
    for i in range(13):
        if i < len(sfx):
            name = sfx[i].replace('_', ' ').replace('-', ' ').upper()
            deck.set_key_image(i, label_image(name[:10], color=COLORS[i % len(COLORS)]))
        else:
            deck.set_key_image(i, blank_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, blank_image())

def _render_macros():
    COLORS = ['#2B5FA6','#8B44CC','#C4610A','#3CB96A','#F5A623','#CC2222',
              '#2B5FA6','#8B44CC','#C4610A','#3CB96A','#F5A623','#CC2222','#2B5FA6']
    macros = pi['macros']
    for i in range(13):
        if i < len(macros):
            m = macros[i]
            deck.set_key_image(i, label_image(m['name'].upper(), color=COLORS[i % len(COLORS)]))
        else:
            deck.set_key_image(i, blank_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, blank_image())

def _render_display():
    """
    Display controls.
    Keys 0–2: fixed controls (Logo, Blackout, Standby).
    Keys 3–12: display files from assets/display/.
    """
    deck.set_key_image(0, label_image('BAE',    'LOGO',     color='#C4610A'))
    deck.set_key_image(1, label_image('BLACK',  'OUT',      color='#222222'))
    deck.set_key_image(2, label_image('STAND',  'BY',       color='#334466'))

    files = pi['display']
    for i in range(10):
        if i < len(files):
            name = os.path.splitext(files[i])[0].replace('_', ' ').upper()
            deck.set_key_image(i + 3, label_image(name[:10], color='#2B5FA6'))
        else:
            deck.set_key_image(i + 3, blank_image())

    deck.set_key_image(13, home_image())
    deck.set_key_image(14, blank_image())

# ── BUTTON PRESS ──────────────────────────────────────────────────────────────
def api_get(path):
    try:
        requests.get(f'{BASE_URL}{path}', timeout=3)
    except Exception as e:
        print(f'API error {path}: {e}')

def on_key_press(deck_ref, key, pressed):
    if not pressed:
        return
    p = state['page']
    if   p == PAGE_MAIN:    _handle_main(key)
    elif p == PAGE_SHOW:    _handle_show(key)
    elif p == PAGE_SHOW2:   _handle_show2(key)
    elif p == PAGE_WALKUPS: _handle_walkups(key)
    elif p == PAGE_ROLES:   _handle_roles(key)
    elif p == PAGE_SFX:     _handle_sfx(key)
    elif p == PAGE_MACROS:  _handle_macros(key)
    elif p == PAGE_DISPLAY: _handle_display(key)

def _nav(page_idx):
    state['page'] = page_idx
    render_current_page()

def _handle_main(key):
    paused = state['audio_paused']
    mvol   = state['music_volume']
    svol   = state['sfx_volume']

    if   key == 0:  api_get('/api/audio/stop')
    elif key == 1:  api_get('/api/audio/' + ('resume' if paused else 'pause'))
    elif key == 2:  _nav(PAGE_SHOW)
    elif key == 3:  _nav(PAGE_WALKUPS)
    elif key == 4:  _nav(PAGE_SFX)
    elif key == 5:
        running = state['timer_running']
        api_get('/api/timer/' + ('pause' if running else 'start'))
    elif key == 6:  api_get('/api/timer/reset')
    elif key == 7:  _nav(PAGE_ROLES)
    elif key == 8:  _nav(PAGE_MACROS)
    elif key == 9:  _nav(PAGE_DISPLAY)
    elif key == 10: api_get('/api/audio/fade')
    elif key == 11:
        new = max(0, mvol - VOL_STEP)
        state['music_volume'] = new
        api_get(f'/api/audio/volume?v={new}')
        with deck_lock:
            deck.set_key_image(11, volume_image('MUS VOL▼', new, color='#1a7a3a'))
            deck.set_key_image(12, volume_image('MUS VOL▲', new, color='#1a7a3a'))
    elif key == 12:
        new = min(100, mvol + VOL_STEP)
        state['music_volume'] = new
        api_get(f'/api/audio/volume?v={new}')
        with deck_lock:
            deck.set_key_image(11, volume_image('MUS VOL▼', new, color='#1a7a3a'))
            deck.set_key_image(12, volume_image('MUS VOL▲', new, color='#1a7a3a'))
    elif key == 13:
        new = max(0, svol - VOL_STEP)
        state['sfx_volume'] = new
        api_get(f'/api/sfx/volume?v={new}')
        with deck_lock:
            deck.set_key_image(13, volume_image('SFX VOL▼', new, color='#1a3d7a'))
            deck.set_key_image(14, volume_image('SFX VOL▲', new, color='#1a3d7a'))
    elif key == 14:
        new = min(100, svol + VOL_STEP)
        state['sfx_volume'] = new
        api_get(f'/api/sfx/volume?v={new}')
        with deck_lock:
            deck.set_key_image(13, volume_image('SFX VOL▼', new, color='#1a3d7a'))
            deck.set_key_image(14, volume_image('SFX VOL▲', new, color='#1a3d7a'))

def _handle_show(key):
    flow = pi['show_flow']
    if key < 12:
        if key < len(flow):
            api_get(f'/api/show/fire?index={key}')
    elif key == 12:
        running = state['timer_running']
        api_get('/api/timer/' + ('pause' if running else 'start'))
    elif key == 13:
        _nav(PAGE_MAIN)
    elif key == 14:
        if len(flow) > 12:
            _nav(PAGE_SHOW2)

def _handle_show2(key):
    flow   = pi['show_flow']
    OFFSET = 12
    if key < 12:
        idx = key + OFFSET
        if idx < len(flow):
            api_get(f'/api/show/fire?index={idx}')
    elif key == 12:
        running = state['timer_running']
        api_get('/api/timer/' + ('pause' if running else 'start'))
    elif key == 13:
        _nav(PAGE_MAIN)
    elif key == 14:
        _nav(PAGE_SHOW)

def _handle_walkups(key):
    circles = pi['circles']
    if key < 13:
        if key < len(circles):
            api_get(f'/api/macro/walkup?circle={circles[key]["id"]}')
    elif key == 13:
        _nav(PAGE_MAIN)

def _handle_roles(key):
    roles = pi['roles']
    if key < 13:
        if key < len(roles):
            api_get(f'/api/macro/walkup?role={roles[key]["id"]}')
    elif key == 13:
        _nav(PAGE_MAIN)

def _handle_sfx(key):
    sfx = pi['sfx']
    if key < 13:
        if key < len(sfx):
            api_get(f'/api/sfx/play?name={sfx[key]}')
    elif key == 13:
        _nav(PAGE_MAIN)

def _handle_macros(key):
    macros = pi['macros']
    if key < 13:
        if key < len(macros):
            api_get(f'/api/macro/run?name={macros[key]["id"]}')
    elif key == 13:
        _nav(PAGE_MAIN)

def _handle_display(key):
    if   key == 0:
        api_get('/api/display/standby')
    elif key == 1:
        api_get('/api/display/clear')
    elif key == 2:
        api_get('/api/display/standby')
    elif 3 <= key <= 12:
        files = pi['display']
        idx   = key - 3
        if idx < len(files):
            import urllib.parse
            api_get(f'/api/display/show_file?file={urllib.parse.quote(files[idx])}')
    elif key == 13:
        _nav(PAGE_MAIN)

# ── PI DATA FETCH ────────────────────────────────────────────────────────────
def load_pi_data():
    try:
        pi['show_flow'] = requests.get(f'{BASE_URL}/api/show_flow',  timeout=5).json()
        pi['circles']   = requests.get(f'{BASE_URL}/api/circles',    timeout=5).json()
        pi['roles']     = requests.get(f'{BASE_URL}/api/roles',      timeout=5).json()
        pi['scenes']    = requests.get(f'{BASE_URL}/api/scenes',     timeout=5).json()
        pi['macros']    = requests.get(f'{BASE_URL}/api/macros',     timeout=5).json()
        raw             = requests.get(f'{BASE_URL}/api/sfx/list',   timeout=5).json()
        pi['sfx']       = [f.rsplit('.', 1)[0] for f in raw] if raw else []
        # Display files direct from filesystem
        disp_dir = os.path.join(ASSETS_DIR, 'display')
        if os.path.isdir(disp_dir):
            pi['display'] = sorted(
                f for f in os.listdir(disp_dir)
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.mp4', '.mov'))
            )
        print(f'Data: {len(pi["circles"])} circles, {len(pi["roles"])} roles, '
              f'{len(pi["macros"])} macros, {len(pi["sfx"])} sfx, '
              f'{len(pi["show_flow"])} show steps, {len(pi["display"])} display files')
        _preload_logos()
    except Exception as e:
        print(f'Data load error: {e}')

# ── WEBSOCKET ─────────────────────────────────────────────────────────────────
def on_ws_message(ws_app, message):
    try:
        msg   = json.loads(message)
        event = msg.get('event')
        data  = msg.get('data', {})

        changed = False

        if event == 'display_walkup':
            wtype = data.get('type', 'circle')
            if wtype == 'role':
                state['active_role']   = data.get('id')
                state['active_circle'] = None
            else:
                state['active_circle'] = data.get('id')
                state['active_role']   = None
            changed = True

        elif event == 'scene_changed':
            state['active_scene'] = data.get('scene')
            changed = True

        elif event == 'audio_state':
            state['audio_playing'] = bool(data.get('playing'))
            state['audio_paused']  = bool(data.get('paused'))
            if not state['audio_playing'] and not state['audio_paused']:
                state['active_circle'] = None
                state['active_role']   = None
            changed = True

        elif event == 'timer_state':
            running   = bool(data.get('running'))
            paused    = bool(data.get('paused'))
            remaining = float(data.get('seconds_remaining', data.get('remaining', 0)))
            state['timer_running'] = running and not paused
            state['timer_paused']  = paused
            if running and not paused:
                state['timer_end'] = time.monotonic() + remaining
                _start_timer_tick()
            else:
                state['timer_remaining'] = remaining
                _stop_timer_tick()
            changed = True

        elif event == 'show_step':
            state['show_step'] = data.get('index')   # None = reset
            changed = True

        elif event == 'kill_all':
            state['active_circle'] = None
            state['active_role']   = None
            state['active_scene']  = None
            state['audio_playing'] = state['audio_paused'] = False
            state['timer_running'] = False
            _stop_timer_tick()
            changed = True

        if changed:
            render_current_page()

    except Exception as e:
        print(f'WS message error: {e}')

def ws_thread():
    while True:
        try:
            ws_app = websocket.WebSocketApp(
                'ws://127.0.0.1/ws',
                on_message=on_ws_message,
                on_error=lambda ws, e: print(f'WS error: {e}'),
                on_close=lambda ws, c, m: print('WS closed, reconnecting…'),
            )
            ws_app.run_forever(ping_interval=20, ping_timeout=8)
        except Exception as e:
            print(f'WS connect error: {e}')
        time.sleep(5)

# ── USB RESET / DECK OPEN ─────────────────────────────────────────────────────
def _usb_reset():
    try:
        subprocess.run(['sudo', '/usr/local/bin/musicman-usbreset'],
                       check=True, timeout=10, capture_output=True)
        print('USB reset applied')
        time.sleep(3)
    except Exception as e:
        print(f'USB reset failed: {e}')

def _open_deck():
    global deck, _key_wh
    attempt = 0
    while True:
        attempt += 1
        try:
            devices = DeviceManager().enumerate()
            if devices:
                devices[0].open()
                deck = devices[0]
                deck.reset()
                deck.set_brightness(BRIGHTNESS)
                # Cache key dimensions for logo resizing
                tmp = PILHelper.create_image(deck)
                _key_wh = tmp.size
                print(f'Connected: {deck.deck_type()} ({deck.key_count()} keys), key size {_key_wh}')
                return
            else:
                print(f'No Stream Deck found (attempt {attempt})…')
        except Exception as e:
            print(f'HID open attempt {attempt} failed: {e}')
        if attempt % 5 == 0:
            _usb_reset()
        time.sleep(3)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global deck

    print('Music Man Stream Deck starting…')

    for _ in range(30):
        try:
            requests.get(f'{BASE_URL}/api/state', timeout=2)
            break
        except Exception:
            print('Waiting for musicman service…')
            time.sleep(2)

    def shutdown(sig, frame):
        print('\nShutting down…')
        if deck:
            try:
                deck.reset()
                deck.close()
            except Exception:
                pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    threading.Thread(target=ws_thread, daemon=True).start()

    def refresh_loop():
        while True:
            time.sleep(60)
            load_pi_data()
            render_current_page()
    threading.Thread(target=refresh_loop, daemon=True).start()

    while True:
        _open_deck()
        load_pi_data()
        deck.set_key_callback(on_key_press)
        render_current_page()
        print('Stream Deck ready.')

        while True:
            time.sleep(1)
            try:
                if not deck.connected():
                    raise RuntimeError('disconnected')
            except Exception:
                print('Stream Deck disconnected. Reconnecting…')
                try:
                    deck.close()
                except Exception:
                    pass
                deck = None
                time.sleep(2)
                break

if __name__ == '__main__':
    main()
