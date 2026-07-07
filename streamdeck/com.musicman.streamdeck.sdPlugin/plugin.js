#!/usr/bin/env node
/**
 * Music Man Stream Deck Plugin
 * Connects to the Pi via HTTP (data) and WebSocket (live events)
 */

const WebSocket = require('ws');
const http      = require('http');
const https     = require('https');

// ── COMMAND LINE ARGS ────────────────────────────────────────────────────────
const argv       = process.argv;
const sdPort     = argv[argv.indexOf('-port')      + 1];
const pluginUUID = argv[argv.indexOf('-pluginUUID') + 1];

// ── PI CONNECTION ─────────────────────────────────────────────────────────────
let PI_BASE = 'http://musicman.local';

// ── STATE ────────────────────────────────────────────────────────────────────
const buttons = new Map();   // context → { action, settings, coords }
let   sdWs    = null;
let   piWs    = null;
let   piWsRetry = null;

// Cached Pi data
let circles  = [];
let roles    = [];
let scenes   = [];
let macros   = [];
let sfxList  = [];
let showFlow = [];

// Cached logos (id → 'data:image/png;base64,...')
const circleLogos = new Map();
const roleLogos   = new Map();

// Live show state
let activeCircleId  = null;
let activeSceneId   = null;
let audioPlaying    = false;
let audioPaused     = false;
let timerRunning    = false;
let timerSeconds    = 0;
let musicVolume     = 80;
let sfxVolume       = 80;
let activeShowStep  = null;          // step index currently playing
const completedSteps = new Set();    // step indices already played

// ── STREAM DECK WEBSOCKET ────────────────────────────────────────────────────
function connectStreamDeck() {
  sdWs = new WebSocket(`ws://localhost:${sdPort}`);

  sdWs.on('open', () => {
    send({ event: 'registerPlugin', uuid: pluginUUID });
    log('Connected to Stream Deck');
    loadPiData();
    connectPiWebSocket();
  });

  sdWs.on('message', raw => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    handleSDEvent(msg);
  });

  sdWs.on('close',  () => { log('Stream Deck disconnected'); });
  sdWs.on('error', e => { log('Stream Deck WS error: ' + e.message); });
}

function send(obj) {
  if (sdWs && sdWs.readyState === WebSocket.OPEN)
    sdWs.send(JSON.stringify(obj));
}

function setTitle(context, title) {
  send({ event: 'setTitle', context, payload: { title, target: 0 } });
}

function setImage(context, base64image) {
  send({ event: 'setImage', context, payload: { image: base64image, target: 0 } });
}

function setState(context, state) {
  send({ event: 'setState', context, payload: { state } });
}

function showAlert(context) {
  send({ event: 'showAlert', context });
}

function showOk(context) {
  send({ event: 'showOk', context });
}

function sendToPI(context, payload) {
  send({ event: 'sendToPropertyInspector', context, action: buttons.get(context)?.action, payload });
}

// ── STREAM DECK EVENT HANDLER ────────────────────────────────────────────────
function handleSDEvent(msg) {
  const { event, context, action, payload } = msg;

  switch (event) {
    case 'willAppear':
      buttons.set(context, { action, settings: payload?.settings || {}, coords: payload?.coordinates });
      initButton(context, action, payload?.settings || {});
      break;

    case 'willDisappear':
      buttons.delete(context);
      break;

    case 'keyDown':
      handlePress(context, action, payload?.settings || {});
      break;

    case 'didReceiveSettings':
      if (buttons.has(context)) {
        buttons.get(context).settings = payload?.settings || {};
        initButton(context, action, payload?.settings || {});
      }
      break;

    case 'sendToPlugin':
      handlePIMessage(context, payload);
      break;

    case 'deviceDidConnect':
      buttons.forEach((btn, ctx) => initButton(ctx, btn.action, btn.settings));
      break;
  }
}

