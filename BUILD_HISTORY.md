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
- Game Entries: named walk-up entries with emoji, music, video, and lighting
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
- Walk-up triggers: circles, roles, and game entries
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

## Phase 7 — Lighting Productization & Show Reliability

### Fixture Type Library
The lighting system grew from raw channel numbers to a named fixture type library. Each fixture type (e.g., "Pin Spot 6ch", "Stage Wash 8ch") stores:
- **Channel definitions (`ch_defs`)** — a name and optional preset values per channel
- **Presets** — named quick-pick values shown as buttons in the scene editor (e.g., MODE: Manual, Strobe, Sound Active)
- **`looks`** — saved full-channel snapshots for common looks (e.g., "White Fast Strobe", "Orange", "Off")

Fixture types are configured once in Admin → Lighting and shared across all pole nodes. The scene editor renders SELECT dropdowns for preset channels and number inputs for intensity channels, automatically.

### DMX Looks
Within each fixture type, operators save named "looks" — a snapshot of all channel values at once. These appear as a dropdown in both:
- The **Admin scene editor** — apply a look directly to a fixture row when building a scene
- The **Console Lights tab** — a full look editor with sliders, preset buttons, SAVE, LOAD, DELETE, and TEST (fires to all matching pole nodes live)

The look editor in the console uses the same fixture type config as admin, so looks saved in either place appear in both.

### Master Dim Fix
The master DIM slider in the console now correctly protects control channels. Channels with named presets (MODE, EFFECT, STROBE RATE, PROGRAM) are excluded from dimming — only intensity channels (RED, GREEN, BLUE, WHITE, DIMMER) scale. This prevents the fixture from entering strobe mode or wrong operating modes when the slider is moved. A `dim_mask` is computed from the fixture type `ch_defs` at scene-fire time and stored alongside the unscaled channel values so re-apply on slider change is always correct.

### Climax Enhancements
The crowd noise Climax now:
- **Stops all audio** immediately — music, SFX, and the playlist advance thread all halt
- **Fires a configurable Climax Scene** (optional) — a full lighting scene replaces the raw 255 DMX blast, giving precise fixture control at the peak moment
- **Fires a configurable Revert Scene** (optional) after the climax duration — instead of blacking out, the system can snap to a specific post-climax state

### Scene Editor Reliability
- Fixed a race condition where opening the scene editor quickly after tab-switch showed "NO POLE NODES CONFIGURED" — the editor now waits for node data before rendering DMX fixture rows
- Fixed look recall for SELECT-type channels — when a look's saved value doesn't match a named preset exactly, a "Custom (v)" option is injected so the value is preserved rather than silently dropped

---

## What Started as an iPhone

It was a Dad's iPhone with some walkup music on it.

Now it's a full show production system — scripted show flow, real-time lighting control across custom-built pole rigs, a live crowd hype gauge with explosion effects, leaderboard games, custom animations, battery-powered lighting nodes built from spare parts, and a Stream Deck. All running on a Pi that was already in a drawer, controlled from an iPad, operated by one person who is also trying to be present at the fire with his kids.

Most of the hardware came from parts already on hand. Most of the scope came from one more idea at the end of a session. The three goals from the beginning — easy to operate, easy to transport, bring Campfire to the next level — have stayed the same throughout.

Henry and Nolan have been part of building it. That matters more than the feature list.

---

---

## Phase 8 — Game Entries

**Game Entries** added as a first-class walk-up type alongside Circles and Roles — purpose-built for named game moments like Chubby Bunny, where a specific participant or moment needs its own music, video, lighting scene, and display treatment.

### What a Game Entry is
Each entry has a name, an optional emoji (shown as the button icon), walk-up music, an optional intro clip, a loop animation, a walkup lighting scene, and an after-fade scene. It fires the exact same walk-up pipeline as a circle or role: music starts, the display shows the name with the video playing, lighting fires, and the system auto-fades on schedule.

### Admin setup (Admin → 🎮 Games → GAME ENTRIES)
- **+ ADD ENTRY** creates a new blank entry
- **Emoji** — a single emoji icon for the console button (defaults to 🎲)
- **Name** — displayed on the HDMI walk-up overlay
- **Walk-Up Music** — upload, pick from music library, or import from USB
- **Intro Clip** — optional video that plays once before the loop animation
- **Loop Animation** — video that plays and loops during the walk-up
- **Timing** — start offset, duration, fade-out
- **Walk-Up Scene / After-Fade Scene** — lighting scenes, same as circle/role walk-ups
- **SAVE / DELETE** buttons

### Console firing (Console → Games tab)
A **GAME ENTRIES** section appears at the top of the Games tab when at least one entry is configured. Each entry is a button labeled with its emoji and name. Tapping it fires the full walk-up sequence.

### Macro integration
Game Entry Walk-Up is a macro step type, grouped under **GAMES** in the step type dropdown. Select an entry, add the step, and the walk-up fires as part of any macro sequence.

### Asset storage
Assets are stored at `assets/game_entries/{id}/` on the Pi — the existing asset upload/USB-import/library-link pipeline handles them automatically.

