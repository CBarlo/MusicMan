# MusicMan Field Setup & Breakdown Guide

For roadies and helpers doing physical load-in / load-out at a campout. This is
about getting boxes out of the truck, gear on the ground, cables connected,
and everything packed back up correctly — not about running the show itself.
For that, see `MusicMan_Manual.html`. For internal wiring/repair (inside the
boxes, not field assembly), see `WIRING_GUIDE.md`.

---

## Charge Before You Leave

Do this the night before / morning of, not at the site:
- PAR lights ×4
- Remote (M5StickC Plus)
- iPad mini
- Solix C300 ×2
- Generator — fill with gas
- Camera

---

## What's in Each Case

### Music Man Box
- MusicMan (Pi unit)
- Stream Deck
- Physical remote (M5StickC Plus — MC's handheld show/timer/SFX control)
- Wireless mic receiver (Rx, 2CH)
- (2) Wireless mics
- Wired mic stand
- Wired mic
- Cables for the mics (XLR runs) + the receiver's power adapter — needs a 110V outlet, lives at the console
- Batteries (AA) — for the wireless mics
- MusicMan plug (110V)
- iPad mini
- 3.5mm-to-dual-XLR (L/R) adapter cable — MusicMan's audio output into the PA
- (2) Pole nodes (ESP32 + MAX485 boxes — these travel in this case, not the Lights Box, even though everything else pole-related does)
- **NOT CURRENTLY IN THIS CASE — needs to be sourced/added:** a long Cat6 cable
  (~100 ft) for the video extender run between the MusicMan console and the
  projector. Wasn't in the photographed inventory; pack it with this case.

### Lights Box
- 4 PAR lights (2 per pole, battery-powered — no cable needed)
- 2 pin spots (1 per pole)
- 2 stage wash bars (1 per pole)
- 2 Anker Solix C300 batteries (1 per pole)
- 2 extension cords, 110V 3-port (1 per pole — runs the pinspot's power up the pole from the Solix's AC output, **not** mains/generator power)
- 2 long DMX cables (1 per pole — Wash Bar at the bottom of the pole up to the
  Pinspot/PAR/PAR cluster at the top; see DMX chain below)
- 8 short DMX cables (Node→Wash Bar, plus the jumpers within the top cluster:
  Pinspot→PAR, PAR→PAR — see DMX chain below)
- 2 LED pixel-strip snakes (1 per pole — 5-way splitter off the Node's aircraft connector, one tail per strip)
- 2 12V power cables (Node — Solix DC output → Pole Node)

### Light Stand Case
- 2 speaker stands
- 2 light stands
- 2 extension bars
- 6" PVC tube with 4 LED modules

### Loose / Separate Items
- Expedition EZ-Up (big canopy — stage backdrop + backstage/prep area)
- Projector (has its own battery, but bring the power adapter — see Power below)
- Screen, with its inflator
- Video extender kit (HDMI-over-Cat6 balun pair — one end at the console, one at
  the projector) + the long Cat6 cable noted above
- Generator (backup only — see Power below)
- Gas can
- PA system: Fender Venue powered speaker + 2 speaker stands (stands live in
  the Light Stand Case)
- General-purpose 110V extension cords — for reaching the Music Man Box,
  screen inflator, projector, and PA from the generator/shore outlet. These
  are separate from the 2 pole extension cords in the Lights Box, which are
  already spoken for running pinspot power up each pole.
- Camera
- Folding table — the console table itself (Music Man Box + PA controls sit here)

---

## Per-Pole Assembly (×2, identical)

Each pole is a self-contained power+lighting island — its own Solix, its own
Node, its own fixture chain. Do the full sequence for one pole before moving
to the second; it's easy to cross-wire two poles' cables if you work on both
at once.

1. **Raise the pole to full extension** at its marked position (see Site
   Layout below). Fully extend it before mounting anything — the pixel strip
   receivers are spaced to line up with the strips' magnets only when the
   pole is fully extended.
2. **Set the Solix C300** at the base of the pole. Confirm it's charged and
   powered on before connecting anything.
3. **Power the Node**: plug the 12V power cable from the Solix's 12V/DC
   output into the Pole Node.
4. **Connect the pixel snake**: plug the snake's aircraft connector into the
   Node. Unfurl the 5 tails and match each numbered tail (1–5, marked on both
   the snake end and the strip end) to its matching WS2811 strip connector —
   number to number, no guessing. Secure the bundle to the Node with the
   velcro strap near the power port so it doesn't hang loose or get
   snagged/tripped on.
5. **Wire the DMX chain**, in this exact order:
   `Node → Wash Bar → Pinspot → PAR → PAR`
   Wash Bar sits at the bottom of the pole with the Node — use a short cable
   for that hop. The **long** cable runs from the Wash Bar up the pole to the
   Pinspot/PAR/PAR cluster at the top. Within that top cluster, short cables
   connect Pinspot→PAR and PAR→PAR.
6. **Power the fixtures**:
   - **Wash bar** — plugs directly into the Solix's AC/inverter output.
   - **Pinspot** — plugs into the 110V extension cord, which runs from the
     Solix's AC output up the pole to wherever the pinspot is mounted.
   - **PARs** — battery-powered, no cable. Just confirm each is charged and
     powered on.
7. **Mount and aim**:
   - **Wash bar** — bottom of the pole, hung on its hook and secured with
     the wingnut. General stage coverage, not a tight aim.
   - **Pinspot and both PARs** — top of the pole, same hook + wingnut
     mounting. PARs are general stage coverage; the pinspot is aimed
     specifically at center stage, at dad/kid standing height (a tight spot
     on wherever people actually stand, not the whole stage).
   - **5 pixel strips** (2 stage-facing, 3 audience-facing) — each snaps on
     via a magnet mount into its receiver on the pole. This only lines up
     correctly if the pole was fully extended in step 1 — if a strip's
     magnet doesn't land on its receiver, check the pole is fully extended
     before assuming the strip or receiver is bad.

Repeat for the second pole.

---

## Site Layout

**Poles** — pick whichever fits the site:
- **Option A** — both poles in the audience area, close to the stage.
- **Option B** — both poles on the line between stage and audience, spread
  apart from each other.

Either way, fixture aim stays the same: PARs and wash bars wash the stage
generally, pinspots hit dead center at dad/kid standing height (see Per-Pole
Assembly above).

**EZ-Up** — the big Expedition EZ-Up serves as the stage backdrop and the
backstage/prep area behind it, not a console shelter — raise it early since
the stage's visual backdrop and where circles/roles/skit performers stage
depends on it being up.

**Screen** — faces the seating area, positioned so the projector has a clear,
reasonably short throw; the ~100ft Cat6 run covers the rest of the distance
back to the console electronically, so the console itself doesn't need to be
anywhere near the projector.

**PA** — the two Fender Venue speakers come off their shared unit and go on
stands, spread apart, facing the crowd (see PA / Audio Setup below).

No fixed distances beyond that — read the site and adjust.

---

## Power

- **Shore power first, generator only as backup.** If the site has shore
  power, use it. The generator only comes out if there's no shore power
  available.
- **Music Man Box** needs power (console table, from shore/generator).
- **Projector** has its own battery, but plug it in when power's available —
  don't rely on battery alone for a full show.
- **Screen inflator** needs power continuously — not just a one-time
  inflate-and-disconnect. It has to stay plugged in and running the whole
  time the screen needs to stay up, or it'll start to sag/collapse.
- **Video extender** needs power at one end — whichever of the console or
  projector end is more convenient for that site's power layout. Most kits
  only need one side powered, not both.

---

## PA / Audio Setup (Fender Venue)

The Venue is the hub — everything else plugs into it. Four separate inputs,
each with its own connection:

| Source | Connects via |
|---|---|
| Wireless mic receiver (2CH) | 2× XLR into the Venue |
| Wired mic | XLR into the Venue |
| MusicMan (Pi audio out) | 3.5mm → dual-XLR (L/R) adapter into the Venue |
| Phone | Bluetooth pairing to the Venue directly |

Setup steps:
1. Disconnect the two speakers from the Venue head unit and set them on their
   stands, spread apart, facing the crowd.
2. Run the wireless mic receiver's power and set it up at the console; pair
   the 2 wireless mics.
3. Cable the wired mic, the wireless receiver, and the MusicMan adapter cable
   into the Venue per the table above.
4. Pair a phone over Bluetooth if it'll be used for anything.
5. Leave the Venue powered off until everything above is connected — plug in
   and power on last, same as MusicMan itself.

---

## Setup Order

1. **Site prep** — walk the site, confirm the fire circle location, mark
   where the two poles, EZ-Up, screen, and PA will go (see Site Layout).
2. **EZ-Up** — raise early. It's the stage backdrop and the backstage/prep
   area behind it (where circles/roles/skit performers stage before walking
   up), not a console tent — the console table sets up separately.
3. **Check for shore power.** If there isn't any, position and start the
   generator downwind/away from seating for noise and exhaust, confirm gas
   level, and let it settle before loading anything onto it (see Power).
4. **Screen** — position facing the seating area, inflate it, and confirm the
   inflator is running on continuous power before moving on — don't walk away
   assuming a one-time inflate is enough.
5. **Projector** — position with a reasonably short throw to the screen; plug
   in power if available. Run the video extender's Cat6 cable back to the
   console and power whichever end of the extender kit is more convenient.
6. **Poles** — full per-pole assembly above, both poles.
7. **PA / Audio setup** — full sequence above; leave the Venue powered off
   until every source is connected.
8. **Music Man Box** — unpack onto the console table: Pi unit, Stream Deck,
   the physical remote, iPad mini, wired mic + stand, wireless mic receiver
   (power it, pair the 2 wireless mics, fresh AA batteries), the MusicMan
   output cable into the Venue.
9. **Power everything on**, in this order: shore power/generator first, then
   the Venue, then the Solix units (if not already on from pole assembly),
   then the MusicMan unit last.
10. **Verify connectivity** — once the Pi is booted, its status light should
    go from amber (booting) to blue (running) to **green** once both pole
    nodes are connected. Don't consider setup complete until it's green — a
    pole node that isn't checking in usually means a cable came loose during
    assembly, not a real failure, so it's worth a quick look before assuming
    something's broken.
11. **Hand off to the show operator** to run through the console and confirm
    lights, sound, and display all respond before the show starts.

---

## Breakdown Order

Roughly the reverse of setup, with two safety notes that matter more than
the order itself:

1. **Shut down the MusicMan Pi properly** — press the power button once and
   wait for the status light to turn **solid white** before unplugging
   anything or cutting power to it. White means the Pi has actually finished
   shutting down; unplugging before then risks corrupting the SD card. This
   normally only takes a few seconds.
2. **Pack up the Music Man Box** — mics off and batteries out if it'll be
   sitting for a while, everything back into its foam/case slots.
3. **Power down each pole**: fixtures off first, then the Node, then the
   Solix. Disconnect DMX chain and pixel snake in reverse order (PAR→PAR,
   PAR→Pinspot, Pinspot→Wash Bar, Wash Bar→Node), coil each cable
   individually rather than as one tangled bundle — it's the fastest way to
   turn "quick teardown" into a slow one next time.
4. **Lower and pack each pole**, fixtures and pixel strips secured so nothing
   swings loose in the case.
5. **Disconnect the Venue's four inputs** (wireless receiver, wired mic,
   MusicMan adapter cable, Bluetooth unpair if needed), then the speakers off
   their stands.
6. **Deflate the screen and power down the inflator**, then the projector —
   coil the ~100ft Cat6 cable separately from everything else so it doesn't
   get lost among shorter cables.
7. **EZ-Up** down.
8. **Generator** (if it was running) — let it cool before packing, confirm
   the gas can is sealed.
9. **Final sweep** — walk the site for anything left behind (tent stakes,
   cable ends, a stray AA battery) before loading the truck.

---

## Related docs

- [`WIRING_GUIDE.md`](WIRING_GUIDE.md) — internal wiring reference (inside
  the boxes, not field assembly)
- [`pole_node/pole_node_schematic.html`](pole_node/pole_node_schematic.html) —
  full pin-level pole node schematic
- `MusicMan_Manual.html` — running the actual show once setup is done