// ── BUTTON INIT (render image + title) ──────────────────────────────────────
function initButton(context, action, settings) {
  switch (action) {
    case 'com.musicman.streamdeck.walkup': {
      const cid = settings.circle_id;
      const c   = circles.find(x => x.id === cid);
      if (c) {
        const isActive = c.id === activeCircleId;
        setImage(context, makeCircleImage(c, isActive, false, circleLogos.get(c.id)));
      } else if (cid) {
        setImage(context, makeSimpleImage('#444', '?', cid));
      } else {
        setImage(context, makeSimpleImage('#333', 'WALK-UP', 'Not set'));
      }
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.role': {
      const rid = settings.role_id;
      const r   = roles.find(x => x.id === rid);
      if (r) {
        const isActive = r.id === activeCircleId;
        setImage(context, makeCircleImage(r, isActive, false, roleLogos.get(r.id)));
      } else if (rid) {
        setImage(context, makeSimpleImage('#444', '?', rid, '★'));
      } else {
        setImage(context, makeSimpleImage('#333', 'ROLE', 'Not set', '★'));
      }
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.showstep': {
      const raw = settings.step_index;
      const idx = typeof raw === 'number' ? raw : parseInt(raw);
      const step = (!isNaN(idx) && idx >= 0) ? showFlow[idx] : null;
      if (step) {
        const isActive    = idx === activeShowStep;
        const isCompleted = completedSteps.has(idx);
        setImage(context, makeShowStepImage(step, idx, isActive, isCompleted));
      } else if (!isNaN(idx)) {
        setImage(context, makeSimpleImage('#333', `STEP ${idx + 1}`, 'Not in show', '▶'));
      } else {
        setImage(context, makeSimpleImage('#333', 'SHOW STEP', 'Not set', '▶'));
      }
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.sfx': {
      const name  = settings.sfx_name || '';
      const label = name.replace(/_/g, ' ').toUpperCase().slice(0, 10) || 'SFX';
      setImage(context, makeSimpleImage('#C4610A', label, '', '🔊'));
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.scene': {
      const sid     = settings.scene_id || '';
      const s       = scenes.find(x => x.id === sid);
      const isActive = sid === activeSceneId;
      if (s) {
        setImage(context, makeSimpleImage(isActive ? '#F5A623' : '#555', s.name.toUpperCase(), '', s.icon || '✦'));
      } else {
        setImage(context, makeSimpleImage('#333', 'SCENE', 'Not set', '✦'));
      }
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.macro': {
      const mid = settings.macro_id || '';
      const m   = macros.find(x => x.id === mid);
      const label = m ? m.name.toUpperCase() : 'MACRO';
      setImage(context, makeSimpleImage('#2B5FA6', label, m ? m.desc || '' : 'Not set', '⚡'));
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.volume': {
      const vt    = settings.vol_type  || 'music';
      const dir   = settings.direction || 'up';
      const isMus = vt === 'music';
      const cur   = isMus ? musicVolume : sfxVolume;
      const color = isMus ? '#1a5c38' : '#1a3d5c';
      const icon  = dir === 'up' ? '🔊' : '🔉';
      const label = `${isMus ? 'MUS' : 'SFX'} ${dir === 'up' ? 'VOL ▲' : 'VOL ▼'}`;
      setImage(context, makeSimpleImage(color, label, `${cur}%`, icon));
      setTitle(context, '');
      break;
    }

    case 'com.musicman.streamdeck.transport': {
      const cmd = settings.command || 'stop';
      const cfg = TRANSPORT_CONFIG[cmd] || TRANSPORT_CONFIG['stop'];
      const isActive = (cmd === 'pause' && audioPaused) ||
                       (cmd === 'timer_toggle' && timerRunning);
      const sub = (cmd === 'timer_toggle' && timerSeconds > 0)
        ? fmtTime(timerSeconds)
        : '';
      setImage(context, makeSimpleImage(isActive ? cfg.activeColor : cfg.color, cfg.label, sub, cfg.icon));
      setTitle(context, '');
      break;
    }
  }
}

const TRANSPORT_CONFIG = {
  stop:         { label: 'STOP',      icon: '⏹', color: '#CC2222', activeColor: '#FF4444' },
  fade:         { label: 'FADE OUT',  icon: '🎵', color: '#C4610A', activeColor: '#F5A623' },
  pause:        { label: 'PAUSE',     icon: '⏸', color: '#8B44CC', activeColor: '#AA66FF' },
  timer_toggle: { label: 'TIMER',     icon: '⏱', color: '#3CB96A', activeColor: '#66FF99' },
  timer_reset:  { label: 'RESET TMR', icon: '↺', color: '#445566', activeColor: '#667788' },
  kill:         { label: 'KILL ALL',  icon: '💀', color: '#880000', activeColor: '#CC0000' },
};

