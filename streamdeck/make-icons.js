// Generates placeholder SVG icons for the plugin manifest
const fs = require('fs');
const path = require('path');

const IMGS = path.join(__dirname, 'com.musicman.streamdeck.sdPlugin/imgs');
fs.mkdirSync(IMGS, { recursive: true });

function makeSVG(bg, label, emoji) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72">
    <rect width="72" height="72" rx="8" fill="${bg}" fill-opacity="0.25"/>
    <rect x="1" y="1" width="70" height="70" rx="7" fill="none" stroke="${bg}" stroke-width="2"/>
    <text x="36" y="30" text-anchor="middle" font-size="22" font-family="Segoe UI Emoji,Apple Color Emoji,sans-serif">${emoji}</text>
    <text x="36" y="52" text-anchor="middle" font-family="Arial Black,sans-serif" font-weight="900" font-size="9" fill="${bg}" letter-spacing="1">${label}</text>
  </svg>`;
}

const icons = {
  'plugin-icon':        ['#F5A623','MUSIC MAN','🎵'],
  'category':           ['#F5A623','MUSIC MAN','🎵'],
  'action-walkup':      ['#2B5FA6','WALK-UP',  '⬡'],
  'action-walkup-active':['#66AAFF','PLAYING',  '▶'],
  'action-sfx':         ['#C4610A','SFX',      '🔊'],
  'action-scene':       ['#3CB96A','SCENE',    '💡'],
  'action-scene-active':['#F5A623','ACTIVE',   '💡'],
  'action-macro':       ['#8B44CC','MACRO',    '⚡'],
  'action-transport':   ['#CC2222','CTRL',     '⏹'],
  'action-transport-active':['#FF4444','ACTIVE','⏸'],
};

for (const [name, [color, label, emoji]] of Object.entries(icons)) {
  fs.writeFileSync(path.join(IMGS, name + '.svg'), makeSVG(color, label, emoji));
  // Also write @2x (Stream Deck looks for these)
  fs.writeFileSync(path.join(IMGS, name + '@2x.svg'), makeSVG(color, label, emoji));
}
console.log('Icons written:', Object.keys(icons).length * 2, 'files');
