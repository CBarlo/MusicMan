#!/usr/bin/env python3
"""
Karaoke Tool -- a local web app wrapping karaoke_process.py's pipeline
(demucs separation + Whisper transcription + line grouping) with drag-and-
drop upload, live progress, and in-browser lyric review/editing before
export. Replaces running karaoke_process.py by hand on the command line.

SETUP: same venv karaoke_process.py already uses, plus Flask:
    source karaoke_venv/bin/activate
    pip install flask

USAGE:
    source karaoke_venv/bin/activate
    python3 scripts/karaoke_tool.py
Then open http://localhost:5151 in a browser.

Output lands in ./karaoke_tool_output/<slug>/ -- instrumental.mp3,
vocals.mp3, lyrics.json, meta.json -- exactly like karaoke_process.py's
output. Upload those three files into Admin -> Karaoke the same as always;
this tool doesn't push to the Pi itself.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
import karaoke_process as kp

OUTPUT_DIR  = Path(__file__).parent.parent / 'karaoke_tool_output'
UPLOAD_DIR  = OUTPUT_DIR / '_uploads'
WORK_DIR    = OUTPUT_DIR / '_work'
OUTPUT_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
WORK_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
_jobs = {}  # job_id -> {stage, message, done, error, result}

WHISPER_MODELS    = ['small', 'medium', 'large-v2', 'large-v3', 'turbo']
SEPARATION_MODELS = ['htdemucs', 'htdemucs_ft']


def _run_job(job_id: str, input_path: Path, title: str, artist: str,
             whisper_model: str, separation_model: str, trim_start: float = 0.0):
    job = _jobs[job_id]
    job_work_dir = WORK_DIR / job_id
    try:
        slug = kp.slugify(title)
        song_dir = OUTPUT_DIR / slug
        song_dir.mkdir(parents=True, exist_ok=True)
        job_work_dir.mkdir(parents=True, exist_ok=True)

        # Cut the front of the file off BEFORE anything else touches it, so
        # separation/transcription never wastes time on a stretch that's
        # getting thrown away anyway, and every timestamp downstream (the
        # preview, the lyrics, GO TO/USE) is already relative to the trimmed
        # start -- no separate "shift everything back" step needed later.
        # -ss after -i (not before) with -c copy trims audio-accurately
        # without a lossy re-encode.
        if trim_start and trim_start > 0:
            job['stage']   = 'trimming'
            job['message'] = f'Trimming the first {trim_start:.0f}s...'
            trimmed_path = job_work_dir / ('trimmed' + input_path.suffix)
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-i', str(input_path),
                 '-ss', str(trim_start), '-c', 'copy', str(trimmed_path)],
                check=True,
            )
            input_path.unlink(missing_ok=True)
            input_path = trimmed_path

        # Keep the real, unsplit upload as the preview reference -- two
        # independently-encoded output tracks (instrumental/vocals) never
        # seek to exactly the same true position for the same requested
        # time, which made scrubbing them together for timing checks
        # unreliable. One file has nothing to drift against.
        original_name = 'original' + input_path.suffix
        shutil.copy2(input_path, song_dir / original_name)

        job['stage']   = 'separating'
        job['message'] = f'Separating vocals from instrumental with {separation_model} — this is the slow step...'
        instrumental_wav, vocals_wav = kp.separate_vocals(input_path, job_work_dir, model=separation_model)

        job['stage']   = 'transcribing'
        job['message'] = f'Transcribing lyrics with {whisper_model} (downloads the model the first time this runs)...'
        result = kp.transcribe_vocals(vocals_wav, model_name=whisper_model)
        lines  = kp.group_into_lines(result)

        # Kept alongside the output so ALIGN LYRICS can run later, any time
        # during review -- not just as a one-shot step right after transcribing.
        whisper_words = kp.flatten_words(result)
        (song_dir / 'whisper_words.json').write_text(json.dumps(whisper_words, indent=2))

        job['stage']   = 'encoding'
        job['message'] = 'Encoding output files...'
        instrumental_mp3 = song_dir / 'instrumental.mp3'
        vocals_mp3       = song_dir / 'vocals.mp3'
        kp.convert_to_mp3(instrumental_wav, instrumental_mp3)
        kp.convert_to_mp3(vocals_wav, vocals_mp3)
        duration = kp.get_duration_seconds(instrumental_mp3)

        (song_dir / 'lyrics.json').write_text(json.dumps(lines, indent=2))
        (song_dir / 'meta.json').write_text(json.dumps(
            {'title': title, 'artist': artist, 'duration': duration}, indent=2))

        job['result'] = {
            'slug': slug, 'title': title, 'artist': artist,
            'duration': duration, 'lines': lines,
            'song_dir': str(song_dir), 'original_file': original_name,
        }
        job['stage']   = 'done'
        job['message'] = 'Done.'
        job['done']    = True
    except Exception as e:
        job['error'] = str(e)
        job['done']  = True
    finally:
        input_path.unlink(missing_ok=True)
        shutil.rmtree(job_work_dir, ignore_errors=True)


@app.route('/')
def index():
    return Response(INDEX_HTML, mimetype='text/html')


def _load_song_result(slug: str):
    """Rebuilds the same shape showResult() expects, straight from what's
    already on disk -- lets a finished song's review screen come back after
    a stray page reload without reprocessing anything."""
    song_dir = OUTPUT_DIR / slug
    meta_file = song_dir / 'meta.json'
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    lyrics_file = song_dir / 'lyrics.json'
    lines = json.loads(lyrics_file.read_text()) if lyrics_file.exists() else []
    originals = sorted(song_dir.glob('original.*'))
    return {
        'slug': slug, 'title': meta.get('title', slug), 'artist': meta.get('artist', ''),
        'duration': meta.get('duration', 0), 'lines': lines,
        'song_dir': str(song_dir), 'original_file': originals[0].name if originals else None,
    }


@app.route('/api/songs')
def api_songs():
    """Every already-processed song, for the "previously processed" picker
    on the setup screen -- so a fresh page load (or a stray reload mid-review)
    isn't a dead end."""
    songs = []
    for d in sorted(OUTPUT_DIR.iterdir()):
        if d.is_dir() and (d / 'meta.json').exists():
            meta = json.loads((d / 'meta.json').read_text())
            songs.append({'slug': d.name, 'title': meta.get('title', d.name), 'artist': meta.get('artist', '')})
    return jsonify(songs)