function fmtTime(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// ── BUTTON PRESS HANDLER ─────────────────────────────────────────────────────
function handlePress(context, action, settings) {
  const base = settings.piBase || PI_BASE;

  switch (action) {
    case 'com.musicman.streamdeck.walkup': {
      const cid = settings.circle_id;
      if (!cid) { showAlert(context); return; }
      piGet(`${base}/api/macro/walkup?circle=${cid}`)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.role': {
      const rid = settings.role_id;
      if (!rid) { showAlert(context); return; }
      piGet(`${base}/api/macro/walkup?role=${rid}`)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.showstep': {
      const raw = settings.step_index;
      const idx = typeof raw === 'number' ? raw : parseInt(raw);
      if (isNaN(idx) || idx < 0) { showAlert(context); return; }
      piGet(`${base}/api/show/fire?index=${idx}`)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.sfx': {
      const name = settings.sfx_name;
      if (!name) { showAlert(context); return; }
      piGet(`${base}/api/sfx/play?name=${encodeURIComponent(name)}`)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.scene': {
      const sid = settings.scene_id;
      if (!sid) { showAlert(context); return; }
      piGet(`${base}/api/lights/scene?name=${encodeURIComponent(sid)}`)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.macro': {
      const mid = settings.macro_id;
      if (!mid) { showAlert(context); return; }
      piGet(`${base}/api/macro/run?name=${encodeURIComponent(mid)}`)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.volume': {
      const vt    = settings.vol_type  || 'music';
      const dir   = settings.direction || 'up';
      const amt   = parseInt(settings.amount) || 10;
      const isMus = vt === 'music';
      let cur = isMus ? musicVolume : sfxVolume;
      cur = dir === 'up' ? Math.min(100, cur + amt) : Math.max(0, cur - amt);
      if (isMus) musicVolume = cur; else sfxVolume = cur;
      const endpoint = isMus ? '/api/audio/volume' : '/api/sfx/volume';
      piGet(`${base}${endpoint}?v=${cur}`)
        .then(() => refreshVolumeButtons())
        .catch(() => showAlert(context));
      break;
    }

    case 'com.musicman.streamdeck.transport': {
      const cmd = settings.command || 'stop';
      const URLS = {
        stop:         `${base}/api/audio/stop`,
        fade:         `${base}/api/audio/fade`,
        pause:        `${base}/api/audio/${audioPaused ? 'resume' : 'pause'}`,
        timer_toggle: `${base}/api/timer/${timerRunning ? 'pause' : 'start'}`,
        timer_reset:  `${base}/api/timer/reset`,
        kill:         `${base}/api/kill`,
      };
      const url = URLS[cmd];
      if (!url) { showAlert(context); return; }
      piGet(url)
        .then(() => showOk(context))
        .catch(() => showAlert(context));
      break;
    }
  }
}

// ── PROPERTY INSPECTOR MESSAGES ──────────────────────────────────────────────
function handlePIMessage(context, payload) {
  if (payload?.event === 'requestData') {
    sendToPI(context, {
      circles,
      roles,
      scenes,
      macros,
      sfxList,
      showFlow,
      settings: buttons.get(context)?.settings || {},
    });
  }
  if (payload?.event === 'setPiBase') {
    PI_BASE = payload.value || PI_BASE;
    loadPiData();
  }
}

// ── PI WEBSOCKET (live state) ─────────────────────────────────────────────────
function connectPiWebSocket() {
  const wsUrl = PI_BASE.replace(/^http/, 'ws') + '/ws';
  log('Connecting to Pi WS: ' + wsUrl);

  try { piWs = new WebSocket(wsUrl, { handshakeTimeout: 5000 }); }
  catch(e) { scheduleWsRetry(); return; }

  piWs.on('open', () => {
    log('Pi WebSocket connected');
    clearTimeout(piWsRetry);
  });

  piWs.on('message', raw => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    handlePiEvent(msg.event, msg.data);
  });

  piWs.on('close',  () => { log('Pi WS closed'); scheduleWsRetry(); });
  piWs.on('error', () => { scheduleWsRetry(); });
}

function scheduleWsRetry() {
  clearTimeout(piWsRetry);
  piWsRetry = setTimeout(() => connectPiWebSocket(), 10000);
}