---

---

## Phase 9 — Interactive Games & Show Reliability

### Interactive Games
Two fully interactive games added as first-class show flow steps and manual console triggers:

**Prize Wheel**
A spin-the-wheel game with a configurable wheel populated from Game Entry names. The wheel spins with physics-based deceleration and lands on a winner. The winner's walk-up fires automatically when the result lands (music, lighting, HDMI overlay). Operator controls: spin, brake, configure. The wheel displays full-screen in the HDMI game iframe overlay; the admin controls it from a dedicated controller tab.

**Shell Game**
A three-cup shell game display (configurable count of cups). The operator controls the pace from a controller panel: hide the ball, shuffle cups, reveal. Display-side animation shows smooth cup sliding. START fires from the Admin panel with a 3-second countdown before cups move.

### Show Flow: Game Steps
The Show Flow can now include game steps — not just macros. When a game step is reached, the HDMI display navigates to the game's display URL (inside an iframe overlay), and the admin console shows the game's controller. Advancing past the step tears down the iframe and transitions back to the normal display.

### Walkup Video Preloading — Delay Eliminated
Walkup videos from Show Flow macros previously showed a 6–7 second black screen before playing. Root cause: Pi 4B Chromium's hardware video decoder takes ~7 seconds to initialize when `video.src` is assigned on an element that has previously decoded content (synchronous GC freeze on the main thread).

**Fix architecture:**
- Eliminated conflicting dual-preload broadcasts that were overwriting each other at button-click time
- Server now includes a `next_preload` field in the `display_walkup` WebSocket payload — only sent when called from Show Flow
- Client uses a **fresh `<video>` element** per preload (never reusing an element with a different src, which was the GC trigger)
- The fresh element is inserted into the DOM and `play()` called (muted) to prime the hardware decoder pipeline; it pauses itself via `oncanplay` once the first frame is decoded, freeing the GPU
- When the next walkup fires, the warm element swaps in instantly (readyState ≥ 2 confirmed); old element is hidden immediately and removed from DOM asynchronously — no `src = ''` call, so no synchronous GC
- Result: step 1 in a show flow still takes ~7 seconds (no prior warmup); steps 2+ are instant

### Display Layer Transition Fixes
- **Game-to-step transition**: When `hideGameFrame()` removed the game iframe overlay, the previous walkup video (from before the game) was revealed underneath because `showStep()` only hid the walkup layer after the step image finished loading. Fixed: walkup layer now fades out immediately when `showStep()` is called, before the image loads
- **No-image step**: `showStep()` previously returned early (no-op) when the step had no display image, leaving the walkup layer permanently visible. Fixed: walkup layer always fades to the title layer, even with no image
- **Warm element z-index**: Fresh preload elements were appended at the end of `#layer-walkup` (after logo and name bar elements), stacking above them. Fixed: inserted before `#wu-frame-hold` with `z-index:1`, matching `#wu-video`
- **Old element not hidden**: On swap, the retired video element stayed `display:block` for 5 seconds (only hidden via a deferred timeout), causing the previous circle's video to show through. Fixed: old element hidden immediately on swap; DOM removal still deferred to avoid GC during the active walkup

### Admin & Config
- **DMX look recall**: Scene editor now auto-detects which saved look matches the stored channel values when re-opening a scene. If an exact match is found, the look dropdown pre-selects that look by name. Applying a look also keeps it selected in the dropdown rather than resetting to the placeholder
- **Game config auto-save**: Editing an existing game config (name, settings) automatically saves on change without requiring an explicit SAVE click

---

## Phase 10 — Admin Reorganization & Visualization Overhaul

The admin panel had grown feature-by-feature for nine phases, and it showed: VS Cards lived in a standalone sidebar tab, Game Entries were buried inside the Games tab, Timer Presets were a plain row-list instead of matching the save/rename/select pattern everything else used, and "Display" had become a catch-all holding Music Viz, Crowd Lighting, Video Overlays, and Climax FX — four unrelated features sharing one page. The nav no longer matched how the system actually worked.

### Nav Restructure
Reorganized the sidebar around what each section is *for*, not just what it configures:

```
WALK-UPS    Circles · Roles
SCREEN      Slides · VS Cards · Game Entries · Games
LIVE        Visualization
CONTENT     Audio · Display · USB Import
SHOW        Show Flow · Macros · Lighting Scenes
SETTINGS    Lighting · System
```

**Screen** groups everything fired to appear on the projector as a discrete element — Slides, VS Cards, Game Entries, and Games all moved here from wherever they'd landed historically. Game Entries got pulled out of the Games tab into its own tab (it shares Circles/Roles' walk-up music+animation+timing pipeline, not the Games Library's game-config pipeline — a different feature that happened to share a tab). Timer/Timer Presets converted from a flat form + plain row-list into the same grid+editor pattern as Circles and Macros: presets grid on the left, full editor on the right, SAVE / GO LIVE / DELETE buttons.

