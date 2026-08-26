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


def separate_vocals(input_path: Path, work_dir: Path) -> tuple[Path, Path]:
    """Run demucs (two-stems vocals) on the input file. Returns
    (instrumental_wav, vocals_wav)."""
    print(f"[1/3] Separating vocals from instrumental ({input_path.name})...")
    subprocess.run(
        [sys.executable, '-m', 'demucs', '--two-stems=vocals',
         '-o', str(work_dir), str(input_path)],
        check=True,
    )
    stem_dir = work_dir / 'htdemucs' / input_path.stem
    return stem_dir / 'no_vocals.wav', stem_dir / 'vocals.wav'


def transcribe_vocals(vocals_wav: Path) -> dict:
    """Run Whisper with word-level timestamps on the isolated vocal stem."""
    print("[2/3] Transcribing vocal track (this loads a speech model, may take a minute)...")
    import whisper
    model = whisper.load_model('small')
    return model.transcribe(str(vocals_wav), word_timestamps=True, language='en')


def group_into_lines(whisper_result: dict) -> list[dict]:
    """Group Whisper's per-word timestamps into karaoke display lines,
    splitting wherever there's a gap longer than LINE_GAP_SECONDS between
    consecutive words — a reasonable proxy for a line/phrase break in sung
    lyrics. Draft only; review in Admin before a real show."""
    words = []
    for seg in whisper_result.get('segments', []):
        words.extend(seg.get('words', []))

    lines, cur = [], []
    for w in words:
        if cur and (w['start'] - cur[-1]['end']) > LINE_GAP_SECONDS:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)

    return [
        {'t': round(ln[0]['start'], 2), 'text': ''.join(w['word'] for w in ln).strip()}
        for ln in lines
    ]


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
