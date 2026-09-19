/*
 * MusicMan Fog Unit — ESP32, one per pole
 *
 * DIY ultrasonic-mist fogger control. WiFi/HTTP, not DMX -- DMX only matters
 * for the commercial fixtures (Pinspot/Wash/PAR) that have no other
 * interface. This is a from-scratch build, so it gets the same control path
 * as every other custom device in the system (pole nodes, WLED, the M5
 * remote): joins the MusicMan AP, runs a tiny HTTP server, musicman.py calls
 * it directly. No MAX485, no DMX chain dependency, no channel bookkeeping --
 * see WIRING_GUIDE.md's "Fog unit" section for the full design writeup this
 * implements.
 *
 * Two relay outputs (mister, fan), MOSFET-switched -- silent, no mechanical
 * wear across a season of burst-fire cycles, unlike a relay module. Each 12V
 * DC load, so a logic-level N-channel MOSFET per channel is the right part;
 * the fan gets a flyback diode across its terminals since a motor is an
 * inductive load and switching it off without one will eventually kill the
 * MOSFET on a voltage spike.
 *
 * ARM/DISARM is a real safety gate, not a UI nicety: checked before *any*
 * fire attempt, local button or network call alike, and re-checked every
 * loop while a sequence is mid-run -- flipping to DISARM mid-burst kills the
 * relays immediately rather than waiting for the sequence to finish. This
 * matters because the whole point is nobody can fire it into someone's
 * hands while the chamber's open for a water refill, regardless of whether
 * that "someone" is standing at the pole or driving it from Console across
 * the field.
 *
 * The fire sequence runs as a non-blocking millis()-based state machine
 * (see loop() / serviceFogSequence()) specifically so the web server keeps
 * answering /status and rejecting overlapping /fire calls during the ~9-10
 * seconds a burst takes, instead of blocking on delay() and going
 * unresponsive right when a status check matters most.
 *
 * Libraries required (Arduino Library Manager):
 *   - ArduinoJson   (Benoit Blanchon) -- same one the M5 remote uses
 *   (WiFi.h and WebServer.h ship with the ESP32 board package, nothing extra)
 *
 * Board: any plain ESP32 dev board (no M5Stack/WLED needed -- this is a
 * 2-relay + 2-input device, WLED's addressable-LED engine would be dead
 * weight here).
 */

#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>

// ── CONFIG ───────────────────────────────────────────────────────────────
const char* WIFI_SSID  = "MusicMan";
const char* WIFI_PASS  = "BrokenArrow";
// Set per unit before flashing -- "musicman-fog-a" for Pole A, "-b" for Pole
// B, so each is easy to find on the network by name (mDNS: <hostname>.local)
// instead of hunting for its DHCP-assigned IP every time.
const char* DEVICE_HOSTNAME = "musicman-fog-a";

const unsigned long WIFI_RETRY_MS = 4000;

// Fire sequence timing -- see WIRING_GUIDE.md's Fog unit section for the
// "charge then release" reasoning (a denser cloud dump than a continuous
// stream). Tune these once the real chamber size/fan CFM is known; these are
// the spec's starting numbers.
const unsigned long CHARGE_MS  = 7000;  // mister runs alone, builds the cloud
const unsigned long RELEASE_MS = 2500;  // fan runs alone, dumps it out

// ── PINS ─────────────────────────────────────────────────────────────────
// Avoids strapping pins (0/2/12/15) and flash pins (6-11) -- safe general-
// purpose GPIOs on any standard ESP32 devkit.
const int PIN_MISTER_GATE = 26;  // -> logic-level N-MOSFET gate -> mister driver board's power line
const int PIN_FAN_GATE    = 27;  // -> logic-level N-MOSFET gate -> fan power line (+ flyback diode across the fan)
const int PIN_ARM_SWITCH  = 32;  // SPDT toggle, other leg to GND -- INPUT_PULLUP, so closed-to-GND = armed
const int PIN_FIRE_BUTTON = 33;  // momentary pushbutton, other leg to GND -- INPUT_PULLUP, pressed = LOW
const int PIN_STATUS_LED  = 25;  // single LED + resistor to GND. Solid = armed, off = disarmed, blink = mid-sequence.

// ── STATE ────────────────────────────────────────────────────────────────
enum FogStage { STAGE_IDLE, STAGE_CHARGING, STAGE_RELEASING };
FogStage fogStage = STAGE_IDLE;
unsigned long stageStartedAt = 0;

bool armed = false;              // live-read from PIN_ARM_SWITCH every loop
bool lastFireBtnState = HIGH;    // for edge detection (debounced)
unsigned long lastFireBtnChangeMs = 0;
const unsigned long DEBOUNCE_MS = 40;

unsigned long lastWifiRetryMs = 0;
unsigned long lastLedBlinkMs = 0;
bool ledBlinkState = false;

WebServer server(80);

// ── FOG SEQUENCE ─────────────────────────────────────────────────────────
// Non-blocking: called every loop(), advances the state machine off
// elapsed millis() rather than delay()-ing through it. Safe to call even
// when idle -- it's a no-op unless fogStage != STAGE_IDLE.
void serviceFogSequence() {
  if (fogStage == STAGE_IDLE) return;

  // DISARM wins immediately, even mid-sequence -- see header comment.
  if (!armed) {
    stopFogHard();
    return;
  }

  unsigned long elapsed = millis() - stageStartedAt;

  if (fogStage == STAGE_CHARGING && elapsed >= CHARGE_MS) {
    digitalWrite(PIN_MISTER_GATE, LOW);
    digitalWrite(PIN_FAN_GATE, HIGH);
    fogStage = STAGE_RELEASING;
    stageStartedAt = millis();
  } else if (fogStage == STAGE_RELEASING && elapsed >= RELEASE_MS) {
    digitalWrite(PIN_FAN_GATE, LOW);
    fogStage = STAGE_IDLE;
  }
}

