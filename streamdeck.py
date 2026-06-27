#!/usr/bin/env python3
"""
Music Man — Stream Deck Controller
Runs on the Pi, drives a USB-connected Stream Deck (15 keys, 5×3).

Pages (in order):
  0  SHOW      — show rundown segments 1–10
  1  SHOW 2    — show rundown segments 11–20 (overflow)
  2  CIRCLES   — circle walk-up buttons
  3  ROLES     — role walk-up buttons
  4  MACROS    — macro buttons
  5  SFX       — sound effect buttons
  6  SCENES    — lighting scene buttons
"""

import threading, time, json, os, sys, signal, subprocess
import requests
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
import websocket

# ── CONFIG ───────────────────────────────────────────────────────────────────
BASE_URL    = 'http://localhost'
FONT_BOLD   = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
BRIGHTNESS  = 80   # 0–100

KEYS_TOTAL  = 15
PAGES       = ['SHOW', 'SHOW2', 'CIRCLES', 'ROLES', 'MACROS', 'SFX', 'SCENES']

# ── LIVE STATE ───────────────────────────────────────────────────────────────
state = {
    'page':            0,
    'show_step':       None,   # index of last-fired show segment
    'active_circle':   None,
    'active_role':     None,
    'active_scene':    None,
    'audio_playing':   False,
    'audio_paused':    False,
    'timer_running':   False,
    'timer_paused':    False,
    'timer_end':       0.0,    # time.time() when countdown reaches 0 (while running)
    'timer_remaining': 0.0,    # snapshot of remaining seconds (while paused/stopped)
}

_timer_tick_id = 0   # increment to invalidate old tick threads

# Cached Pi data
pi = {
    'show_flow': [],   # [{id, name, color, macro_id}, …] in show order
    'circles':   [],
    'roles':     [],
    'scenes':    [],
    'macros':    [],
    'sfx':       [],   # filenames without extension
}

deck       = None
deck_lock  = threading.Lock()

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

# ── IMAGE BUILDERS ────────────────────────────────────────────────────────────
def blank_image():
    img = PILHelper.create_image(deck)
    return PILHelper.to_native_format(deck, img)