### Visualization: Music Viz + Crowd Meter Merged
Originally split into a separate Crowd Meter tab, then folded back into one **Visualization** tab once it became clear the split was redundant — both Music and Crowd presets needed the same Low/Mid/High color concept, just applied to different targets (on-screen bars vs. physical WLED/DMX lighting). Now one preset type, toggled between 🎵 MUSIC and 📢 CROWD, using the established list+editor pattern.

**Crowd presets** own the full lighting picture in one place: WLED node picker + level effect (brightness or VU-meter climb), a new **DMX Fixture Tracking** feature (pole pinspot/wash fixtures dim up and blend through the same colors as the crowd level rises — defaults to all fixtures, opt out individually), Video Overlays, and **On Peak** (the climax behavior).

Saving a Crowd preset makes it live immediately — unlike Music presets (fired on demand via macro/show flow), Crowd lighting drives real hardware continuously, so there's no separate "go live" step. Multiple named Crowd variations can be built and swapped by selecting one and hitting Save.

### Climax Becomes a Macro
Climax was previously three separate config blocks: a "Climax FX" panel (explosion video + SFX, never actually wired to anything — dead config), a raw-DMX-blast fallback, and scene/revert-scene dropdowns buried inside Crowd Lighting. All three collapsed into one field: **On Peak → Run Macro**. Build a "Climax" macro with whatever scene/SFX/video steps you want; the crowd system just decides which macro fires when the slider peaks. A separate **Revert Scene** field restores normal lighting after the configured climax duration — answering "what happens after climax" explicitly instead of leaving it to whatever the macro's last step set.

Backend: `crowd_climax_macro_id` and `crowd_revert_scene_id` replaced `crowd_climax_scene_id`/`crowd_climax_dmx`/`crowd_revert_scene_id`'s old raw-DMX logic. Since none of the old fields were populated in the live config, no migration was needed — a clean swap.

### DMX Crowd-Level Tracking
New: pole DMX fixtures (pinspot, wash) now track the crowd level continuously, matching the WLED strips' existing climb behavior. Channel math follows the documented pole hardware layout — 6ch pinspot has no dedicated dimmer channel (CH1 must stay unlocked at 255; brightness comes from scaling the RGB channels directly), 8ch wash has a real CH1 master dimmer (RGB stays at full color, CH1 carries the level). Fixture picker in the admin defaults to tracking every configured fixture, with per-fixture opt-out.

Known follow-up (not yet built): DMX updates currently fire on every slider tick rather than debouncing on release with a smoothed ramp between old/new values. WLED's climb mode already avoids this by running its own animation loop — DMX doesn't have an equivalent yet.

### Six Music Visualization Styles
Music Viz previously had exactly one look (bars) with no way to change it. Added a Display Type picker with five more options, each deliberately cheap on the Pi's Canvas 2D renderer (avoiding `shadowBlur`, the single most expensive canvas operation, wherever a new style could get the same look without it):

- **Wave** — one smoothed `Path2D` through the 32 FFT bins, gradient stroke + soft fill + a faint mirror reflection reusing the same path via a canvas transform. One fill, two strokes, no shadowBlur.
- **Dots** — each frequency bin renders as a dot that grows and shifts Low→Mid→High color with its own level, plus a cheap halo (a second translucent circle, not `shadowBlur`).
- **VU Meter** — a classic segmented LED bargraph driven by the track's aggregate RMS energy (already present in the `viz_music` broadcast but unused until now) rather than the 32 FFT bins — green/amber/red zones by position, with a peak-hold marker.
- **Radial Bars / Radial Dots** — the same Bars/Dots concepts, radiating outward from a center circle instead of a horizontal row, sized to use most of the available screen while still leaving room for visible bar/dot travel.

**Center logo**: Radial styles support a center media slot — a static image or a looping video (MP4/WebM, drawn frame-by-frame via `drawImage()` on a `<video>` element, same per-frame cost as a static image). Circular-clipped and cover-fit to the available circle, with a Zoom slider (100–250%, default 115%) to crop past any padding baked into an exported logo file without needing a re-export. Upload/serving follows the same per-target asset pattern already used for VS Card photos (`assets/viz/<preset_id>/logo.<ext>`, served via `/api/viz/presets/<id>/logo`).

---

---

## Phase 11 — Stream Deck Rewrite, DMX Reliability, BAND Slideshow, Pi Power System

### Stream Deck: Full Rewrite
The original Stream Deck driver was a flat set of hardcoded pages, each button wired individually — adding a new page meant writing a new render/handle function from scratch, and nothing scaled past whatever list length was hardcoded at build time. Rewritten around a generic paginated-list engine: any page that's fundamentally "a list of things with a tap action" (scenes, music, slides, VS cards, viz presets, the display library) is now one render function and one handle function, parameterized by data source and fire action. A new **MENU** directory page fans out to all of them, uncapped — a library of 60 scenes pages exactly as well as one of 6.

