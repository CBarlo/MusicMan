#!/usr/bin/env python3
"""
Music Man — Stream Deck Controller
Runs on the Pi, drives a USB-connected Stream Deck (15 keys, 5×3).

Pages:
  0  MAIN      — transport, timer (live), volume, nav to Show/Circles/SFX/Roles/Macros/Lights
  1  SHOW      — show rundown steps, paginated (◂ PAGE / PAGE ▸, no fixed cap)
  3  WALKUPS   — circle walk-up buttons (with logos)
  4  ROLES     — role walk-up buttons (with logos)
  5  SFX       — sound effect buttons (paginated)
  6  MACROS    — macro buttons (paginated)
  7  DISPLAY   — display/projector controls (paginated, 3 fixed controls always visible)
  8  MENU      — directory to Music / Slides / VS Cards / Viz / Games / Display
  9  SCENES    — lighting scene buttons (paginated)
  10 MUSIC     — music library buttons (paginated)
  11 SLIDES    — saved slide buttons (paginated)
  12 VS CARDS  — saved VS card buttons (paginated)
  13 VIZ       — viz preset buttons (paginated)
  14 GAMES     — saved game configs (GO LIVE), or the live game's own controls if one's up
"""

import threading, time, json, os, sys, signal, subprocess, math, random
import urllib.parse
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
import websocket

# ── CONFIG ───────────────────────────────────────────────────────────────────
BASE_URL   = 'http://localhost'
ASSETS_DIR = str(Path(__file__).parent / 'assets')
_FONT_CANDIDATES = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
]
FONT_BOLD = next((f for f in _FONT_CANDIDATES if os.path.exists(f)), _FONT_CANDIDATES[0])
BRIGHTNESS = 80   # 0–100

KEYS_TOTAL = 15

PAGE_MAIN    = 0
PAGE_SHOW    = 1
PAGE_WALKUPS = 3
PAGE_ROLES   = 4
PAGE_SFX     = 5
PAGE_MACROS  = 6
PAGE_DISPLAY = 7
PAGE_MENU    = 8
PAGE_SCENES  = 9
PAGE_MUSIC   = 10
PAGE_SLIDES  = 11
PAGE_VSCARDS = 12
PAGE_VIZ     = 13
PAGE_GAMES   = 14

VOL_STEP = 10   # percent per button press

# Menu directory: label, color, destination page
MENU_ITEMS = [
    {'name': 'Music',    'color': '#2B5FA6', 'page': PAGE_MUSIC},
    {'name': 'Slides',   'color': '#8B44CC', 'page': PAGE_SLIDES},
    {'name': 'VS Cards', 'color': '#CC2222', 'page': PAGE_VSCARDS},
    {'name': 'Viz',      'color': '#3CB96A', 'page': PAGE_VIZ},
    {'name': 'Games',    'color': '#F5A623', 'page': PAGE_GAMES},
    {'name': 'Display',  'color': '#C4610A', 'page': PAGE_DISPLAY},
]

# ── LIVE STATE ───────────────────────────────────────────────────────────────
state = {
    'page':            PAGE_MAIN,
    'sub_page':        0,        # pagination offset within whatever list page is active
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
    'sfx_category':    None,     # None = category picker; else the tag currently drilled into
}

_timer_tick_id = 0