def label_image(top, bottom='', color='#F5A623', active=False, dot=False):
    """Generic colored label button. dot=True draws a small active indicator."""
    rgb  = hex_rgb(color)
    bg   = dim(rgb, 0.20 if active else 0.10)
    edge = brighten(rgb, 1.3) if active else rgb
    bw   = 2 if active else 1

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=bw)

    if bottom:
        draw.text((w//2, h//2 - 9), top[:11], font=font(10), fill=edge, anchor='mm')
        draw.text((w//2, h//2 + 9), bottom[:13], font=font(8), fill=(*edge, 180), anchor='mm')
    else:
        size = 10 if len(top) > 8 else 12
        draw.text((w//2, h//2), top[:11], font=font(size), fill=edge, anchor='mm')

    if dot:
        draw.ellipse([w-13, 3, w-3, 13], fill=edge)

    return PILHelper.to_native_format(deck, img)

def show_step_image(idx, entry, active=False, done=False):
    """Show flow segment button. Active = currently firing. Done = already ran."""
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

    # Step number in upper-left
    num_color = brighten(rgb, 1.2) if active else dim(rgb, 0.6) if done else rgb
    draw.text((8, 8), str(idx + 1), font=font(9), fill=num_color, anchor='lt')

    # Check mark for done steps
    if done and not active:
        draw.text((w - 8, 8), '✓', font=font(9), fill=num_color, anchor='rt')

    # Name centered
    name = entry.get('name', '').upper()
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
    lines = lines[:3]
    total_h = len(lines) * 13
    y0 = h // 2 - total_h // 2 + 6
    for i, line in enumerate(lines):
        draw.text((w // 2, y0 + i * 13), line, font=font(9), fill=edge, anchor='mm')

    return PILHelper.to_native_format(deck, img)

def circle_image(circle, active=False):
    """Circle walk-up button with number and name."""
    rgb  = hex_rgb(circle.get('color', '#888888'))
    bg   = dim(rgb, 0.18 if active else 0.10)
    edge = brighten(rgb, 1.3) if active else rgb
    bw   = 3 if active else 1

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=bw)

    num  = str(circle.get('number', ''))
    name = circle.get('name', '').upper()

    if num:
        draw.text((w//2, h//2 - 12), num, font=font(24), fill=edge, anchor='mm')
        words  = name.split()
        lines, cur = [], ''
        for w_ in words:
            test = (cur + ' ' + w_).strip()
            if len(test) <= 9:
                cur = test
            else:
                if cur: lines.append(cur)
                cur = w_
        if cur: lines.append(cur)
        lines = lines[:2]
        y0 = h//2 + 12
        for i, line in enumerate(lines):
            draw.text((w//2, y0 + i * 13), line, font=font(9), fill=edge, anchor='mm')
    else:
        lines = [name[j:j+9] for j in range(0, min(len(name), 18), 9)]
        y0 = h//2 - (len(lines)-1)*7
        for i, line in enumerate(lines):
            draw.text((w//2, y0 + i*14), line, font=font(10), fill=edge, anchor='mm')

    if active:
        draw.ellipse([w-13, 3, w-3, 13], fill=edge)

    return PILHelper.to_native_format(deck, img)

def role_image(role, active=False):
    """Role walk-up button."""
    rgb  = hex_rgb(role.get('color', '#8899AA'))
    bg   = dim(rgb, 0.18 if active else 0.10)
    edge = brighten(rgb, 1.3) if active else rgb
    bw   = 3 if active else 1

    img  = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size

    draw.rounded_rectangle([0, 0, w-1, h-1], radius=8, fill=bg)
    draw.rounded_rectangle([1, 1, w-2, h-2], radius=7, outline=edge, width=bw)

    name  = role.get('name', '').upper()
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
    lines = lines[:3]
    total_h = len(lines) * 13
    y0 = h // 2 - total_h // 2 + 6
    for i, line in enumerate(lines):
        draw.text((w // 2, y0 + i * 13), line, font=font(9), fill=edge, anchor='mm')

    if active:
        draw.ellipse([w-13, 3, w-3, 13], fill=edge)

    return PILHelper.to_native_format(deck, img)

def nav_image(direction, label=''):
    arrow = '→' if direction == 'next' else '←'
    text  = f'{arrow} {label}' if direction == 'next' else f'{label} {arrow}'
    return label_image(text.strip(), color='#445566')

def page_label_image(page_name):
    """Dim label showing current page name — used on first/last nav slot."""
    return label_image(page_name, color='#333344')

def timer_image(remaining_sec, paused=False):
    """Live countdown key — MM:SS big, RESET or PAUSED label below."""
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
    draw.text((w//2, h//2 - 6), text,                       font=font(20), fill=rgb,          anchor='mm')
    draw.text((w//2, h//2 + 13), 'PAUSED' if paused else 'RESET', font=font(8),  fill=(*rgb, 160), anchor='mm')
    return PILHelper.to_native_format(deck, img)

def _timer_key_image():
    """Current timer key image for key 12 on SHOW/SHOW2 pages."""
    if state['timer_running']:
        return timer_image(state['timer_end'] - time.time())
    elif state['timer_paused']:
        return timer_image(state['timer_remaining'], paused=True)
    return label_image('RESET', 'TIMER', color='#445566')

def _start_timer_tick():
    global _timer_tick_id
    _timer_tick_id += 1
    my_id = _timer_tick_id
    def _tick():
        while True:
            time.sleep(1)
            if _timer_tick_id != my_id or not state['timer_running']:
                break
            if state['page'] in (0, 1) and deck:
                with deck_lock:
                    deck.set_key_image(12, timer_image(state['timer_end'] - time.time()))
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
        if   p == 0: _render_show()
        elif p == 1: _render_show2()
        elif p == 2: _render_circles()
        elif p == 3: _render_roles()
        elif p == 4: _render_macros()
        elif p == 5: _render_sfx()
        elif p == 6: _render_scenes()

def _render_show():
    """Show rundown page — up to 10 ordered segments + timer controls + nav."""
    flow      = pi['show_flow']
    cur_step  = state['show_step']
    running   = state['timer_running']
    paused_au = state['audio_paused']

    for i in range(10):
        if i < len(flow):
            active = (i == cur_step)
            done   = (cur_step is not None) and (i < cur_step)
            deck.set_key_image(i, show_step_image(i, flow[i], active=active, done=done))
        else:
            deck.set_key_image(i, blank_image())

    # Row 3: audio stop | timer toggle | timer countdown/reset | page label | → SHOW 2
    deck.set_key_image(10, label_image('STOP',   'AUDIO',              color='#CC2222'))
    deck.set_key_image(11, label_image('PAUSE' if running else 'START', 'TIMER',
                                       color='#3CB96A', active=running))
    deck.set_key_image(12, _timer_key_image())
    deck.set_key_image(13, page_label_image('SHOW'))   # first page — no prev
    deck.set_key_image(14, nav_image('next', 'SHOW 2'))

def _render_circles():
    circles = pi['circles']
    for i in range(9):
        if i < len(circles):
            c   = circles[i]
            img = circle_image(c, active=(c['id'] == state['active_circle']))
        else:
            img = blank_image()
        deck.set_key_image(i, img)

    paused  = state['audio_paused']
    running = state['timer_running']

    deck.set_key_image(9,  label_image('STOP',   'AUDIO',                         color='#CC2222'))
    deck.set_key_image(10, label_image('FADE',   'OUT',                           color='#C4610A'))
    deck.set_key_image(11, label_image('RESUME' if paused else 'PAUSE', 'AUDIO',
                                       color='#66AAFF' if paused else '#8B44CC',  active=paused))
    deck.set_key_image(12, label_image('PAUSE' if running else 'START', 'TIMER',
                                       color='#3CB96A',                           active=running))
    deck.set_key_image(13, nav_image('prev', 'SHOW 2'))
    deck.set_key_image(14, nav_image('next', 'ROLES'))

def _render_roles():
    roles = pi['roles']
    for i in range(12):
        if i < len(roles):
            r   = roles[i]
            img = role_image(r, active=(r['id'] == state['active_role']))
        else:
            img = blank_image()
        deck.set_key_image(i, img)

    deck.set_key_image(12, blank_image())
    deck.set_key_image(13, nav_image('prev', 'CIRCLES'))
    deck.set_key_image(14, nav_image('next', 'MACROS'))

def _render_macros():
    macros = pi['macros']
    COLORS = ['#2B5FA6','#8B44CC','#C4610A','#3CB96A','#F5A623','#CC2222',
              '#2B5FA6','#8B44CC','#C4610A','#3CB96A','#F5A623','#CC2222','#2B5FA6']
    for i in range(13):
        if i < len(macros):
            m = macros[i]
            deck.set_key_image(i, label_image(m['name'].upper()[:10], color=COLORS[i % len(COLORS)]))
        else:
            deck.set_key_image(i, blank_image())
    deck.set_key_image(13, nav_image('prev', 'ROLES'))
    deck.set_key_image(14, nav_image('next', 'SFX'))

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
    deck.set_key_image(13, nav_image('prev', 'MACROS'))
    deck.set_key_image(14, nav_image('next', 'SCENES'))

def _render_scenes():
    scenes = pi['scenes']
    active = state['active_scene']
    SCENE_COLORS = ['#F5A623','#EEEEEE','#F5A623','#2B5FA6','#8B44CC','#222222']

    for i in range(6):
        if i < len(scenes):
            s   = scenes[i]
            col = SCENE_COLORS[i] if i < len(SCENE_COLORS) else '#888888'
            deck.set_key_image(i, label_image(s['name'].upper()[:10], color=col,
                                              active=(s['id'] == active)))
        else:
            deck.set_key_image(i, blank_image())

    for i in range(6, 13):
        deck.set_key_image(i, blank_image())

    deck.set_key_image(13, nav_image('prev', 'SFX'))
    deck.set_key_image(14, label_image('HOME', color='#333355'))

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
    if   p == 0: _handle_show(key)
    elif p == 1: _handle_show2(key)
    elif p == 2: _handle_circles(key)
    elif p == 3: _handle_roles(key)
    elif p == 4: _handle_macros(key)
    elif p == 5: _handle_sfx(key)
    elif p == 6: _handle_scenes(key)

def _nav(page_idx):
    state['page'] = page_idx
    render_current_page()

def _handle_show(key):
    flow = pi['show_flow']
    if key < 10:
        if key < len(flow):
            entry = flow[key]
            macro_id = entry.get('macro_id', '')
            if macro_id:
                api_get(f'/api/macro/run?name={macro_id}')
            # show_step state updates via WS broadcast from /api/macro/run
    elif key == 10:
        api_get('/api/audio/stop')
    elif key == 11:
        api_get('/api/timer/toggle')   # server owns running state
    elif key == 12:
        api_get('/api/timer/reset')
    elif key == 14:
        _nav(1)   # → SHOW 2

def _render_show2():
    """Show rundown overflow — steps 11-20 + controls + nav."""
    flow     = pi['show_flow']
    cur_step = state['show_step']
    running  = state['timer_running']
    OFFSET   = 10

    for i in range(10):
        idx = i + OFFSET
        if idx < len(flow):
            active = (idx == cur_step)
            done   = (cur_step is not None) and (idx < cur_step)
            deck.set_key_image(i, show_step_image(idx, flow[idx], active=active, done=done))
        else:
            deck.set_key_image(i, blank_image())

    deck.set_key_image(10, label_image('STOP',  'AUDIO',              color='#CC2222'))
    deck.set_key_image(11, label_image('PAUSE' if running else 'START', 'TIMER',
                                       color='#3CB96A', active=running))
    deck.set_key_image(12, _timer_key_image())
    deck.set_key_image(13, nav_image('prev', 'SHOW'))
    deck.set_key_image(14, nav_image('next', 'CIRCLES'))

def _handle_show2(key):
    flow   = pi['show_flow']
    OFFSET = 10
    if key < 10:
        idx = key + OFFSET
        if idx < len(flow):
            entry    = flow[idx]
            macro_id = entry.get('macro_id', '')
            if macro_id:
                api_get(f'/api/macro/run?name={macro_id}')
    elif key == 10:
        api_get('/api/audio/stop')
    elif key == 11:
        api_get('/api/timer/toggle')
    elif key == 12:
        api_get('/api/timer/reset')
    elif key == 13:
        _nav(0)   # ← SHOW
    elif key == 14:
        _nav(2)   # → CIRCLES

def _handle_circles(key):
    circles = pi['circles']
    if key < 9:
        if key < len(circles):
            api_get(f'/api/macro/walkup?circle={circles[key]["id"]}')
    elif key == 9:
        api_get('/api/audio/stop')
    elif key == 10:
        api_get('/api/audio/fade')
    elif key == 11:
        api_get('/api/audio/' + ('resume' if state['audio_paused'] else 'pause'))
    elif key == 12:
        api_get('/api/timer/toggle')   # server owns running state
    elif key == 13:
        _nav(1)   # ← SHOW 2
    elif key == 14:
        _nav(3)   # → ROLES

def _handle_roles(key):
    roles = pi['roles']
    if key < 12:
        if key < len(roles):
            api_get(f'/api/macro/walkup?role={roles[key]["id"]}')
    elif key == 13:
        _nav(2)   # ← CIRCLES
    elif key == 14:
        _nav(4)   # → MACROS

def _handle_macros(key):
    macros = pi['macros']
    if key < 13:
        if key < len(macros):
            api_get(f'/api/macro/run?name={macros[key]["id"]}')
    elif key == 13:
        _nav(3)   # ← ROLES
    elif key == 14:
        _nav(5)   # → SFX

def _handle_sfx(key):
    sfx = pi['sfx']
    if key < 13:
        if key < len(sfx):
            api_get(f'/api/sfx/play?name={sfx[key]}')
    elif key == 13:
        _nav(4)   # ← MACROS
    elif key == 14:
        _nav(6)   # → SCENES

def _handle_scenes(key):
    scenes = pi['scenes']
    if key < 6:
        if key < len(scenes):
            api_get(f'/api/lights/scene?name={scenes[key]["id"]}')
    elif key == 13:
        _nav(5)   # ← SFX
    elif key == 14:
        _nav(0)   # HOME → SHOW page

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
        print(f'Data loaded: {len(pi["circles"])} circles, {len(pi["roles"])} roles, '
              f'{len(pi["scenes"])} scenes, {len(pi["macros"])} macros, '
              f'{len(pi["sfx"])} sfx, {len(pi["show_flow"])} show steps')
    except Exception as e:
        print(f'Data load error: {e}')

# ── WEBSOCKET (live Pi events) ────────────────────────────────────────────────
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
            state['timer_running']   = running and not paused
            state['timer_paused']    = paused
            if running and not paused:
                state['timer_end'] = time.time() + remaining
                _start_timer_tick()
            else:
                state['timer_remaining'] = remaining
                _stop_timer_tick()
            changed = True
        elif event == 'show_step':
            state['show_step'] = data.get('index')  # None = reset
            changed = True
        elif event == 'kill_all':
            state['active_circle'] = None
            state['active_role']   = None
            state['active_scene']  = None
            state['audio_playing'] = state['audio_paused'] = state['timer_running'] = False
            changed = True

        if changed:
            render_current_page()
    except Exception as e:
        print(f'WS message error: {e}')

def ws_thread():
    while True:
        try:
            ws_app = websocket.WebSocketApp(
                'ws://localhost/ws',
                on_message=on_ws_message,
                on_error=lambda ws, e: print(f'WS error: {e}'),
                on_close=lambda ws, c, m: print('WS closed, reconnecting…'),
            )
            ws_app.run_forever(ping_interval=20, ping_timeout=8)
        except Exception as e:
            print(f'WS connect error: {e}')
        time.sleep(5)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def _usb_reset():
    """Toggle USB authorized flag to recover a stuck HID interface."""
    try:
        subprocess.run(['sudo', '/usr/local/bin/musicman-usbreset'],
                       check=True, timeout=10, capture_output=True)
        print('USB reset applied')
        time.sleep(3)
    except Exception as e:
        print(f'USB reset failed: {e}')


def _open_deck():
    """Try to open the Stream Deck, retrying indefinitely until success."""
    global deck
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
                print(f'Connected: {deck.deck_type()} ({deck.key_count()} keys)')
                return
            else:
                print(f'No Stream Deck found (attempt {attempt})…')
        except Exception as e:
            print(f'HID open attempt {attempt} failed: {e}')
        if attempt % 5 == 0:
            _usb_reset()
        time.sleep(3)

def main():
    global deck

    print('Music Man Stream Deck starting…')

    # Wait for the musicman Flask service to be ready
    for _ in range(30):
        try:
            requests.get(f'{BASE_URL}/api/state', timeout=2)
            break
        except Exception:
            print('Waiting for musicman service…')
            time.sleep(2)

    # Clean shutdown handler
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

    # WebSocket in background (reconnects internally)
    threading.Thread(target=ws_thread, daemon=True).start()

    # Refresh Pi data every 60s (picks up admin changes)
    def refresh_loop():
        while True:
            time.sleep(60)
            load_pi_data()
            render_current_page()
    threading.Thread(target=refresh_loop, daemon=True).start()

    # Outer loop: open deck, run, reconnect if it disappears
    while True:
        _open_deck()
        load_pi_data()
        deck.set_key_callback(on_key_press)
        render_current_page()
        print('Stream Deck ready.')

        # Stay alive until deck disconnects
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
