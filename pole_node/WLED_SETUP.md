# WLED Setup — MusicMan Pole Node

## LED Configuration (WLED UI → LED Preferences)

| Setting | Value |
|---------|-------|
| LED type | WS2812B (works for WS2812E) |
| Color order | GRB |
| Data pin | GPIO 2 |
| LED count | 300 (5 strips × 60 LEDs/m) |
| Max current | 4500mA (safe for 75W buck at 5V) |

## Segments (WLED UI → Segments)

| Segment | LEDs | Label | Notes |
|---------|------|-------|-------|
| 0 | 0–59   | Stage 1   | Strip 1 (stage-facing) |
| 1 | 60–119 | Stage 2   | Strip 2 (stage-facing, mirror seg 0) |
| 2 | 120–179 | Audience L | Strip 3 |
| 3 | 180–239 | Audience C | Strip 4 |
| 4 | 240–299 | Audience R | Strip 5 |

To mirror Stage 1+2: in WLED set segment 1 to **Reverse** and give it the same effect/color as segment 0, or group segments 0+1 as one effect target.

Set segment 0 to mirror (reversed clone of itself) if strips face opposite directions.

## WiFi / Network

- **At campsite (field):** Connect to the MusicMan AP (SSID and password set during Pi setup); DHCP pool is 192.168.4.10–50
- **At home (testing):** Connect to home WiFi; update IP in MusicMan admin (Nodes tab)
- Suggested field DHCP reservations: Pole A → 192.168.4.51, Pole B → 192.168.4.52
- Reserve by MAC in dnsmasq: `/etc/dnsmasq.conf` on Pi

## Build Notes

- WLED v0.15.4 (clone from https://github.com/Aircoookie/WLED)
- Usermod at `pole_node/usermod_musicman.h` (copy to `usermods/musicman/` in your WLED source tree)
- `usermods_list.cpp`: `UsermodManager::add(new MusicManUsermod());` (already done)
- `/dmx` route registered in usermod's own `setup()` — no wled_server.cpp edit needed
- Usermod ID: 100 (clear of all WLED built-ins up to 54)
- Status LEDs (3 × on GPIO4, `MM_STATUS_PIN`/`MM_STATUS_COUNT` in the usermod) show link/DMX/Pi-alive state
- Sound-reactive usermod (AudioReactive): built in, digital I2S mic on GPIO 25/26/27 (`digitalmic.pin` in `cfg.json` — data/clock/word-select). Enable via WLED UI → Sound Settings if a mic is connected.

## Config Backups

`wled_cfg_pole_a.json` / `wled_cfg_pole_b.json` / `wled_presets_reference.json` in this folder
are point-in-time exports of each pole's live WLED config and preset storage (fetched via
`GET /cfg.json` and `GET /presets.json`), kept as a rebuild/restore reference and to make config
drift between poles easy to diff. They're not live-synced — re-export after any config change
you want preserved. WLED redacts WiFi passwords in these exports (only `pskl`, the password
length, is included), so they're safe to commit.

To restore onto a fresh or reflashed node: `POST` the cfg file's contents to `/json/cfg`, and
upload the presets file via `POST /upload` (multipart, field `data`, filename `presets.json`),
then reboot the node. The Admin → Lighting → Pole Nodes page has a **⇄ SYNC PRESETS** button
that does the presets half of this automatically, pole-to-pole, over the network.

## Flash Instructions

```bash
# Use esptool from PlatformIO's newer package (v4.9.0 — NOT the bundled v3.1):
# Replace ~/WLED with wherever you cloned the WLED repo
python3 ~/.platformio/packages/tool-esptoolpy/esptool.py \
  --chip esp32 --port /dev/cu.usbserial-0001 --baud 460800 \
  --before default_reset --after hard_reset write_flash -z \
  --flash_mode dout --flash_freq 40m --flash_size detect \
  0x1000  ~/.platformio/packages/framework-arduinoespressif32/tools/sdk/bin/bootloader_dout_40m.bin \
  0x8000  ~/WLED/.pio/build/esp32dev/partitions.bin \
  0xe000  ~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin \
  0x10000 ~/WLED/.pio/build/esp32dev/firmware.bin
```

## Pi → Pole API

**Set DMX channels:**
```
POST http://192.168.4.51/dmx
Content-Type: application/json

{
  "fixtures": [
    { "start": 1, "channels": [255, 255, 100, 30, 180, 0] },
    { "start": 7, "channels": [200, 255, 100, 30, 180, 0, 0, 0] }
  ]
}
```

**Set pixels (standard WLED JSON API):**
```
POST http://192.168.0.149/json/state
{ "on": true, "bri": 200, "seg": [{"id":0,"col":[[255,100,0]],"fx":0}] }
```

**Check status (includes mm_dmx_ch1, mm_dmx_ch7 from usermod):**
```
GET http://192.168.4.51/json/info
```
