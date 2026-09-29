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
const unsigned long WIFI_RETRY_MS      = 8000;   // was 4000: WiFi.begin() every 4 s restarted an association/DHCP still in progress
const unsigned long HEARTBEAT_MS       = 5000;   // matches _REMOTE_HEARTBEAT_STALE_S=15 on the Pi
const unsigned long CONN_LOST_AFTER_MS = 12000;
const unsigned long SCREEN_SLEEP_MS    = 20000;  // browsing screens: backlight is the biggest power draw
// A game screen is picked up by kids/contestants and the MC mid-round -- a dark screen there
// meant a wasted wake-press (the first press only wakes) at exactly the wrong moment.
const unsigned long SCREEN_SLEEP_GAME_MS = 90000;
// The screen sleeping already kills the biggest draw, but the ESP32 + WiFi
// radio keep retrying every WIFI_RETRY_MS forever underneath -- real drain
// left running unattended for hours after a show ends and MusicMan itself
// is powered down. 10 minutes was too short for a campfire: a remote set down
// while the Pi rebooted (or carried out of AP range) was found dead. 30 minutes
// still saves the battery if it's genuinely forgotten, and the last minute is
// a visible countdown (any button cancels it), not a 2.5 s flash.
const unsigned long AUTO_POWEROFF_AFTER_MS = 30UL * 60 * 1000;
const unsigned long POWEROFF_WARN_MS       = 60UL * 1000;

#define MAX_FLOW_STEPS 150
#define MAX_SFX 150
#define MAX_CIRCLES 40
#define MAX_ROLES 40

// ── MENU MODEL ───────────────────────────────────────────────────────────
enum MenuLevel { LEVEL_CATEGORY, LEVEL_SHOWFLOW, LEVEL_SFX, LEVEL_CIRCLES, LEVEL_ROLES, LEVEL_TIMER,
                 LEVEL_GAME_CHAIRS, LEVEL_GAME_TRIVIA, LEVEL_GAME_TIMEDCOMP, LEVEL_GAME_CLOSEST, LEVEL_KARAOKE,
                 LEVEL_REMOTE_TEST, LEVEL_GAME_WHEEL, LEVEL_GAME_SHELLGAME };
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
  String gameTypeId;
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
long liveGameSeq         = -1;
long lastSeenLiveGameSeq = -1;        // catches a RElaunch of the same game_type_id, which the string above can't
// True only for the poll in which updateMenuLevelForLiveGame() pulled the
// remote onto a game's screen. A Show Flow game step changes BOTH the live
// game and the current step index in that same poll, so handleStepChange()
// (which runs right after) used to read the game's OWN launching step as
// "the show moved on" and immediately kicked Closest to the Mark's screen
// back to the menu -- the remote flipped and un-flipped before ever drawing.
bool gameEnteredThisPoll = false;
bool liveGameRevealed    = false;     // server: the live game is what's actually on the projector right now
bool haveSeenLiveGame    = false;     // false until the first poll after boot -- see updateMenuLevelForLiveGame()
unsigned long lastStateOkMs = 0;      // last time /api/remote/state parsed cleanly -- drives the STALE marker
int  lastHttpCode = 0;                // <=0 = no reply at all (timeout/refused): the action MAY have worked
bool listsTruncated = false;
MenuLevel levelBeforeTest = LEVEL_CATEGORY;   // "back to whatever it was on before" when test mode ends
bool powerOffWarning = false;
unsigned long lastLowBattWarn = 0;

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

bool wheelLive       = false;
int  wheelEntryCount = 0;

bool shellGameLive = false;

// Closest to the Mark: the remote's only job on this screen is the STOP press
// (see fireClosestStop()) -- Console owns starting each contestant's run and
// resetting between them. live_value/target/unit/decimals/title come
// pre-computed from /api/remote/state's own closest.live_value (server does
// the run/loop/bounce math, same reasoning as the stopwatch's elapsed_ms --
// this device just displays what it's told).
bool   closestLive        = false;
bool   closestRunning     = false;
int    closestCountdown   = 0;
float  closestLiveValue   = 0;
float  closestTargetValue = 0;
int    closestDecimals    = 0;
String closestUnitLabel   = "";
String closestTitle       = "";

// Remote self-test mode -- independent of which game (if any) is live, so it
// gets its own top-level field rather than living under game_live. Pulls
// focus from ANYWHERE the same way karaoke/timer do (see the edge-wake block
// below), and forces the screen back off LEVEL_REMOTE_TEST the instant
// Console ends it, even if no game is live to fall back to.
bool remoteTestActive       = false;
bool lastSeenRemoteTest     = false;
bool haveSeenRemoteTest     = false;

// Karaoke isn't a GAME_TYPES game (no liveGameTypeId of its own) -- it's its
// own state branch in /api/state, same shape as the timer/stopwatch below,
// so it gets the same "pulls focus from anywhere while live" treatment
// rather than the liveGameTypeId edge-detect chairs/trivia use.
bool   karaokeLive    = false;
bool   karaokePlaying = false;
bool   karaokeMuted   = false;
String karaokeTitle   = "";
String karaokeArtist  = "";
bool lastSeenKaraokeLiveGlobal = false;
bool haveSeenKaraokeLiveGlobal = false;
bool lastSeenKaraokePlaying = false;
bool lastSeenKaraokeMuted   = false;
bool haveSeenKaraokeState   = false;

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

// Separate from lastSeenTimerRunning/haveSeenTimerState above -- those reset
// every time menuLevel leaves LEVEL_TIMER (they only track wake-while-already-
// there), so they can't detect a timer starting while the MC is elsewhere,
// e.g. Show Flow. This pair persists across screens so a skit's timer step
// can pull the remote to the Timer screen no matter where it currently is.
bool lastSeenTimerRunningGlobal = false;
bool haveSeenTimerRunningGlobal = false;

// Same pattern, for the stopwatch -- Console's own plain Stopwatch widget
// (GAMES tab) drives the exact same /api/stopwatch/* state the Timed
// Competition screen already controls, so starting it from Console should
// pull the remote there too, not just leave it as a read-only footer number.
bool lastSeenStopwatchRunningGlobal = false;
bool haveSeenStopwatchRunningGlobal = false;

// Same two-pair pattern as timer/stopwatch above: the in-screen pair tracks
// a change while already on LEVEL_GAME_CLOSEST (updateGameStateWake), the
// global pair catches a NEW run starting from wherever the remote currently
// is (updateMenuLevelForLiveGame) -- needed because Console can start/reset
// many rounds in a row within the same launched game, not just the first.
bool lastSeenClosestRunning       = false;
bool haveSeenClosestState         = false;
bool lastSeenClosestRunningGlobal = false;
bool haveSeenClosestRunningGlobal = false;

enum ConnState { CONN_OK, CONN_RECONNECTING, CONN_OFFLINE };
ConnState connState = CONN_OFFLINE;