# Cached Pi data
pi = {
    'show_flow':    [],
    'circles':      [],
    'roles':        [],
    'scenes':       [],
    'macros':       [],
    'sfx':          [],
    'sfx_tags':     {},   # filename (with ext) -> [tags]
    'display':      [],   # display file names (without path)
    'music':        [],
    'slides':       [],
    'vs_cards':     [],
    'viz_presets':  [],
    'game_configs': [],
    'current_live_game': {'game_type_id': None, 'config_id': None},
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

def page_nav_image(direction):
    """Generic paginated-list page-turn key — Chris's preferred 'PAGE' label
    rather than PREV/NEXT."""
    return label_image('◂ PAGE' if direction == 'prev' else 'PAGE ▸', color='#334455')

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
    threading.Thread(target=_tick, daemon=True).start()

def _stop_timer_tick():
    global _timer_tick_id
    _timer_tick_id += 1

# ── GENERIC PAGINATED LIST ENGINE ─────────────────────────────────────────────
# Used by every "list of things you fire with one tap" page — SFX, Macros,
# Scenes, Music, Slides, VS Cards, Viz, Games (config list), and Menu. Page
# count is computed from len(items), so a growing list can never silently
# overflow past a hardcoded slot cap again. Keys 12/14 are the page-turn keys
# ("◂ PAGE" / "PAGE ▸" — Chris's preferred label), key 13 is always HOME.
# reserved_keys lets a page (Display) pin fixed controls at the front of
# every sub-page. Show uses this same sub_page/PAGE-key convention but with
# its own render/handle (its fire action needs the raw list index, not an
# item id field, so it doesn't go through _render_list_page/_handle_list_page).

def _list_page_count(items, num_slots):
    return max(1, math.ceil(len(items) / num_slots)) if items else 1

def _render_list_page(items, image_fn, num_slots=12, key_offset=0):
    total_pages = _list_page_count(items, num_slots)
    sub_page = max(0, min(state['sub_page'], total_pages - 1))
    state['sub_page'] = sub_page
    start = sub_page * num_slots
    page_items = items[start:start + num_slots]
    for i in range(num_slots):
        key = key_offset + i
        if i < len(page_items):
            deck.set_key_image(key, image_fn(page_items[i], start + i))
        else:
            deck.set_key_image(key, blank_image())
    deck.set_key_image(12, page_nav_image('prev') if sub_page > 0 else blank_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, page_nav_image('next') if sub_page < total_pages - 1 else blank_image())

def _handle_list_page(items, key, on_fire, num_slots=12, key_offset=0):
    """Returns 'home' | 'nav' | 'fired' | None. Caller re-renders/navigates on 'nav'/'home'."""
    total_pages = _list_page_count(items, num_slots)
    sub_page = state['sub_page']
    if key == 12:
        if sub_page > 0:
            state['sub_page'] = sub_page - 1
            return 'nav'
        return None
    elif key == 14:
        if sub_page < total_pages - 1:
            state['sub_page'] = sub_page + 1
            return 'nav'
        return None
    elif key == 13:
        return 'home'
    idx = key - key_offset
    if 0 <= idx < num_slots:
        real_idx = sub_page * num_slots + idx
        if real_idx < len(items):
            on_fire(items[real_idx])
            return 'fired'
    return None

# ── PAGE RENDERERS ────────────────────────────────────────────────────────────
def render_current_page():
    if not deck:
        return
    with deck_lock:
        p = state['page']
        if   p == PAGE_MAIN:    _render_main()
        elif p == PAGE_SHOW:    _render_show()
        elif p == PAGE_WALKUPS: _render_walkups()
        elif p == PAGE_ROLES:   _render_roles()
        elif p == PAGE_SFX:     _render_sfx()
        elif p == PAGE_MACROS:  _render_macros()
        elif p == PAGE_DISPLAY: _render_display()
        elif p == PAGE_MENU:    _render_menu()
        elif p == PAGE_SCENES:  _render_scenes()
        elif p == PAGE_MUSIC:   _render_music()
        elif p == PAGE_SLIDES:  _render_slides()
        elif p == PAGE_VSCARDS: _render_vscards()
        elif p == PAGE_VIZ:     _render_viz()
        elif p == PAGE_GAMES:   _render_games()

def _render_main():
    """
    Main page layout (3×5) — unchanged from before except key 9's destination
    (Display → Lights/Scenes, per Chris's confirmed priority order):
      Row 0: STOP    | PAUSE      | →SHOW    | →CIRCLES | →SFX
      Row 1: TIMER▶  | TIMER RST  | →ROLES   | →MACROS  | →LIGHTS
      Row 2: FADE    | MUS VOL-   | MUS VOL+ | SFX VOL- | SFX VOL+
    """
    paused  = state['audio_paused']
    mvol    = state['music_volume']
    svol    = state['sfx_volume']

    deck.set_key_image(0,  label_image('STOP',   'AUDIO',                            color='#CC2222'))
    deck.set_key_image(1,  label_image('RESUME' if paused else 'PAUSE', 'AUDIO',
                                       color='#66AAFF' if paused else '#8B44CC', active=paused))
    deck.set_key_image(2,  nav_image('next', 'SHOW'))
    deck.set_key_image(3,  nav_image('next', 'CIRCLES'))
    deck.set_key_image(4,  nav_image('next', 'SFX'))

    deck.set_key_image(5,  _timer_button_image())
    deck.set_key_image(6,  label_image('RESET', 'TIMER',  color='#445566'))
    deck.set_key_image(7,  nav_image('next', 'ROLES'))
    deck.set_key_image(8,  nav_image('next', 'MACROS'))
    deck.set_key_image(9,  nav_image('next', 'LIGHTS'))

    deck.set_key_image(10, label_image('FADE',   'OUT',    color='#C4610A'))
    deck.set_key_image(11, volume_image('MUS VOL▼', mvol,  color='#1a7a3a'))
    deck.set_key_image(12, volume_image('MUS VOL▲', mvol,  color='#1a7a3a'))
    deck.set_key_image(13, volume_image('SFX VOL▼', svol,  color='#1a3d7a'))
    deck.set_key_image(14, volume_image('SFX VOL▲', svol,  color='#1a3d7a'))

def _render_show():
    """
    Show flow steps, paginated 12/page — no fixed cap, so a run longer than
    24 steps stays reachable instead of silently dropping off the deck.
    Keys 12/13/14 are ◂ PAGE / HOME / PAGE ▸, same as every other paginated
    page; the timer itself lives on MAIN (key 5), one Home tap away.
    """
    flow        = pi['show_flow']
    cur_step    = state['show_step']
    num_slots   = 12
    total_pages = _list_page_count(flow, num_slots)
    sub_page    = max(0, min(state['sub_page'], total_pages - 1))
    state['sub_page'] = sub_page
    start = sub_page * num_slots

    for i in range(num_slots):
        idx = start + i
        if idx < len(flow):
            active = (idx == cur_step)
            done   = (cur_step is not None) and (idx < cur_step)
            deck.set_key_image(i, show_step_image(idx, flow[idx], active=active, done=done))
        else:
            deck.set_key_image(i, blank_image())

    deck.set_key_image(12, page_nav_image('prev') if sub_page > 0 else blank_image())
    deck.set_key_image(13, home_image())
    deck.set_key_image(14, page_nav_image('next') if sub_page < total_pages - 1 else blank_image())

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

_SFX_COLORS = ['#C4610A','#F5A623','#CC2222','#3CB96A','#2B5FA6','#8B44CC']
_MACRO_COLORS = ['#2B5FA6','#8B44CC','#C4610A','#3CB96A','#F5A623','#CC2222']
_SCENE_COLORS = ['#F5A623','#3CB96A','#2B5FA6','#8B44CC','#CC2222','#C4610A']
_MUSIC_COLORS = ['#2B5FA6','#3CB96A','#8B44CC','#F5A623','#C4610A','#CC2222']

_SFX_UNTAGGED = '__untagged__'

def _sfx_categories():
    """Sorted tag list, plus an Uncategorized bucket if any sfx has no tags --
    same two buckets Console's own filter chips use."""
    tags = pi.get('sfx_tags', {}) or {}
    all_tags = sorted({t for tag_list in tags.values() for t in tag_list}, key=str.lower)
    if any(not tags.get(name) for name in pi['sfx']):
        all_tags.append(_SFX_UNTAGGED)
    return all_tags

def _sfx_items_for_category(cat):
    tags = pi.get('sfx_tags', {}) or {}
    if cat == _SFX_UNTAGGED:
        return [n for n in pi['sfx'] if not tags.get(n)]
    return [n for n in pi['sfx'] if cat in (tags.get(n) or [])]

def _render_sfx():
    # Key 0 is a pinned STOP control on every SFX sub-page (category picker
    # and item list alike) -- pushes the item grid to 11 slots/page instead
    # of 12, same reserved-key convention Display already uses.
    deck.set_key_image(0, label_image('STOP', 'SFX', color='#CC2222'))
    if state['sfx_category'] is None:
        def image_fn(cat, i):
            label = 'NO TAG' if cat == _SFX_UNTAGGED else cat
            return label_image(label.upper()[:10], color=_SFX_COLORS[i % len(_SFX_COLORS)])
        _render_list_page(_sfx_categories(), image_fn, num_slots=11, key_offset=1)
    else:
        def image_fn(name, i):
            return label_image(name.replace('_', ' ').replace('-', ' ').upper()[:10],
                                color=_SFX_COLORS[i % len(_SFX_COLORS)])
        _render_list_page(_sfx_items_for_category(state['sfx_category']), image_fn, num_slots=11, key_offset=1)

def _handle_sfx(key):
    if key == 0:
        api_get('/api/sfx/stop')
        return
    if state['sfx_category'] is None:
        def enter_category(cat):
            state['sfx_category'] = cat
            state['sub_page'] = 0
        result = _handle_list_page(_sfx_categories(), key, enter_category, num_slots=11, key_offset=1)
        if result in ('fired', 'nav'):
            render_current_page()
        elif result == 'home':
            _nav(PAGE_MAIN)
    else:
        def fire(name):
            api_get(f'/api/sfx/play?name={urllib.parse.quote(name)}')
        result = _handle_list_page(_sfx_items_for_category(state['sfx_category']), key, fire, num_slots=11, key_offset=1)
        if result == 'home':
            # Back up one level (to the category picker), not all the way to
            # PAGE_MAIN -- matches how deep menus elsewhere are expected to behave.
            state['sfx_category'] = None
            state['sub_page'] = 0
            render_current_page()
        else:
            _apply_list_result(result)

def _render_macros():
    def image_fn(m, i):
        return label_image(m['name'].upper()[:14], color=_MACRO_COLORS[i % len(_MACRO_COLORS)])
    _render_list_page(pi['macros'], image_fn)

def _handle_macros(key):
    def fire(m):
        api_get(f'/api/macro/run?name={urllib.parse.quote(m["id"])}')
    _apply_list_result(_handle_list_page(pi['macros'], key, fire))

def _render_display():
    """
    Display controls.
    Keys 0–2: fixed controls (Logo, Blackout, Standby) — pinned on EVERY sub-page.
    Keys 3–11: display files, paginated (9 slots/page).
    """
    deck.set_key_image(0, label_image('BAE',    'LOGO',     color='#C4610A'))
    deck.set_key_image(1, label_image('BLACK',  'OUT',      color='#222222'))
    deck.set_key_image(2, label_image('STAND',  'BY',       color='#334466'))

    def image_fn(name, i):
        return label_image(os.path.splitext(name)[0].replace('_', ' ').upper()[:10], color='#2B5FA6')
    _render_list_page(pi['display'], image_fn, num_slots=9, key_offset=3)

def _handle_display(key):
    if   key == 0:
        api_get('/api/display/standby')
        return
    elif key == 1:
        api_get('/api/display/clear')
        return
    elif key == 2:
        api_get('/api/display/standby')
        return
    def fire(name):
        api_get(f'/api/display/show_file?file={urllib.parse.quote(name)}')
    _apply_list_result(_handle_list_page(pi['display'], key, fire, num_slots=9, key_offset=3))

def _render_scenes():
    def image_fn(s, i):
        return label_image((s.get('name') or s['id']).upper()[:14], color=_SCENE_COLORS[i % len(_SCENE_COLORS)])
    _render_list_page(pi['scenes'], image_fn)

def _handle_scenes(key):
    def fire(s):
        api_get(f'/api/lights/scene?name={urllib.parse.quote(s["id"])}')
    _apply_list_result(_handle_list_page(pi['scenes'], key, fire))

def _render_music():
    # Key 0 pinned STOP, same reserved-key convention as SFX/Display -- 11
    # item slots/page instead of 12.
    deck.set_key_image(0, label_image('STOP', 'MUSIC', color='#CC2222'))
    def image_fn(name, i):
        return label_image(os.path.splitext(name)[0].replace('_', ' ').upper()[:10],
                            color=_MUSIC_COLORS[i % len(_MUSIC_COLORS)])
    _render_list_page(pi['music'], image_fn, num_slots=11, key_offset=1)

def _handle_music(key):
    if key == 0:
        api_get('/api/music/stop')
        return
    def fire(name):
        api_get(f'/api/music/play?file={urllib.parse.quote(name)}')
    _apply_list_result(_handle_list_page(pi['music'], key, fire, num_slots=11, key_offset=1))

def _render_slides():
    def image_fn(s, i):
        return label_image((s.get('name') or 'SLIDE').upper()[:14], color='#8B44CC')
    _render_list_page(pi['slides'], image_fn)

def _handle_slides(key):
    def fire(s):
        api_get(f'/api/slides/{urllib.parse.quote(s["id"])}/push')
    _apply_list_result(_handle_list_page(pi['slides'], key, fire))

def _render_vscards():
    def image_fn(c, i):
        left  = ((c.get('left')  or {}).get('name')  or '?')
        right = ((c.get('right') or {}).get('name') or '?')
        return label_image(left.upper()[:8], right.upper()[:8], color='#CC2222')
    _render_list_page(pi['vs_cards'], image_fn)

def _handle_vscards(key):
    def fire(c):
        api_post_json('/api/display/vs_card', {'id': c['id']})
    _apply_list_result(_handle_list_page(pi['vs_cards'], key, fire))

def _render_viz():
    def image_fn(v, i):
        return label_image((v.get('name') or 'VIZ').upper()[:14], color='#3CB96A')
    _render_list_page(pi['viz_presets'], image_fn)

def _handle_viz(key):
    def fire(v):
        api_post_json(f'/api/viz/presets/{urllib.parse.quote(v["id"])}/activate', {})
    _apply_list_result(_handle_list_page(pi['viz_presets'], key, fire))

def _render_menu():
    def image_fn(m, i):
        return label_image(m['name'].upper(), color=m['color'])
    _render_list_page(MENU_ITEMS, image_fn)

def _handle_menu(key):
    def fire(m):
        _nav(m['page'])
    result = _handle_list_page(MENU_ITEMS, key, fire)
    if result == 'home':
        _nav(PAGE_MAIN)
    elif result == 'nav':
        render_current_page()
    # 'fired' already navigated via _nav() inside fire(); nothing else to do.

# ── GAMES: config list (GO LIVE) or the live game's own controls ─────────────
def _wheel_spin_action():
    live = pi['current_live_game']
    cfg = next((c for c in pi['game_configs'] if c['id'] == live.get('config_id')), None)
    if not cfg:
        return
    data = cfg.get('data', {})
    entries = data.get('entries', [])
    if not entries:
        return
    api_post_json('/api/games/wheel/spin', {
        'winner_index':  random.randrange(len(entries)),
        'entries':       entries,
        'entry_colors':  data.get('entry_colors', {}),
        'spin_duration': int((data.get('spin_duration') or 6) * 1000),
    })

def _chairs_toggle_action():
    live = pi['current_live_game']
    try:
        playing = requests.get(f'{BASE_URL}/api/games/chairs/state', timeout=3).json().get('playing', False)
    except Exception:
        playing = False
    if playing:
        api_post_json('/api/games/chairs/stop', {})
    else:
        api_post_json('/api/games/chairs/start', {'config_id': live.get('config_id', '')})

def _trivia_action(action):
    def _fire():
        live = pi['current_live_game']
        api_post_json('/api/games/trivia/action', {'config_id': live.get('config_id', ''), 'action': action})
    return _fire

# game_type_id -> list of (label, color, fire_fn) — only types with a real
# controller today get one; anything else falls back to the config list.
GAME_ACTION_SETS = {
    'wheel': [
        ('SPIN', '#F5A623', _wheel_spin_action),
    ],
    'musical_chairs': [
        ('START/STOP', '#3CB96A', _chairs_toggle_action),
    ],
    'trivia': [
        ('REVEAL',    '#F5A623', _trivia_action('reveal')),
        ('NEXT',      '#2B5FA6', _trivia_action('next')),
        ('CORRECT',   '#3CB96A', _trivia_action('correct')),
        ('INCORRECT', '#CC2222', _trivia_action('incorrect')),
    ],
    'shell_game': [
        ('START', '#3CB96A', lambda: api_post_json('/api/shell-game/start', {})),
        ('RESET', '#445566', lambda: api_post_json('/api/shell-game/reset', {})),
    ],
}

def _games_live_actions():
    live = pi['current_live_game']
    return GAME_ACTION_SETS.get(live.get('game_type_id'))

def _render_games():
    actions = _games_live_actions()
    if actions:
        for i in range(12):
            if i < len(actions):
                label, color, _ = actions[i]
                deck.set_key_image(i, label_image(label, color=color))
            else:
                deck.set_key_image(i, blank_image())
        deck.set_key_image(12, blank_image())
        deck.set_key_image(13, home_image())
        deck.set_key_image(14, label_image('CONFIGS', 'LIST', color='#445566'))
        return

    def image_fn(c, i):
        gt = next((g for g in GAME_TYPES_CACHE if g['id'] == c.get('game_type_id')), None)
        label = (gt.get('label') if gt else c.get('game_type_id', '')).upper()
        return label_image(c['name'].upper()[:12], label[:12], color='#F5A623')
    _render_list_page(pi['game_configs'], image_fn)

def _handle_games(key):
    actions = _games_live_actions()
    if actions:
        if key == 13:
            _nav(PAGE_MAIN)
        elif key == 14:
            pi['current_live_game'] = {'game_type_id': None, 'config_id': None}
            render_current_page()
        elif key < len(actions):
            actions[key][2]()
        return
    def fire(c):
        api_post_json('/api/games/launch', {'game_type_id': c.get('game_type_id', ''), 'config_id': c['id']})
    _apply_list_result(_handle_list_page(pi['game_configs'], key, fire))

# ── SHARED NAV HELPERS ────────────────────────────────────────────────────────
def _apply_list_result(result):
    if result == 'home':
        _nav(PAGE_MAIN)
    elif result == 'nav':
        render_current_page()

# ── BUTTON PRESS ──────────────────────────────────────────────────────────────
def api_get(path):
    try:
        requests.get(f'{BASE_URL}{path}', timeout=3)
    except Exception as e:
        print(f'API error {path}: {e}')

def api_post_json(path, body):
    try:
        requests.post(f'{BASE_URL}{path}', json=body, timeout=5)
    except Exception as e:
        print(f'API error {path}: {e}')

def on_key_press(deck_ref, key, pressed):
    if not pressed:
        return
    p = state['page']
    if   p == PAGE_MAIN:    _handle_main(key)
    elif p == PAGE_SHOW:    _handle_show(key)
    elif p == PAGE_WALKUPS: _handle_walkups(key)
    elif p == PAGE_ROLES:   _handle_roles(key)
    elif p == PAGE_SFX:     _handle_sfx(key)
    elif p == PAGE_MACROS:  _handle_macros(key)
    elif p == PAGE_DISPLAY: _handle_display(key)
    elif p == PAGE_MENU:    _handle_menu(key)
    elif p == PAGE_SCENES:  _handle_scenes(key)
    elif p == PAGE_MUSIC:   _handle_music(key)
    elif p == PAGE_SLIDES:  _handle_slides(key)
    elif p == PAGE_VSCARDS: _handle_vscards(key)
    elif p == PAGE_VIZ:     _handle_viz(key)
    elif p == PAGE_GAMES:   _handle_games(key)

def _nav(page_idx):
    state['page'] = page_idx
    state['sub_page'] = 0   # always land on page 1 of whatever we're navigating to
    state['sfx_category'] = None   # always re-enter SFX at the category picker, not wherever it was left
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
    elif key == 9:  _nav(PAGE_SCENES)
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
    flow        = pi['show_flow']
    num_slots   = 12
    total_pages = _list_page_count(flow, num_slots)
    sub_page    = state['sub_page']

    if key < num_slots:
        idx = sub_page * num_slots + key
        if idx < len(flow):
            api_get(f'/api/show/fire?index={idx}')
    elif key == 12:
        if sub_page > 0:
            state['sub_page'] = sub_page - 1
            render_current_page()
    elif key == 13:
        _nav(PAGE_MAIN)
    elif key == 14:
        if sub_page < total_pages - 1:
            state['sub_page'] = sub_page + 1
            render_current_page()

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

STATUS_FILE = Path(__file__).parent / 'logs' / 'streamdeck_status.json'

def _write_status(ready):
    """Written after every load attempt so musicman.py's health check can
    tell whether the deck has actually connected and loaded real show data,
    not just whether the systemd unit is still running (a hung/retrying
    process still shows 'active' the whole time it's stuck)."""
    try:
        STATUS_FILE.parent.mkdir(exist_ok=True)
        STATUS_FILE.write_text(json.dumps({'ready': ready, 'ts': time.time()}))
    except Exception:
        pass

# ── PI DATA FETCH ────────────────────────────────────────────────────────────
GAME_TYPES_CACHE = []

def load_pi_data(retries=4, retry_delay=2):
    """Fetch all show data from musicman.service. Retries a few times on
    failure — a fresh reconnect (HID or WS) can easily race a musicman.service
    restart that's still finishing its own boot, and a single failed attempt
    used to be permanent until the next 60s refresh cycle."""
    global GAME_TYPES_CACHE
    for attempt in range(1, retries + 1):
        try:
            pi['show_flow']    = requests.get(f'{BASE_URL}/api/show_flow',     timeout=5).json()
            pi['circles']      = requests.get(f'{BASE_URL}/api/circles',       timeout=5).json()
            pi['roles']        = requests.get(f'{BASE_URL}/api/roles',        timeout=5).json()
            pi['scenes']       = requests.get(f'{BASE_URL}/api/scenes',        timeout=5).json()
            pi['macros']       = requests.get(f'{BASE_URL}/api/macros',        timeout=5).json()
            raw                = requests.get(f'{BASE_URL}/api/sfx/list',      timeout=5).json()
            pi['sfx']          = [f.rsplit('.', 1)[0] for f in raw] if raw else []
            raw_tags           = requests.get(f'{BASE_URL}/api/sfx/tags',      timeout=5).json()
            # Re-keyed by stem (no extension) since that's what pi['sfx'] and
            # the SFX page's item list are keyed by everywhere else.
            pi['sfx_tags']     = {f.rsplit('.', 1)[0]: tags for f, tags in (raw_tags or {}).items()}
            pi['music']        = requests.get(f'{BASE_URL}/api/music/list',    timeout=5).json()
            pi['slides']       = requests.get(f'{BASE_URL}/api/slides',        timeout=5).json()
            pi['vs_cards']     = requests.get(f'{BASE_URL}/api/vs_cards',      timeout=5).json()
            pi['viz_presets']  = requests.get(f'{BASE_URL}/api/viz/presets',   timeout=5).json()
            pi['game_configs'] = requests.get(f'{BASE_URL}/api/game_configs',  timeout=5).json()
            GAME_TYPES_CACHE   = requests.get(f'{BASE_URL}/api/game_types',    timeout=5).json()
            try:
                pi['current_live_game'] = requests.get(f'{BASE_URL}/api/games/current', timeout=5).json()
            except Exception:
                pass
            # Display files direct from filesystem
            disp_dir = os.path.join(ASSETS_DIR, 'display')
            if os.path.isdir(disp_dir):
                pi['display'] = sorted(
                    f for f in os.listdir(disp_dir)
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.mp4', '.mov'))
                )
            print(f'Data: {len(pi["circles"])} circles, {len(pi["roles"])} roles, '
                  f'{len(pi["macros"])} macros, {len(pi["sfx"])} sfx, {len(pi["scenes"])} scenes, '
                  f'{len(pi["music"])} music, {len(pi["slides"])} slides, {len(pi["vs_cards"])} vs_cards, '
                  f'{len(pi["viz_presets"])} viz, {len(pi["game_configs"])} game configs, '
                  f'{len(pi["show_flow"])} show steps, {len(pi["display"])} display files')
            _preload_logos()
            _write_status(True)
            return True
        except Exception as e:
            print(f'Data load error (attempt {attempt}/{retries}): {e}')
            if attempt < retries:
                time.sleep(retry_delay)
    print('Data load failed after all retries — will try again on next refresh/reconnect')
    _write_status(False)
    return False

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

        elif event == 'scenes_updated':
            # Scene library changed from Admin — refetch just the scenes list
            # (not a full load_pi_data(), which would also re-hit every other
            # endpoint) so the LIGHTS page stays current without waiting on
            # the 60s refresh_loop.
            try:
                pi['scenes'] = requests.get(f'{BASE_URL}/api/scenes', timeout=5).json()
            except Exception as e:
                print(f'Scene refetch error: {e}')
            if state['page'] == PAGE_SCENES:
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
