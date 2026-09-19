# MusicMan Wiring Guide

Physical wiring reference for the whole rig: the Pi, its power button and status
light, each pole node, the DMX fixture chain, and how the Solix battery units
feed everything. For day-to-day operation see `MusicMan_Manual.html`; this doc
is for building, repairing, or rewiring hardware.

## System Overview

```
                         ┌─────────────────────────┐
                         │   Raspberry Pi 4B        │
                         │   (musicman.service,     │
                         │    streamdeck.service,   │
                         │    musicman-statusled)   │
                         └───────┬─────┬─────┬──────┘
                 WiFi AP ────────┘     │     └──────── USB
             (iPad Console,            │HDMI          (Stream Deck)
              MusicMan network)        │
                                   Projector/TV
        WiFi (per pole)  ──────────────┴──────────────  WiFi (per pole)
              │                                               │
       ┌──────▼───────┐                                ┌──────▼───────┐
       │  Pole Node A  │                                │  Pole Node B  │
       │  ESP32+MAX485 │◄── BLE ──┐          ┌── BLE ──►│  ESP32+MAX485 │
       └──────┬────────┘          │          │          └──────┬────────┘
              │ DMX + 5V/12V power │          │ DMX + 5V/12V power │
     ┌────────┼─────────┐   ┌─────▼─────┐┌─────▼─────┐  ┌────────┼─────────┐
     │ Pinspot│Wash│PAR │   │ Solix C300 ││ Solix C300 │  │ Pinspot│Wash│PAR │
     │  (DMX chain)      │   │  (Pole A)  ││  (Pole B)  │  │  (DMX chain)      │
     └───────────────────┘   └───────────┘└───────────┘  └───────────────────┘
              │ WS2811 pixel data (5 strips)                        │
        (driven off the same ESP32, see Pole Node section)   (same, Pole B)
```

The Pi and each pole node talk over WiFi only (HTTP + the MusicMan WS
broadcast) — there's no wired data run between the Pi and the poles. Each pole
is a self-contained island: its own ESP32, its own Solix C300 for power, its
own DMX fixture chain and pixel strips.

---

## Raspberry Pi

**Power:** USB-C, from a dedicated USB battery bank (or wall power when
available) — separate from the two Solix C300 units, which power the poles
only, not the Pi.

**Audio:** HiFiBerry DAC+ HAT, stacked on the 40-pin header. It claims:
- GPIO2/GPIO3 (I2C, pins 3/5) — used at boot to configure the DAC chip
- GPIO18/19/20/21 (I2S, pins 12/35/38/40) — the actual audio data lines

Both ranges are reserved. Nothing else on the header should use them — this
is exactly the conflict that forced the shutdown button off GPIO3 (see below).

**HDMI:** to the projector/TV, kiosk Chromium via `cage` on tty7.

**USB:** Stream Deck (direct HID, driven by `streamdeck.py`), USB WiFi adapter
for the MusicMan AP, USB stick for music/SFX library (single fixed slot).

### Power button + status LED

Added to give the Pi startup/shutdown feedback without needing a monitor —
four states (booting / running / running+nodes connected / safe to unplug),
one button, one WS2812B pixel. Full detail and the reasoning behind each pin
choice is in `pi_status_led.py`'s header comment; this is the wiring itself.

| Signal | GPIO | Physical pin | Connects to |
|---|---|---|---|
| Wake-from-halt | GPIO3 | pin 5 | Button leg 1 |
| Shutdown trigger | GPIO17 | pin 11 | Button leg 1 (same leg, both legs tied together) |
| Button return | GND | pin 6 or 9 | Button leg 2 |
| Status pixel data | GPIO13 | pin 33 | WS2812B DIN |
| Status pixel power | 5V | pin 2 or 4 | WS2812B 5V |
| Status pixel ground | GND | pin 6, 9, 14, etc. | WS2812B GND |

