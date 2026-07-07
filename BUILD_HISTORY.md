# MusicMan — Build History & Project Chronicle

*A campfire show controller built by a dad and his kids, for a dad and his kids.*

---

## Origin

Music Man started as a Dad's iPhone with walkup music on it.

Adventure Guides runs campfire shows — circles, roles, skits, ceremonies, games. The "production" was whatever was on the phone: a playlist, maybe some sound effects if you remembered to download them, and whatever lights happened to be nearby. It worked until it didn't, and "didn't" usually meant the wrong track at the wrong moment, fumbling with the lock screen in the dark, or just winging it because there was no way to coordinate everything at once.

The goals were straightforward:
- **Easy to operate** — one person running the whole show, no help needed
- **Easy to use and transport** — packed up quickly, setup in minutes, nothing that requires a truck
- **Bring Campfire to the next level** — not just background music but a real produced show that the kids actually feel

Most of the hardware is parts that were already around. The Pi was spare. The lights were salvaged. The power banks were repurposed. The audio gear already existed. The build philosophy throughout has been: use what's available, add what's missing, keep it simple enough that it actually gets used.

That philosophy and those three goals have stayed constant. The scope has not.

---

## Phase 2 — The Raspberry Pi Controller

The Pi was already on hand. Flask runs on anything. The idea: a local web app the iPad talks to — one screen, one operator, everything in one place, accessible at `musicman.local` from any device on the network with no app install required.

A Raspberry Pi 4B became the brain — small, silent, always-on, powered off a USB-C brick.

**Core stack:**
- Raspberry Pi 4B (4GB RAM)
- Python / Flask web app + Gunicorn (1 worker, 8–32 threads)
- flask-sock for real-time WebSocket communication
- Chromium kiosk on HDMI output (display browser, no keyboard, no mouse)
- USB drive for all show media (hot-swappable)
- Audio HAT for clean audio output to PA

**First software features:**
- Sound effects (one-tap triggers from iPad)
- Background music (playlist with volume control)
- Walk-up music for circles and roles
- HDMI display (show images fullscreen from the iPad)
- Basic show flow (scripted sequence of steps)

The whole system became accessible at `musicman.local` from any device on the same Wi-Fi network — no app install, just a browser.

---

## Phase 3 — Show Production Features

Once the foundation was solid, the scope expanded to match what a real show needs:

**Circles & Roles (Walk-Ups)**
Each Adventure Guide circle and each role (Expedition Navigator, Firestarter, Story Teller, etc.) got its own walk-up: a dedicated music track, display image or video, and lighting scene that fires together with one tap. Walk-ups automatically sequence through the induction ceremony.

**Macros**
Multi-step automated sequences: play a sound, change a light, show an image, wait, fire the timer — all triggered by one button. Macros can be chained into the Show Flow or run manually. Steps are reorderable with ▲/▼ buttons.

**Show Flow**
A scriptable sequence of show steps — the full show as a single ordered list. The operator advances through it one button at a time. Each step can run a macro, trigger a walk-up, start a timer, or just be a named marker. The whole show is "press next" from start to finish.

**Skit Timer**
A configurable countdown timer with:
- Custom duration and display name
- Start sound (loopable during skit, fades on pause)
- 15-second warning (always appears on HDMI regardless of other settings)
- End sound
- Named presets — save and reload full timer configurations
- Load Timer Preset macro step for automated timer setup

**Games Timer**
Separate from the skit timer. Full countdown + stopwatch + leaderboard. The leaderboard displays on the HDMI with participant names and times, sortable fastest-first or longest-first. The left panel mirrors the countdown so the operator always knows where the game stands.

**Live Slides**
On-the-fly slides pushed to the HDMI without going to admin — big title, bulletin, next event, spotlight, QR code. Templates with accent color pickers. Used for last-minute announcements and schedule changes.

**USB Playlist**
Browse a USB drive by folder and play it as a shuffled playlist directly from the console. Avoids having to pre-configure every track in the media library.

---

## Phase 4 — Lighting

**WLED Network Lighting**
WLED LED controllers integrated over the local network. Lighting scenes are configured in Admin and pushed to WLED devices via their HTTP API. Scenes trigger automatically from macros and walk-ups, or manually from the Scenes section of the console.

**Pole Lighting Nodes**
Custom-built per-pole lighting rigs controlled over the network, with DMX driven locally by an ESP32:

| Component | Spec |
|-----------|------|
| Pinspot | 10W, 6-channel DMX |
| Wash light | ~40W, 8-channel DMX |
| Pixel ring | WS2811 80mm LEDs |
| Controller | ESP32 + MAX485 RS-485 transceiver |
| Power | Buck converter, ~75W per pole |
| Runtime | ~4 hours on Anker SOLIX C300 battery |

Each pole is self-contained: the ESP32 drives DMX locally, receives commands over Wi-Fi from the Pi. DMX channel map: 14 channels per pole (6 pinspot + 8 wash).

**Anker SOLIX Battery Integration**
The SOLIX C300 power stations for the poles are monitored through the Admin panel — battery state, AC/DC output control — without needing a separate app.

---