**Games** got a different treatment: rather than a static config-picker list, the Games page tracks whichever game was most recently launched (from GO LIVE, a show step, or a macro) and swaps to that game's real live controls — SPIN for Prize Wheel, START/STOP for Musical Chairs, REVEAL/NEXT/CORRECT/INCORRECT for Trivia, START/RESET for Shell Game. No live game yet, or a game type with no deck controller defined, falls back to the GO LIVE config picker. Show Flow Keys, walk-up/role buttons, and audio transport carried forward from the original build, now sitting on top of the same paginated engine as everything else.

### DMX Reliability — Three Real Bugs, One Redesign
Traced and fixed across a multi-session debugging arc after poles started "wigging out" mid-show — a fixture would silently drop into its own autonomous/sound-reactive mode (visible as random color chases unrelated to any scene) and stay there until manually recovered, always during DMX animation playback, never during a static scene:

1. **Buffer race**: `_sendDmx()`'s continuous 30Hz retransmission loop and the `/dmx` HTTP handler's write into the same channel buffer had no mutual exclusion — a write landing mid-send could hand the transmit loop a half-updated buffer. Fixed with a `portMUX_TYPE` critical section around both the read and the write.
2. **Chunked-body parsing**: AsyncWebServer delivers POST bodies in chunks via an `onBody` callback; the `/dmx` handler was treating each chunk as if it were the complete JSON payload. A multi-chunk body (anything over ~1 packet) would fail `deserializeJson()` silently or parse a truncated fragment, which could produce a channel value the fixture read as a mode/effect command instead of a color value. Rewrote the handler to reassemble the full body across chunks before parsing, with real validation (size bounds, JSON parse errors) instead of assuming success.
3. **Frame interpolation**: independent of both bugs above, `_run_dmx_anim()` was smoothly interpolating between keyframes rather than hard-cutting — every intermediate value along that fade was a value the fixture had never been scene-tested with, and any one of them landing in the fixture's own mode/effect channel range could trigger the same autonomous-mode failsafe cheap DMX fixtures use when they think they've lost a valid signal. Redesigned the animation loop to hold each frame's exact stored values for the configured interval, then hard-cut to the next frame — zero intermediate values ever sent.

