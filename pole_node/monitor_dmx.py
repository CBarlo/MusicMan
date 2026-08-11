#!/usr/bin/env python3
"""
Live DMX channel monitor for the pole nodes — polls /json/info every 150ms
and logs pole_a/pole_b's raw channel state. Flags any read where a known
mode-select channel (pinspot CH1, pinspot CH6, wash CH7, or PAR's Effects
channel) is outside its safe/manual range, so a live failure gets caught
with real data instead of guessed at after the fact.

Usage: python3 monitor_dmx.py [seconds]   (default: runs until Ctrl-C)
"""
import requests, time, sys, datetime

POLES = {'pole_a': 'http://192.168.4.35', 'pole_b': 'http://192.168.4.33'}
POLL_INTERVAL = 0.15
LOG_FILE = '/tmp/dmx_monitor.log'

# (index, label, safe_min, safe_max) — indices into the mm_dmx_raw array,
# which is channels 1-24 (0-indexed 0-23): pinspot 0-5, wash 6-13, par 14-23
CHECKS = [
    (0,  'pinspot CH1 (master)', 0, 255),   # informational only, wide range
    (5,  'pinspot CH6 (effect)', 0, 2),
    (12, 'wash CH7 (mode)',      0, 50),
    (22, 'par CH9 (effects)',    0, 10),    # ~0-10 covers "manual"; real presets start at 11
]

def fmt_raw(raw_str):
    try:
        return [int(x) for x in raw_str.split(',')]
    except Exception:
        return []

def check(pole, chans):
    flags = []
    for idx, label, lo, hi in CHECKS:
        if idx < len(chans) and not (lo <= chans[idx] <= hi):
            flags.append(f'{label}={chans[idx]} (expected {lo}-{hi})')
    return flags

def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    start = time.time()
    last_raw = {}
    print(f'Monitoring {list(POLES)} every {POLL_INTERVAL*1000:.0f}ms — log: {LOG_FILE}')
    with open(LOG_FILE, 'a') as log:
        log.write(f'\n=== monitor started {datetime.datetime.now().isoformat()} ===\n')
        log.flush()
        while duration is None or (time.time() - start) < duration:
            ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
            for pole, base in POLES.items():
                try:
                    r = requests.get(f'{base}/json/info', timeout=1)
                    u = r.json().get('u', {})
                    raw = fmt_raw(u.get('mm_dmx_raw', ''))
                except Exception as e:
                    print(f'{ts} {pole} POLL ERROR: {e}')
                    log.write(f'{ts} {pole} POLL ERROR: {e}\n')
                    continue
                if raw and raw != last_raw.get(pole):
                    line = f'{ts} {pole} CHANGED: {raw}'
                    flags = check(pole, raw)
                    if flags:
                        line += '  !!! ' + ' | '.join(flags)
                        print(f'\033[91m{line}\033[0m')
                    else:
                        print(line)
                    log.write(line + '\n')
                    log.flush()
                    last_raw[pole] = raw
            time.sleep(POLL_INTERVAL)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
