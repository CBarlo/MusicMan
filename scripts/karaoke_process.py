#!/usr/bin/env python3
"""
Karaoke song processor — offline prep for MusicMan's Karaoke feature.

Takes a song file (an iTunes purchase, whatever) and produces the three
things Admin's Karaoke tab needs: an instrumental track, an isolated vocal
guide track, and a line-timed lyrics JSON file — generated automatically
from the audio itself, no lyrics website needed.

This does NOT run on the Pi. Vocal separation and speech transcription are
real compute (a few minutes per song on a Mac, likely impractical on a Pi
4B) — run this here, then upload the three output files into Admin's
Karaoke tab for the song.

SETUP (one time):
    python3.11 -m venv karaoke_venv
    source karaoke_venv/bin/activate
    pip install openai-whisper demucs

    (python3.11 specifically — at the time this was written, PyTorch/whisper/
    demucs did not yet have wheels for very new Python releases like 3.14.
    `brew install python@3.11` if you don't have it.)

USAGE:
    source karaoke_venv/bin/activate
    python3 scripts/karaoke_process.py \\
        --input "/path/to/song.mp3" \\
        --title "Sweet Caroline" \\
        --artist "Neil Diamond" \\
        --out ./karaoke_output

Output lands in ./karaoke_output/<slug>/:
    instrumental.mp3   — upload to the INSTRUMENTAL slot
    vocals.mp3          — upload to the VOCAL GUIDE slot
    lyrics.json         — import via the LYRICS "IMPORT JSON" button
    meta.json           — title/artist/duration, for reference

Review the draft lyrics in Admin before a real show — automatic
transcription of a live/band vocal (not a clean studio isolated take) is
very good but not perfect. Expect to fix an occasional misheard word or a
merged line that should've split in two, not retype the whole song.
"""
import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Gap (seconds) between two words' timestamps that's treated as a line break
# when grouping Whisper's word-level output into karaoke display lines.
LINE_GAP_SECONDS = 0.6


