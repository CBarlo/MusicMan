#pragma once

// wled.h defines R/G/B as macros that collide with NeoPixelBus member names.
// usermods_list.cpp includes wled.h before this file, so the macros are already
// in scope. Push them off, include NeoPixelBus, then restore.
#pragma push_macro("R")
#pragma push_macro("G")
#pragma push_macro("B")
#pragma push_macro("W")
#undef R
#undef G
#undef B
#undef W
#include <NeoPixelBus.h>
#pragma pop_macro("W")
#pragma pop_macro("B")
#pragma pop_macro("G")
#pragma pop_macro("R")

#include "wled.h"
#include "esp32-hal-matrix.h"
#include "soc/gpio_sig_map.h"
#include <WiFiClient.h>

/*
 * MusicMan Pole Node Usermod — V6
 *
 * GPIO assignments:
 *   GPIO2  — WS2811 strip data (WLED LED output, configured in LED preferences)
 *   GPIO4  — Status LED chain (3× WS2812B), driven here
 *   GPIO16 — MAX485 DE/RE (HIGH = transmit)
 *   GPIO17 — UART2 TX → MAX485 DI (DMX data out)
 *   GPIO25 — I2S WS  (INMP441 mic, handled by WLED sound-reactive)
 *   GPIO26 — I2S SCK (INMP441 mic)
 *   GPIO27 — I2S SD  (INMP441 mic)
 *
 * Status LED meanings (GPIO4 chain, 3× WS2812B):
 *   LED 0  POWER  red=booting  green=WLED running
 *   LED 1  PI     red=no wifi  orange=wifi up/Pi unreachable  green=Pi responding
 *   LED 2  DATA   off=idle  blue=DMX actively transmitting  purple flash=new /dmx command received
 *
 * Pi DMX API — HTTP POST to /dmx:
 *   { "fixtures": [ { "start": 1, "channels": [255,128,0,0,255,0] }, ... ] }
 */

// ── PIN / BUS CONFIG ──────────────────────────────────────────────────────────
#define MM_DMX_DE_PIN    16
#define MM_DMX_TX_PIN    17
#define MM_STATUS_PIN     4
#define MM_STATUS_COUNT   3

// RMT channel 6 — WLED's BusManager allocates from 0 upward; 6/7 are safe for one strip bus.
// If compile fails with "RMT channel in use", try 5 or 7.
#define MM_STATUS_RMT_CH  NeoEsp32Rmt6800KbpsMethod

#define MM_PI_IP         IPAddress(192, 168, 4, 1)
#define MM_PI_PORT       80
#define MM_PI_TIMEOUT_MS  1000
#define MM_PI_INTERVAL_MS 5000

// ── DMX CONFIG ────────────────────────────────────────────────────────────────
#define MM_DMX_CHANNELS   64
#define MM_DMX_BAUDRATE   250000
#define MM_DMX_INTERVAL   33   // ~30Hz

// ── STATUS LED COLORS (RGB) ───────────────────────────────────────────────────
static const RgbColor MM_OFF    (  0,   0,   0);
static const RgbColor MM_RED    ( 90,   0,   0);
static const RgbColor MM_GREEN  (  0,  90,   0);
static const RgbColor MM_ORANGE (100,  40,   0);
static const RgbColor MM_BLUE   (  0,   0, 200);
static const RgbColor MM_PURPLE (120,   0, 200);

class MusicManUsermod : public Usermod {
private:

  // ── STATUS LEDS ──────────────────────────────────────────────────────────────
  NeoPixelBus<NeoGrbFeature, MM_STATUS_RMT_CH>* _statusBus = nullptr;

  bool     _booted        = false;
  bool     _piReachable   = false;
  uint32_t _lastLedUpdate = 0;
  uint32_t _wledFlashEnd  = 0;   // millis() when purple flash expires

  // ── DMX ──────────────────────────────────────────────────────────────────────
  uint8_t  _dmx[MM_DMX_CHANNELS] = {0};
  uint32_t _lastDmxSend   = 0;
  uint32_t _lastPoll      = 0;
  bool     _dmxActive     = false;  // true if any channel > 0

  // ── PI HEALTH CHECK — runs on core 0, low priority ───────────────────────────
  TaskHandle_t _healthTask = nullptr;

  static void _healthCheckTask(void* param) {
    MusicManUsermod* self = (MusicManUsermod*)param;
    vTaskDelay(pdMS_TO_TICKS(4000));  // let wifi settle before first check
    for (;;) {
      if (WiFi.status() == WL_CONNECTED) {
        WiFiClient client;
        client.setTimeout(MM_PI_TIMEOUT_MS / 1000 + 1);
        bool ok = client.connect(IPAddress(192, 168, 4, 1), 80);
        if (ok) client.stop();
        self->_piReachable = ok;
      } else {
        self->_piReachable = false;
      }
      vTaskDelay(pdMS_TO_TICKS(MM_PI_INTERVAL_MS));
    }
  }