// Short vs. long press, both resolved on release -- see handleButtons().
const uint32_t LONG_PRESS_MS = 450;

// LED (single red, GPIO10, active LOW) -- driven via LEDC PWM instead of a
// plain digitalWrite so CONN_OK can breathe/heartbeat instead of sitting
// solid-on. Solid-on was both harder to glance-check at a distance (a lit
// LED and an unlit one look almost the same in bright daylight; a pulsing
// one doesn't) and pointless steady current for an indicator nobody's
// staring at continuously.
#define LED_PIN 10
unsigned long lastBlink = 0;
bool ledOn = false;

// One heartbeat cycle -- two quick pulses then a rest, like an ECG trace,
// not a plain sine breathe. Keyframes are (ms into cycle, brightness
// 0.0-1.0), linearly interpolated between consecutive points in
// heartbeatBrightness(). Deliberately dim overall: even the peaks don't
// reach full brightness, and the LED sits near-off the rest of the ~1.4s
// cycle -- average current is a small fraction of the old always-on level.
struct LedKeyframe { uint16_t t; float b; };
const LedKeyframe HEARTBEAT[] = {
  {0,    0.04f},
  {80,   0.85f},
  {160,  0.12f},
  {240,  0.55f},
  {360,  0.04f},
  {1400, 0.04f},
};
const int HEARTBEAT_LEN       = sizeof(HEARTBEAT) / sizeof(HEARTBEAT[0]);
const uint16_t HEARTBEAT_CYCLE_MS = HEARTBEAT[HEARTBEAT_LEN - 1].t;

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
void fireWheelSpin();
void fireShellGameStart();
void fireTimerToggle();
void fireTimerToggleDisplay();
void fireTimerReset();
void fireStopwatchToggle();
void fireStopwatchReset();
void fireClosestStop();
void fireKaraokeMuteToggle();
void fireRemoteTestSfx();
void updateMenuLevelForLiveGame();
void updateGameStateWake();
void handleStepChange();
void drawScreen();
void drawBootLogo();
void updateLed();
void setLedBrightness(float brightness);
float heartbeatBrightness(unsigned long now);
void updateScreenSleep();
void checkAutoPowerOff();
void beepConfirm();
void beepFail();
void showToast(const String& msg);
int  getBatteryPct();
String httpGet(const String& path, bool* ok, unsigned long timeoutMs = 2500);
String httpPostJson(const String& path, const String& jsonBody, bool* ok, unsigned long timeoutMs = 2500);
void failToast(const String& what);
String urlEncode(const String& in);
bool batteryCharging();
void checkBatteryWarning();
int printWrapped(const String& text, int x, int y, int maxCharsPerLine, int maxLines);

// ── SETUP ────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n\n=== MusicMan Remote v2 booting ===");

  M5.begin();
  // Default is 240MHz -- this device spends nearly all its time waiting on a
  // poll timer or a button press, never doing anything CPU-heavy (the
  // busiest thing it does is compose a 240x135 sprite and blit it over SPI,
  // already throttled to ~8fps in drawScreen()). 80MHz is the ESP32's floor
  // while WiFi still works at all (it needs >=80MHz), and cuts real active
  // power for all that idle/waiting time.
  setCpuFrequencyMhz(80);
  M5.IMU.Init();   // M5.begin() does NOT init the MPU6886 -- without this, getAccelData() returns dead values and tilt never fires
  M5.Lcd.setRotation(3);
  M5.Lcd.fillScreen(BLACK);
  // AXP192 boots the backlight to its own power-on-reset level (register
  // default, not set through this API -- works out to roughly 70% on this
  // 0-100 scale). Backlight is the single biggest power draw on this device
  // (see SCREEN_SLEEP_MS below) -- SCREEN_SLEEP_MS already cuts it off
  // after 12s idle, but it was always full-ish brightness for however long
  // the screen stays awake before that. Dimming the *intensity* while awake
  // is a second, independent lever on top of that -- 60 is a conservative
  // pick, still clearly readable; tune this one number if it's off in
  // practice (dimmer = lower, brighter = higher, 0-100).
  M5.Axp.ScreenBreath(60);
  ledcAttach(LED_PIN, 5000, 8);  // 5kHz, 8-bit duty -- current ESP32 core's pin-based LEDC API (no channel handle needed)
  setLedBrightness(0.0f);  // off until connected

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
  checkBatteryWarning();
  updateLed();
  drawScreen();

  static bool truncToasted = false;
  if (listsTruncated && !truncToasted) { truncToasted = true; showToast("LIST TOO LONG - SOME HIDDEN"); }
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
// Connect timeout is set explicitly: the core default (5 s) plus a 2.5 s read timeout meant a
// hung Pi froze loop() -- and dropped every button press -- for up to ~7.5 s per call, several
// calls in a row.  Actions get a longer READ timeout than polls: a server that is just busy
// (audio lock, cold SFX load) still finishes the action, and a too-short timeout produced a
// false "FAILED" for something that actually ran, so the MC pressed again and double-fired it.
String httpGet(const String& path, bool* ok, unsigned long timeoutMs) {
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":" + PI_PORT + path;
  http.begin(url);
  http.setConnectTimeout(1500);
  http.setTimeout(timeoutMs);
  int code = http.GET();
  lastHttpCode = code;
  String body;
  if (code == 200) { body = http.getString(); *ok = true; } else { *ok = false; }
  http.end();
  return body;
}

String httpPostJson(const String& path, const String& jsonBody, bool* ok, unsigned long timeoutMs) {
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":" + PI_PORT + path;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setConnectTimeout(1500);
  http.setTimeout(timeoutMs);
  int code = http.POST(jsonBody);
  lastHttpCode = code;
  String body;
  if (code == 200) { body = http.getString(); *ok = true; } else { *ok = false; }
  http.end();
  return body;
}

// A failed ACTION with no reply at all (timeout / connection dropped) may well have run on the
// Pi -- saying plain "FAILED" invites a second press that double-fires it.  A real HTTP error
// (the Pi answered "no") is a genuine failure.
void failToast(const String& what) {
  beepFail();
  if (lastHttpCode <= 0) showToast(what + ": NO REPLY - CHECK SCREEN");
  else                   showToast(what + " FAILED");
}