@app.route('/api/song/<slug>')
def api_song(slug):
    result = _load_song_result(slug)
    if result is None:
        return jsonify({'ok': False, 'error': 'Unknown song'}), 404
    return jsonify({'ok': True, 'result': result})


@app.route('/api/process', methods=['POST'])
def api_process():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'No file'}), 400
    title            = request.form.get('title', '').strip() or 'Untitled'
    artist           = request.form.get('artist', '').strip()
    whisper_model    = request.form.get('whisper_model') or kp.WHISPER_MODEL
    separation_model = request.form.get('separation_model') or kp.SEPARATION_MODEL
    try:
        trim_start = max(0.0, float(request.form.get('trim_start') or 0))
    except ValueError:
        trim_start = 0.0

    job_id     = uuid.uuid4().hex[:12]
    ext        = Path(f.filename).suffix or '.mp3'
    input_path = UPLOAD_DIR / f'{job_id}{ext}'
    f.save(str(input_path))

    _jobs[job_id] = {'stage': 'queued', 'message': 'Queued...', 'done': False, 'error': None, 'result': None}
    threading.Thread(
        target=_run_job, args=(job_id, input_path, title, artist, whisper_model, separation_model, trim_start),
        daemon=True,
    ).start()
    return jsonify({'ok': True, 'job_id': job_id})


@app.route('/api/status/<job_id>')
def api_status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Unknown job'}), 404
    return jsonify({'ok': True, **job})


@app.route('/api/save_lyrics/<slug>', methods=['POST'])
def api_save_lyrics(slug):
    data = request.get_json() or {}
    song_dir = OUTPUT_DIR / slug
    if not song_dir.exists():
        return jsonify({'ok': False, 'error': 'Unknown song'}), 404
    (song_dir / 'lyrics.json').write_text(json.dumps(data.get('lyrics', []), indent=2))
    return jsonify({'ok': True})


@app.route('/api/align_lyrics/<slug>', methods=['POST'])
def api_align_lyrics(slug):
    """Matches user-pasted lyrics text (from wherever they already have
    legitimate access to it -- this endpoint never fetches or stores lyrics
    itself) against this song's own Whisper word timing. Returns the aligned
    lines for review; doesn't touch lyrics.json until the user saves."""
    data = request.get_json() or {}
    official_text = data.get('text', '')
    song_dir = OUTPUT_DIR / slug
    words_file = song_dir / 'whisper_words.json'
    if not words_file.exists():
        return jsonify({'ok': False, 'error': 'No Whisper timing saved for this song'}), 404
    whisper_words = json.loads(words_file.read_text())
    lines = kp.align_lyrics_to_words(official_text, whisper_words)
    return jsonify({'ok': True, 'lines': lines})


@app.route('/output/<slug>/<path:filename>')
def serve_output(slug, filename):
    return send_from_directory(OUTPUT_DIR / slug, filename)


