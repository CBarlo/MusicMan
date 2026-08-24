/*
 * MusicMan Remote — M5StickC Plus 1.1 show remote (v2)
 *
 * Walk-around control for the MC. Went through two interaction models before
 * this one: v1's three-button scheme (the power-chip "top" button was too
 * unreliable to build navigation around) and a tilt-to-scroll scheme (never
 * felt reliable enough for live use, even after moving from an absolute-tilt
 * threshold to a self-calibrating rest-position baseline -- holding a
 * precise angle steady enough to read as "neutral" just isn't a thing a
 * walking MC can do). Landed on two buttons, short vs. long press:
 *
 *   FRONT (BtnA) short = next / advance
 *   FRONT (BtnA) long  = select / fire whatever's highlighted
 *   SIDE  (BtnB) short = previous
 *   SIDE  (BtnB) long  = back up to the category picker -- ALWAYS means
 *                        this, in every screen, so it's the one gesture
 *                        that never has to be relearned per context
 *
 * Menu shape:
 *   Category picker (SHOW FLOW / SFX / CIRCLES / ROLES)
 *     -> front/side short to browse, front long to fire, side long to back out
 *   Whenever a game goes live (Musical Chairs or Trivia, launched ANY way --
 *   Show Flow, a macro step, or Admin/Console's direct "GO LIVE") the remote
 *   jumps into that game's own mode automatically:
 *     MUSICAL CHAIRS: front (short) toggles start/stop
 *     TRIVIA: page-turner only -- front = next question, side = previous,
 *       hold front = reveal the answer on the projector. Scoring (correct/
 *       incorrect against the team scoreboard) lives at Console/the trivia
 *       controller, not here.
 *   Side-long from a game mode returns to the category picker -- it stops
 *   this device *watching* the game, not the game itself.
 *
 * Live-step sync: the show step can change from Console/Admin/a macro while
 * an MC is holding this thing on the other side of a fire circle, so the
 * remote never assumes what's on screen is still current. A persistent
 * "LIVE: <step>" strip at the bottom of the picker/browse screens always
 * reflects the real live step. If the step changes while the screen is
 * asleep, it wakes and jumps straight to Show Flow on the new step; if the
 * MC is already awake and mid-browse elsewhere, only the strip updates --
 * their navigation isn't yanked out from under a button they're about to
 * press. See handleStepChange().
 *
 * Connection status is a periodic HTTP heartbeat, not a WebSocket connect/
 * disconnect callback -- see musicman.py's _remote_status comment for why
 * that changed. Deliberately not "parse a live event stream" either: state
 * comes from polling GET /api/remote/state, so a gap of a couple of seconds
 * while walking through a WiFi dead spot just means the next poll catches
 * up, rather than needing any reconnect-specific logic at all.
 *
 * Reliability rule this whole project has been built around: never show a
 * confident wrong answer. If we're not sure what's current, say so.
 *
 * Libraries required (Arduino Library Manager):
 *   - M5StickCPlus       (official M5Stack)
 *   - ArduinoJson         (Benoit Blanchon)
 */

#include <M5StickCPlus.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "boot_logo.h"

// Off-screen frame buffer for drawScreen(). Drawing straight to M5.Lcd was
// visibly janky -- every redraw did a full fillScreen(BLACK) on the live
// panel followed by each element painting in one at a time over SPI, so the
// black wipe and the partial redraw were both visible as flicker. Composing
// the whole frame into this sprite first and blitting it with one
// pushSprite() call makes each frame appear atomically instead.
TFT_eSprite screenBuf(&M5.Lcd);

// ── CONFIG ───────────────────────────────────────────────────────────────
const char* WIFI_SSID       = "MusicMan";
const char* WIFI_PASS       = "BrokenArrow";
const char* PI_HOST         = "192.168.4.1";   // MusicMan AP's own gateway IP
const uint16_t PI_PORT      = 80;
const char* DEVICE_HOSTNAME = "MusicMan-Remote";

const unsigned long STATE_POLL_MS      = 3000;   // was 2000 -- battery
const unsigned long LIBRARY_POLL_MS    = 45000;  // SFX/circles/roles, rarely change
const unsigned long FLOW_POLL_MS       = 30000;  // show flow, rarely changes
const unsigned long WIFI_RETRY_MS      = 4000;
const unsigned long HEARTBEAT_MS       = 5000;   // matches _REMOTE_HEARTBEAT_STALE_S=15 on the Pi
const unsigned long CONN_LOST_AFTER_MS = 12000;
const unsigned long SCREEN_SLEEP_MS    = 12000;  // was 30000 -- backlight is the biggest power draw
// The screen sleeping already kills the biggest draw, but the ESP32 + WiFi
// radio keep retrying every WIFI_RETRY_MS forever underneath -- real drain
// left running unattended for hours after a show ends and MusicMan itself
// is powered down. 10 minutes is long enough that a normal WiFi hiccup or a
// lull between show segments never triggers it, short enough to actually
// save battery if the remote gets left on.
const unsigned long AUTO_POWEROFF_AFTER_MS = 10UL * 60 * 1000;

#define MAX_FLOW_STEPS 120
#define MAX_SFX 100
#define MAX_CIRCLES 24
#define MAX_ROLES 24

// ── MENU MODEL ───────────────────────────────────────────────────────────
enum MenuLevel { LEVEL_CATEGORY, LEVEL_SHOWFLOW, LEVEL_SFX, LEVEL_CIRCLES, LEVEL_ROLES, LEVEL_TIMER,
                 LEVEL_GAME_CHAIRS, LEVEL_GAME_TRIVIA, LEVEL_GAME_TIMEDCOMP };
MenuLevel menuLevel = LEVEL_CATEGORY;

const char* CATEGORY_NAMES[] = {"SHOW FLOW", "SFX", "CIRCLES", "ROLES", "TIMER"};
const int CATEGORY_COUNT = 5;
int categoryIndex = 0;

struct FlowStep  { int index; String type; String name; String gameTypeId; };
struct NamedItem { String id; String name; };

FlowStep  flowSteps[MAX_FLOW_STEPS];   int flowStepCount = 0;   int flowBrowseIndex  = -1;
NamedItem sfxItems[MAX_SFX];           int sfxCount      = 0;   int sfxBrowseIndex    = 0;
NamedItem circleItems[MAX_CIRCLES];    int circleCount   = 0;   int circleBrowseIndex = 0;
NamedItem roleItems[MAX_ROLES];        int roleCount     = 0;   int roleBrowseIndex   = 0;

