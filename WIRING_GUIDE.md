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
Node.

**WiFi-controlled, not DMX.** DMX is only load-bearing for Pinspot/Wash/PAR
because they're commercial fixtures with no other interface — this is a
from-scratch build, so it gets the same control path as every other custom
device in the system (pole nodes, WLED devices, the M5 remote): its own
ESP32 on the MusicMan WiFi, a tiny HTTP endpoint, `musicman.py` calling it
directly the same shape as `_dmx_post`'s `requests.post(f'http://{ip}/...')`.
No MAX485, no DMX receive library, no physical daisy-chain dependency, no
DMX-address bookkeeping — it doesn't even need to sit near the DMX chain.
Drops straight into the existing "flat list of network devices with tracked
IPs" pattern Admin → System → Pole Nodes already uses.

Two relay outputs (mister, fan) same as the DMX version would have had —
switched by logic-level MOSFETs (silent, no mechanical wear across a season
of burst-fire cycles) rather than a relay module, plus a flyback diode across
the fan since a motor is an inductive load.

**Local physical controls** (wired directly to the ESP32's GPIO — work with
zero network dependency, same "always-available hardware fallback" idea as
the Pi's own power button):

| Control | Type | Behavior |
|---|---|---|
| **ARM / DISARM** | SPDT toggle switch | Safety gate, checked first by both the button and the HTTP handler. DISARM makes the unit refuse every fire command — local button and WiFi alike — so nobody can trigger it remotely while someone's hands are in the box refilling water. Read before anything else, every time. |
| **FIRE** | Momentary pushbutton | Runs the exact same charge→release→off sequence as the WiFi command, only while armed. Lets you test/fire it standing at the pole without touching Console — useful at load-in, and as a manual fallback if WiFi ever hiccups mid-show. |
| **Status LED** *(optional, matches the pole's own status-pixel convention)* | Single LED or WS2812B | Solid = armed, off = disarmed, blink = mid-sequence. Cheap, and gives an at-a-glance read the way every other subsystem here already does. |

Debounce both inputs in firmware; drive the fire sequence as a non-blocking
state machine keyed off `millis()` rather than `delay()`, so the ESP32's web
server keeps answering requests (including a "how's it doing" status poll)
while a burst is mid-cycle instead of freezing for the whole 8-10 seconds.

**Sequence** (identical logic whether triggered by the FIRE button or a WiFi
call):

1. Mister relay ON
2. Wait 6–8s (chamber fills)
3. Mister OFF, fan ON
4. Wait 2–3s (dump)
5. Fan OFF

**Built:** firmware is [`firmware/musicman_fog/musicman_fog.ino`](firmware/musicman_fog/musicman_fog.ino)
(plain ESP32 sketch, no WLED — that engine's addressable-LED overhead has no
use here) — compiles clean against a generic ESP32 devkit (69% flash, 14%
RAM). `musicman.py` side is in too: `_fog_fire()`/`_fog_stop()` and a
`fog_fire`/`fog_stop` macro action, keyed off a `fog_units` list in
`config.yaml` (`[{id, name, ip}]`, empty until real units exist — completely
inert on the live Pi until you add entries). **Not yet deployed to the live
Pi** — no hardware to test the round trip against yet; happens once a unit's
built and addressable.

Physical wiring, matching the pins the firmware actually uses:

| Signal | ESP32 pin | Goes to |
|---|---|---|
| Mister relay | GPIO26 | Logic-level N-MOSFET gate → mister driver board's power line |
| Fan relay | GPIO27 | Logic-level N-MOSFET gate → fan power line (+ flyback diode across the fan — it's a motor, an inductive load, skipping this eventually kills the MOSFET on the switch-off spike) |
| ARM switch | GPIO32 | One leg of an SPDT toggle; other leg to GND. Internal pullup — closed to GND reads armed |
| FIRE button | GPIO33 | One leg of a momentary pushbutton; other leg to GND. Internal pullup — press reads LOW |
| Status LED | GPIO25 | LED + ~330Ω resistor to GND |

Each MOSFET gate wants a small series resistor (~100–220Ω, damps gate
ringing) and a gate-to-source pulldown (~10kΩ) so the load can't glitch on
during ESP32 boot, before `setup()` has run and claimed the pin — same
boot-glitch caution as the Pi's own GPIO3/GPIO17 button wiring above.

**Float switch is not a GPIO input** — it's a plain mechanical/electrical
cutoff wired in series with the mister driver's own power line, upstream of
the MOSFET. That way "don't run dry" holds even if the ESP32 crashes or
reboots mid-show, instead of depending on firmware that might not be running.

ESP32 runs off the pole's 12V via its own small 12V→5V buck (same part
category as the one already feeding the pole node's ESP32) into the board's
5V/VIN pin — most ESP32 devkits have their own onboard 3.3V regulator from
there, no separate 3.3V supply needed.

Two units total, one per pole — flash each with its own `DEVICE_HOSTNAME`
(`musicman-fog-a` / `musicman-fog-b`, set near the top of the .ino) before
uploading, so they're distinguishable on the network.

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