String urlEncode(const String& in) {
  String out;
  const char* hex = "0123456789ABCDEF";
  for (unsigned i = 0; i < in.length(); i++) {
    unsigned char c = (unsigned char)in[i];
    if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '-' || c == '_' || c == '.' || c == '~') out += (char)c;
    else { out += '%'; out += hex[c >> 4]; out += hex[c & 15]; }
  }
  return out;
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
  currentStep.gameTypeId = String((const char*)(step["game_type_id"] | ""));
  currentStep.hasTimer = step["has_timer"] | false;

  JsonObject lg = doc["live_game"];
  liveGameTypeId   = String((const char*)(lg["game_type_id"] | ""));
  liveGameConfigId = String((const char*)(lg["config_id"] | ""));
  liveGameSeq      = lg["seq"] | -1;
  liveGameRevealed = lg["revealed"] | false;

  JsonObject timer = doc["timer"];
  timerRunning          = timer["running"] | false;
  timerPaused           = timer["paused"] | false;
  timerSecondsRemaining = timer["seconds_remaining"] | 0;
  timerVisibleOnDisplay = timer["visible_on_display"] | false;

  JsonObject stopwatch = doc["stopwatch"];
  stopwatchRunning   = stopwatch["running"] | false;
  stopwatchElapsedMs = stopwatch["elapsed_ms"] | 0;

  JsonObject karaoke = doc["karaoke"];
  karaokeLive    = karaoke["live"] | false;
  karaokePlaying = karaoke["playing"] | false;
  karaokeMuted   = karaoke["vocal_muted"] | false;
  karaokeTitle   = String((const char*)(karaoke["title"] | ""));
  karaokeArtist  = String((const char*)(karaoke["artist"] | ""));

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

  if (doc["wheel"].is<JsonObject>()) {
    wheelLive       = true;
    wheelEntryCount = doc["wheel"]["entry_count"] | 0;
  } else {
    wheelLive = false;
  }
  // Shell Game has no per-game_live sub-object (nothing dynamic worth
  // polling -- it's just "ready" or not) -- liveGameTypeId alone is enough,
  // same reasoning timed_competition's own liveGameTypeId-only check uses.
  shellGameLive = (liveGameTypeId == "shell_game");
  Serial.printf("[fetchRemoteState] liveGameTypeId=%s triviaLive=%d qlen=%d alen=%d menuLevel=%d heap=%u\n",
                liveGameTypeId.c_str(), triviaLive, triviaQuestion.length(), triviaAnswer.length(),
                (int)menuLevel, (unsigned)ESP.getFreeHeap());

  if (doc["chairs"].is<JsonObject>()) {
    chairsLive    = true;
    chairsPlaying = doc["chairs"]["playing"] | false;
  } else {
    chairsLive = false;
  }

  if (doc["closest"].is<JsonObject>()) {
    closestLive = true;
    JsonObject cl = doc["closest"];
    closestRunning     = cl["running"] | false;
    closestCountdown   = cl["countdown"] | 0;
    closestLiveValue   = cl["live_value"]   | 0.0f;
    closestTargetValue = cl["target_value"] | 0.0f;
    closestDecimals    = cl["decimals"] | 0;
    closestUnitLabel   = String((const char*)(cl["unit_label"] | ""));
    closestTitle       = String((const char*)(cl["title"] | ""));
  } else {
    closestLive = false;
  }

  remoteTestActive = doc["remote_test"] | false;

  // Timer/stopwatch values are only as fresh as the last poll (every 3s) --
  // stamping when THIS poll landed lets drawScreen() interpolate the live
  // value locally between polls instead of the number visibly jumping in
  // 3-second steps, without polling any more often than it already does.
  lastStateFetchMs = millis();
  lastStateOkMs    = lastStateFetchMs;

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
    if (flowStepCount >= MAX_FLOW_STEPS) { listsTruncated = true; break; }
    flowSteps[flowStepCount].index      = s["index"] | 0;
    flowSteps[flowStepCount].type       = String((const char*)(s["type"] | ""));
    flowSteps[flowStepCount].name       = String((const char*)(s["name"] | ""));
    flowSteps[flowStepCount].gameTypeId = String((const char*)(s["game_type_id"] | ""));
    flowStepCount++;
  }
  if (flowBrowseIndex < 0 && flowStepCount > 0) {
    flowBrowseIndex = (currentStep.index >= 0 && currentStep.index < flowStepCount) ? currentStep.index : 0;
  }
  // Steps can be removed in Admin while this device is showing the list.
  if (flowStepCount > 0 && flowBrowseIndex >= flowStepCount) flowBrowseIndex = flowStepCount - 1;
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
    if (sfxCount >= MAX_SFX) { listsTruncated = true; break; }
    sfxItems[sfxCount].id   = String((const char*)(s["id"]   | ""));
    sfxItems[sfxCount].name = String((const char*)(s["name"] | ""));
    sfxCount++;
  }
  circleCount = 0;
  for (JsonObject c : doc["circles"].as<JsonArray>()) {
    if (circleCount >= MAX_CIRCLES) { listsTruncated = true; break; }
    circleItems[circleCount].id   = String((const char*)(c["id"]   | ""));
    circleItems[circleCount].name = String((const char*)(c["name"] | ""));
    circleCount++;
  }
  roleCount = 0;
  for (JsonObject r : doc["roles"].as<JsonArray>()) {
    if (roleCount >= MAX_ROLES) { listsTruncated = true; break; }
    roleItems[roleCount].id   = String((const char*)(r["id"]   | ""));
    roleItems[roleCount].name = String((const char*)(r["name"] | ""));
    roleCount++;
  }
  Serial.printf("[fetchLibrary] sfx=%d circles=%d roles=%d\n", sfxCount, circleCount, roleCount);
  if (sfxBrowseIndex    >= sfxCount    && sfxCount    > 0) sfxBrowseIndex    = sfxCount - 1;
  if (circleBrowseIndex >= circleCount && circleCount > 0) circleBrowseIndex = circleCount - 1;
  if (roleBrowseIndex   >= roleCount   && roleCount   > 0) roleBrowseIndex   = roleCount - 1;
  return true;
}