// ── STATE ────────────────────────────────────────────────────────────────
unsigned long lastHeartbeatOkMs = 0;
unsigned long lastHeartbeatTry  = 0;
unsigned long lastStatePoll = 0;
unsigned long lastFlowPoll  = 0;
unsigned long lastLibraryPoll = 0;
unsigned long lastWifiRetry = 0;
bool didInitialFetch = false;

struct {
  int    index = -1;
  String type;
  String name;
  bool   hasTimer = false;
} currentStep;

unsigned long lastStateFetchMs = 0;   // millis() when timer/stopwatch fields were last refreshed from a poll
bool timerRunning = false;
bool timerPaused  = false;
int  timerSecondsRemaining = 0;
bool timerVisibleOnDisplay = false;
bool stopwatchRunning = false;
long stopwatchElapsedMs = 0;

String liveGameTypeId   = "";
String liveGameConfigId = "";
String lastSeenLiveGameTypeId = "";   // edge-detect a NEW game going live, don't re-force-navigate on every poll

int  lastSeenStepIndex = -999;        // sentinel distinct from -1 (no step), forces first-poll sync
bool haveSeenFirstStep = false;

bool   triviaLive     = false;
int    triviaIndex    = 0;
bool   triviaRevealed = false;
int    triviaCount    = 0;
String triviaQuestion = "";
String triviaAnswer   = "";

bool chairsLive    = false;
bool chairsPlaying = false;

// Tracks in-game changes (question advanced, answer revealed, chairs
// started/stopped) so the screen wakes for those too -- updateMenuLevelForLiveGame()
// only wakes it for a game going NEWLY live, so "Next Question" from Console
// while the remote had already dozed off (12s timeout) silently updated the
// data but never brought the screen back to show it.
int  lastSeenTriviaIndex    = -1;
bool lastSeenTriviaRevealed = false;
bool haveSeenTriviaState    = false;
bool lastSeenChairsPlaying  = false;
bool haveSeenChairsState    = false;
bool lastSeenStopwatchRunning = false;
bool haveSeenStopwatchState   = false;
bool lastSeenTimerRunning = false;
bool haveSeenTimerState   = false;

enum ConnState { CONN_OK, CONN_RECONNECTING, CONN_OFFLINE };
ConnState connState = CONN_OFFLINE;

// Short vs. long press, both resolved on release -- see handleButtons().
const uint32_t LONG_PRESS_MS = 450;

// LED (single red, GPIO10, active LOW)
#define LED_PIN 10
unsigned long lastBlink = 0;
bool ledOn = false;

// Screen backlight power-save
bool screenAwake = true;
unsigned long lastActivity = 0;
float wakeBaseAx = 0, wakeBaseAy = 0, wakeBaseAz = 0;
bool haveWakeBaseline = false;

String toastMsg = "";
unsigned long toastUntil = 0;

// ── FORWARD DECLARATIONS ────────────────────────────────────────────────
void connectWiFi();
void sendHeartbeat();
bool fetchRemoteState();
bool fetchShowFlow();
bool fetchLibrary();
void handleButtons();
void moveSelection(int delta);
void fireBrowsedStep();
void fireSfx();
void fireWalkupCircle();
void fireWalkupRole();
void fireTriviaAction(const String& action);
void fireChairsToggle();
void fireTimerToggle();
void fireTimerToggleDisplay();
void fireTimerReset();
void fireStopwatchToggle();
void fireStopwatchReset();
void updateMenuLevelForLiveGame();
void updateGameStateWake();
void handleStepChange();
void drawScreen();
void drawBootLogo();
void updateLed();
void updateScreenSleep();
void checkAutoPowerOff();
void beepConfirm();
void beepFail();
void showToast(const String& msg);
int  getBatteryPct();
String httpGet(const String& path, bool* ok);
String httpPostJson(const String& path, const String& jsonBody, bool* ok);
int printWrapped(const String& text, int x, int y, int maxCharsPerLine, int maxLines);

// ── SETUP ────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n\n=== MusicMan Remote v2 booting ===");

  M5.begin();
  M5.IMU.Init();   // M5.begin() does NOT init the MPU6886 -- without this, getAccelData() returns dead values and tilt never fires
  M5.Lcd.setRotation(3);
  M5.Lcd.fillScreen(BLACK);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);

  screenBuf.createSprite(240, 135);
  screenBuf.setTextFont(1);

  drawBootLogo();
  delay(1800);

  WiFi.setHostname(DEVICE_HOSTNAME);
  WiFi.setSleep(true);   // modem-sleep between transmissions -- real battery win, no user-visible cost at these poll rates
  connectWiFi();

  lastActivity = millis();
}

// ── LOOP ─────────────────────────────────────────────────────────────────
void loop() {
  M5.update();

  if (WiFi.status() != WL_CONNECTED) {
    connState = CONN_OFFLINE;
    if (millis() - lastWifiRetry > WIFI_RETRY_MS) {
      lastWifiRetry = millis();
      connectWiFi();
    }
  } else {
    unsigned long sinceOk = millis() - lastHeartbeatOkMs;
    connState = (lastHeartbeatOkMs == 0) ? CONN_RECONNECTING
              : (sinceOk > CONN_LOST_AFTER_MS) ? CONN_OFFLINE
              : (sinceOk > HEARTBEAT_MS * 2)    ? CONN_RECONNECTING
              : CONN_OK;

    if (!didInitialFetch) {
      didInitialFetch = true;
      Serial.println("[loop] initial fetch on WiFi up");
      fetchRemoteState();
      fetchShowFlow();
      updateMenuLevelForLiveGame();
      updateGameStateWake();
      handleStepChange();
      fetchLibrary();
    }

    unsigned long now = millis();
    if (now - lastHeartbeatTry > HEARTBEAT_MS) { lastHeartbeatTry = now; sendHeartbeat(); }
    if (now - lastStatePoll > STATE_POLL_MS) {
      lastStatePoll = now;
      fetchRemoteState();
      updateMenuLevelForLiveGame();
      updateGameStateWake();
      handleStepChange();
    }
    if (now - lastFlowPoll > FLOW_POLL_MS)       { lastFlowPoll = now;    fetchShowFlow(); }
    if (now - lastLibraryPoll > LIBRARY_POLL_MS) { lastLibraryPoll = now; fetchLibrary(); }
  }

  handleButtons();
  updateScreenSleep();
  checkAutoPowerOff();
  updateLed();
  drawScreen();
}