function handlePiEvent(event, data) {
  switch (event) {
    case 'display_walkup':
      activeCircleId = data?.id || null;
      refreshWalkupButtons();
      refreshRoleButtons();
      break;

    case 'scene_changed':
      activeSceneId = data?.scene || null;
      refreshSceneButtons();
      break;

    case 'audio_state':
      audioPlaying = !!data?.playing;
      audioPaused  = !!data?.paused;
      if (!audioPlaying && !audioPaused) activeCircleId = null;
      refreshWalkupButtons();
      refreshRoleButtons();
      refreshTransportButtons();
      break;

    case 'timer_state':
      timerRunning = !!data?.running;
      timerSeconds = data?.seconds_remaining || 0;
      refreshTransportButtons();
      break;

    case 'show_step': {
      const idx = data?.index;
      if (idx === null || idx === undefined) {
        // Reset — clear all show step state
        activeShowStep = null;
        completedSteps.clear();
      } else {
        // Mark previous active step as completed
        if (activeShowStep !== null && activeShowStep !== idx) {
          completedSteps.add(activeShowStep);
        }
        activeShowStep = idx;
      }
      refreshShowStepButtons();
      break;
    }

    case 'kill_all':
      activeCircleId = null;
      activeSceneId  = null;
      audioPlaying   = false;
      audioPaused    = false;
      activeShowStep = null;
      completedSteps.clear();
      refreshWalkupButtons();
      refreshRoleButtons();
      refreshSceneButtons();
      refreshTransportButtons();
      refreshShowStepButtons();
      break;
  }
}

function refreshWalkupButtons() {
  buttons.forEach((btn, ctx) => {
    if (btn.action === 'com.musicman.streamdeck.walkup')
      initButton(ctx, btn.action, btn.settings);
  });
}

function refreshRoleButtons() {
  buttons.forEach((btn, ctx) => {
    if (btn.action === 'com.musicman.streamdeck.role')
      initButton(ctx, btn.action, btn.settings);
  });
}

function refreshSceneButtons() {
  buttons.forEach((btn, ctx) => {
    if (btn.action === 'com.musicman.streamdeck.scene')
      initButton(ctx, btn.action, btn.settings);
  });
}

function refreshTransportButtons() {
  buttons.forEach((btn, ctx) => {
    if (btn.action === 'com.musicman.streamdeck.transport')
      initButton(ctx, btn.action, btn.settings);
  });
}

function refreshVolumeButtons() {
  buttons.forEach((btn, ctx) => {
    if (btn.action === 'com.musicman.streamdeck.volume')
      initButton(ctx, btn.action, btn.settings);
  });
}

function refreshShowStepButtons() {
  buttons.forEach((btn, ctx) => {
    if (btn.action === 'com.musicman.streamdeck.showstep')
      initButton(ctx, btn.action, btn.settings);
  });
}

// ── PI DATA FETCH ────────────────────────────────────────────────────────────
function loadPiData() {
  Promise.all([
    piGet(PI_BASE + '/api/circles').then(d   => { circles  = Array.isArray(d) ? d : []; }),
    piGet(PI_BASE + '/api/roles').then(d     => { roles    = Array.isArray(d) ? d : []; }),
    piGet(PI_BASE + '/api/scenes').then(d    => { scenes   = Array.isArray(d) ? d : []; }),
    piGet(PI_BASE + '/api/macros').then(d    => { macros   = Array.isArray(d) ? d : []; }),
    piGet(PI_BASE + '/api/sfx/list').then(d  => {
      sfxList = Array.isArray(d) ? d.map(f => f.replace(/\.[^.]+$/, '')) : [];
    }),
    piGet(PI_BASE + '/api/show_flow').then(d => { showFlow = Array.isArray(d) ? d : []; }),
  ]).then(() => {
    log(`Loaded: ${circles.length} circles, ${roles.length} roles, ${scenes.length} scenes, ${macros.length} macros, ${sfxList.length} sfx, ${showFlow.length} show steps`);
    fetchAllLogos();
    buttons.forEach((btn, ctx) => initButton(ctx, btn.action, btn.settings));
  }).catch(e => log('Pi data load error: ' + e.message));
}

function fetchAllLogos() {
  circles.forEach(c => {
    fetchImageAsDataUrl(`${PI_BASE}/assets/circles/${c.id}/logo.png`)
      .then(url => {
        if (url) {
          circleLogos.set(c.id, url);
          // Re-render any circle buttons showing this circle
          buttons.forEach((btn, ctx) => {
            if (btn.action === 'com.musicman.streamdeck.walkup' && btn.settings.circle_id === c.id)
              initButton(ctx, btn.action, btn.settings);
          });
        }
      });
  });
  roles.forEach(r => {
    fetchImageAsDataUrl(`${PI_BASE}/assets/roles/${r.id}/logo.png`)
      .then(url => {
        if (url) {
          roleLogos.set(r.id, url);
          buttons.forEach((btn, ctx) => {
            if (btn.action === 'com.musicman.streamdeck.role' && btn.settings.role_id === r.id)
              initButton(ctx, btn.action, btn.settings);
          });
        }
      });
  });
}