// Starts a fresh charge->release->off cycle. Caller (handleFire() or the
// local button handler) is responsible for the armed check -- this function
// doesn't re-check, so it's never called from anywhere that hasn't already.
void startFogSequence() {
  digitalWrite(PIN_MISTER_GATE, HIGH);
  digitalWrite(PIN_FAN_GATE, LOW);
  fogStage = STAGE_CHARGING;
  stageStartedAt = millis();
}

// Immediate abort -- both relays off, no matter what stage we were in.
// Used by DISARM-mid-sequence and the /stop endpoint.
void stopFogHard() {
  digitalWrite(PIN_MISTER_GATE, LOW);
  digitalWrite(PIN_FAN_GATE, LOW);
  fogStage = STAGE_IDLE;
}

const char* stageName() {
  switch (fogStage) {
    case STAGE_CHARGING:  return "charging";
    case STAGE_RELEASING: return "releasing";
    default:               return "idle";
  }
}

// ── HTTP HANDLERS ────────────────────────────────────────────────────────
void handleStatus() {
  JsonDocument doc;
  doc["armed"]     = armed;
  doc["stage"]     = stageName();
  doc["firing"]    = (fogStage != STAGE_IDLE);
  doc["uptime_s"]  = millis() / 1000;
  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

void handleFire() {
  if (!armed) {
    server.send(409, "application/json", "{\"ok\":false,\"error\":\"disarmed\"}");
    return;
  }
  if (fogStage != STAGE_IDLE) {
    server.send(409, "application/json", "{\"ok\":false,\"error\":\"already firing\"}");
    return;
  }
  startFogSequence();
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleStop() {
  // Always allowed, armed or not -- this is the "get it back to a known-
  // good state" escape hatch, same spirit as every other recovery path in
  // this project (REFRESH DISPLAY, the title-video recover() timeout, etc).
  stopFogHard();
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleNotFound() {
  server.send(404, "application/json", "{\"ok\":false,\"error\":\"not found\"}");
}

// ── LOCAL PHYSICAL CONTROLS ──────────────────────────────────────────────
void serviceLocalControls() {
  // ARM switch: level-based, no debounce needed beyond the natural mechanical
  // settle time of a toggle switch (unlike a momentary button, it isn't
  // being rapidly pressed/released).
  armed = (digitalRead(PIN_ARM_SWITCH) == LOW);

  // FIRE button: debounced edge detection. Only acts on the press edge
  // (HIGH->LOW), not the release, so holding it down doesn't refire.
  static bool debouncedState = HIGH;
  bool raw = digitalRead(PIN_FIRE_BUTTON);
  if (raw != lastFireBtnState) {
    lastFireBtnChangeMs = millis();
    lastFireBtnState = raw;
  }
  if ((millis() - lastFireBtnChangeMs) > DEBOUNCE_MS && raw != debouncedState) {
    if (debouncedState == HIGH && raw == LOW && armed && fogStage == STAGE_IDLE) {
      startFogSequence();
    }
    debouncedState = raw;
  }
}

void serviceStatusLed() {
  if (fogStage != STAGE_IDLE) {
    // Blink while mid-sequence, ~4Hz, regardless of armed state (armed is
    // implied -- can't be mid-sequence while disarmed, serviceFogSequence()
    // guarantees that).
    if (millis() - lastLedBlinkMs > 125) {
      ledBlinkState = !ledBlinkState;
      digitalWrite(PIN_STATUS_LED, ledBlinkState ? HIGH : LOW);
      lastLedBlinkMs = millis();
    }
  } else {
    digitalWrite(PIN_STATUS_LED, armed ? HIGH : LOW);
  }
}

// ── WIFI ─────────────────────────────────────────────────────────────────
void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiRetryMs < WIFI_RETRY_MS) return;
  lastWifiRetryMs = millis();
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

// ── SETUP / LOOP ─────────────────────────────────────────────────────────
void setup() {
  pinMode(PIN_MISTER_GATE, OUTPUT);
  pinMode(PIN_FAN_GATE, OUTPUT);
  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_MISTER_GATE, LOW);
  digitalWrite(PIN_FAN_GATE, LOW);
  digitalWrite(PIN_STATUS_LED, LOW);

  pinMode(PIN_ARM_SWITCH, INPUT_PULLUP);
  pinMode(PIN_FIRE_BUTTON, INPUT_PULLUP);

  WiFi.setHostname(DEVICE_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  server.on("/status", HTTP_GET, handleStatus);
  server.on("/fire", HTTP_POST, handleFire);
  server.on("/fire", HTTP_GET, handleFire);   // GET too -- lets you test from a browser/curl with no body
  server.on("/stop", HTTP_POST, handleStop);
  server.onNotFound(handleNotFound);
  server.begin();
}

void loop() {
  ensureWifi();
  server.handleClient();
  serviceLocalControls();
  serviceFogSequence();
  serviceStatusLed();
}