// ── WIFI ─────────────────────────────────────────────────────────────────
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void sendHeartbeat() {
  JsonDocument doc;
  doc["battery_pct"] = getBatteryPct();
  String body;
  serializeJson(doc, body);
  bool ok = false;
  httpPostJson("/api/remote/heartbeat", body, &ok);
  if (ok) lastHeartbeatOkMs = millis();
}

// ── HTTP HELPERS ─────────────────────────────────────────────────────────
String httpGet(const String& path, bool* ok) {
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":" + PI_PORT + path;
  http.begin(url);
  http.setTimeout(2500);
  int code = http.GET();
  String body;
  if (code == 200) { body = http.getString(); *ok = true; } else { *ok = false; }
  http.end();
  return body;
}

String httpPostJson(const String& path, const String& jsonBody, bool* ok) {
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":" + PI_PORT + path;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(2500);
  int code = http.POST(jsonBody);
  String body;
  if (code == 200) { body = http.getString(); *ok = true; } else { *ok = false; }
  http.end();
  return body;
}

// ── STATE FETCH ──────────────────────────────────────────────────────────
bool fetchRemoteState() {
  bool ok = false;
  String body = httpGet("/api/remote/state", &ok);
  if (!ok) return false;

  JsonDocument doc;
  if (deserializeJson(doc, body)) return false;

  JsonObject step = doc["step"];
  currentStep.index    = step["index"] | -1;
  currentStep.type     = String((const char*)(step["type"] | ""));
  currentStep.name     = String((const char*)(step["name"] | ""));
  currentStep.hasTimer = step["has_timer"] | false;

  JsonObject lg = doc["live_game"];
  liveGameTypeId   = String((const char*)(lg["game_type_id"] | ""));
  liveGameConfigId = String((const char*)(lg["config_id"] | ""));

  JsonObject timer = doc["timer"];
  timerRunning          = timer["running"] | false;
  timerPaused           = timer["paused"] | false;
  timerSecondsRemaining = timer["seconds_remaining"] | 0;
  timerVisibleOnDisplay = timer["visible_on_display"] | false;

  JsonObject stopwatch = doc["stopwatch"];
  stopwatchRunning   = stopwatch["running"] | false;
  stopwatchElapsedMs = stopwatch["elapsed_ms"] | 0;

  if (doc["trivia"].is<JsonObject>()) {
    triviaLive = true;
    JsonObject triv = doc["trivia"];
    triviaIndex    = triv["index"] | 0;
    triviaRevealed = triv["revealed"] | false;
    triviaCount    = triv["count"] | 0;
    triviaQuestion = String((const char*)(triv["question"] | ""));
    triviaAnswer   = String((const char*)(triv["answer"] | ""));
  } else {
    triviaLive = false;
  }
  Serial.printf("[fetchRemoteState] liveGameTypeId=%s triviaLive=%d qlen=%d alen=%d menuLevel=%d heap=%u\n",
                liveGameTypeId.c_str(), triviaLive, triviaQuestion.length(), triviaAnswer.length(),
                (int)menuLevel, (unsigned)ESP.getFreeHeap());

  if (doc["chairs"].is<JsonObject>()) {
    chairsLive    = true;
    chairsPlaying = doc["chairs"]["playing"] | false;
  } else {
    chairsLive = false;
  }

  // Timer/stopwatch values are only as fresh as the last poll (every 3s) --
  // stamping when THIS poll landed lets drawScreen() interpolate the live
  // value locally between polls instead of the number visibly jumping in
  // 3-second steps, without polling any more often than it already does.
  lastStateFetchMs = millis();

  return true;
}

bool fetchShowFlow() {
  bool ok = false;
  String body = httpGet("/api/remote/show_flow", &ok);
  if (!ok) return false;

  JsonDocument doc;
  if (deserializeJson(doc, body)) return false;

  flowStepCount = 0;
  for (JsonObject s : doc["steps"].as<JsonArray>()) {
    if (flowStepCount >= MAX_FLOW_STEPS) break;
    flowSteps[flowStepCount].index      = s["index"] | 0;
    flowSteps[flowStepCount].type       = String((const char*)(s["type"] | ""));
    flowSteps[flowStepCount].name       = String((const char*)(s["name"] | ""));
    flowSteps[flowStepCount].gameTypeId = String((const char*)(s["game_type_id"] | ""));
    flowStepCount++;
  }
  if (flowBrowseIndex < 0 && flowStepCount > 0) {
    flowBrowseIndex = (currentStep.index >= 0 && currentStep.index < flowStepCount) ? currentStep.index : 0;
  }
  return true;
}

bool fetchLibrary() {
  bool ok = false;
  String body = httpGet("/api/remote/library", &ok);
  if (!ok) return false;

  JsonDocument doc;
  if (deserializeJson(doc, body)) return false;

  sfxCount = 0;
  for (JsonObject s : doc["sfx"].as<JsonArray>()) {
    if (sfxCount >= MAX_SFX) break;
    sfxItems[sfxCount].id   = String((const char*)(s["id"]   | ""));
    sfxItems[sfxCount].name = String((const char*)(s["name"] | ""));
    sfxCount++;
  }
  circleCount = 0;
  for (JsonObject c : doc["circles"].as<JsonArray>()) {
    if (circleCount >= MAX_CIRCLES) break;
    circleItems[circleCount].id   = String((const char*)(c["id"]   | ""));
    circleItems[circleCount].name = String((const char*)(c["name"] | ""));
    circleCount++;
  }
  roleCount = 0;
  for (JsonObject r : doc["roles"].as<JsonArray>()) {
    if (roleCount >= MAX_ROLES) break;
    roleItems[roleCount].id   = String((const char*)(r["id"]   | ""));
    roleItems[roleCount].name = String((const char*)(r["name"] | ""));
    roleCount++;
  }
  Serial.printf("[fetchLibrary] sfx=%d circles=%d roles=%d\n", sfxCount, circleCount, roleCount);
  return true;
}