Also added live diagnostics to the pole firmware (`mm_dmx_gap_now`/`mm_dmx_gap_max`, exposed via WLED's info API) to test and rule out a fourth theory — WiFi/send-loop stalling — before landing on the interpolation fix above; gap measurements came back symmetric and boot-transient-only across both poles, disproving the stall theory with real data rather than continued guessing.

Separately, `kill_lights()` gained a 2-attempt retry on both the WLED-off and DMX-off calls, and its DMX zero-out list gained the PAR fixture (address 15), which had been silently left out — kill_lights was leaving the PAR fixture lit on every use even though scene-fire and the rest of the DMX path both knew about it.

### Pi Power Button + Status LED
The Pi previously had no physical power control or boot feedback at all — startup/shutdown status was invisible without a monitor plugged in. Added a manual power button and a WS2812B status pixel: amber while booting, blue once `musicman.service` is up, green once every configured pole node is connected, solid white once a shutdown signal is received (holds indefinitely rather than timing out, so it never claims "safe to unplug" before the OS has actually finished shutting down).

Hardware note worth keeping: the button drives both GPIO3 (the Pi's dedicated wake-from-halt pin, hardware-level and non-transferable to any other GPIO) and GPIO17 (running the `gpio-shutdown` overlay for the press-to-shutdown trigger) — GPIO3 alone can't run the overlay because it doubles as the HiFiBerry audio HAT's I2C clock line. Full wiring in [`WIRING_GUIDE.md`](WIRING_GUIDE.md). Driven by a new `pi_status_led.py` + `musicman-statusled.service`, polling `musicman.service`'s own API rather than depending on it structurally, so the amber "booting" state shows immediately even before the service is fully up.

### BAND Photo Slideshow
New Display feature: pulls photos from the troop's BAND app group album and shows them full-screen on the projector between show segments, with a floating QR code inviting attendees to add their own photos. One-time OAuth setup (manual code-paste flow, since the Pi has no public redirect URL to register) yields a token good for roughly ten years — no ongoing auth maintenance. Sync is boot-time/on-demand only, never a live poll during a show; photos cache to the Pi's SD card with size-capped eviction of the oldest images. Season mode cycles every synced album, Event mode shows just one. v1 is a manual Start/Stop from Admin — not yet wired into Show Flow or the Stream Deck.

### Solix DC Auto-Cycle on Boot
Each pole's Solix C300 occasionally came out of the prior session in a stale DC-disconnected state that a plain reboot didn't clear on its own. Fixed by cycling the DC output off and back on once, automatically, on the first BLE reconnect after each Pi boot — confirmed via the Solix's own `dc_output` telemetry rather than blind timing, so the pole reliably powers back up without a manual toggle at setup.

### Live Scene List Sync
Adding, deleting, or reordering a lighting scene in Admin now broadcasts a `scenes_updated` WebSocket event, so the Console and Stream Deck's scene lists refresh immediately instead of requiring a page reload or tab switch to pick up the change.

---

## Phase 12 — SD Backup, Dual Battery Monitoring, Video Reliability, Shutdown Hardening

### SD Card Backup
The Pi's SD card had never been backed up — a single point of failure for the whole rig with no recovery path if it died mid-season. Added a full clone-based backup system: `scripts/backup_sd_image.sh` partitions and formats a destination USB card reader disk to match the live card's layout, then `rsync -aHAXx`'s the running root filesystem onto it (never a raw `dd` of a live, mounted partition — the standard risk with cloning a filesystem that's being written to the whole time), and rewrites the destination's PARTUUID references so the clone boots independently rather than colliding with the source.

Device selection carries three independent, deliberately redundant safety gates given how unrecoverable a wrong target would be: the Admin UI only lists removable, non-root disks; the Flask route re-validates the same exclusions before spawning anything; the script itself refuses to run against anything that isn't `/dev/sd?`, isn't removable, or backs `/`. Two more gates got added after real testing surfaced real gaps — the candidate list was initially including the mounted music-library USB stick as a selectable (destructive) target, fixed by a `_backup_disk_has_mount()` check applied both in the Python candidate list and as a fourth bash-side gate; and the removable-flag check itself was broken by `lsblk -no RM` returning multi-line output (one line per partition, not just the disk) that never matched the expected single-character comparison, fixed with `lsblk -dno RM`.

Admin gained a System-tab card tracking last-backup time and a running count of config changes since, with a device-picker flow (insert reader → detect → confirm the specific device by name/size → erase & clone) and a live progress bar polling named stages rather than a blind percentage. One manual step, done by Chris rather than by Claude per the sudoers/security boundary: a single NOPASSWD sudoers line scoping the clone script (not a wildcard across `parted`/`mkfs`/`rsync`) so the backend can trigger it without broader root access.

### Dual Battery Monitoring (Pole A + Pole B)
A second Solix C300 unit came online mid-season, paired and labeled "B" to match the existing "A" convention. Both units were already monitored in parallel from day one — the battery poller was never single-unit-only, it just hadn't been exercised with two real units connected until now. The rich battery panel (charge %, AC/DC state, per-unit AC/DC/flashlight controls) that previously only lived on Console got ported to Admin's System tab as its own card, same polling and control functions reused rather than re-implemented.

### Walkup Video Staleness — Cache-Busting Fix
Replacing a walk-up's video in Admin (e.g. re-uploading the Wizard role's video) kept playing the *old* file — a real "game breaker" risk if it happened live. Root cause: uploads always overwrite the same fixed filename (`walkup.mp4`), and both the browser's own HTTP cache (`max_age=3600` on `/assets/`) and the app's own client-side preload caches (`_preloadPool`, `_wuWarm` in `display.html`) key purely by URL with no TTL — a same-URL re-upload was invisible to any of them. Fixed with `_versioned_walkup_file()`, appended as `?v=<mtime>` everywhere a walkup video URL gets built (walk-up fire, game-entry fire, preload payloads, show-flow asset collection) — same file, new mtime, new URL, guaranteed-fresh fetch. Verified live by touching the actual Wizard file and confirming the version number changed in the API response.

### Console / Admin Auth-Gate Mismatches (9 routes)
Several Console buttons (Default Logo, Timer presets/update, Scene reorder, Pole Nodes, Fixture Types, DMX test, WLED presets) called `/api/admin/...` routes that are gated to admin sessions by design — Console runs unauthenticated. The 401 JSON response's missing fields (`.logo`, empty `{error, ok}` objects rendered as literal dropdown options) got silently misread as valid-but-empty data instead of an auth failure, producing confusing but wrong-looking bugs ("Default Logo isn't set" when it clearly was). Fixed by adding public `/api/...` mirror routes for each (sharing extracted helper functions with their `/api/admin/...` counterparts to avoid logic drift) and repointing Console's fetches at them.

### Display Timing Fixes
Two separate display-timing bugs, both surfacing as a walkup video going blank mid-play:
- **Musical Chairs → next walkup**: `hideGameFrame()`'s deferred game-iframe clear (a flat 10s delay working around a documented Chromium GC stall) could fire mid-walkup if the next walkup ran longer than that window, blanking an actively-playing video. Fixed by extending the deferred clear to cover the actual walkup's configured duration.
- **Slide image picker showing junk entries**: the recursive asset scan for the Bulletin slide image picker was pulling in `.thumbs`-style auto-generated thumbnail files as if they were pickable images. Fixed by skipping any path with a hidden (dot-prefixed) directory or file component during the scan.

### Macro Loop
New macro option: **🔁 LOOP**, repeats the step list indefinitely until interrupted by another macro or Kill All. Implemented with zero reindentation of the existing ~200-line step-execution loop — `itertools.chain.from_iterable(itertools.repeat(steps))` swapped in as the iterator only when the macro's `loop` flag is set, otherwise a plain `iter(steps)`. Kill All was extended to also cancel a running loop (previously it only reset the walkup-fade cancel event, not the macro-cancel event).