def slugify(title: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')
    return slug or 'song'


SEPARATION_MODEL = 'htdemucs_ft'  # fine-tuned model -- noticeably fewer gating/dropout
                                    # artifacts in the isolated vocal than the plain
                                    # 'htdemucs' default, at the cost of ~4x runtime
                                    # (it ensembles 4 sub-models). Worth it here since
                                    # this is an offline batch step, not live.

def separate_vocals(input_path: Path, work_dir: Path, model: str = SEPARATION_MODEL) -> tuple[Path, Path]:
    """Run demucs (two-stems vocals) on the input file. Returns
    (instrumental_wav, vocals_wav)."""
    print(f"[1/3] Separating vocals from instrumental ({input_path.name}) -- "
          f"using {model}, this is the slow step, be patient...")
    subprocess.run(
        [sys.executable, '-m', 'demucs', '--two-stems=vocals', '-n', model,
         '-o', str(work_dir), str(input_path)],
        check=True,
    )
    stem_dir = work_dir / model / input_path.stem
    return stem_dir / 'no_vocals.wav', stem_dir / 'vocals.wav'


WHISPER_MODEL = 'large-v3'  # 'small' was noticeably prone to repetition-loop
                             # hallucinations on separated vocal stems (the model
                             # loses confidence on a rough patch of audio and starts
                             # re-generating earlier lyrics instead of new ones,
                             # producing one giant run-on line) and flubbed several
                             # otherwise-clear passages outright. Confirmed on a real
                             # song: large-v3 transcribes the isolated vocal stem in
                             # ~1-2x the song's own length on CPU, which is nothing
                             # next to demucs's multi-minute separation step -- no
                             # reason not to run the best model available for an
                             # offline, not-time-constrained batch step. One-time
                             # cost: the model itself is a ~2.9GB download the first
                             # time this runs on a given machine.


def transcribe_vocals(vocals_wav: Path, model_name: str = WHISPER_MODEL) -> dict:
    """Run Whisper with word-level timestamps on the isolated vocal stem."""
    print(f"[2/3] Transcribing vocal track with {model_name} (may download the model "
          f"the first time, then loads/runs -- be patient)...")
    import whisper
    model = whisper.load_model(model_name)
    return model.transcribe(
        str(vocals_wav), word_timestamps=True, language='en',
        # The default (True) feeds each segment's own output back in as context
        # for the next one — on a rough patch of separated-vocal audio that
        # snowballs into the model re-transcribing earlier lyrics verbatim
        # instead of moving on. Off trades a little cross-line consistency for
        # not getting stuck in that loop.
        condition_on_previous_text=False,
    )


def flatten_words(whisper_result: dict) -> list[dict]:
    """Every word Whisper recognized, in order, as plain {word, start, end}
    dicts. Shared by group_into_lines() (its own draft-line grouping) and
    align_lyrics_to_words() (matching real lyrics text to Whisper's timing).

    Word-level timestamps come from a secondary cross-attention alignment
    step *within* each segment and are noticeably less reliable than the
    segment's own start/end (which come from the model's primary
    segmentation) -- worse still on sung audio than speech. Left alone, that
    per-word error just accumulates across the whole song, which is exactly
    what "lyrics line up fine at first, drift more and more by the end"
    looks like. Rescaling each segment's words to fit exactly within its own
    (more trustworthy) start/end resets that error at every segment
    boundary instead of letting it compound for the full track."""
    words = []
    for seg in whisper_result.get('segments', []):
        # Whisper's word_timestamps output isn't always clean -- a segment
        # can occasionally carry a None entry or one missing start/end (seen
        # in practice with condition_on_previous_text=False on real audio).
        # Drop anything malformed rather than crash the whole song's run.
        seg_words = [w for w in seg.get('words', []) if w and 'start' in w and 'end' in w and 'word' in w]
        seg_start, seg_end = seg.get('start'), seg.get('end')
        if seg_words and seg_start is not None and seg_end is not None and seg_end > seg_start:
            orig_start = seg_words[0]['start']
            orig_span  = seg_words[-1]['end'] - orig_start
            if orig_span > 0:
                scale = (seg_end - seg_start) / orig_span
                for w in seg_words:
                    w['start'] = round(seg_start + (w['start'] - orig_start) * scale, 3)
                    w['end']   = round(seg_start + (w['end']   - orig_start) * scale, 3)
        words.extend(seg_words)
    return words


def group_into_lines(whisper_result: dict) -> list[dict]:
    """Group Whisper's per-word timestamps into karaoke display lines,
    splitting wherever there's a gap longer than LINE_GAP_SECONDS between
    consecutive words — a reasonable proxy for a line/phrase break in sung
    lyrics. Draft only; review in Admin before a real show."""
    words = flatten_words(whisper_result)

    lines, cur = [], []
    for w in words:
        if cur and (w['start'] - cur[-1]['end']) > LINE_GAP_SECONDS:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)

    result = [
        {'t': round(ln[0]['start'], 2), 'text': ''.join(w['word'] for w in ln).strip()}
        for ln in lines
    ]
    _warn_suspicious_lines(result)
    return result


def _warn_suspicious_lines(lines: list[dict]):
    """Flag lines worth a second look in Admin: unusually long (a sign several
    real lines got merged with no detected gap) or repeating an earlier line's
    text (the hallucination-loop pattern this whole file works around)."""
    seen_texts = set()
    for i, ln in enumerate(lines):
        text = ln['text']
        if len(text) > 120:
            print(f"  ! line {i} at {ln['t']}s is unusually long ({len(text)} chars) — "
                  f"likely several lines merged, check it in Admin:\n      {text[:80]}...")
        norm = text.lower().strip()
        if norm and norm in seen_texts:
            print(f"  ! line {i} at {ln['t']}s repeats an earlier line verbatim — "
                  f"possible transcription loop, check it in Admin:\n      {text[:80]}")
        seen_texts.add(norm)


def _normalize_word(w: str) -> str:
    """Loose match key for aligning two independent transcriptions of the
    same word -- case and punctuation shouldn't count as a mismatch."""
    return re.sub(r"[^a-z0-9']", '', w.lower())