void updateMenuLevelForLiveGame() {
  if (liveGameTypeId != lastSeenLiveGameTypeId) {
    Serial.printf("[updateMenuLevelForLiveGame] %s -> %s (triviaLive=%d chairsLive=%d)\n",
                  lastSeenLiveGameTypeId.c_str(), liveGameTypeId.c_str(), triviaLive, chairsLive);
    lastSeenLiveGameTypeId = liveGameTypeId;
    bool enteredGame = false;
    if (liveGameTypeId == "trivia" && triviaLive) {
      menuLevel = LEVEL_GAME_TRIVIA;
      enteredGame = true;
    } else if (liveGameTypeId == "musical_chairs" && chairsLive) {
      menuLevel = LEVEL_GAME_CHAIRS;
      enteredGame = true;
    } else if (liveGameTypeId == "timed_competition") {
      // No per-game sub-object to gate on like trivia/chairs have -- the
      // stopwatch is a generic, always-present field in /api/remote/state,
      // so the live_game type alone is enough to know this game is up.
      menuLevel = LEVEL_GAME_TIMEDCOMP;
      enteredGame = true;
    } else if (liveGameTypeId == "countdown_game") {
      // Reuses the existing standalone Timer screen entirely -- it already
      // drives the same shared /api/timer/* state this game type loads its
      // duration into, so there's nothing game-specific to render.
      menuLevel = LEVEL_TIMER;
      enteredGame = true;
    }
    // if it became something else / empty, don't force-navigate the user away
    if (enteredGame) {
      // A game going live has to wake the screen itself -- handleStepChange()
      // does this for ordinary step changes, but this is a separate edge
      // trigger and drawScreen() bails out completely while asleep. Without
      // this, menuLevel flips to the game correctly but nothing ever
      // actually renders until some OTHER activity happens to wake it, and
      // the first button press just wakes the screen instead of firing --
      // exactly what "controls don't work or populate" looks like from the
      // MC's side if the screen had already timed out (12s) by the time the
      // game went live.
      if (!screenAwake) {
        screenAwake = true;
        M5.Axp.SetLDO2(true);
        lastActivity = millis();
      }
      beepConfirm();
    }
  }
}

// Wakes the screen for a meaningful change WITHIN an already-live game --
// question advanced/revealed, chairs started/stopped -- not just a brand new
// game going live. Without this, the underlying data still updates correctly
// on every poll (that part was never broken), but a dozed-off screen just
// stays dark showing nothing until some unrelated activity happens to wake
// it, which reads as "the remote didn't advance" even though it actually did.
void updateGameStateWake() {
  if (menuLevel == LEVEL_GAME_TRIVIA && triviaLive) {
    bool changed = haveSeenTriviaState &&
                   (triviaIndex != lastSeenTriviaIndex || triviaRevealed != lastSeenTriviaRevealed);
    lastSeenTriviaIndex    = triviaIndex;
    lastSeenTriviaRevealed = triviaRevealed;
    haveSeenTriviaState    = true;
    if (changed && !screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
      beepConfirm();
    }
  } else {
    haveSeenTriviaState = false;
  }

  if (menuLevel == LEVEL_GAME_CHAIRS && chairsLive) {
    bool changed = haveSeenChairsState && (chairsPlaying != lastSeenChairsPlaying);
    lastSeenChairsPlaying = chairsPlaying;
    haveSeenChairsState   = true;
    if (changed && !screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
      beepConfirm();
    }
  } else {
    haveSeenChairsState = false;
  }

  if (menuLevel == LEVEL_GAME_TIMEDCOMP) {
    bool changed = haveSeenStopwatchState && (stopwatchRunning != lastSeenStopwatchRunning);
    lastSeenStopwatchRunning = stopwatchRunning;
    haveSeenStopwatchState   = true;
    if (changed && !screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
      beepConfirm();
    }
  } else {
    haveSeenStopwatchState = false;
  }

  if (menuLevel == LEVEL_TIMER) {
    bool changed = haveSeenTimerState && (timerRunning != lastSeenTimerRunning);
    lastSeenTimerRunning = timerRunning;
    haveSeenTimerState   = true;
    if (changed && !screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
      beepConfirm();
    }
  } else {
    haveSeenTimerState = false;
  }
}

// Keeps the remote from silently going stale relative to Console/Admin/
// macros -- any of those can change the live show step out from under an MC
// who's holding this thing. Edge-triggered on currentStep.index so it fires
// once per real change, not every 3s poll.
//
// If the screen was ASLEEP, a step change wakes it and jumps straight to
// Show Flow with the new step highlighted -- glance-and-go. If the screen
// was already AWAKE (MC mid-browsing SFX/Circles/Roles), don't yank their
// navigation out from under a button they might be about to press -- the
// persistent "LIVE:" strip in drawScreen() still updates so they see it
// either way, just without losing their place. Game-mode steps are left to
// updateMenuLevelForLiveGame(), which already owns that jump.
void handleStepChange() {
  if (!haveSeenFirstStep) {
    haveSeenFirstStep = true;
    lastSeenStepIndex = currentStep.index;
    if (currentStep.index >= 0) showToast("LIVE: " + currentStep.name);
    return;
  }
  if (currentStep.index == lastSeenStepIndex) return;
  lastSeenStepIndex = currentStep.index;
  if (currentStep.index < 0) return;   // show reset / cleared

  bool wasAsleep = !screenAwake;
  if (wasAsleep) {
    screenAwake = true;
    M5.Axp.SetLDO2(true);
    lastActivity = millis();
  }

  if (currentStep.hasTimer) {
    // A skit/macro firing its own timer_start is exactly like a game going
    // live from the MC's perspective -- jump straight to the Timer screen
    // (same as Trivia/Chairs already do) instead of making them navigate
    // there by hand, regardless of what they were doing on the remote a
    // moment ago. This doesn't start/stop anything -- the macro's own
    // timer_start step (if it has one) still owns that, same as always.
    menuLevel = LEVEL_TIMER;
  } else if (menuLevel == LEVEL_TIMER) {
    // The timer-carrying step just ended (the show moved on to something
    // that isn't one) -- leave Timer mode automatically rather than
    // stranding the MC there for the rest of the show. Side-hold already
    // backs out manually at any time; this is the automatic version of
    // that same exit, triggered by the show's own progression.
    menuLevel = LEVEL_CATEGORY;
  } else if (currentStep.type != "game" && menuLevel != LEVEL_GAME_CHAIRS && menuLevel != LEVEL_GAME_TRIVIA
      && menuLevel != LEVEL_GAME_TIMEDCOMP) {
    if (wasAsleep) {
      menuLevel = LEVEL_SHOWFLOW;
      if (currentStep.index < flowStepCount) flowBrowseIndex = currentStep.index;
    }
  }
  showToast("LIVE: " + currentStep.name);
  beepConfirm();
}