## Phase 5 — Crowd Noise System

The biggest production feature: a live crowd noise gauge displayed on the HDMI, controlled by the operator from the iPad.

**How it works:**
- Operator drags a slider on the iPad (send-on-release, exponential ramp)
- The HDMI shows a large animated gauge — VU meter style, with needle and LED bars
- The needle ramping is frame-rate independent, physics-based (exponential approach)
- At full level, the gauge shows organic needle wavering — like a real instrument pegging
- WLED crowd lighting follows the slider: color-interpolates from cool green → amber → red, brightness tracks level, strobe on climax

**Gauge display modes** (configurable in Admin):
- Needle only
- Bars only
- Needle + bars
- Needle + bars + number
- Needle + number
- Bars + number

**Text overlays** (e.g. "GET LOUD!", "LOUDER", "LET'S GOOO")
- WebM/MP4 video files played over the gauge using `mix-blend-mode: screen`
- Black background in the video renders transparent; colored text appears over the gauge
- Two video elements swap on first-frame-ready to eliminate flash between switches

**Climax FX**
When the operator hits CLIMAX:
1. Gauge needle slams instantly to maximum
2. Any text overlay is killed
3. A fullscreen explosion video fires on top of the gauge (z-index 20), in sync with the explosion sound
4. Canvas animation stops (frees CPU for video decode)
5. When the video ends, the gauge hard-cuts to black — no fade

Climax FX are configured in Admin: choose the explosion video (WebM or MP4) and the explosion SFX.

---

## Phase 6 — Stream Deck Integration

An Elgato Stream Deck can control MusicMan directly — walk-ups, show flow advance, SFX, macros — with physical buttons and custom icons. Configured via a Stream Deck plugin bundled with the project.

---

## Current System Architecture

```
iPad (Console / Admin)
    │
    └── Wi-Fi ──► Raspberry Pi 4B
                      │
                      ├── Audio HAT ──► PA system
                      ├── HDMI ──────► TV / projector (Chromium kiosk)
                      ├── USB ───────► Media drive (music, SFX, video, images)
                      ├── Wi-Fi ─────► WLED crowd lighting controllers
                      ├── Wi-Fi ─────► Pole lighting nodes (ESP32 per pole)
                      └── Wi-Fi ─────► Anker SOLIX battery monitors
```

**Software components:**
- `musicman.py` — Flask app (API + WebSocket server)
- `streamdeck.py` — Stream Deck plugin bridge
- `static/musicman_ui.html` — Operator console (iPad)
- `static/musicman_admin.html` — Admin configuration panel
- `static/display.html` — HDMI display (Chromium kiosk)
- `static/updater.html` — OTA update interface

---

## Feature Inventory (current)

### Admin Panel
- Circles (walk-ups): name, image/video, music, lighting scene, macro
- Roles (walk-ups): same structure as circles
- Lighting scenes: WLED device zones, effect, brightness
- Macros: multi-step sequences with reorderable steps
- Show Flow: ordered list of show steps
- Timer presets: named skit timer configurations
- Games: countdown, stopwatch, leaderboard
- Crowd Noise settings: gauge display mode, color mapping (low/mid/high), crowd lights assignment
- Climax FX: explosion video + SFX configuration
- Display logo: default logo image for standby
- WLED device management
- Pole node management
- System: update, reboot, reload display, Wi-Fi management

### Console (Operator iPad)
- Show Flow navigation (advance / jump to step)
- Transport: Stop, Pause, Fade & End, Track ±, BAE Logo, Clear Display, Kill All
- Volume sliders: music + SFX
- Walk-up triggers: circles and roles
- Macro triggers (manual)
- SFX grid
- Music library browser
- Lighting scenes + brightness
- Skit Timer: start/pause/reset, show-on-screen toggle
- Games: countdown timer, stopwatch, leaderboard
- Display tab: image list, live slide builder
- USB playlist tab
- Wi-Fi management
- Crowd Noise: slider, text overlay picker, CLIMAX button, crowd viz show/hide
- Help (manual)

### HDMI Display Capabilities
- Walkup overlays (image + name bar + music)
- Show step animations
- Fullscreen images
- Live slides (title, bulletin, next event, spotlight, QR)
- Countdown timer (15-second warning always visible)
- Leaderboard
- Crowd noise gauge (multiple display modes)
- Text video overlays (via mix-blend-mode screen)
- Climax FX fullscreen video
- Standby mode

---

## What Started as an iPhone

It was a Dad's iPhone with some walkup music on it.

Now it's a full show production system — scripted show flow, real-time lighting control across custom-built pole rigs, a live crowd hype gauge with explosion effects, leaderboard games, custom animations, battery-powered lighting nodes built from spare parts, and a Stream Deck. All running on a Pi that was already in a drawer, controlled from an iPad, operated by one person who is also trying to be present at the fire with his kids.

Most of the hardware came from parts already on hand. Most of the scope came from one more idea at the end of a session. The three goals from the beginning — easy to operate, easy to transport, bring Campfire to the next level — have stayed the same throughout.

Henry and Nolan have been part of building it. That matters more than the feature list.

---

*Last updated: July 2026*
