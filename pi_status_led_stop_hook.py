#!/usr/bin/env python3
"""
Backup "safe to unplug" signal for the Pi status LED — run by systemd as
musicman-statusled.service's ExecStop=, independent of the main
pi_status_led.py process.

Why this exists: pi_status_led.py's own SIGTERM handler is supposed to set
the pixel white before exiting, but if that main process is ever killed
outright (SIGKILL, a hung poll loop that never got a chance to process the
signal, systemd deciding it missed its stop window) instead of exiting
cleanly, nothing inside that process can save it — SIGKILL can't be caught
by definition. This script is systemd's own guarantee, run as a completely
separate process during the normal stop sequence, so the light still turns
white even if the main script's own handling fails for any reason. It's a
backup, not a replacement — pi_status_led.py's handler is still the
fast/normal path; this only matters when that path doesn't run.

Deliberately minimal: no network calls, no config parsing, nothing that can
itself hang. If it can't grab the PWM/DMA channel (e.g. genuinely still
held by the main process mid-exit), it retries briefly then gives up
quietly — it must never make shutdown slower or fail loudly.
"""
import sys
import time

try:
    from rpi_ws281x import PixelStrip, Color
except Exception:
    sys.exit(0)

LED_COUNT      = 1
LED_PIN        = 13
LED_FREQ_HZ    = 800000
LED_DMA        = 10
LED_BRIGHTNESS = 140
LED_INVERT     = False
LED_CHANNEL    = 1
COLOR_SAFE     = Color(255, 255, 255)

for attempt in range(3):
    try:
        strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA, LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
        strip.begin()
        strip.setPixelColor(0, COLOR_SAFE)
        strip.show()
        break
    except Exception:
        time.sleep(0.3)

sys.exit(0)