def align_lyrics_to_words(official_text: str, whisper_words: list[dict]) -> list[dict]:
    """Matches real lyrics text (pasted in by the user, from whatever source
    they already have legitimate access to -- this never fetches or stores
    lyrics content itself) against Whisper's word-level timing, so the
    output uses the correct/complete text but automatic timestamps.

    Whisper's transcript often mishears or drops words, so a straight
    word-by-word walk of both lists at the same time would drift out of
    sync after the first error. difflib.SequenceMatcher finds the longest
    common subsequence of matching words between the two lists even with
    insertions/deletions/substitutions in between (Whisper's error, or a
    line the user added that was never sung, e.g. a live-only ad-lib) --
    exactly the same technique used for text diffing. Only the matched
    words get a real Whisper timestamp; anything else gets one interpolated
    between its nearest matched neighbors so every line still lands
    somewhere reasonable, ready for manual fine-tuning with GO TO/USE."""
    lines = [ln.strip() for ln in official_text.split('\n') if ln.strip()]
    if not lines or not whisper_words:
        return []

    # Flat official word list, remembering which line + position each word
    # came from so line timestamps can be recovered after alignment.
    flat_words, word_line_idx = [], []
    for li, line in enumerate(lines):
        for w in line.split():
            flat_words.append(w)
            word_line_idx.append(li)

    official_norm = [_normalize_word(w) for w in flat_words]
    whisper_norm  = [_normalize_word(w['word']) for w in whisper_words]

    matcher = difflib.SequenceMatcher(None, whisper_norm, official_norm, autojunk=False)
    official_time = [None] * len(flat_words)
    for whisper_i, official_j, n in matcher.get_matching_blocks():
        for k in range(n):
            official_time[official_j + k] = whisper_words[whisper_i + k]['start']

    # Interpolate the gaps between matched words (and extrapolate past the
    # first/last match) so nothing is left with no timestamp at all.
    known = [i for i, t in enumerate(official_time) if t is not None]
    if not known:
        return []
    for i in range(len(official_time)):
        if official_time[i] is not None:
            continue
        prev_known = max((k for k in known if k < i), default=None)
        next_known = min((k for k in known if k > i), default=None)
        if prev_known is not None and next_known is not None:
            span = next_known - prev_known
            frac = (i - prev_known) / span
            official_time[i] = official_time[prev_known] + frac * (official_time[next_known] - official_time[prev_known])
        elif next_known is not None:
            official_time[i] = max(0.0, official_time[next_known] - 0.3 * (next_known - i))
        else:
            official_time[i] = official_time[prev_known] + 0.3 * (i - prev_known)

    result = []
    for li, line in enumerate(lines):
        idxs = [i for i, l in enumerate(word_line_idx) if l == li]
        t = round(min(official_time[i] for i in idxs), 2)
        result.append({'t': t, 'text': line})
    result.sort(key=lambda ln: ln['t'])
    return result


def convert_to_mp3(wav_path: Path, mp3_path: Path):
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(wav_path), '-codec:a', 'libmp3lame', '-q:a', '2', str(mp3_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def get_duration_seconds(mp3_path: Path) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(mp3_path)],
        capture_output=True, text=True, check=True,
    )
    return round(float(result.stdout.strip()), 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',  required=True, help='Path to the source song file (mp3, m4a, wav, ...)')
    ap.add_argument('--title',  required=True, help='Song title')
    ap.add_argument('--artist', default='',    help='Artist name')
    ap.add_argument('--out',    default='./karaoke_output', help='Output directory')
    args = ap.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")
    if not shutil.which('ffmpeg'):
        sys.exit("ffmpeg not found — install it first (brew install ffmpeg)")

    out_dir  = Path(args.out).expanduser()
    slug     = slugify(args.title)
    song_dir = out_dir / slug
    work_dir = out_dir / '_work'
    song_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    instrumental_wav, vocals_wav = separate_vocals(input_path, work_dir)
    result = transcribe_vocals(vocals_wav)
    lyrics = group_into_lines(result)

    print("[3/3] Encoding output files...")
    instrumental_mp3 = song_dir / 'instrumental.mp3'
    vocals_mp3       = song_dir / 'vocals.mp3'
    convert_to_mp3(instrumental_wav, instrumental_mp3)
    convert_to_mp3(vocals_wav, vocals_mp3)
    duration = get_duration_seconds(instrumental_mp3)

    (song_dir / 'lyrics.json').write_text(json.dumps(lyrics, indent=2))
    (song_dir / 'meta.json').write_text(json.dumps({
        'title': args.title, 'artist': args.artist, 'duration': duration,
    }, indent=2))

    shutil.rmtree(work_dir, ignore_errors=True)

    print()
    print(f"Done: {song_dir}")
    print(f"  {len(lyrics)} lyric lines, {duration:.0f}s")
    print("  Upload instrumental.mp3 / vocals.mp3 / lyrics.json into Admin → Karaoke.")
    print("  Review the lyrics there before using this live.")


if __name__ == '__main__':
    main()
