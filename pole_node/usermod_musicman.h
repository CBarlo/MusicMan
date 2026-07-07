#pragma once
#include "wled.h"
#include <NeoPixelBus.h>

/*
 * MusicMan Pole Node Usermod — V5
 *
 * GPIO assignments:
 *   GPIO2  — WS2812B strip data (set in WLED LED preferences)
 *   GPIO4  — Status LED chain (3× WS2812B via 74AHCT125 pin 5→2Y)
 *   GPIO16 — MAX485 DE/RE (HIGH = transmit)
 *   GPIO17 — UART2 TX → MAX485 DI (DMX data)
 *   GPIO25 — I2S WS  (INMP441 mic, handled by WLED sound-reactive)
 *   GPIO26 — I2S SCK (INMP441 mic)
 *   GPIO27 — I2S SD  (INMP441 mic)
 *
 * Status LEDs (3× WS2812B chain on GPIO4):
 *   LED 0 — PWR:  dim green at boot, always on
 *   LED 1 — WiFi: amber while connecting, green when connected
 *   LED 2 — DMX:  brief red flash on each DMX frame (~30Hz)
 *
 * Pi DMX API — HTTP POST to /dmx:
 *   { "fixtures": [ { "start": 1, "channels": [255,255,100,30,180,0] }, ... ] }
 */

// ── PIN CONFIG ────────────────────────────────────────────────────────────────
#define MM_DMX_DE_PIN       16
#define MM_DMX_TX_PIN       17
#define MM_STATUS_LED_PIN    4   // → 74AHCT125 2A → 2Y → WS2812B DIN
#define MM_STATUS_LED_COUNT  3

// ── DMX ───────────────────────────────────────────────────────────────────────
#define MM_DMX_CHANNELS  64
#define MM_DMX_BAUDRATE  250000

// ── STATUS LED COLORS ─────────────────────────────────────────────────────────
#define SL_OFF     RgbColor(  0,   0,  0)
#define SL_PWR     RgbColor(  0,  30,  0)   // dim green — always on
#define SL_WIFI_OK RgbColor(  0, 180,  0)   // green — connected
#define SL_WIFI_NO RgbColor(180,  80,  0)   // amber — no connection
#define SL_DMX_TX  RgbColor(180,   0,  0)   // red flash — frame sent

// Uses RMT channel 1; WLED's main strip takes channel 0
using StatusLedBus = NeoPixelBus<NeoGrbFeature, NeoEsp32Rmt1Ws2812xMethod>;

class MusicManUsermod : public Usermod {
private:
  uint8_t  _dmx[MM_DMX_CHANNELS] = {0};
  uint32_t _lastDmxSend = 0;
  uint32_t _lastPoll    = 0;

  StatusLedBus _status{MM_STATUS_LED_COUNT, MM_STATUS_LED_PIN};

  // ── DMX SEND ───────────────────────────────────────────────────────────────
  void _sendDmx() {
    digitalWrite(MM_DMX_DE_PIN, HIGH);

    // BREAK: ~88µs low at reduced baud (one byte ≈ 88µs at 83333 baud)
    uart_set_baudrate(UART_NUM_2, 83333);
    uint8_t brk = 0x00;
    uart_write_bytes(UART_NUM_2, (const char*)&brk, 1);
    uart_wait_tx_done(UART_NUM_2, pdMS_TO_TICKS(5));
    delayMicroseconds(12); // MAB

    // Data at DMX baud, 8N2
    uart_set_baudrate(UART_NUM_2, MM_DMX_BAUDRATE);
    uint8_t startCode = 0x00;
    uart_write_bytes(UART_NUM_2, (const char*)&startCode, 1);
    uart_write_bytes(UART_NUM_2, (const char*)_dmx, MM_DMX_CHANNELS);
    uart_wait_tx_done(UART_NUM_2, pdMS_TO_TICKS(10));

    digitalWrite(MM_DMX_DE_PIN, LOW);
  }

  // ── STATUS LEDs ────────────────────────────────────────────────────────────
  void _updateStatus(bool wifiUp, bool dmxTx) {
    _status.SetPixelColor(0, SL_PWR);
    _status.SetPixelColor(1, wifiUp ? SL_WIFI_OK : SL_WIFI_NO);
    _status.SetPixelColor(2, dmxTx  ? SL_DMX_TX  : SL_OFF);
    _status.Show();
  }

public:
  void setup() override {
    // MAX485 DE/RE — idle LOW (receive)
    pinMode(MM_DMX_DE_PIN, OUTPUT);
    digitalWrite(MM_DMX_DE_PIN, LOW);

    // UART2 for DMX output (8N2 at 250kbaud)
    uart_config_t cfg = {
      .baud_rate = MM_DMX_BAUDRATE,
      .data_bits = UART_DATA_8_BITS,
      .parity    = UART_PARITY_DISABLE,
      .stop_bits = UART_STOP_BITS_2,
      .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
    };
    uart_param_config(UART_NUM_2, &cfg);
    uart_set_pin(UART_NUM_2, MM_DMX_TX_PIN, UART_PIN_NO_CHANGE,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    uart_driver_install(UART_NUM_2, 256, 0, 0, NULL, 0);

    // Status LEDs — show boot state
    _status.Begin();
    _updateStatus(false, false);
  }

  void loop() override {
    uint32_t now = millis();
    if (now - _lastPoll < 20) return; // 50Hz poll
    _lastPoll = now;

    bool wifiUp = (WiFi.status() == WL_CONNECTED);

    // DMX at ~30Hz
    bool dmxTx = false;
    if (now - _lastDmxSend >= 33) {
      _lastDmxSend = now;
      _sendDmx();
      dmxTx = true;
    }

    _updateStatus(wifiUp, dmxTx);
  }

  // Called by POST /dmx HTTP endpoint
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
  }

  void addToJsonInfo(JsonObject& root) override {
    JsonObject user = root["u"];
    if (user.isNull()) user = root.createNestedObject("u");
    user["mm_dmx_ch1"] = _dmx[0];
    user["mm_dmx_ch7"] = _dmx[6];
  }

  uint16_t getId() override { return USERMOD_ID_RESERVED; }
};

// ── HTTP ENDPOINT: POST /dmx ──────────────────────────────────────────────────
// In wled00/wled-server.cpp, add after other server.on() calls:
//
//   server.on("/dmx", HTTP_POST, [](AsyncWebServerRequest *r){}, NULL,
//     [](AsyncWebServerRequest *r, uint8_t *data, size_t len, size_t, size_t){
//       DynamicJsonDocument doc(1024);
//       deserializeJson(doc, data, len);
//       JsonArray fixtures = doc["fixtures"];
//       ((MusicManUsermod*)usermods.lookup(USERMOD_ID_RESERVED))->setDmxChannels(fixtures);
//       r->send(200, "application/json", "{\"ok\":true}");
//     });
//
// In wled00/usermods_list.cpp, inside registerUsermods():
//   usermods.add(new MusicManUsermod());