### Console: Send to Display + Games Library Go Live
Two small Console additions: a **📺 Send to Display** button on every circle/role tile pushes that entry's card and animation to the projector without triggering its music or lighting (`_broadcast_display_only()` — already existed for macro steps, just hadn't been exposed as a direct Console action), for cases where the operator wants the visual up without playing the full walk-up. And a **GO LIVE** button was added to the Games Library game overlay header, covering the case where controls were opened via "join controls" (a card tap, not launch) and nothing is actually live yet.

### Pi Status LED — Shutdown Hang Fix
Reported live: pressing the power button left the status LED green instead of turning white. Reproducing it with `systemctl stop` showed the color *was* being set correctly (`state -> SAFE` logged), but the process then hung for the full 10s `TimeoutStopSec` before systemd SIGKILLed it. `strace` on the hung process caught the exact point: an infinite `clock_nanosleep` busy-wait inside `rpi_ws281x`'s own cleanup code, apparently waiting on a DMA-completion flag that never flips on this hardware — not the color-write call itself, which had already succeeded. `sys.exit()` waits for that cleanup before the process actually terminates, so it never did; replaced with `os._exit(0)`, which ends the process immediately at the OS level and skips the hang entirely, since nothing after the pixel write needs to run anyway. Verified with the identical live reproduction test: hang + SIGKILL before the fix, `0.163s` clean exit after.

Also added, as defense in depth: `pi_status_led_stop_hook.py`, a minimal, independent `ExecStop=` script that sets the pixel white a second way if the main process is ever killed outright (SIGKILL, a hung poll loop) before its own handler gets a chance to run; and enabled persistent (disk-backed) journald logging so a future incident like this one has real logs to diagnose from, rather than the volatile in-memory journal that had already lost the evidence for this one by the time it was investigated.