**Why two GPIOs on one button:** GPIO3 is the only pin on the Pi with
hardware-level wake-from-halt — grounding it powers the board back on even
with Linux fully shut down, a capability tied to that specific pin and not
transferable to any other GPIO. But GPIO3 is also the I2C clock line the
HiFiBerry HAT uses at boot, so it can't *also* run the `gpio-shutdown`
overlay's momentary-press detection without conflicting with the DAC.
Solution: both GPIO3 and GPIO17 go to the same button leg. GPIO3 handles
power-on only (kernel/bootloader-level, doesn't touch the HAT's I2C timing
since it's not "in use" while halted). GPIO17 runs
`dtoverlay=gpio-shutdown,gpio_pin=17` in `/boot/firmware/config.txt` and
handles the press-to-shutdown trigger while running. One button, one press,
does the right thing either way depending on whether the Pi is off or on.

**Status pixel behavior** (driven by `pi_status_led.py`, systemd unit
`musicman-statusled.service`, runs as root for PWM/DMA access):

| Color | Meaning |
|---|---|
| Amber | Booting — `musicman.service` not up yet |
| Blue | Running — service is up, but not every configured pole node is connected |
| Green | Fully connected — every pole node in `config.yaml` is reachable |
| White (solid) | Shutdown signal received. Holds white indefinitely (not a timer) — wait a few seconds after it turns white, then it's safe to flip the switch/unplug |

GPIO13 was chosen specifically because it's `rpi_ws281x`'s PWM channel 1 —
channel 0 defaults to GPIO18, which the HiFiBerry HAT already owns for I2S.

---

## Pole Node (×2, identical)

Full pin-level schematic with every connection: [`pole_node/pole_node_schematic.html`](pole_node/pole_node_schematic.html)
(open in a browser). Condensed summary:

**Core:** ESP32 (DORHEA ESP-WROOM-32) running WLED + the `usermod_musicman`
firmware. Powered from 5V VIN, sourced from a 12V→5V buck converter fed by
the Solix's DC output.

| Function | ESP32 pin | Goes to |
|---|---|---|
| DMX data | G17 | MAX485 DI |
| DMX direction | G16 | MAX485 DE + RE (tied together) |
| WS2811 pixel data | G2 | 74AHCT125 level shifter (3.3V→5V) → pixel strip DIN |
| Pole status LEDs | G4 | 3× WS2812B chain (power/connectivity/DMX-activity indicators, on-pole not to be confused with the Pi's own status pixel) |
| Mic (I2S) | G25/G26/G27 | INMP441 — WS/SCK/SD |

The 74AHCT125 level shifter exists because the ESP32's 3.3V logic isn't
reliably read as "high" by 5V WS2811 pixels at cable length — one channel of
the chip steps G2 up to a clean 5V signal before it reaches the strip.

**DMX fixture chain** (MAX485 A/B → pinspot → wash → PAR, addressed
sequentially on one universe):

| Fixture | DMX address | Channels |
|---|---|---|
| Pinspot (10W) | 1 | 6 |
| Stage wash (~40W) | 7 | 8 |
| PAR | 15 | 10 |

Pinspot and wash channel maps (master/RGBW/mode — set mode channels to 0/
manual for scene control, see `project_pole_dmx` notes) are documented in
Admin → Lighting Hardware → fixture types, which is also the source of truth
for the PAR's channel layout since it's configured there rather than
hardcoded.

**Pixel strips:** 5× WS2811 80mm strips per pole (2 stage-facing, 3
audience-facing), 60 LEDs each, daisy-chained off the single level-shifted
data line from G2.

**Fog unit (planned, one per pole — not yet built/wired)**

DIY ultrasonic-mist fogger: a sealed accumulation chamber (mist charges it for
several seconds before release, for a denser cloud than a continuous stream),
water reservoir with a float switch (never run the transducer dry), fan to
push the accumulated fog out. Runs off the same 12V line already feeding the
Node. No pole node firmware changes needed — the node is a generic DMX bridge
(`musicman.py` posts `{fixtures: [{start, channels}]}` to its `/dmx` endpoint
and it just outputs whatever it's told), so this is a config-only addition,
same as any other fixture.

| Fixture | DMX address | Channels |
|---|---|---|
| Pinspot (10W) | 1 | 6 |
| Stage wash (~40W) | 7 | 8 |
| PAR | 15 | 10 |
| **Fog Relay** | **25** | **2 — ch1 mister relay, ch2 fan relay, both simple on/off (0 or 255)** |

Wire the DMX relay board as the next link in the existing chain: Node → Wash
Bar → Pinspot → PAR → PAR → **Fog Relay board**. Board needs its own tap off
the pole's 12V feed alongside the Node.

Once the fixture physically exists and is addressed for real, add a "Fog
Relay" entry in Admin → Lighting Hardware → fixture types (2 channels, as
above), add it to that pole's fixture list at address 25, then build a
**Fog Burst** macro the same way Cheer Director Climax sequences light_ramp/
wait/display_anim — scene → wait → scene → wait → scene, all just switching
the two Fog Relay channels:

1. `scene` — **Fog Charge**: ch1 (mister) = 255, ch2 (fan) = 0
2. `wait` — 6–8s (chamber fills)
3. `scene` — **Fog Release**: ch1 = 0, ch2 = 255
4. `wait` — 2–3s (dump)
5. `scene` — **Fog Off**: ch1 = 0, ch2 = 0

Not created in `config.yaml` yet — needs the real fixture wired and DMX-tested
live before scenes/macro get built against it, same as any other new fixture.

**OTA firmware updates:** once the pole is on the MusicMan WiFi, flash via
`curl -X POST http://<pole-ip>/update -F "file=@firmware.bin"` (WLED's
built-in OTA endpoint — no physical access needed). Pole IPs are listed in
Admin → System → Pole Nodes.

---

## Solix C300 (×2, one per pole)

Each pole has its own Anker SOLIX C300, controlled over BLE from
`musicman.py` (charge %, AC/DC state, flashlight mode — see Admin → System →
Battery Monitor).

| Output | Feeds | Notes |
|---|---|---|
| DC output (12V car socket) | 12V→5V buck converter → ESP32 + pixel strips | Cycled once automatically on the first BLE reconnect after each Pi boot, to clear a stale disconnected-DC state left over from the prior session — see `_cycle_dc_once_per_boot()` in `musicman.py` |
| AC outlet (inverter) | Pinspot + stage wash, straight 120VAC power | Independent of the DMX signal cable — DMX controls color/mode, the AC outlet is just the fixtures' own power feed. Also switchable per-scene via BLE (off/low/high/SOS is the pole *flashlight* mode, a separate feature from the AC outlet toggle) |

~75W draw per pole at typical show levels → roughly 3.5–4 hours of runtime
per C300 charge.

---

## Related docs

- [`pole_node/pole_node_schematic.html`](pole_node/pole_node_schematic.html) — full pin-level pole node schematic (open in browser)
- [`pole_node/WLED_SETUP.md`](pole_node/WLED_SETUP.md) — WLED config, segments, flash instructions, Pi↔pole API
- [`pi_status_led.py`](pi_status_led.py) — status pixel state machine source, header comment explains the GPIO choices in full
- `MusicMan_Manual.html` — operator-facing manual, including the Power Button & Status Light section
