#!/usr/bin/env python3
"""
Music Man Pi — status indicator LED.

Drives a single WS2812B pixel on GPIO13 (rpi_ws281x PWM channel 1 — GPIO18/
channel 0 is already claimed by the hifiberry-dacplus audio HAT's I2S pins,
see /boot/firmware/config.txt) through four states:

  BOOTING    (amber)  — script just started, musicman.service not up yet
  RUNNING    (blue)   — musicman.service is responding, but not every
                         configured pole node is reachable yet
  CONNECTED  (green)  — musicman.service is up AND every pole node in
                         config.yaml is currently reachable
  SAFE       (white)  — set once, on SIGTERM, as the very last thing this
                         process does before exiting — systemd sends SIGTERM
                         to every running service during a full system
                         shutdown (not just when this one service is
                         stopped directly), so this fires automatically
                         whether shutdown was triggered by the physical
                         button (via the gpio-shutdown overlay) or a plain
                         `sudo shutdown -h now`.

Runs as root (required for PWM/DMA access) via musicman-statusled.service.
"""
import time, signal, sys
import requests
import yaml
from pathlib import Path
from rpi_ws281x import PixelStrip, Color

LED_COUNT      = 1
LED_PIN        = 13        # GPIO13 — PWM channel 1
LED_FREQ_HZ    = 800000
LED_DMA        = 10
LED_BRIGHTNESS = 140
LED_INVERT     = False
LED_CHANNEL    = 1

COLOR_BOOTING   = Color(255, 140, 0)
COLOR_RUNNING   = Color(0, 90, 255)
COLOR_CONNECTED = Color(0, 200, 60)
COLOR_SAFE      = Color(255, 255, 255)

CONFIG_PATH   = Path('/home/pi/musicman/config.yaml')
BASE_URL      = 'http://localhost'
POLL_INTERVAL = 2.0

strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA, LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
strip.begin()


_last_state = None

def set_color(c, state=None):
    strip.setPixelColor(0, c)
    strip.show()
    global _last_state
    if state is not None and state != _last_state:
        print(f'state -> {state}', flush=True)
        _last_state = state


def _configured_node_ids():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        return [n['id'] for n in cfg.get('pole_nodes', []) if n.get('ip')]
    except Exception:
        return []


def _shutdown(signum, frame):
    # Last action this process ever takes. Deliberately does NOT turn the
    # pixel off afterward — a WS2812B holds whatever color it was last
    # shown with no ongoing signal required, so setting white and exiting
    # leaves it solid white for as long as the Pi has any power at all,
    # through the rest of the shutdown sequence. That used to be a timed
    # hold (white, sleep, then off) — but the timer had no relationship to
    # when the OS actually finishes unmounting the filesystem (the real
    # last step that makes it safe to cut power), so the light going dark
    # was a false "done" signal that could land before shutdown actually
    # finished. "Stays white" isn't exact either, but it never claims
    # done before it's true, which is the property that actually matters.
    set_color(COLOR_SAFE, 'SAFE')
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

set_color(COLOR_BOOTING, 'BOOTING')

while True:
    try:
        musicman_up = requests.get(f'{BASE_URL}/api/state', timeout=2).ok
    except Exception:
        musicman_up = False

    if not musicman_up:
        set_color(COLOR_BOOTING, 'BOOTING')
        time.sleep(POLL_INTERVAL)
        continue

    node_ids = _configured_node_ids()
    all_connected = False
    if node_ids:
        try:
            levels = requests.get(f'{BASE_URL}/api/sound/levels', timeout=2).json()
            all_connected = all(levels.get(nid, {}).get('level', -1) != -1 for nid in node_ids)
        except Exception:
            all_connected = False

    if all_connected:
        set_color(COLOR_CONNECTED, 'CONNECTED')
    else:
        set_color(COLOR_RUNNING, 'RUNNING')
    time.sleep(POLL_INTERVAL)