// ── ACTIONS ──────────────────────────────────────────────────────────────
void moveSelection(int delta) {
  lastActivity = millis();
  switch (menuLevel) {
    case LEVEL_CATEGORY: categoryIndex = constrain(categoryIndex + delta, 0, CATEGORY_COUNT - 1); break;
    case LEVEL_SHOWFLOW: if (flowStepCount > 0)  flowBrowseIndex   = constrain(flowBrowseIndex   + delta, 0, flowStepCount - 1);  break;
    case LEVEL_SFX:      if (sfxCount > 0)       sfxBrowseIndex    = constrain(sfxBrowseIndex    + delta, 0, sfxCount - 1);       break;
    case LEVEL_CIRCLES:  if (circleCount > 0)    circleBrowseIndex = constrain(circleBrowseIndex + delta, 0, circleCount - 1);    break;
    case LEVEL_ROLES:    if (roleCount > 0)      roleBrowseIndex   = constrain(roleBrowseIndex   + delta, 0, roleCount - 1);      break;
    case LEVEL_TIMER: break;
    case LEVEL_GAME_TRIVIA: break;
    case LEVEL_GAME_CHAIRS: break;
    case LEVEL_GAME_TIMEDCOMP: break;
  }
}

void fireBrowsedStep() {
  if (flowBrowseIndex < 0 || flowBrowseIndex >= flowStepCount) return;
  bool ok = false;
  httpGet("/api/show/fire?index=" + String(flowBrowseIndex), &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("FIRED: " + flowSteps[flowBrowseIndex].name); fetchRemoteState(); updateMenuLevelForLiveGame(); }
  else    { beepFail(); showToast("FAILED TO FIRE"); }
}

void fireSfx() {
  if (sfxBrowseIndex < 0 || sfxBrowseIndex >= sfxCount) return;
  bool ok = false;
  httpGet("/api/sfx/play?name=" + sfxItems[sfxBrowseIndex].id, &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("SFX: " + sfxItems[sfxBrowseIndex].name); }
  else    { beepFail(); showToast("SFX FAILED"); }
}

void fireWalkupCircle() {
  if (circleBrowseIndex < 0 || circleBrowseIndex >= circleCount) return;
  bool ok = false;
  httpGet("/api/macro/walkup?circle=" + circleItems[circleBrowseIndex].id, &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("WALKUP: " + circleItems[circleBrowseIndex].name); }
  else    { beepFail(); showToast("WALKUP FAILED"); }
}

void fireWalkupRole() {
  if (roleBrowseIndex < 0 || roleBrowseIndex >= roleCount) return;
  bool ok = false;
  httpGet("/api/macro/walkup?role=" + roleItems[roleBrowseIndex].id, &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("WALKUP: " + roleItems[roleBrowseIndex].name); }
  else    { beepFail(); showToast("WALKUP FAILED"); }
}