@app.route('/api/probe_metadata', methods=['POST'])
def api_probe_metadata():
    """Reads whatever title/artist tags are embedded in a dropped file (ID3
    on an mp3, the moov atom on an m4a, etc.) via ffprobe -- already a hard
    dependency here, so no new library needed. Used to prefill TITLE/ARTIST
    before processing starts, instead of just guessing from the filename."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'No file'}), 400
    tmp_path = UPLOAD_DIR / f'probe_{uuid.uuid4().hex[:12]}{Path(f.filename).suffix or ".mp3"}'
    f.save(str(tmp_path))
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format_tags=title,artist',
             '-of', 'json', str(tmp_path)],
            capture_output=True, text=True, timeout=15,
        )
        tags = json.loads(result.stdout or '{}').get('format', {}).get('tags', {}) or {}
        # Tag casing isn't consistent across encoders/formats (title vs Title, etc)
        tags_lower = {k.lower(): v for k, v in tags.items()}
        return jsonify({'ok': True, 'title': tags_lower.get('title', ''), 'artist': tags_lower.get('artist', '')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})
    finally:
        tmp_path.unlink(missing_ok=True)


@app.route('/api/reveal/<slug>', methods=['POST'])
def api_reveal(slug):
    song_dir = OUTPUT_DIR / slug
    if song_dir.exists() and sys.platform == 'darwin':
        subprocess.run(['open', str(song_dir)])
    return jsonify({'ok': True, 'path': str(song_dir)})


@app.route('/api/restart', methods=['POST'])
def api_restart():
    """Re-exec this same process in place, so a code update (this file or
    karaoke_process.py) takes effect from a button in the browser -- no
    terminal needed. Runs in the background so the response below actually
    reaches the browser before this process replaces itself; the reloader
    (see app.run(use_reloader=True) below) already auto-restarts on every
    file save, this is only for forcing it on demand."""
    def _do_restart():
        time.sleep(0.3)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({'ok': True})


INDEX_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Karaoke Tool</title>
<style>
  :root {
    --bg: #14110d; --bg2: #1c1812; --surface: #241f18; --border: #3a3226;
    --text: #f0e6d2; --text-dim: #a89a80; --muted: #7a6e5a;
    --amber: #e0a640; --amber-dim: #4a3a1a; --blue: #4488cc; --rust: #c0392b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
    padding: 24px;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; letter-spacing: 2px; margin: 0 0 4px; }
  .sub { font-size: 11px; color: var(--text-dim); letter-spacing: 1px; margin-bottom: 24px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px; margin-bottom: 16px;
  }
  .card-title { font-size: 11px; font-weight: 700; letter-spacing: 1.5px; color: var(--text-dim); margin-bottom: 12px; }
  label { display: block; font-size: 10px; font-weight: 700; letter-spacing: 1px; color: var(--muted); margin-bottom: 4px; }
  input[type=text], select, textarea {
    width: 100%; background: var(--bg2); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 8px 10px; font-size: 13px; font-family: inherit;
  }
  #align-text { resize: vertical; min-height: 90px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
  #dropzone {
    border: 2px dashed var(--border); border-radius: 8px; padding: 28px; text-align: center;
    cursor: pointer; color: var(--text-dim); font-size: 13px; margin-bottom: 14px; transition: all .15s;
  }
  #dropzone.drag { border-color: var(--amber); background: var(--amber-dim); color: var(--text); }
  #dropzone.has-file { border-color: var(--amber); color: var(--text); border-style: solid; }
  button {
    font-family: inherit; font-size: 12px; font-weight: 700; letter-spacing: 1px;
    border-radius: 6px; padding: 10px 16px; cursor: pointer; border: 1px solid var(--border);
    background: var(--bg2); color: var(--text);
  }
  button:hover { border-color: var(--amber); }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button.primary { background: var(--amber); color: #241f18; border-color: var(--amber); }
  button.small { padding: 5px 9px; font-size: 10px; }
  #progress { display: none; }
  #progress .stage { font-size: 13px; color: var(--amber); margin-bottom: 6px; }
  #progress .bar { height: 4px; background: var(--bg2); border-radius: 2px; overflow: hidden; margin-bottom: 8px; }
  #progress .bar > div {
    height: 100%; width: 40%; background: var(--amber);
    animation: slide 1.4s ease-in-out infinite;
  }
  @keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
  #progress .elapsed { font-size: 11px; color: var(--muted); font-family: 'Courier New', monospace; }
  #result { display: none; }
  .previous-item {
    display: flex; align-items: center; gap: 10px; padding: 8px 10px;
    background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 6px;
  }
  .previous-item .info { flex: 1; min-width: 0; }
  .previous-item .title { font-size: 13px; font-weight: 700; }
  .previous-item .artist { font-size: 11px; color: var(--muted); }
  #preview-bar { display: none; flex-direction: column; gap: 6px; background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; margin-bottom: 12px; }
  #preview-bar .row1 { display: flex; align-items: center; gap: 10px; }
  #preview-time { font-family: 'Courier New', monospace; font-size: 11px; color: var(--amber); min-width: 80px; text-align: right; }
  #lyrics-rows { max-height: 420px; overflow-y: auto; }
  .lyric-row { display: flex; gap: 4px; align-items: center; margin-bottom: 5px; }
  .lyric-row .movecol { display: flex; flex-direction: column; gap: 1px; }
  .lyric-row .movecol button { background: none; border: none; color: var(--text-dim); padding: 0 4px; font-size: 10px; line-height: 1; }
  .lyric-row .time-input {
    width: 58px; font-family: 'Courier New', monospace; font-size: 12px; color: var(--amber);
    background: var(--bg2); border: 1px solid var(--border); border-radius: 4px; padding: 6px 4px;
  }
  .lyric-row input[type=text] {
    flex: 1; font-size: 13px; background: var(--bg2); border: 1px solid var(--border); color: var(--text);
    border-radius: 4px; padding: 6px 8px;
  }
  .lyric-row .del { background: none; border: none; color: var(--rust); cursor: pointer; font-size: 14px; padding: 2px 6px; }
  .lyric-row .ins { background: none; border: none; color: var(--blue); cursor: pointer; font-size: 13px; padding: 2px 6px; }
  .hint { font-size: 10px; color: var(--muted); margin-bottom: 10px; line-height: 1.5; }
  .btn-row { display: flex; gap: 8px; margin-top: 14px; }
  .toast {
    position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
    background: var(--surface); border: 1px solid var(--amber); color: var(--text);
    padding: 8px 16px; border-radius: 6px; font-size: 12px; opacity: 0; transition: opacity .2s;
    pointer-events: none;
  }
  .toast.show { opacity: 1; }
  .err { color: var(--rust); }
</style>
</head>
<body>
<div class="wrap">
  <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px">
    <h1 style="margin:0">KARAOKE TOOL</h1>
    <button onclick="restartServer()" title="Reloads this tool after an update -- no terminal needed"
      style="background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:6px;padding:5px 10px;font-size:11px;letter-spacing:1px;cursor:pointer">🔄 RESTART</button>
  </div>
  <div class="sub">DRAG IN A SONG · PROCESS IT OFFLINE · REVIEW &amp; FIX LYRICS · EXPORT FOR ADMIN</div>

  <div class="card" id="previous-card" style="display:none">
    <div class="card-title">PREVIOUSLY PROCESSED</div>
    <div class="hint" style="margin-bottom:8px">Reopen a song's review screen without reprocessing — survives a stray page reload.</div>
    <div id="previous-list"></div>
  </div>

  <div class="card" id="setup-card">
    <div class="card-title">SOURCE SONG</div>
    <div id="dropzone">Drop an audio or video file here, or click to choose one</div>
    <input type="file" id="file-input" accept="audio/*,video/*" style="display:none">

    <div class="row">
      <div>
        <label>TITLE</label>
        <input type="text" id="title" placeholder="e.g. Sweet Caroline" oninput="updateOutputPathPreview()">
      </div>
      <div>
        <label>ARTIST</label>
        <input type="text" id="artist" placeholder="e.g. Neil Diamond">
      </div>
    </div>
    <div class="hint" id="output-path-preview" style="margin-top:-4px"></div>
    <div class="row">
      <div>
        <label>TRIM START (OPTIONAL)</label>
        <input type="text" id="trim-start" placeholder="e.g. 0:22">
        <div class="hint" style="margin-top:4px;margin-bottom:0">Cuts everything before this point (a cappella opener, count-in, dead air) before separation/transcription even runs -- the whole song, lyrics included, ends up timed relative to the trimmed start. Leave blank to use the whole file.</div>
      </div>
      <div></div>
    </div>
    <div class="row">
      <div>
        <label>VOCAL SEPARATION MODEL</label>
        <select id="separation-model"></select>
      </div>
      <div>
        <label>TRANSCRIPTION MODEL</label>
        <select id="whisper-model"></select>
        <div class="hint" style="margin-top:4px;margin-bottom:0">large-v3 is the most accurate — the first run on this machine downloads it (~3GB), one time only.</div>
      </div>
    </div>
    <button class="primary" id="process-btn" onclick="startProcessing()" disabled>PROCESS SONG</button>

    <div id="progress">
      <div class="stage" id="progress-stage"></div>
      <div class="bar"><div></div></div>
      <div class="elapsed" id="progress-elapsed"></div>
    </div>
  </div>

  <div class="card" id="result" >
    <div class="card-title" id="result-title">LYRICS</div>
    <div class="hint">Fix a misheard word directly, or use <b>↳</b> to insert a line that isn't in the transcription (crowd ad-libs, etc). <b>USE</b> grabs the preview player's current time for that row.</div>

    <div id="preview-bar">
      <div class="row1">
        <button class="small" id="play-btn" onclick="togglePreview()" style="width:34px">▶</button>
        <input type="range" id="seek" min="0" max="100" value="0" step="0.1" oninput="seekPreview(this.value)" style="flex:1">
        <span id="preview-time">0:00 / 0:00</span>
        <audio id="preview-audio" style="display:none" ontimeupdate="previewTick()" onloadedmetadata="previewTick()" onended="previewEnded()"></audio>
        <audio id="preview-audio-vocals" style="display:none" ontimeupdate="previewTick()" onloadedmetadata="previewTick()" onended="previewEnded()"></audio>
      </div>
    </div>

    <div style="margin-bottom:14px">
      <div style="display:flex;align-items:baseline;justify-content:space-between">
        <label style="margin-bottom:0">CHECK AGAINST THE REAL LYRICS (optional)</label>
        <a href="https://www.azlyrics.com" target="_blank" rel="noopener" style="font-size:11px;color:var(--amber);text-decoration:none">🔍 AZLYRICS ↗</a>
      </div>
      <textarea id="align-text" placeholder="Paste the actual lyrics here, one line per line, from wherever you already have it (liner notes, a lyrics site, memory) -- this tool never fetches lyrics on its own."></textarea>
      <div class="hint" style="margin-top:4px;margin-bottom:0">Matches your text against Whisper's timing -- fixes misheard/garbled words using your text, keeps automatic timing. Lines you add that Whisper never heard (ad-libs) get a best-guess time, ready to fine-tune with GO/USE below.</div>
      <button style="margin-top:8px" onclick="alignLyrics()">🔗 ALIGN LYRICS</button>
    </div>

    <div id="lyrics-rows"></div>

    <div class="btn-row">
      <button class="primary" onclick="saveLyrics()">✓ SAVE LYRICS</button>
      <button onclick="revealFolder()">📁 SHOW OUTPUT FOLDER</button>
    </div>
    <div class="hint" id="output-hint" style="margin-top:10px"></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const WHISPER_MODELS = {whisper_models};
const SEPARATION_MODELS = {separation_models};
const DEFAULT_WHISPER = {default_whisper};
const DEFAULT_SEPARATION = {default_separation};
const OUTPUT_DIR_DISPLAY = {output_dir_display};

let chosenFile = null;
let jobId = null;
let currentSlug = null;
let lyrics = [];
let statusTimer = null;
let startTime = null;

function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  setTimeout(() => t.className = 'toast', 2200);
}

function slugify(title) {
  const slug = (title || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return slug || 'song';
}

function updateOutputPathPreview() {
  const title = document.getElementById('title').value;
  const slug = slugify(title);
  document.getElementById('output-path-preview').textContent =
    `Output will land in: ${OUTPUT_DIR_DISPLAY}/${slug}/ (instrumental.mp3, vocals.mp3, lyrics.json)`;
}
updateOutputPathPreview();

function initSelects() {
  const wsel = document.getElementById('whisper-model');
  WHISPER_MODELS.forEach(m => {
    const o = document.createElement('option'); o.value = m; o.textContent = m;
    if (m === DEFAULT_WHISPER) o.selected = true;
    wsel.appendChild(o);
  });
  const ssel = document.getElementById('separation-model');
  SEPARATION_MODELS.forEach(m => {
    const o = document.createElement('option'); o.value = m; o.textContent = m;
    if (m === DEFAULT_SEPARATION) o.selected = true;
    ssel.appendChild(o);
  });
}
initSelects();

const dz = document.getElementById('dropzone');
const fi = document.getElementById('file-input');
dz.addEventListener('click', () => fi.click());
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag');
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
fi.addEventListener('change', () => { if (fi.files.length) setFile(fi.files[0]); });

async function setFile(f) {
  chosenFile = f;
  dz.textContent = `🎵 ${f.name}`;
  dz.classList.add('has-file');
  document.getElementById('process-btn').disabled = false;
  // Filename guess first (instant), upgraded to the file's real embedded
  // tags a moment later if it has any -- title/artist metadata usually
  // beats whatever's in the filename, but no reason to make PROCESS SONG
  // wait on a round-trip just to fill in a field.
  if (!document.getElementById('title').value) {
    const guess = f.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
    document.getElementById('title').value = guess;
  }
  updateOutputPathPreview();

  try {
    const fd = new FormData();
    fd.append('file', f);
    const d = await fetch('/api/probe_metadata', { method: 'POST', body: fd }).then(r => r.json());
    if (d.ok && chosenFile === f) {  // still the chosen file -- not superseded by a newer pick meanwhile
      if (d.title)  document.getElementById('title').value  = d.title;
      if (d.artist) document.getElementById('artist').value = d.artist;
      updateOutputPathPreview();
    }
  } catch (e) { /* filename guess above already stands */ }
}

async function startProcessing() {
  if (!chosenFile) return;
  const title  = document.getElementById('title').value.trim() || 'Untitled';
  const artist = document.getElementById('artist').value.trim();
  const fd = new FormData();
  fd.append('file', chosenFile);
  fd.append('title', title);
  fd.append('artist', artist);
  fd.append('whisper_model', document.getElementById('whisper-model').value);
  fd.append('separation_model', document.getElementById('separation-model').value);
  const trimVal = document.getElementById('trim-start').value.trim();
  if (trimVal) fd.append('trim_start', String(parseRowTime(trimVal)));

  document.getElementById('process-btn').disabled = true;
  document.getElementById('progress').style.display = 'block';
  startTime = Date.now();

  try {
    const res = await fetch('/api/process', { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.ok) { toast(data.error || 'Failed to start', true); document.getElementById('process-btn').disabled = false; return; }
    jobId = data.job_id;
    statusTimer = setInterval(pollStatus, 1500);
    pollStatus();
  } catch (e) { toast('Failed to start', true); document.getElementById('process-btn').disabled = false; }
}

async function pollStatus() {
  if (!jobId) return;
  try {
    const d = await fetch(`/api/status/${jobId}`).then(r => r.json());
    const elapsed = Math.round((Date.now() - startTime) / 1000);
    document.getElementById('progress-stage').textContent = d.message || d.stage || '...';
    document.getElementById('progress-elapsed').textContent = `${Math.floor(elapsed/60)}:${String(elapsed%60).padStart(2,'0')} elapsed`;
    if (d.error) {
      clearInterval(statusTimer);
      toast('Processing failed: ' + d.error, true);
      document.getElementById('progress').style.display = 'none';
      document.getElementById('process-btn').disabled = false;
      return;
    }
    if (d.done && d.result) {
      clearInterval(statusTimer);
      document.getElementById('progress').style.display = 'none';
      showResult(d.result);
      loadPreviousSongs();
    }
  } catch (e) {}
}

async function loadPreviousSongs() {
  try {
    const songs = await fetch('/api/songs').then(r => r.json());
    const card = document.getElementById('previous-card');
    const list = document.getElementById('previous-list');
    if (!songs.length) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    list.innerHTML = songs.map(s => `
      <div class="previous-item">
        <div class="info">
          <div class="title">${escHtml(s.title)}</div>
          <div class="artist">${escHtml(s.artist || '')}</div>
        </div>
        <button class="small" style="width:auto;padding:6px 12px" onclick="reopenSong('${escHtml(s.slug)}')">REOPEN</button>
      </div>`).join('');
  } catch (e) {}
}

async function reopenSong(slug) {
  try {
    const d = await fetch(`/api/song/${slug}`).then(r => r.json());
    if (d.ok) showResult(d.result);
    else toast(d.error || 'Could not reopen', true);
  } catch (e) { toast('Could not reopen', true); }
}
loadPreviousSongs();

function showResult(result) {
  currentSlug = result.slug;
  lyrics = result.lines.map(l => ({ t: l.t, text: l.text, singer: l.singer || '' }));
  document.getElementById('result-title').textContent = `LYRICS — ${result.title.toUpperCase()}`;
  document.getElementById('result').style.display = 'block';
  document.getElementById('output-hint').textContent =
    `Files are in ${result.song_dir} — upload instrumental.mp3 / vocals.mp3 / lyrics.json into Admin → Karaoke the same as always.`;
  // The real, unsplit upload as the single preview source -- one file has
  // nothing to drift against, unlike stitching the two independently-encoded
  // output tracks back together for scrubbing. Falls back to both split
  // tracks for a song reopened from before this existed.
  const v = Date.now();
  if (result.original_file) {
    document.getElementById('preview-audio').src = `/output/${result.slug}/${result.original_file}?v=${v}`;
    document.getElementById('preview-audio-vocals').removeAttribute('src');
  } else {
    document.getElementById('preview-audio').src        = `/output/${result.slug}/instrumental.mp3?v=${v}`;
    document.getElementById('preview-audio-vocals').src = `/output/${result.slug}/vocals.mp3?v=${v}`;
  }
  document.getElementById('preview-bar').style.display = 'flex';
  renderRows();
  document.getElementById('result').scrollIntoView({behavior:'smooth'});
}

function previewElems() {
  return [document.getElementById('preview-audio'), document.getElementById('preview-audio-vocals')].filter(el => el.src);
}
function previewRef() {
  const audio = document.getElementById('preview-audio');
  return audio.src ? audio : document.getElementById('preview-audio-vocals');
}

function togglePreview() {
  const elems = previewElems();
  if (!elems.length) return;
  if (previewRef().paused) { elems.forEach(el => el.play().catch(()=>{})); document.getElementById('play-btn').textContent = '⏸'; }
  else { elems.forEach(el => el.pause()); document.getElementById('play-btn').textContent = '▶'; }
}
function seekPreview(val) {
  const ref = previewRef();
  if (!ref.duration) return;
  const t = (parseFloat(val) / 100) * ref.duration;
  const elems = previewElems();
  elems.forEach(el => el.currentTime = t);
  // Two independently-encoded MP3s rarely land at the exact same true
  // position for the same requested time -- once the reference track's own
  // seek is actually confirmed done, force the other track to match its
  // real resulting position exactly.
  const other = elems.find(el => el !== ref);
  if (other) {
    const onSeeked = () => { other.currentTime = ref.currentTime; ref.removeEventListener('seeked', onSeeked); };
    ref.addEventListener('seeked', onSeeked);
  }
}
function fmtTime(s) { s = Math.max(0, s||0); return Math.floor(s/60) + ':' + String(Math.floor(s%60)).padStart(2,'0'); }
function previewTick() {
  const ref = previewRef();
  if (!ref.duration) return;
  document.getElementById('seek').value = (ref.currentTime / ref.duration) * 100;
  document.getElementById('preview-time').textContent = `${fmtTime(ref.currentTime)} / ${fmtTime(ref.duration)}`;
  const elems = previewElems();
  if (elems.length === 2 && Math.abs(elems[0].currentTime - elems[1].currentTime) > 0.15) {
    elems[1].currentTime = elems[0].currentTime;
  }
}
function previewEnded() { previewElems().forEach(el => el.pause()); document.getElementById('play-btn').textContent = '▶'; }

// Same M:SS format as the preview clock -- raw seconds ("169.16") told you
// nothing about where that was in the song relative to the transport bar.
function fmtRowTime(seconds) {
  seconds = Math.max(0, seconds || 0);
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1).padStart(4, '0');
  return `${m}:${s}`;
}
function parseRowTime(str) {
  str = (str || '').trim();
  const m = str.match(/^(\d+):(\d+(?:\.\d+)?)$/);
  if (m) return parseInt(m[1], 10) * 60 + parseFloat(m[2]);
  const plain = parseFloat(str);
  return isNaN(plain) ? 0 : plain;
}

function useCurrentTime(i) {
  const ref = previewRef();
  if (!lyrics[i] || !ref.src) return;
  lyrics[i].t = Math.round(ref.currentTime * 10) / 10;
  renderRows();
}
function goToTime(i) {
  const elems = previewElems();
  if (!lyrics[i] || !elems.length) return;
  const t = lyrics[i].t;
  elems.forEach(el => { el.currentTime = t; el.play().catch(() => {}); });
  document.getElementById('play-btn').textContent = '⏸';
}
function insertAfter(i) {
  const cur = lyrics[i], next = lyrics[i+1];
  const t = next ? (cur.t + next.t) / 2 : cur.t + 2;
  // Inherits the previous line's singer -- a duet is usually several
  // consecutive lines by the same voice, so defaulting to "same as last"
  // needs fewer taps than always resetting to solo/untagged.
  lyrics.splice(i+1, 0, { t: Math.round(t*10)/10, text: '', singer: cur.singer || '' });
  renderRows();
  setTimeout(() => {
    const rows = document.querySelectorAll('#lyrics-rows .lyric-text');
    if (rows[i+1]) rows[i+1].focus();
  }, 0);
}
function removeLine(i) { lyrics.splice(i, 1); renderRows(); }
function moveLine(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= lyrics.length) return;
  [lyrics[i], lyrics[j]] = [lyrics[j], lyrics[i]];
  renderRows();
}
function updateLine(i, field, val) {
  if (!lyrics[i]) return;
  lyrics[i][field] = field === 't' ? parseRowTime(val) : val;
}
function escHtml(s) { return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// Duet support: '' (solo/untagged) -> A -> B -> BOTH -> ''. A single cycling
// button instead of a dropdown since most songs never touch this -- it
// should stay out of the way for the common solo case. Mirrors Admin's own
// Karaoke lyrics editor exactly, since lyrics.json round-trips into Admin
// via IMPORT JSON and the singer tag needs to survive that trip.
const SINGER_CYCLE = ['', 'A', 'B', 'BOTH'];
const SINGER_LABEL = { '': '—', A: 'A', B: 'B', BOTH: 'A+B' };
const SINGER_COLOR = { '': '#888', A: '#F5A623', B: '#4FC3F7', BOTH: '#c9a9f5' };
function cycleSinger(i) {
  if (!lyrics[i]) return;
  const cur = SINGER_CYCLE.indexOf(lyrics[i].singer || '');
  lyrics[i].singer = SINGER_CYCLE[(cur + 1) % SINGER_CYCLE.length];
  renderRows();
}

function renderRows() {
  const c = document.getElementById('lyrics-rows');
  c.innerHTML = lyrics.map((l, i) => `
    <div class="lyric-row">
      <div class="movecol">
        <button onclick="moveLine(${i},-1)" ${i===0?'disabled':''}>▲</button>
        <button onclick="moveLine(${i},1)" ${i===lyrics.length-1?'disabled':''}>▼</button>
      </div>
      <input type="text" class="time-input" value="${fmtRowTime(l.t)}" oninput="updateLine(${i},'t',this.value)" title="M:SS.s, same format as the preview clock">
      <button class="small" onclick="useCurrentTime(${i})">USE</button>
      <button class="small" onclick="goToTime(${i})" title="Jump the preview here and play">▶ GO</button>
      <button class="small" onclick="cycleSinger(${i})" title="Duet tagging: click to cycle who sings this line (untagged / A / B / both)"
        style="width:34px;font-weight:700;color:${SINGER_COLOR[l.singer || '']}">${SINGER_LABEL[l.singer || '']}</button>
      <input type="text" class="lyric-text" value="${escHtml(l.text)}" oninput="updateLine(${i},'text',this.value)" placeholder="e.g. BAH BAH BAH">
      <button class="ins" onclick="insertAfter(${i})" title="Insert a line after this one">↳</button>
      <button class="del" onclick="removeLine(${i})" title="Delete">✕</button>
    </div>`).join('');
}

async function saveLyrics() {
  if (!currentSlug) return;
  try {
    await fetch(`/api/save_lyrics/${currentSlug}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ lyrics: lyrics.filter(l => l.text.trim()).sort((a,b) => a.t - b.t) }),
    });
    toast('Lyrics saved to lyrics.json');
  } catch (e) { toast('Save failed', true); }
}

async function alignLyrics() {
  if (!currentSlug) return;
  const text = document.getElementById('align-text').value;
  if (!text.trim()) { toast('Paste the lyrics in first', true); return; }
  try {
    const d = await fetch(`/api/align_lyrics/${currentSlug}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text }),
    }).then(r => r.json());
    if (d.ok) {
      lyrics = d.lines.map(l => ({ t: l.t, text: l.text, singer: l.singer || '' }));
      renderRows();
      toast(`Aligned ${lyrics.length} lines — review timings below before saving`);
    } else { toast(d.error || 'Align failed', true); }
  } catch (e) { toast('Align failed', true); }
}

async function revealFolder() {
  if (!currentSlug) return;
  try {
    const d = await fetch(`/api/reveal/${currentSlug}`, {method:'POST'}).then(r => r.json());
    toast(d.path || 'Opened');
  } catch (e) {}
}

async function restartServer() {
  toast('Restarting…');
  try { await fetch('/api/restart', {method:'POST'}); } catch (e) {}
  // The server is mid-restart right after that request returns -- give it a
  // moment, then poll until it actually answers again before reloading, so
  // this doesn't just land on a "can't connect" page mid-restart.
  setTimeout(() => {
    const tryReload = () => {
      fetch('/', {cache: 'no-store'}).then(() => location.reload()).catch(() => setTimeout(tryReload, 400));
    };
    tryReload();
  }, 600);
}
</script>
</body>
</html>
"""

INDEX_HTML = INDEX_HTML.replace('{whisper_models}', json.dumps(WHISPER_MODELS)) \
                       .replace('{separation_models}', json.dumps(SEPARATION_MODELS)) \
                       .replace('{default_whisper}', json.dumps(kp.WHISPER_MODEL)) \
                       .replace('{default_separation}', json.dumps(kp.SEPARATION_MODEL)) \
                       .replace('{output_dir_display}', json.dumps(str(OUTPUT_DIR)))


if __name__ == '__main__':
    print(f"Karaoke Tool -- open http://localhost:5151 in a browser")
    print(f"Output folder: {OUTPUT_DIR}")
    # use_reloader=True (independent of debug=False -- no interactive
    # debugger, this only enables Werkzeug's file-watcher) means a future
    # edit to this file or karaoke_process.py restarts the server on its
    # own within about a second, no button or terminal action needed at
    # all -- see /api/restart above for forcing a restart on demand instead.
    app.run(host='127.0.0.1', port=5151, threaded=True, use_reloader=True)