// Poll every 30s
setInterval(loadPiData, 30000);

// ── HTTP HELPERS ─────────────────────────────────────────────────────────────
function piGet(url) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { timeout: 5000 }, res => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch { resolve(body); }
      });
    });
    req.on('error',   reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
  });
}

function fetchImageAsDataUrl(url) {
  return new Promise(resolve => {
    const lib = url.startsWith('https') ? https : http;
    const req = lib.get(url, { timeout: 4000 }, res => {
      if (res.statusCode !== 200) { res.resume(); resolve(null); return; }
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        const ct = res.headers['content-type'] || 'image/png';
        resolve(`data:${ct};base64,` + Buffer.concat(chunks).toString('base64'));
      });
    });
    req.on('error',   () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

// ── IMAGE GENERATION ─────────────────────────────────────────────────────────
function makeCircleImage(circle, isActive, isCompleted, logoUrl) {
  const color      = circle.color || '#888888';
  const name       = (circle.name || 'CIRCLE').toUpperCase();
  const num        = circle.number || '';
  const bgOpacity  = isCompleted ? '0.08' : (isActive ? '0.35' : '0.18');
  const border     = isActive
    ? `stroke="${color}" stroke-width="4"`
    : `stroke="${color}" stroke-width="2" stroke-opacity="${isCompleted ? '0.3' : '0.6'}"`;
  const glow       = isActive ? `<circle cx="72" cy="72" r="70" fill="${color}" fill-opacity="0.25"/>` : '';
  const activeDot  = isActive ? `<circle cx="128" cy="16" r="8" fill="${color}"/>` : '';
  const checkmark  = isCompleted
    ? `<text x="72" y="90" text-anchor="middle" font-family="Arial Black,sans-serif" font-size="42" fill="${color}" fill-opacity="0.5">✓</text>`
    : '';
  const textAlpha  = isCompleted ? ' fill-opacity="0.4"' : '';

  if (logoUrl) {
    const dimOpacity    = isCompleted ? '0.35' : (isActive ? '1.0' : '0.75');
    const overlayAlpha  = isCompleted ? '0.5' : '0';
    const numLabel      = num !== ''
      ? `<text x="72" y="26" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="16" fill="${color}" stroke="#000" stroke-width="3" paint-order="stroke">${esc(String(num))}</text>`
      : '';
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144">
      ${glow}
      <defs><clipPath id="c"><rect width="144" height="144" rx="12"/></clipPath></defs>
      <image href="${logoUrl}" x="0" y="0" width="144" height="144" clip-path="url(#c)" opacity="${dimOpacity}" preserveAspectRatio="xMidYMid slice"/>
      <rect width="144" height="144" rx="12" fill="#000" fill-opacity="${overlayAlpha}"/>
      <rect x="2" y="2" width="140" height="140" rx="10" fill="none" ${border}/>
      ${numLabel}
      ${checkmark}
      ${activeDot}
    </svg>`;
    return svgToDataUrl(svg);
  }

  // Text-based fallback
  const words = name.split(' ');
  let line1 = '', line2 = '';
  for (const w of words) {
    if ((line1 + ' ' + w).trim().length <= 10) line1 = (line1 + ' ' + w).trim();
    else line2 = (line2 + ' ' + w).trim();
  }
  const textY1     = line2 ? (num ? 96 : 88) : (num ? 100 : 80);
  const textY2     = textY1 + 20;
  const numFontSz  = name.length > 12 ? 26 : 32;
  const txtFontSz  = name.length > 12 ? 11 : (name.length > 8 ? 12 : 14);

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144">
    ${glow}
    <rect width="144" height="144" rx="12" fill="${color}" fill-opacity="${bgOpacity}"/>
    <rect x="2" y="2" width="140" height="140" rx="10" fill="none" ${border}/>
    ${num !== '' ? `<text x="72" y="${line2 ? 58 : 62}" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="${numFontSz}" fill="${color}"${textAlpha}>${esc(String(num))}</text>` : ''}
    <text x="72" y="${textY1}" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="${txtFontSz}" fill="${color}" letter-spacing="1"${textAlpha}>${esc(line1)}</text>
    ${line2 ? `<text x="72" y="${textY2}" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="${txtFontSz}" fill="${color}" letter-spacing="1"${textAlpha}>${esc(line2)}</text>` : ''}
    ${checkmark}
    ${activeDot}
  </svg>`;
  return svgToDataUrl(svg);
}

function makeShowStepImage(step, idx, isActive, isCompleted) {
  const color     = step.color || '#F5A623';
  const label     = (step.name || `Step ${idx + 1}`).toUpperCase();
  const bgOpacity = isCompleted ? '0.06' : (isActive ? '0.35' : '0.15');
  const border    = isActive
    ? `stroke="${color}" stroke-width="4"`
    : `stroke="${color}" stroke-width="2" stroke-opacity="${isCompleted ? '0.25' : '0.5'}"`;
  const glow      = isActive ? `<circle cx="72" cy="72" r="70" fill="${color}" fill-opacity="0.2"/>` : '';
  const activeDot = isActive ? `<circle cx="128" cy="16" r="8" fill="${color}"/>` : '';
  const textAlpha = isCompleted ? ' fill-opacity="0.3"' : '';
  const numLabel  = `<text x="72" y="52" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="28" fill="${color}"${textAlpha}>${idx + 1}</text>`;

  const words  = label.split(' ');
  let line1 = '', line2 = '';
  for (const w of words) {
    if ((line1 + ' ' + w).trim().length <= 10) line1 = (line1 + ' ' + w).trim();
    else line2 = (line2 + ' ' + w).trim();
  }
  const textY1   = line2 ? 80 : 88;
  const textY2   = textY1 + 18;
  const fontSize = label.length > 10 ? 10 : label.length > 7 ? 11 : 13;
  const checkmark = isCompleted
    ? `<text x="72" y="92" text-anchor="middle" font-family="Arial Black,sans-serif" font-size="30" fill="${color}" fill-opacity="0.4">✓</text>`
    : '';

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144">
    ${glow}
    <rect width="144" height="144" rx="12" fill="${color}" fill-opacity="${bgOpacity}"/>
    <rect x="2" y="2" width="140" height="140" rx="10" fill="none" ${border}/>
    ${numLabel}
    <text x="72" y="${textY1}" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="${fontSize}" fill="${color}" letter-spacing="1"${textAlpha}>${esc(line1)}</text>
    ${line2 ? `<text x="72" y="${textY2}" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="${fontSize}" fill="${color}" letter-spacing="1"${textAlpha}>${esc(line2)}</text>` : ''}
    ${checkmark}
    ${activeDot}
  </svg>`;
  return svgToDataUrl(svg);
}

function makeSimpleImage(color, label, sublabel, emoji) {
  const lines    = (label || '').slice(0, 20).match(/.{1,10}/g) || [''];
  const hasEmoji = !!emoji;
  const emojiY   = 52;
  const labelY   = hasEmoji ? 82 + (lines.length > 1 ? -8 : 0) : 70 + (lines.length > 1 ? -8 : 0);
  const fontSize = label.length > 10 ? 11 : label.length > 7 ? 12 : 14;

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144">
    <rect width="144" height="144" rx="12" fill="${color}" fill-opacity="0.2"/>
    <rect x="2" y="2" width="140" height="140" rx="10" fill="none" stroke="${color}" stroke-width="2" stroke-opacity="0.7"/>
    ${hasEmoji ? `<text x="72" y="${emojiY}" text-anchor="middle" font-size="36" font-family="Segoe UI Emoji,Apple Color Emoji,sans-serif">${esc(emoji)}</text>` : ''}
    ${lines.map((l, i) => `<text x="72" y="${labelY + i * 18}" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="${fontSize}" fill="${color}" letter-spacing="1">${esc(l)}</text>`).join('')}
    ${sublabel ? `<text x="72" y="${labelY + lines.length * 18 + 4}" text-anchor="middle" font-family="Arial,sans-serif" font-size="10" fill="${color}" fill-opacity="0.7">${esc(String(sublabel).slice(0, 18))}</text>` : ''}
  </svg>`;
  return svgToDataUrl(svg);
}

function svgToDataUrl(svg) {
  return 'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64');
}

function esc(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── LOGGING ──────────────────────────────────────────────────────────────────
function log(msg) {
  process.stdout.write('[MusicMan] ' + msg + '\n');
}

// ── START ────────────────────────────────────────────────────────────────────
connectStreamDeck();