void fireTriviaAction(const String& action) {
  JsonDocument doc;
  doc["config_id"] = liveGameConfigId;
  doc["action"] = action;
  String body; serializeJson(doc, body);
  bool ok = false;
  httpPostJson("/api/games/trivia/action", body, &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { beepFail(); showToast("TRIVIA ACTION FAILED"); }
}

void fireChairsToggle() {
  bool ok = false;
  if (chairsPlaying) {
    httpPostJson("/api/games/chairs/stop", "{}", &ok);
  } else {
    JsonDocument doc;
    doc["config_id"] = liveGameConfigId;
    String body; serializeJson(doc, body);
    httpPostJson("/api/games/chairs/start", body, &ok);
  }
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { beepFail(); showToast("CHAIRS ACTION FAILED"); }
}

// Toggle mirrors Console's own START button exactly -- /api/timer/toggle
// already does the "running? pause : start" branch server-side, so the
// remote doesn't need to duplicate that logic or care which state it's in.
void fireTimerToggle() {
  bool ok = false;
  httpGet("/api/timer/toggle", &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { beepFail(); showToast("TIMER ACTION FAILED"); }
}

// Toggles the countdown's visibility on HDMI -- reads the server's own
// timerVisibleOnDisplay (not a locally-tracked flag) so this stays correct
// even when auto-hide or the warning-at pop-back-up changed visibility on
// their own, without the remote ever being told directly.
void fireTimerToggleDisplay() {
  bool ok = false;
  const char* path = timerVisibleOnDisplay ? "/api/timer/hide" : "/api/timer/show";
  httpGet(path, &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast(timerVisibleOnDisplay ? "TIMER HIDDEN" : "TIMER SHOWN"); fetchRemoteState(); }
  else    { beepFail(); showToast("TIMER ACTION FAILED"); }
}

void fireTimerReset() {
  bool ok = false;
  httpGet("/api/timer/reset", &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("TIMER RESET"); fetchRemoteState(); }
  else    { beepFail(); showToast("TIMER ACTION FAILED"); }
}

// Timed Competition's stopwatch -- MC start/stops per runner from here;
// results (name + time) get recorded by MM at Console, not on the remote.
void fireStopwatchToggle() {
  bool ok = false;
  httpGet(stopwatchRunning ? "/api/stopwatch/stop" : "/api/stopwatch/start", &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { beepFail(); showToast("STOPWATCH ACTION FAILED"); }
}

void fireStopwatchReset() {
  bool ok = false;
  httpGet("/api/stopwatch/reset", &ok);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("STOPWATCH RESET"); fetchRemoteState(); }
  else    { beepFail(); showToast("STOPWATCH ACTION FAILED"); }
}

// ── INPUT ────────────────────────────────────────────────────────────────
// Short vs. long press, both resolved on release. wasReleasefor(ms) sets the
// Button object's internal hold-time threshold as a SIDE EFFECT (see
// M5StickCPlus's utility/Button.cpp) -- calling it before wasReleased()
// every tick makes wasReleased() mean "released after a SHORT hold" for
// that same tick, which is what the two calls below rely on.
void handleButtons() {
  bool frontLong  = M5.BtnA.wasReleasefor(LONG_PRESS_MS);
  bool frontShort = M5.BtnA.wasReleased();
  bool sideLong   = M5.BtnB.wasReleasefor(LONG_PRESS_MS);
  bool sideShort  = M5.BtnB.wasReleased();
  // Power-chip "top" button deliberately unused for navigation -- see
  // header comment. Reading it here would just reintroduce the flakiness.

  if (!frontShort && !frontLong && !sideShort && !sideLong) return;
  lastActivity = millis();

  if (!screenAwake) {
    screenAwake = true;
    M5.Axp.SetLDO2(true);
    return;   // first press only wakes the screen
  }

  if (connState == CONN_OFFLINE) {
    beepFail();
    showToast("OFFLINE");
    return;
  }

  // SIDE LONG = back, in every screen, always -- the one gesture that never
  // has to be relearned per context.
  if (sideLong) {
    if (menuLevel != LEVEL_CATEGORY) {
      menuLevel = LEVEL_CATEGORY;
      beepConfirm();
    }
    return;
  }

  switch (menuLevel) {
    case LEVEL_CATEGORY:
      if      (frontShort) moveSelection(+1);
      else if (sideShort)  moveSelection(-1);
      else if (frontLong) {
        if      (categoryIndex == 0) menuLevel = LEVEL_SHOWFLOW;
        else if (categoryIndex == 1) menuLevel = LEVEL_SFX;
        else if (categoryIndex == 2) menuLevel = LEVEL_CIRCLES;
        else if (categoryIndex == 3) menuLevel = LEVEL_ROLES;
        else                         menuLevel = LEVEL_TIMER;
        beepConfirm();
      }
      break;
    case LEVEL_SHOWFLOW:
      if      (frontShort) moveSelection(+1);
      else if (sideShort)  moveSelection(-1);
      else if (frontLong)  fireBrowsedStep();
      break;
    case LEVEL_SFX:
      if      (frontShort) moveSelection(+1);
      else if (sideShort)  moveSelection(-1);
      else if (frontLong)  fireSfx();
      break;
    case LEVEL_CIRCLES:
      if      (frontShort) moveSelection(+1);
      else if (sideShort)  moveSelection(-1);
      else if (frontLong)  fireWalkupCircle();
      break;
    case LEVEL_ROLES:
      if      (frontShort) moveSelection(+1);
      else if (sideShort)  moveSelection(-1);
      else if (frontLong)  fireWalkupRole();
      break;
    case LEVEL_TIMER:
      if      (frontShort) fireTimerToggle();
      else if (sideShort)  fireTimerToggleDisplay();
      else if (frontLong)  fireTimerReset();
      break;
    case LEVEL_GAME_CHAIRS:
      if (frontShort) fireChairsToggle();
      break;
    case LEVEL_GAME_TIMEDCOMP:
      // Reset gets a short press, not a hold -- unlike Timer's reset (a rare,
      // deliberate "start this skit over" action), the MC resets between
      // EVERY runner, so it needs to be the fast, no-friction gesture.
      if      (frontShort) fireStopwatchToggle();
      else if (sideShort)  fireStopwatchReset();
      break;
    case LEVEL_GAME_TRIVIA:
      // Correct/incorrect scoring lives at Console/the trivia controller now
      // (where the team scoreboard actually is) -- the remote is just a
      // page-turner: next/prev question, reveal on request.
      if      (frontShort) fireTriviaAction("next");
      else if (sideShort)  fireTriviaAction("prev");
      else if (frontLong)  fireTriviaAction("reveal");
      break;
  }
}

// ── FEEDBACK: buzzer, LED, screen sleep ────────────────────────────────
void beepConfirm() { M5.Beep.tone(2000, 60); }
void beepFail()    { M5.Beep.tone(300, 220); }

void showToast(const String& msg) {
  toastMsg = msg;
  toastUntil = millis() + 1600;
}

void updateLed() {
  unsigned long now = millis();
  switch (connState) {
    case CONN_OK:
      digitalWrite(LED_PIN, LOW);
      break;
    case CONN_RECONNECTING:
      if (now - lastBlink > 250) { lastBlink = now; ledOn = !ledOn; digitalWrite(LED_PIN, ledOn ? LOW : HIGH); }
      break;
    case CONN_OFFLINE:
      if (now - lastBlink > 500) { lastBlink = now; ledOn = !ledOn; digitalWrite(LED_PIN, ledOn ? LOW : HIGH); }
      break;
  }
}

void updateScreenSleep() {
  if (screenAwake && millis() - lastActivity > SCREEN_SLEEP_MS) {
    screenAwake = false;
    M5.Axp.SetLDO2(false);
    return;
  }
  if (!screenAwake) {
    float ax, ay, az;
    M5.IMU.getAccelData(&ax, &ay, &az);
    if (!haveWakeBaseline) { wakeBaseAx = ax; wakeBaseAy = ay; wakeBaseAz = az; haveWakeBaseline = true; }
    float delta = fabs(ax - wakeBaseAx) + fabs(ay - wakeBaseAy) + fabs(az - wakeBaseAz);
    if (delta > 0.25) {
      screenAwake = true;
      lastActivity = millis();
      M5.Axp.SetLDO2(true);
    }
    wakeBaseAx = ax; wakeBaseAy = ay; wakeBaseAz = az;
  }
}

// Fully cuts power (not just the screen) once the remote's been both
// unreachable AND untouched for AUTO_POWEROFF_AFTER_MS -- gated on activity
// too so someone actively troubleshooting with WiFi down doesn't get the
// remote yanked out from under them. Ground it back in by pressing either
// button (it's off, not asleep -- the same power button that turns it back on).
void checkAutoPowerOff() {
  if (connState != CONN_OFFLINE) return;
  unsigned long idleFor = millis() - lastActivity;
  if (idleFor < AUTO_POWEROFF_AFTER_MS) return;

  M5.Axp.SetLDO2(true);  // screen may already be asleep -- wake it so the message is actually seen
  screenBuf.fillSprite(BLACK);
  screenBuf.setTextColor(RED);
  screenBuf.setTextSize(2);
  screenBuf.setCursor(20, 40);
  screenBuf.print("NO CONNECTION");
  screenBuf.setTextSize(1);
  screenBuf.setTextColor(0xC618);
  screenBuf.setCursor(20, 70);
  screenBuf.print("Powering off to save battery.");
  screenBuf.setCursor(20, 84);
  screenBuf.print("Press power button to turn back on.");
  screenBuf.pushSprite(0, 0);
  beepFail();
  delay(2500);
  M5.Axp.PowerOff();
}

int getBatteryPct() {
  float v = M5.Axp.GetBatVoltage();
  float pct = (v - 3.0) / (4.2 - 3.0) * 100.0;
  return (int)constrain(pct, 0.0f, 100.0f);
}

// ── DISPLAY ──────────────────────────────────────────────────────────────
void drawBootLogo() {
  M5.Lcd.fillScreen(BLACK);
  M5.Lcd.pushImage((240 - BOOT_LOGO_WIDTH) / 2, (135 - BOOT_LOGO_HEIGHT) / 2,
                    BOOT_LOGO_WIDTH, BOOT_LOGO_HEIGHT, boot_logo);
}

// Returns the number of lines actually printed, so callers can position
// whatever comes next based on real content instead of a fixed guess -- a
// long question used to run a hard-coded 3-line budget and then just
// overlap whatever was drawn below it (the answer, then the button hints)
// once it needed more room than that.
int printWrapped(const String& text, int x, int y, int maxCharsPerLine, int maxLines) {
  int start = 0;
  int line = 0;
  int len = text.length();
  while (start < len && line < maxLines) {
    int end = start + maxCharsPerLine;
    if (end >= len) {
      end = len;
    } else {
      int lastSpace = -1;
      for (int i = start; i < end; i++) if (text[i] == ' ') lastSpace = i;
      if (lastSpace > start) end = lastSpace;
    }
    String chunk = text.substring(start, end);
    chunk.trim();
    if (line == maxLines - 1 && end < len) {
      while (chunk.length() > maxCharsPerLine - 2) chunk.remove(chunk.length() - 1);
      chunk += "..";
    }
    screenBuf.setCursor(x, y + line * 11);
    screenBuf.print(chunk);
    start = end + 1;
    line++;
  }
  return line;
}

String flowItemName(int i)   { return flowSteps[i].name; }
String sfxItemName(int i)    { return sfxItems[i].name; }
String circleItemName(int i) { return circleItems[i].name; }
String roleItemName(int i)   { return roleItems[i].name; }

void drawNamedList(String (*getName)(int), int count, int highlightIdx, const char* emptyMsg) {
  if (count == 0) {
    screenBuf.setTextColor(0x8410);
    screenBuf.setCursor(4, 40);
    screenBuf.print(emptyMsg);
    return;
  }
  const int windowSize = 6;
  int start = highlightIdx - windowSize / 2;
  if (start < 0) start = 0;
  if (start + windowSize > count) start = max(0, count - windowSize);
  int end = min(count, start + windowSize);

  int y = 16;
  for (int i = start; i < end; i++) {
    bool hl = (i == highlightIdx);
    String label = getName(i);
    if (label.length() > 27) label = label.substring(0, 26) + "..";
    if (hl) {
      screenBuf.fillRect(0, y - 1, 240, 13, 0x07E0);
      screenBuf.setTextColor(BLACK, 0x07E0);
    } else {
      screenBuf.setTextColor(0xC618, BLACK);
    }
    screenBuf.setCursor(3, y);
    screenBuf.print(label);
    y += 13;
  }
}

void drawScreen() {
  if (!screenAwake) return;

  static unsigned long lastDraw = 0;
  if (millis() - lastDraw < 120) return;
  lastDraw = millis();

  // fillScreen() isn't virtual in the base TFT_eSPI class, so calling it on a
  // TFT_eSprite resolves to the REAL-PANEL version at compile time instead of
  // clearing the sprite's own memory -- fillSprite() is the sprite-specific
  // one that actually wipes the buffer. Using fillScreen() here left stale
  // pixels (e.g. green list-highlight bars) sitting in the sprite across
  // frames, which pushSprite() then faithfully blitted back out.
  screenBuf.fillSprite(BLACK);
  screenBuf.setTextSize(1);

  if (connState != CONN_OK) {
    screenBuf.fillSprite(connState == CONN_OFFLINE ? RED : (uint16_t)0x8000);
    screenBuf.setTextColor(WHITE);
    screenBuf.setCursor(10, 55);
    screenBuf.setTextSize(2);
    screenBuf.print(connState == CONN_OFFLINE ? "OFFLINE" : "RECONNECTING");
    screenBuf.pushSprite(0, 0);
    return;
  }

  // Header
  screenBuf.setTextColor(0xFD20);   // amber
  screenBuf.setCursor(3, 2);
  switch (menuLevel) {
    case LEVEL_CATEGORY:    screenBuf.print("MUSICMAN REMOTE");        break;
    case LEVEL_SHOWFLOW:    screenBuf.print("SHOW FLOW");              break;
    case LEVEL_SFX:         screenBuf.print("SFX");                    break;
    case LEVEL_CIRCLES:     screenBuf.print("CIRCLES");                break;
    case LEVEL_ROLES:       screenBuf.print("ROLES");                  break;
    case LEVEL_TIMER:       screenBuf.print("SKIT TIMER");             break;
    case LEVEL_GAME_CHAIRS: screenBuf.print("MUSICAL CHAIRS - LIVE");  break;
    case LEVEL_GAME_TRIVIA: screenBuf.print("TRIVIA - LIVE");          break;
    case LEVEL_GAME_TIMEDCOMP: screenBuf.print("TIMED COMPETITION");   break;
  }
  screenBuf.setTextColor(0x8410);
  screenBuf.setCursor(200, 2);
  screenBuf.printf("%d%%", getBatteryPct());

  switch (menuLevel) {
    case LEVEL_CATEGORY: {
      for (int i = 0; i < CATEGORY_COUNT; i++) {
        bool hl = (i == categoryIndex);
        if (hl) { screenBuf.fillRect(0, 16 + i * 15, 240, 14, 0x07E0); screenBuf.setTextColor(BLACK, 0x07E0); }
        else    { screenBuf.setTextColor(WHITE, BLACK); }
        screenBuf.setCursor(4, 19 + i * 15);
        screenBuf.print(CATEGORY_NAMES[i]);
      }
      break;
    }
    case LEVEL_SHOWFLOW: drawNamedList(flowItemName, flowStepCount, flowBrowseIndex, "NO SHOW FLOW LOADED"); break;
    case LEVEL_SFX:      drawNamedList(sfxItemName, sfxCount, sfxBrowseIndex, "NO SFX LOADED");              break;
    case LEVEL_CIRCLES:  drawNamedList(circleItemName, circleCount, circleBrowseIndex, "NO CIRCLES LOADED"); break;
    case LEVEL_ROLES:    drawNamedList(roleItemName, roleCount, roleBrowseIndex, "NO ROLES LOADED");         break;

    case LEVEL_TIMER: {
      uint16_t statusColor = timerRunning ? 0x07E0 : (timerPaused ? 0xFD20 : 0xF800);
      const char* statusText = timerRunning ? "RUNNING" : (timerPaused ? "PAUSED" : "STOPPED");
      screenBuf.setTextColor(statusColor);
      screenBuf.setCursor(4, 20);
      screenBuf.print(statusText);

      int liveSecondsRemaining = timerSecondsRemaining;
      if (timerRunning) {
        long elapsedSincePoll = (millis() - lastStateFetchMs) / 1000;
        liveSecondsRemaining = max(0L, (long)timerSecondsRemaining - elapsedSincePoll);
      }
      int m = liveSecondsRemaining / 60, s = liveSecondsRemaining % 60;
      char buf[8];
      snprintf(buf, sizeof(buf), "%d:%02d", m, s);
      screenBuf.setTextSize(3);
      screenBuf.setTextColor(WHITE);
      int textW = strlen(buf) * 18;
      screenBuf.setCursor((240 - textW) / 2, 42);
      screenBuf.print(buf);
      screenBuf.setTextSize(1);

      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print(timerVisibleOnDisplay ? "FRONT=START/PAUSE  SIDE=HIDE" : "FRONT=START/PAUSE  SIDE=SHOW");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("HOLD FRONT=RESET HOLD SIDE=BACK");
      break;
    }

    case LEVEL_GAME_CHAIRS: {
      screenBuf.setTextColor(chairsPlaying ? 0x07E0 : 0xF800);
      screenBuf.setTextSize(2);
      screenBuf.setCursor(4, 30);
      screenBuf.print(chairsPlaying ? "PLAYING" : "STOPPED");
      screenBuf.setTextSize(1);
      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 60);
      screenBuf.print("FRONT = ");
      screenBuf.print(chairsPlaying ? "STOP" : "START");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("HOLD SIDE = BACK (game keeps going)");
      break;
    }
    case LEVEL_GAME_TIMEDCOMP: {
      screenBuf.setTextColor(stopwatchRunning ? 0x07E0 : 0xF800);
      screenBuf.setCursor(4, 20);
      screenBuf.print(stopwatchRunning ? "RUNNING" : "STOPPED");

      long totalMs = stopwatchElapsedMs;
      if (stopwatchRunning) totalMs += (millis() - lastStateFetchMs);
      long totalSec = totalMs / 1000;
      int m = totalSec / 60, s = totalSec % 60, t = (totalMs / 100) % 10;
      char buf[10];
      snprintf(buf, sizeof(buf), "%d:%02d.%d", m, s, t);
      screenBuf.setTextSize(3);
      screenBuf.setTextColor(WHITE);
      int textW = strlen(buf) * 18;
      screenBuf.setCursor((240 - textW) / 2, 42);
      screenBuf.print(buf);
      screenBuf.setTextSize(1);

      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print("FRONT = ");
      screenBuf.print(stopwatchRunning ? "STOP" : "START");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("SIDE = RESET   HOLD SIDE = BACK");
      break;
    }
    case LEVEL_GAME_TRIVIA: {
      // The answer is a host cheat-sheet -- shown on the remote the instant
      // the question is live, regardless of "revealed". "Revealed" only
      // controls what the AUDIENCE sees on the projector via the reveal
      // action below; the MC needs to already know the answer to judge
      // called-out responses before that moment, not after.
      static unsigned long lastTriviaDbg = 0;
      if (millis() - lastTriviaDbg > 2000) {
        lastTriviaDbg = millis();
        Serial.printf("[drawScreen/trivia] qlen=%d alen=%d idx=%d count=%d revealed=%d q=\"%s\"\n",
                      triviaQuestion.length(), triviaAnswer.length(), triviaIndex, triviaCount,
                      triviaRevealed, triviaQuestion.c_str());
      }
      screenBuf.setTextColor(WHITE);
      screenBuf.setCursor(4, 16);
      screenBuf.printf("Q %d/%d", triviaIndex + 1, triviaCount);
      // The answer's Y position (and how many lines it gets) floats based on
      // how much room the question actually used, instead of both having
      // fixed slots that a long question could run past and overlap.
      int qLines = printWrapped(triviaQuestion, 4, 27, 33, 5);
      int aY = 27 + qLines * 11 + 3;
      int aMaxLines = (106 - aY) / 11;
      if (aMaxLines < 1) aMaxLines = 1;
      if (aMaxLines > 3) aMaxLines = 3;

      screenBuf.setTextColor(0x07E0);
      screenBuf.setCursor(4, aY);
      screenBuf.print("A: ");
      screenBuf.setTextColor(WHITE);
      printWrapped(triviaAnswer, 22, aY, 30, aMaxLines);

      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print("FRONT = NEXT   SIDE = PREV");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print(triviaRevealed ? "HOLD SIDE = BACK" : "HOLD FRONT = REVEAL");
      break;
    }
  }

  // Bottom status bar -- one row, one thing at a time, in priority order.
  // Game modes (chairs/trivia) already use this row for their own control
  // hints, so this bar only applies to the category picker and the browse
  // lists -- exactly the screens where "what's actually live" can otherwise
  // silently drift from what's on screen (e.g. Console fires a step while
  // the MC is browsing SFX).
  bool gameMode = (menuLevel == LEVEL_GAME_CHAIRS || menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_TIMER
                    || menuLevel == LEVEL_GAME_TIMEDCOMP);
  if (!gameMode) {
    if (millis() < toastUntil) {
      screenBuf.fillRect(0, 118, 240, 17, 0x2965);
      screenBuf.setTextColor(WHITE);
      screenBuf.setCursor(4, 121);
      String t = toastMsg;
      if (t.length() > 36) t = t.substring(0, 35) + "..";
      screenBuf.print(t);
    } else if (timerRunning || stopwatchRunning) {
      screenBuf.fillRect(0, 118, 240, 17, 0x2104);
      screenBuf.setTextColor(0xFD20);
      screenBuf.setCursor(4, 121);
      if (timerRunning) {
        int m = timerSecondsRemaining / 60, s = timerSecondsRemaining % 60;
        screenBuf.printf("TIMER %d:%02d", m, s);
      } else {
        long totalSec = stopwatchElapsedMs / 1000;
        screenBuf.printf("STOPWATCH %ld:%02ld", totalSec / 60, totalSec % 60);
      }
    } else if (currentStep.index >= 0) {
      screenBuf.fillRect(0, 118, 240, 17, 0x18E3);
      screenBuf.setTextColor(0x8C11);
      screenBuf.setCursor(4, 121);
      String live = "LIVE: " + currentStep.name;
      if (live.length() > 36) live = live.substring(0, 35) + "..";
      screenBuf.print(live);
    }
  }

  screenBuf.pushSprite(0, 0);
}