  // ── DMX SEND ─────────────────────────────────────────────────────────────────
  void _sendDmx() {
    digitalWrite(MM_DMX_DE_PIN, HIGH);

    // Break: detach GPIO17 from Serial2, hold LOW directly
    pinMatrixOutDetach(MM_DMX_TX_PIN, false, false);
    digitalWrite(MM_DMX_TX_PIN, LOW);
    delayMicroseconds(200);   // 200µs break (spec min 88µs)

    // MAB: drive HIGH, then reattach to Serial2 (UART idle = HIGH, seamless)
    digitalWrite(MM_DMX_TX_PIN, HIGH);
    delayMicroseconds(12);    // MAB (spec min 8µs)
    pinMatrixOutAttach(MM_DMX_TX_PIN, U2TXD_OUT_IDX, false, false);

    // Start code + channel data
    uint8_t buf[MM_DMX_CHANNELS + 1];
    buf[0] = 0x00;
    memcpy(buf + 1, _dmx, MM_DMX_CHANNELS);
    Serial2.write(buf, sizeof(buf));
    Serial2.flush();

    digitalWrite(MM_DMX_DE_PIN, LOW);
  }

  // ── STATUS LED UPDATE ─────────────────────────────────────────────────────────
  void _updateStatusLeds() {
    if (!_statusBus) return;
    uint32_t now = millis();

    // LED 0 — POWER
    _statusBus->SetPixelColor(0, _booted ? MM_GREEN : MM_RED);

    // LED 1 — PI connectivity
    if (WiFi.status() != WL_CONNECTED) {
      _statusBus->SetPixelColor(1, MM_RED);
    } else if (!_piReachable) {
      _statusBus->SetPixelColor(1, MM_ORANGE);
    } else {
      _statusBus->SetPixelColor(1, MM_GREEN);
    }

    // LED 2 — DATA
    // Purple flash (new command) takes priority; then blue if DMX active; else off
    if (now < _wledFlashEnd) {
      _statusBus->SetPixelColor(2, MM_PURPLE);
    } else if (_dmxActive) {
      _statusBus->SetPixelColor(2, MM_BLUE);
    } else {
      _statusBus->SetPixelColor(2, MM_OFF);
    }

    _statusBus->Show();
  }

public:

  void setup() override {
    // Status LEDs — show red immediately on boot
    _statusBus = new NeoPixelBus<NeoGrbFeature, MM_STATUS_RMT_CH>(MM_STATUS_COUNT, MM_STATUS_PIN);
    _statusBus->Begin();
    _statusBus->SetPixelColor(0, MM_RED);
    _statusBus->SetPixelColor(1, MM_RED);
    _statusBus->SetPixelColor(2, MM_OFF);
    _statusBus->Show();

    // MAX485 — idle in receive mode
    pinMode(MM_DMX_DE_PIN, OUTPUT);
    digitalWrite(MM_DMX_DE_PIN, LOW);

    // UART2 for DMX output — 250kbaud, 8N2, TX only
    Serial2.begin(MM_DMX_BAUDRATE, SERIAL_8N2, -1, MM_DMX_TX_PIN);

    // Pi health check — core 0, priority 1
    xTaskCreatePinnedToCore(_healthCheckTask, "mm_health", 4096, this, 1, &_healthTask, 0);

    // /dmx HTTP endpoint
    server.on("/dmx", HTTP_POST, [](AsyncWebServerRequest *r){}, NULL,
      [this](AsyncWebServerRequest *r, uint8_t *data, size_t len, size_t, size_t) {
        DynamicJsonDocument doc(1024);
        deserializeJson(doc, data, len);
        JsonArray fixtures = doc["fixtures"];
        setDmxChannels(fixtures);
        _wledFlashEnd = millis() + 300;  // purple flash for 300ms
        r->send(200, "application/json", "{\"ok\":true}");
      });
  }

  void loop() override {
    uint32_t now = millis();
    if (now - _lastPoll < 20) return;  // 50Hz tick
    _lastPoll = now;

    // Mark booted once WLED interfaces are up
    if (!_booted && interfacesInited) _booted = true;

    // DMX at 30Hz
    if (now - _lastDmxSend >= MM_DMX_INTERVAL) {
      _lastDmxSend = now;
      _sendDmx();
    }

    // Status LEDs at 20Hz
    if (now - _lastLedUpdate >= 50) {
      _lastLedUpdate = now;
      _updateStatusLeds();
    }
  }

  void setDmxChannels(JsonArray& fixtures) {
    for (JsonObject fix : fixtures) {
      uint8_t start = fix["start"] | 1;
      JsonArray chans = fix["channels"];
      if (start < 1 || start > MM_DMX_CHANNELS) continue;
      uint8_t addr = start - 1;
      for (uint8_t val : chans) {
        if (addr >= MM_DMX_CHANNELS) break;
        _dmx[addr++] = val;
      }
    }
    // Recheck whether any channel is active
    _dmxActive = false;
    for (int i = 0; i < MM_DMX_CHANNELS; i++) {
      if (_dmx[i] > 0) { _dmxActive = true; break; }
    }
  }

  void addToJsonInfo(JsonObject& root) override {
    JsonObject user = root["u"];
    if (user.isNull()) user = root.createNestedObject("u");
    user["mm_dmx_ch1"] = _dmx[0];
    user["mm_dmx_ch7"] = _dmx[6];
    user["mm_pi_ok"]   = _piReachable;

    // Expose AudioReactive volume for Pi sound-level polling
    um_data_t *ar = nullptr;
    UsermodManager::getUMData(&ar);
    if (ar && ar->u_data && ar->u_size > 0 && ar->u_data[0])
      user["mm_sampleAvg"] = (uint8_t)constrain(*((float*)ar->u_data[0]), 0.0f, 255.0f);
    else
      user["mm_sampleAvg"] = 0;
  }

  uint16_t getId() override { return 100; }
};