void updateMenuLevelForLiveGame() {
  gameEnteredThisPoll = false;
  // seq (bumped server-side on every single launch) catches a RElaunch of the
  // same game_type_id -- e.g. Console re-firing the same Show Flow game step
  // for a second round -- which the type string alone can't, since it never
  // changed. Without this, only the FIRST launch of a given type in a session
  // pulled the remote's screen over; every relaunch after that silently no-op'd.
  // First poll after boot: a game the server still calls "live" may have ended (or been
  // covered by other content) hours ago -- the server never clears it.  Only jump into it if
  // it is genuinely what's on the projector right now (revealed); otherwise just adopt the
  // current state silently so a reboot doesn't land the MC on a dead game screen where the
  // first press would fire a spin/start.
  bool bootAdopt = false;
  if (!haveSeenLiveGame) {
    haveSeenLiveGame = true;
    // "Actually in use" = on the projector (revealed) OR visibly mid-round (Chairs playing, stopwatch/meter running).
    if (liveGameTypeId != "" && !liveGameRevealed && !chairsPlaying && !stopwatchRunning && !closestRunning) {
      lastSeenLiveGameTypeId = liveGameTypeId;
      lastSeenLiveGameSeq    = liveGameSeq;
      bootAdopt = true;
    }
  }
  if (!bootAdopt && (liveGameTypeId != lastSeenLiveGameTypeId || liveGameSeq != lastSeenLiveGameSeq)) {
    Serial.printf("[updateMenuLevelForLiveGame] %s -> %s (seq %ld -> %ld, triviaLive=%d chairsLive=%d)\n",
                  lastSeenLiveGameTypeId.c_str(), liveGameTypeId.c_str(), lastSeenLiveGameSeq, liveGameSeq, triviaLive, chairsLive);
    lastSeenLiveGameTypeId = liveGameTypeId;
    lastSeenLiveGameSeq    = liveGameSeq;
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
    } else if (liveGameTypeId == "closest_to_mark") {
      menuLevel = LEVEL_GAME_CLOSEST;
      enteredGame = true;
    } else if (liveGameTypeId == "wheel" && wheelLive) {
      menuLevel = LEVEL_GAME_WHEEL;
      enteredGame = true;
    } else if (liveGameTypeId == "shell_game") {
      menuLevel = LEVEL_GAME_SHELLGAME;
      enteredGame = true;
    }
    // The server no longer has ANY live game (it restarted, or a fresh state): a game screen
    // left up would strand the MC on stale data ("Q 1/0", a Chairs button that errors) until a
    // manual side-hold.  Hand control back to the menu.
    if (!enteredGame && liveGameTypeId == "" && menuLevel != LEVEL_REMOTE_TEST
        && (menuLevel == LEVEL_GAME_CHAIRS || menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_GAME_TIMEDCOMP
            || menuLevel == LEVEL_GAME_CLOSEST || menuLevel == LEVEL_GAME_WHEEL || menuLevel == LEVEL_GAME_SHELLGAME)) {
      menuLevel = LEVEL_CATEGORY;
    }
    // if it became something else, don't force-navigate the user away
    if (enteredGame) {
      gameEnteredThisPoll = true;
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

  // A timer starting should pull the MC to the Timer screen even when it
  // wasn't launched as its own "live game" -- e.g. a skit's show-flow step
  // just starting the shared timer, with liveGameTypeId never changing at
  // all. Edge-triggered on running flipping false->true so it only fires
  // once per start, and skipped while genuinely in a different live game so
  // it doesn't steal focus from Trivia/Chairs/Timed Competition mid-play.
  bool inOtherLiveGame = (menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_GAME_CHAIRS
                           || menuLevel == LEVEL_GAME_TIMEDCOMP || menuLevel == LEVEL_GAME_CLOSEST
                           || menuLevel == LEVEL_GAME_WHEEL || menuLevel == LEVEL_GAME_SHELLGAME
                           || menuLevel == LEVEL_REMOTE_TEST);
  if (timerRunning && !lastSeenTimerRunningGlobal && haveSeenTimerRunningGlobal
      && !inOtherLiveGame && menuLevel != LEVEL_TIMER) {
    menuLevel = LEVEL_TIMER;
    if (!screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
    }
    beepConfirm();
  }
  lastSeenTimerRunningGlobal = timerRunning;
  haveSeenTimerRunningGlobal = true;

  // Same treatment for the stopwatch, consistent with the timer above --
  // Console's plain Stopwatch widget shares state with the Timed Competition
  // screen (both hit /api/stopwatch/*), so starting it from Console pulls
  // the remote to the same interactive controls a Timed Competition game
  // would, rather than leaving it on whatever screen it was already showing
  // with only the read-only footer number.
  bool inOtherLiveGame2 = (menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_GAME_CHAIRS
                            || menuLevel == LEVEL_TIMER || menuLevel == LEVEL_GAME_CLOSEST
                            || menuLevel == LEVEL_GAME_WHEEL || menuLevel == LEVEL_GAME_SHELLGAME
                            || menuLevel == LEVEL_REMOTE_TEST);
  if (stopwatchRunning && !lastSeenStopwatchRunningGlobal && haveSeenStopwatchRunningGlobal
      && !inOtherLiveGame2 && menuLevel != LEVEL_GAME_TIMEDCOMP) {
    menuLevel = LEVEL_GAME_TIMEDCOMP;
    if (!screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
    }
    beepConfirm();
  }
  lastSeenStopwatchRunningGlobal = stopwatchRunning;
  haveSeenStopwatchRunningGlobal = true;

  // Same treatment for Closest to the Mark -- Console starts/resets every
  // contestant's run itself (the remote's only job on this screen is the
  // STOP press), so this is what pulls focus back to the screen for each
  // new round within the same launched game -- "entered game" above only
  // fires once, the moment the game TYPE first goes live.
  bool inOtherLiveGameClosest = (menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_GAME_CHAIRS
                                  || menuLevel == LEVEL_TIMER || menuLevel == LEVEL_GAME_TIMEDCOMP
                                  || menuLevel == LEVEL_GAME_WHEEL || menuLevel == LEVEL_GAME_SHELLGAME
                                  || menuLevel == LEVEL_REMOTE_TEST);
  if (closestRunning && !lastSeenClosestRunningGlobal && haveSeenClosestRunningGlobal
      && !inOtherLiveGameClosest && menuLevel != LEVEL_GAME_CLOSEST) {
    menuLevel = LEVEL_GAME_CLOSEST;
    if (!screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
    }
    beepConfirm();
  }
  lastSeenClosestRunningGlobal = closestRunning;
  haveSeenClosestRunningGlobal = true;

  // Karaoke going live pulls focus the same way, and likewise backs off if
  // the MC is genuinely mid-game elsewhere (Trivia/Chairs/Timer/Timed Comp) --
  // whichever of these got there first keeps the screen until its own
  // side-long backs out, rather than the two fighting over it.
  //
  // Also pulls on the device's very FIRST poll if karaoke is *already* live
  // at that point (haveSeenKaraokeLiveGlobal still false) -- e.g. a reboot/
  // reflash while a song is already playing. The plain edge-trigger below
  // only catches false->true transitions and would otherwise never fire for
  // a state that was already true the first time this device ever looked.
  bool inOtherLiveGame3 = (menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_GAME_CHAIRS
                            || menuLevel == LEVEL_TIMER || menuLevel == LEVEL_GAME_TIMEDCOMP
                            || menuLevel == LEVEL_GAME_CLOSEST || menuLevel == LEVEL_GAME_WHEEL
                            || menuLevel == LEVEL_GAME_SHELLGAME || menuLevel == LEVEL_REMOTE_TEST);
  bool karaokeJustWentLive     = karaokeLive && !lastSeenKaraokeLiveGlobal && haveSeenKaraokeLiveGlobal;
  bool karaokeAlreadyLiveAtBoot = karaokeLive && !haveSeenKaraokeLiveGlobal;
  if ((karaokeJustWentLive || karaokeAlreadyLiveAtBoot)
      && !inOtherLiveGame3 && menuLevel != LEVEL_KARAOKE) {
    menuLevel = LEVEL_KARAOKE;
    if (!screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
    }
    beepConfirm();
  }
  lastSeenKaraokeLiveGlobal = karaokeLive;
  haveSeenKaraokeLiveGlobal = true;

  // Remote test mode pulls focus from ANYWHERE, unconditionally -- Console
  // triggered it as a deliberate, explicit action, so unlike karaoke/timed
  // comp/closest above (which back off from each other), it should win over
  // whatever game screen happens to already be up.
  if (remoteTestActive && !lastSeenRemoteTest && haveSeenRemoteTest && menuLevel != LEVEL_REMOTE_TEST) {
    levelBeforeTest = menuLevel;   // remembered so ending test mode really goes "back to whatever it was on before"
    menuLevel = LEVEL_REMOTE_TEST;
    if (!screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
    }
    beepConfirm();
  } else if (!remoteTestActive && lastSeenRemoteTest && menuLevel == LEVEL_REMOTE_TEST) {
    // Console ended test mode -- restore the screen the MC was on before it started
    // (the old code went home and then re-ran the game-live edge check, which could
    // resurrect a stale game instead).
    menuLevel = levelBeforeTest;
    if (menuLevel == LEVEL_REMOTE_TEST) menuLevel = LEVEL_CATEGORY;
  }
  lastSeenRemoteTest = remoteTestActive;
  haveSeenRemoteTest = true;
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

  if (menuLevel == LEVEL_GAME_CLOSEST && closestLive) {
    bool changed = haveSeenClosestState && (closestRunning != lastSeenClosestRunning);
    lastSeenClosestRunning = closestRunning;
    haveSeenClosestState   = true;
    if (changed && !screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
      beepConfirm();
    }
  } else {
    haveSeenClosestState = false;
  }

  if (menuLevel == LEVEL_KARAOKE && karaokeLive) {
    bool changed = haveSeenKaraokeState &&
                   (karaokePlaying != lastSeenKaraokePlaying || karaokeMuted != lastSeenKaraokeMuted);
    lastSeenKaraokePlaying = karaokePlaying;
    lastSeenKaraokeMuted   = karaokeMuted;
    haveSeenKaraokeState   = true;
    if (changed && !screenAwake) {
      screenAwake = true;
      M5.Axp.SetLDO2(true);
      lastActivity = millis();
      beepConfirm();
    }
  } else {
    haveSeenKaraokeState = false;
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

  // The step that just changed IS the live game's own launching step -- true in the poll that
  // pulled the remote onto the game (gameEnteredThisPoll), but ALSO whenever the step index and
  // the game land in different polls (a remote-fired step, or a poll falling between the server
  // launching the game and recording the step).  Either way this is not "the show moved on".
  bool stepIsLiveGame = (currentStep.type == "game" && currentStep.gameTypeId.length() > 0
                          && currentStep.gameTypeId == liveGameTypeId);
  if (gameEnteredThisPoll || stepIsLiveGame) {
    // updateMenuLevelForLiveGame() already owns this jump, nothing here should undo it.
  } else if (menuLevel == LEVEL_REMOTE_TEST) {
    // Test mode is an explicit Console action -- a step firing must not yank the MC off it.
  } else if (currentStep.hasTimer) {
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
  } else if (menuLevel == LEVEL_GAME_CLOSEST) {
    // Unlike Chairs/Trivia/Timed Competition just below (a multi-round
    // session or an ongoing scoreboard, deliberately left up across an
    // advancing step) a Closest to the Mark round is done the moment the
    // show moves on to anything else -- the next circle's walkup, a macro,
    // whatever's next. Chris's own call: this screen always hands control
    // straight back to normal show-flow browsing here, unconditionally,
    // same as Timer above -- no manual side-hold needed for the common case.
    menuLevel = LEVEL_CATEGORY;
  } else if (currentStep.type != "game" && menuLevel != LEVEL_GAME_CHAIRS && menuLevel != LEVEL_GAME_TRIVIA
      && menuLevel != LEVEL_GAME_TIMEDCOMP && menuLevel != LEVEL_REMOTE_TEST
      && menuLevel != LEVEL_GAME_WHEEL && menuLevel != LEVEL_GAME_SHELLGAME) {
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
    case LEVEL_GAME_CLOSEST: break;
    case LEVEL_KARAOKE: break;
    case LEVEL_REMOTE_TEST: break;
    case LEVEL_GAME_WHEEL: break;
    case LEVEL_GAME_SHELLGAME: break;
  }
}

void fireBrowsedStep() {
  if (flowBrowseIndex < 0 || flowBrowseIndex >= flowStepCount) return;
  bool ok = false;
  // ?expect= = the name being shown: the browsed list can be up to FLOW_POLL_MS old, and if the
  // flow was reordered in Admin since, index N is a different step.  The Pi refuses (409)
  // rather than firing the wrong thing.
  String name = flowSteps[flowBrowseIndex].name;
  httpGet("/api/show/fire?index=" + String(flowBrowseIndex) + "&expect=" + urlEncode(name), &ok, 4000);
  lastActivity = millis();
  if (ok) {
    beepConfirm();
    fetchRemoteState();
    updateMenuLevelForLiveGame();
    // Run the step-change logic NOW, in the same breath as the game-entry check.  Otherwise the
    // step index change is only noticed on the next poll -- by which time gameEnteredThisPoll
    // is long false and a Closest screen just entered was kicked straight back to the menu.
    handleStepChange();
    showToast("FIRED: " + name);
  } else if (lastHttpCode == 409) {
    beepFail();
    showToast("SHOW CHANGED - LIST REFRESHED");
    fetchShowFlow();
  } else {
    failToast("FIRE");
  }
}

void fireSfx() {
  if (sfxBrowseIndex < 0 || sfxBrowseIndex >= sfxCount) return;
  bool ok = false;
  httpGet("/api/sfx/play?name=" + sfxItems[sfxBrowseIndex].id, &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("SFX: " + sfxItems[sfxBrowseIndex].name); }
  else    { failToast("SFX"); }
}

void fireWalkupCircle() {
  if (circleBrowseIndex < 0 || circleBrowseIndex >= circleCount) return;
  bool ok = false;
  httpGet("/api/macro/walkup?circle=" + circleItems[circleBrowseIndex].id, &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("WALKUP: " + circleItems[circleBrowseIndex].name); }
  else    { failToast("WALKUP"); }
}

void fireWalkupRole() {
  if (roleBrowseIndex < 0 || roleBrowseIndex >= roleCount) return;
  bool ok = false;
  httpGet("/api/macro/walkup?role=" + roleItems[roleBrowseIndex].id, &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("WALKUP: " + roleItems[roleBrowseIndex].name); }
  else    { failToast("WALKUP"); }
}

void fireTriviaAction(const String& action) {
  JsonDocument doc;
  doc["config_id"] = liveGameConfigId;
  doc["action"] = action;
  String body; serializeJson(doc, body);
  bool ok = false;
  httpPostJson("/api/games/trivia/action", body, &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { failToast("TRIVIA"); }
}

void fireWheelSpin() {
  bool ok = false;
  String body = httpPostJson("/api/games/wheel/remote_spin", "{}", &ok, 4000);
  lastActivity = millis();
  if (ok && body.indexOf("spinning") >= 0) { showToast("STILL SPINNING..."); }   // server ignored a second press mid-spin
  else if (ok) { beepConfirm(); fetchRemoteState(); }
  else if (lastHttpCode == 400) { beepFail(); showToast("WHEEL: NEEDS 2+ ENTRIES"); }
  else    { failToast("SPIN"); }
}

void fireShellGameStart() {
  bool ok = false;
  httpPostJson("/api/shell-game/start", "{}", &ok, 4000);
  lastActivity = millis();
  if (ok) beepConfirm();
  else    { failToast("SHELL GAME"); }
}

void fireChairsToggle() {
  bool ok = false;
  if (chairsPlaying) {
    httpPostJson("/api/games/chairs/stop", "{}", &ok, 4000);
  } else {
    JsonDocument doc;
    doc["config_id"] = liveGameConfigId;
    String body; serializeJson(doc, body);
    httpPostJson("/api/games/chairs/start", body, &ok, 4000);
  }
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else if (lastHttpCode == 400) { beepFail(); showToast("CHAIRS: NO SONG SET"); }
  else    { failToast("CHAIRS"); }
}

// Toggle mirrors Console's own START button exactly -- /api/timer/toggle
// already does the "running? pause : start" branch server-side, so the
// remote doesn't need to duplicate that logic or care which state it's in.
void fireTimerToggle() {
  bool ok = false;
  httpGet("/api/timer/toggle", &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { failToast("TIMER"); }
}

// Toggles the countdown's visibility on HDMI -- reads the server's own
// timerVisibleOnDisplay (not a locally-tracked flag) so this stays correct
// even when auto-hide or the warning-at pop-back-up changed visibility on
// their own, without the remote ever being told directly.
void fireTimerToggleDisplay() {
  bool ok = false;
  const char* path = timerVisibleOnDisplay ? "/api/timer/hide" : "/api/timer/show";
  httpGet(path, &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast(timerVisibleOnDisplay ? "TIMER HIDDEN" : "TIMER SHOWN"); fetchRemoteState(); }
  else    { failToast("TIMER"); }
}

void fireTimerReset() {
  bool ok = false;
  httpGet("/api/timer/reset", &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("TIMER RESET"); fetchRemoteState(); }
  else    { failToast("TIMER"); }
}

// Timed Competition's stopwatch -- MC start/stops per runner from here;
// results (name + time) get recorded by MM at Console, not on the remote.
void fireStopwatchToggle() {
  bool ok = false;
  // display=0: Timed Competition's stopwatch is NOT put on the projector by starting it (Console's
  // START passes the same) -- the server default is display=1, which used to flash the stopwatch
  // onto the HDMI screen every time the MC pressed front.  Showing it stays an explicit
  // SHOW button on Console.
  httpGet(stopwatchRunning ? "/api/stopwatch/stop" : "/api/stopwatch/start?display=0", &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { failToast("STOPWATCH"); }
}

void fireStopwatchReset() {
  bool ok = false;
  httpGet("/api/stopwatch/reset", &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); showToast("STOPWATCH RESET"); fetchRemoteState(); }
  else    { failToast("STOPWATCH"); }
}

void fireClosestStop() {
  bool ok = false;
  // src=remote: the Pi ignores this press while the 3-2-1 is still counting down -- a kid
  // mashing the button early used to cancel the whole round (only Console's own STOP may).
  httpPostJson("/api/games/closest/stop?src=remote", "{}", &ok, 4000);
  lastActivity = millis();
  // Fires unconditionally even if the round already ended a moment ago --
  // the server-side route itself is a safe no-op when nothing's running (see
  // api_closest_stop() in musicman.py), so there's no local running-state
  // check to get out of sync with reality.
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { failToast("STOP"); }
}

void fireKaraokeMuteToggle() {
  bool ok = false;
  httpPostJson("/api/karaoke/vocal_mute_toggle", "{}", &ok, 4000);
  lastActivity = millis();
  if (ok) { beepConfirm(); fetchRemoteState(); }
  else    { failToast("KARAOKE"); }
}

void fireRemoteTestSfx() {
  bool ok = false;
  httpPostJson("/api/remote/test/fire", "{}", &ok);
  lastActivity = millis();
  if (ok) beepConfirm();
  else    { beepFail(); showToast("TEST FIRE FAILED"); }
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

  // Tick the instant a hold crosses the long-press threshold, so the MC can FEEL where "short"
  // ends and "long" (fire / back) begins -- without a cue, a hesitant tap that ran past 450 ms
  // fired a step, and a slightly short intended fire just scrolled.  pressedFor() has no side
  // effect on the release logic above.
  static bool cueA = false, cueB = false;
  if (M5.BtnA.isPressed() && M5.BtnA.pressedFor(LONG_PRESS_MS)) { if (!cueA) { cueA = true; if (screenAwake) M5.Beep.tone(3200, 25); } } else cueA = false;
  if (M5.BtnB.isPressed() && M5.BtnB.pressedFor(LONG_PRESS_MS)) { if (!cueB) { cueB = true; if (screenAwake) M5.Beep.tone(3200, 25); } } else cueB = false;

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
    case LEVEL_GAME_CLOSEST:
      // Contestant-only screen: the remote's one job is stopping the mark.
      // Starting each run and resetting between contestants both live on
      // Console -- there is deliberately no side/reset gesture here at all.
      // Either press length fires it, since a kid mid-reflex won't be
      // timing a precise short tap.
      if (frontShort || frontLong) fireClosestStop();
      break;
    case LEVEL_KARAOKE:
      // One job: mute/unmute the vocal guide so the MC can drop it out to
      // hear the crowd sing, then bring it back for effect. Either press
      // does it -- there's nothing else on this screen to disambiguate.
      if (frontShort || frontLong) fireKaraokeMuteToggle();
      break;
    case LEVEL_REMOTE_TEST:
      // The whole point is "does pressing a button work" -- any of the three
      // presses that don't mean "back" (sideLong, handled globally above)
      // fires a test SFX, not just one specific gesture.
      if (frontShort || frontLong || sideShort) fireRemoteTestSfx();
      break;
    case LEVEL_GAME_TRIVIA:
      // Correct/incorrect scoring lives at Console/the trivia controller now
      // (where the team scoreboard actually is) -- the remote is just a
      // page-turner: next/prev question, reveal on request. Starting the
      // game from the lobby is deliberately Console-only, not the remote --
      // Chris's own call.
      if      (frontShort) fireTriviaAction("next");
      else if (sideShort)  fireTriviaAction("prev");
      else if (frontLong)  fireTriviaAction("reveal");
      break;
    case LEVEL_GAME_WHEEL:
      // One job: spin. Picks a winner from whatever's currently SAVED for
      // this config server-side (see api_games_wheel_remote_spin) -- there's
      // no screen here to show/edit entries, so live-but-unsaved changes in
      // some open controller tab aren't visible to this button.
      if (frontShort || frontLong) fireWheelSpin();
      break;
    case LEVEL_GAME_SHELLGAME:
      // One job: start (shuffle/reveal). Console owns navigating the display
      // there in the first place -- pressing this before that happens is a
      // safe no-op server-side (see api_shell_game_start), same as pressing
      // Console's own START button too early would be.
      if (frontShort || frontLong) fireShellGameStart();
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

// brightness: 0.0 = off, 1.0 = full on. Hides the active-LOW inversion --
// LEDC duty is "how much of the cycle the pin spends HIGH", and HIGH is off
// for this LED, so full brightness is duty 0, not duty 255.
void setLedBrightness(float brightness) {
  brightness = constrain(brightness, 0.0f, 1.0f);
  uint32_t duty = (uint32_t)((1.0f - brightness) * 255.0f);
  ledcWrite(LED_PIN, duty);
}

float heartbeatBrightness(unsigned long now) {
  uint16_t t = now % HEARTBEAT_CYCLE_MS;
  for (int i = 0; i < HEARTBEAT_LEN - 1; i++) {
    if (t >= HEARTBEAT[i].t && t <= HEARTBEAT[i + 1].t) {
      float span = HEARTBEAT[i + 1].t - HEARTBEAT[i].t;
      float frac = span > 0 ? (t - HEARTBEAT[i].t) / span : 0;
      return HEARTBEAT[i].b + (HEARTBEAT[i + 1].b - HEARTBEAT[i].b) * frac;
    }
  }
  return HEARTBEAT[0].b;
}

void updateLed() {
  unsigned long now = millis();
  switch (connState) {
    case CONN_OK:
      setLedBrightness(heartbeatBrightness(now));
      break;
    // Reconnecting/offline stay full on/off blinks, not dimmed -- these mean
    // something's actually wrong and need to read as unmissable, not calm.
    case CONN_RECONNECTING:
      if (now - lastBlink > 250) { lastBlink = now; ledOn = !ledOn; setLedBrightness(ledOn ? 1.0f : 0.0f); }
      break;
    case CONN_OFFLINE:
      if (now - lastBlink > 500) { lastBlink = now; ledOn = !ledOn; setLedBrightness(ledOn ? 1.0f : 0.0f); }
      break;
  }
}

void updateScreenSleep() {
  bool onGameScreen = (menuLevel == LEVEL_GAME_CHAIRS || menuLevel == LEVEL_GAME_TRIVIA || menuLevel == LEVEL_GAME_TIMEDCOMP
                        || menuLevel == LEVEL_GAME_CLOSEST || menuLevel == LEVEL_GAME_WHEEL || menuLevel == LEVEL_GAME_SHELLGAME
                        || menuLevel == LEVEL_REMOTE_TEST || menuLevel == LEVEL_KARAOKE || menuLevel == LEVEL_TIMER);
  unsigned long sleepAfter = onGameScreen ? SCREEN_SLEEP_GAME_MS : SCREEN_SLEEP_MS;
  if (screenAwake && millis() - lastActivity > sleepAfter) {
    screenAwake = false;
    M5.Axp.SetLDO2(false);
    return;
  }
  if (!screenAwake) {
    float ax, ay, az;
    M5.IMU.getAccelData(&ax, &ay, &az);
    if (!haveWakeBaseline) { wakeBaseAx = ax; wakeBaseAy = ay; wakeBaseAz = az; haveWakeBaseline = true; }
    float delta = fabs(ax - wakeBaseAx) + fabs(ay - wakeBaseAy) + fabs(az - wakeBaseAz);
    // 0.25 g woke it from just walking with it in a pocket -- constant wake-ups, battery drain.
    if (delta > 0.45) {
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
// remote yanked out from under them.  The last POWEROFF_WARN_MS is a visible,
// counting-down warning (any button press cancels it -- handleButtons() resets
// lastActivity).  It is OFF, not asleep: bringing it back takes the POWER
// button (the small one on the side of the case), not either of the two
// navigation buttons.
void checkAutoPowerOff() {
  if (connState != CONN_OFFLINE) { powerOffWarning = false; return; }
  unsigned long idleFor = millis() - lastActivity;
  if (idleFor < AUTO_POWEROFF_AFTER_MS - POWEROFF_WARN_MS) { powerOffWarning = false; return; }

  powerOffWarning = true;
  if (idleFor < AUTO_POWEROFF_AFTER_MS) {
    if (!screenAwake) { screenAwake = true; M5.Axp.SetLDO2(true); }
    long secsLeft = (long)((AUTO_POWEROFF_AFTER_MS - idleFor) / 1000);
    screenBuf.fillSprite(BLACK);
    screenBuf.setTextColor(RED);
    screenBuf.setTextSize(2);
    screenBuf.setCursor(14, 24);
    screenBuf.print("NO CONNECTION");
    screenBuf.setTextColor(WHITE);
    screenBuf.setCursor(14, 52);
    screenBuf.printf("OFF IN %lds", secsLeft);
    screenBuf.setTextSize(1);
    screenBuf.setTextColor(0xC618);
    screenBuf.setCursor(14, 84);
    screenBuf.print("Press any button to stay on.");
    screenBuf.setCursor(14, 98);
    screenBuf.print("To turn back on: POWER button.");
    screenBuf.pushSprite(0, 0);
    return;
  }

  M5.Axp.SetLDO2(true);
  screenBuf.fillSprite(BLACK);
  screenBuf.setTextColor(RED);
  screenBuf.setTextSize(2);
  screenBuf.setCursor(20, 40);
  screenBuf.print("POWERING OFF");
  screenBuf.pushSprite(0, 0);
  beepFail();
  delay(1200);
  M5.Axp.PowerOff();
}

// LiPo discharge is far from linear: the old straight 3.0-4.2 V line said ~42% at 3.5 V, where the
// cell is nearly empty, so Console's LOW alert came with only minutes left.  Piecewise-linear
// through typical resting-voltage points instead.
int getBatteryPct() {
  static const float V[]   = {3.30f, 3.50f, 3.60f, 3.70f, 3.80f, 3.90f, 4.00f, 4.10f, 4.20f};
  static const float PCT[] = {0.0f,  4.0f,  10.0f, 25.0f, 45.0f, 65.0f, 80.0f, 90.0f, 100.0f};
  float v = M5.Axp.GetBatVoltage();
  if (v <= V[0]) return 0;
  if (v >= V[8]) return 100;
  for (int i = 0; i < 8; i++) {
    if (v <= V[i + 1]) return (int)(PCT[i] + (PCT[i + 1] - PCT[i]) * (v - V[i]) / (V[i + 1] - V[i]));
  }
  return 100;
}

// USB power inflates the voltage reading (and the % with it) and can't run down the cell.
bool batteryCharging() {
  return M5.Axp.GetVBusVoltage() > 4.0f;
}

// The remote itself now says so when it's getting low (it used to show only a % in the header,
// hidden entirely behind the full-screen OFFLINE/RECONNECTING states).
void checkBatteryWarning() {
  if (batteryCharging()) return;
  int pct = getBatteryPct();
  if (pct > 15) return;
  unsigned long now = millis();
  if (lastLowBattWarn != 0 && now - lastLowBattWarn < 5UL * 60 * 1000) return;
  lastLowBattWarn = now;
  beepFail();
  showToast(String("LOW BATTERY ") + pct + "%");
}

// ── DISPLAY ──────────────────────────────────────────────────────────────
void drawBootLogo() {
  M5.Lcd.fillScreen(BLACK);
  M5.Lcd.setTextColor(0xFD20);   // amber -- same accent as the live menu header
  M5.Lcd.setTextSize(2);
  M5.Lcd.setCursor(22, 50);
  M5.Lcd.print("MUSICMAN REMOTE");
  M5.Lcd.setTextColor(0x8410);   // dim gray -- same as other secondary/status text
  M5.Lcd.setTextSize(1);
  M5.Lcd.setCursor(88, 78);
  M5.Lcd.print("booting...");
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
  if (powerOffWarning) return;   // checkAutoPowerOff() is painting the countdown itself

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
    case LEVEL_GAME_CLOSEST:   screenBuf.print("CLOSEST TO THE MARK"); break;
    case LEVEL_KARAOKE:     screenBuf.print("KARAOKE - LIVE");         break;
    case LEVEL_REMOTE_TEST: screenBuf.print("REMOTE TEST");            break;
    case LEVEL_GAME_WHEEL:     screenBuf.print("PRIZE WHEEL - LIVE");  break;
    case LEVEL_GAME_SHELLGAME: screenBuf.print("SHELL GAME - LIVE");   break;
  }
  // Never show a confident wrong answer: if the last good state poll is old (parse failure, slow
  // Pi) say STALE rather than presenting frozen data as current.
  if (lastStateOkMs != 0 && millis() - lastStateOkMs > 10000) {
    screenBuf.setTextColor(RED);
    screenBuf.setCursor(150, 2);
    screenBuf.print("STALE");
  }
  {
    int bp = getBatteryPct();
    bool chg = batteryCharging();
    screenBuf.setTextColor((!chg && bp <= 15) ? RED : 0x8410);
    screenBuf.setCursor(chg ? 188 : 200, 2);
    if (chg) screenBuf.printf("+%d%%", bp); else screenBuf.printf("%d%%", bp);
  }

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
    case LEVEL_GAME_CLOSEST: {
      // Contestant-facing, not the MC's -- big, plain, one instruction.
      // No RESET/SIDE hint at all: that control lives on Console, not here.
      screenBuf.setTextColor(closestRunning ? 0x07E0 : 0x8410);
      screenBuf.setCursor(4, 20);
      screenBuf.print(closestRunning ? "GO!" : (closestCountdown > 0 ? "GET READY..." : "GET READY"));

      char buf[16];
      if (closestCountdown > 0 && !closestRunning) snprintf(buf, sizeof(buf), "%d", closestCountdown);   // show the 3-2-1 here too
      else snprintf(buf, sizeof(buf), "%.*f", closestDecimals, closestLiveValue);
      screenBuf.setTextSize(3);
      screenBuf.setTextColor(WHITE);
      int textW = strlen(buf) * 18;
      screenBuf.setCursor((240 - textW) / 2, 42);
      screenBuf.print(buf);
      screenBuf.setTextSize(1);
      if (closestUnitLabel.length()) {
        screenBuf.setTextColor(0x8410);
        int uw = closestUnitLabel.length() * 6;
        screenBuf.setCursor((240 - uw) / 2, 78);
        screenBuf.print(closestUnitLabel);
      }

      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print(closestRunning ? "PRESS FRONT TO STOP!" : (closestCountdown > 0 ? "WAIT FOR GO..." : "WAIT FOR CONSOLE"));
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("HOLD SIDE = BACK (game keeps going)");
      break;
    }
    case LEVEL_KARAOKE: {
      screenBuf.setTextColor(karaokePlaying ? 0x07E0 : 0xFD20);
      screenBuf.setCursor(4, 18);
      screenBuf.print(karaokePlaying ? "NOW SINGING" : "WAITING FOR SONG");

      if (karaokeTitle.length() > 0) {
        screenBuf.setTextColor(WHITE);
        printWrapped(karaokeTitle, 4, 34, 33, 2);
        if (karaokeArtist.length() > 0) {
          screenBuf.setTextColor(0x8410);
          screenBuf.setCursor(4, 62);
          screenBuf.print(karaokeArtist);
        }
      }

      screenBuf.setTextColor(karaokeMuted ? 0xF800 : 0x07E0);
      screenBuf.setTextSize(2);
      screenBuf.setCursor(4, 80);
      screenBuf.print(karaokeMuted ? "VOCALS MUTED" : "VOCALS ON");
      screenBuf.setTextSize(1);

      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 118);
      screenBuf.print("FRONT = TOGGLE MUTE   HOLD SIDE = BACK");
      break;
    }
    case LEVEL_REMOTE_TEST: {
      // Soundcheck screen -- press anything, hear a random SFX on the
      // projector, confirms the remote is actually reaching the Pi.
      screenBuf.setTextColor(0xFD20);
      screenBuf.setCursor(4, 30);
      screenBuf.print("PRESS ANY");
      screenBuf.setCursor(4, 50);
      screenBuf.print("BUTTON TO TEST");

      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print("Console ends this mode");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("HOLD SIDE = BACK (test stays on)");
      break;
    }
    case LEVEL_GAME_WHEEL: {
      screenBuf.setTextColor(0x07E0);
      screenBuf.setCursor(4, 30);
      screenBuf.print("PRIZE WHEEL READY");
      screenBuf.setTextColor(WHITE);
      screenBuf.setCursor(4, 52);
      screenBuf.printf("%d saved entries", wheelEntryCount);
      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print("PRESS TO SPIN");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("Uses the SAVED entries, not live edits");
      break;
    }
    case LEVEL_GAME_SHELLGAME: {
      screenBuf.setTextColor(0x07E0);
      screenBuf.setCursor(4, 30);
      screenBuf.print("SHELL GAME READY");
      screenBuf.setTextColor(0xC618);
      screenBuf.setCursor(4, 106);
      screenBuf.print("PRESS TO START");
      screenBuf.setCursor(4, 118);
      screenBuf.setTextColor(0x8410);
      screenBuf.print("Console must already be showing it");
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
                    || menuLevel == LEVEL_GAME_TIMEDCOMP || menuLevel == LEVEL_GAME_CLOSEST || menuLevel == LEVEL_KARAOKE
                    || menuLevel == LEVEL_GAME_WHEEL || menuLevel == LEVEL_GAME_SHELLGAME || menuLevel == LEVEL_REMOTE_TEST);
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