### Admin Dropdown Population Races (Head to Head SFX, Timer SFX/Scene)
Reported on the iPad: opening Head to Head or Timer preset editors right after the page loaded left the SFX (and Timer's scene) dropdowns empty. Root cause: `sfxList`/`scenes` load via a long *sequential* chain of fetches in the page's `DOMContentLoaded` handler, and both editors build their dropdown options directly from those globals with no wait — on the iPad's slower AP WiFi, tapping into the Games tab could beat that chain, permanently leaving the dropdown built from an empty list. Fixed with a dedicated `openGamesTab()` that explicitly awaits `loadStepLists()`/`loadScenes()` before building any tab-specific UI, regardless of how far the page's own init sequence has gotten. Verified live by forcing the exact race (emptied the globals, throttled fetches, opened the tab) and confirming the dropdowns still populate correctly every time.

### Game Walkup Video Preload
Reported live: a game's intro walk-up video (e.g. the Prize Wheel's) took a noticeably long time to actually start playing after GO LIVE, despite show-flow circle/role walkups already having a one-step-ahead warm-preload path (`_wuWarm` — a fully decoded, instant-play `<video>` element chained via each walkup's `next_preload` field). Game entries fired via `fire_game_entry()` had no equivalent — a known, previously-documented gap. Closed in two places: Console's "join controls" action (opening a game's controller, which normally happens well before GO LIVE is pressed) now fires `/api/games/preload_intro` to warm that game's intro video ahead of time; and `_get_next_walkup_preload()` — the same function that already chains circle/role preloads through the show flow — now also recognizes `game` steps (both direct show-flow steps and `game` actions nested in a macro) and warms their intro entry the same way. Verified live end-to-end: preloaded video reached `readyState 4` (fully buffered) before GO LIVE, then played with zero cold-start delay when fired.

---

## Phase 13 — Physical Timer/Stopwatch Control, Two New Game Types, SFX Tagging

### The Remote Learns Timer and Stopwatch
The MC's handheld M5StickC Plus remote could already fire show flow steps, SFX, and walk-ups, but had no way to run a timer or stopwatch — every skit timer start/pause and every race time had to go through Console. Rather than bolt on a standalone "TIMER" picker entry with no context (the first pass at this, before a proper design conversation ruled it out), the remote now auto-detects when the current show step or macro carries a timer action (`timer_start`/`setup_skit_timer`) and switches itself into Timer mode automatically — then switches back out again the moment that step ends. A held side-button always backs out early, on-screen text says so. Both the countdown and (new) stopwatch tick live on the remote's own screen between its ~3-second state polls, using local `millis()`-based interpolation rather than sitting frozen until the next poll lands.

A real bug surfaced building this: the remote's stopwatch would visibly reset to zero every ~3 seconds while running, even though Console showed the correct elapsed time the whole time. Root cause — `stopwatch_state['elapsed_ms']` is only updated at *stop* time; every browser-based consumer computes live elapsed client-side against its own synced wall clock, but the ESP32 has no wall-clock sync at all, so it was polling a value frozen at 0 and re-deriving a wrong number every cycle. Fixed by having `/api/remote/state` compute a live snapshot server-side (the Pi has correct time, the ESP32 doesn't) before sending it, sidestepping the sync problem entirely.

The skit timer also gained an **auto-hide** option: start it, let it fade off HDMI after a configurable delay so it's not a constant on-screen distraction, while keeping the existing warning-at-15-seconds pop-back-up behavior completely untouched.

### Timed Competition and Countdown Timer — Two New Game Types
**Timed Competition**: a race or timed-challenge game type where the MC runs a stopwatch from the remote (start/stop on the front button, reset on the side) while the console operator records each contestant's name and time into the existing leaderboard/`games.json` pipeline — no parallel results system built, the leaderboard machinery already there for other games does the job. Supports both fastest-wins (races) and longest-wins (a pull-up hold, a plank) via a per-config sort mode, toggleable live from the controller.

**Countdown Timer**: a standalone shared group countdown, distinct from both the Skit Timer and the Timed Competition stopwatch — its own Game Types entry so it can be launched from Show Flow/the Games Library with its own intro screen, controlled from the console, the game controller, or the remote (which reuses the same auto-detecting Timer screen already built for skits).

Both game types launch through the same `_launch_game()` choke point every other game type uses, and both got a real bug fixed along the way: the Timed Competition controller's sort-order badge looked exactly like Console's own (working) sort toggle button but was actually a plain non-interactive `<span>` — it displayed the config's saved sort mode but didn't respond to taps. Converted to a real button with its own session-local toggle state.

### Restart Without Replaying the Walkup
"Restart Game" previously replayed the full intro walkup every time, which was disruptive mid-testing or mid-recovery. Added a general `skip_intro` flag threaded through `_launch_game()`/`/api/games/launch` — skips every walkup/fallback-screen/display-navigate side effect for any game type while still doing the full state reset (fresh trivia index, wiped scores, wiped stopwatch, fresh timer duration). Built for Trivia's Restart button specifically, but available to every game type since it lives in the shared launch path, not a Trivia-only branch.

### Trivia HDMI Question Overflow
Long trivia questions were running off the bottom of the HDMI display — the auto-shrink logic (`_fitQuestionText()`) was supposed to scale the question down to fit, but wasn't working. Root cause: the shrink loop set a smaller `font-size` then immediately measured `.scrollHeight` on the same tick, but the element had an active CSS `transition` on font-size — the measurement was reading a value from mid-transition, not the size that had just been set, so the loop's decisions were made off stale data. Fixed by disabling the transition for the duration of the measurement loop, forcing a layout, then restoring it — verified against both a real 133-character question and a deliberately extreme 280-character stress test in an isolated test harness before deploying, to avoid contaminating a live show. While in there: the "AG Trivia" brand row was hidden entirely in display mode and stage padding was cut from 8vw/5vh to 3vw/2vh, reclaiming real screen space per direct feedback that the projector had far more room to work with than the layout was using.

Separately, the Games Library's **Trivia** game type label was corrected from "AG Trivia" (which was actually just the name of one specific themed config, not the type) to plain "Trivia" — "AG Trivia" and "Pirate Trivia" are both configs *of* the Trivia type, not the type itself.

### SFX Tagging — Console, Admin, and Stream Deck
The SFX library had grown past the point of being scannable by eye. Rejected the obvious first idea (subfolders by theme) once it became clear some sounds genuinely belong in more than one category, and folders can't do that without either duplicating files or picking one folder arbitrarily — plus moving files would break every existing filename-based reference in macros, walk-ups, timer sounds, and the Stream Deck. Built instead as a pure metadata layer: `config['sfx_tags']` maps a filename to a list of tags, entirely additive, nothing physically moves. Admin gained a per-sound 🏷 tag editor; Console gained filter chips above the SFX grid (including an "untagged" bucket); the Stream Deck's SFX page was rebuilt into a two-level category picker — tap a tag, then tap a sound — reusing the same generic paginated-list engine every other Stream Deck page already runs on, rather than one-off UI.

### Memes Tab Missing GIFs
Reported: GIFs uploaded to the Display library weren't showing up under the Console's MEMES tab. Root cause: MEMES is just the Display library filtered down to a hardcoded video-extension set (`.mp4`/`.webm`/`.mov`/`.ogv`) — `.gif` was never in that list, even though GIFs already played back fine everywhere else in Display (they're served as an image, not a video, and that path was always correct). One-line fix: added `.gif` to the filter.

### Stream Deck: Stop Buttons on SFX and Music
Neither the SFX nor Music pages had a way to cut a currently-playing sound without leaving the page. Added a pinned red STOP button at key 0 on both — SFX gets a new SFX-only `/api/sfx/stop` (stops just the SFX channel, leaves music alone), Music gets a new `/api/music/stop` wired to the existing music-only `_stop_music()` helper (already used internally for the auto-mute-on-timer path, just never exposed as its own endpoint). Both pages drop from 12 items/page to 11 to make room for the pinned key, same reserved-key convention the Display page already established.

---

## Phase 14 — Trivia Lobby Screen, H2H Theme Override, Dropped-Reveal Self-Heal

### Trivia Lobby Screen
Trivia previously went straight from the intro walkup's frozen last frame into question 1 the moment the operator hit Start Game — no beat in between, unlike the Prize Wheel's idle-spin-then-real-spin pattern. Added a proper lobby state: the walkup now auto-reveals into a lobby screen (team scoreboard across the top if a Head to Head game is linked, the config's own name centered — "AG Trivia," "Pirate Trivia," whatever it's named) instead of freezing on the walkup. Start Game now switches the already-visible screen from lobby to question 1, rather than performing the reveal itself. Tracked as a real `started` flag on the trivia session state (not a client-side guess), pushed to every connected screen so a second display tab or a mid-session HDMI reload resyncs correctly, and Restart Game now correctly sends the display back to the lobby too (previously it left whichever question was on screen frozen, since nothing told the display about the reset).

Also added an optional **Lobby Image** — pick any Display Library image to replace the name/"Get Ready" text entirely (team scores still show either way). Building this surfaced a real CSS sizing bug worth remembering: `width:auto; height:auto` with a `max-height` cap only ever *shrinks* an oversized image, it never *grows* a smaller one — a 1920×1080 lobby image was rendering at native pixel size (exactly half-width, half-height) on a 4K kiosk output, and no amount of raising the cap did anything since the image was never hitting it. Confirmed by grabbing an actual screenshot straight off the physical HDMI output via `grim` over SSH rather than trusting an isolated browser test, which had used a test image large enough to actually mask the bug. Fixed by forcing the image element to `width:100%; height:100%` and letting `object-fit:contain` do the scaling in both directions.

### Head to Head Theme Override
A trivia game linked to a Head to Head scoreboard had its own Correct/Incorrect scene+SFX fields, but they did nothing — a linked scoreboard replaced the buttons that would have fired them with H2H's own +/− controls, which used H2H's own (separately configured) scene+SFX instead. Chris's framing: a Head to Head scoreboard is a persistent team/competitor list, often reused season-to-season across many differently-themed games, so the theme belongs to whichever game is actually running, not the scoreboard. `/api/head_to_head/<id>/score` now accepts optional `scene`/`sfx` overrides in the request — present (even blank) they win over the H2H game's own fields; absent, standalone H2H scoring (Console's own panel, Stream Deck) is unaffected. Trivia's controller now always sends its own theme along with every H2H score press. Verified live by temporarily pointing the H2H game's own scene at something different from the linked trivia config's and confirming the trivia-themed scene fired instead, then confirming a plain H2H score call (no overrides) still used H2H's own.

### Dropped-Reveal Self-Heal
Reported live: Trivia's Start Game left the display stuck on the intro walkup's frozen last frame, with no way to get questions, scores, or anything else onto HDMI. Root cause: the reveal is a one-shot WebSocket broadcast, and display.html's reconnect handler only checked for a full service restart (`startup_id` change) — a WiFi hiccup landing at the exact moment the reveal fired would drop the event with no recovery path short of a full reload. Fixed generically: `_current_live_game` now tracks whether the live game is actually supposed to be revealed right now (`revealed`/`disp_url`), exposed via `/api/state`, and display.html's reconnect handler self-heals by re-applying the reveal if the server says it should be showing but isn't. Scoped deliberately to Trivia's server-driven reveal only — other game types' intro-video reveals are client-timed (the video's own `onended`), which the server has no way to know the exact moment of either way.

### Timer Show/Hide Toggle on the Remote
The remote's side button on the Timer screen only ever showed the countdown, with no way to hide it again short of waiting for auto-hide. Added a real `visible_on_display` flag to `timer_state`, kept accurate across every place visibility changes (manual show/hide, auto-start, auto-hide, the warning-at pop-back-up, expiry) so the remote's toggle reads genuine server state rather than tracking its own guess — it stays correct even if auto-hide fires between button presses. Side button now toggles; on-screen hint updates between "SIDE=SHOW" and "SIDE=HIDE" to match.

### Circle & Role Delete
Admin had create/update for Circles and Roles but no delete at all. Added `/api/admin/circle/<id>` and `/api/admin/role/<id>` DELETE routes (same pattern as the existing Game Entry delete — removes it from the list and cleans up its asset folder), with a confirm-gated 🗑 button in each editor.

### Musical Chairs: Remote Playing the Wrong Song
Reported live: after switching songs on the console mid-testing, pressing START on the physical remote played the *original* song instead. Root cause: the remote has no song picker at all — it just presses START with no song specified — and the server's fallback for "no song given" went straight to the config's static Admin-set default, completely ignoring whatever song was actually active for the live session. Fixed the fallback chain to check the session's current song first, only dropping to the config default after a real Reset (which already correctly clears it). Also fixed a related but separate gap: the song dropdown itself was pure local per-tab state with no sync at all, so a second screen (iPad) kept showing whatever song it happened to load with even after another screen started a different one — now every connected screen updates its dropdown to match the session's actual current song, the same way the rest of the game's state already syncs.

---

*Last updated: August 2026 — Phase 14*
