#!/bin/sh
# Forces the Pi's HDMI-A-1 connector to "connected" even with nothing plugged in, so the
# kiosk browser (musicman-display.service) always has a screen and video sound plays out
# through the audio HAT. Runs as root via ExecStartPre=+ in the display service.
for f in /sys/class/drm/card*-HDMI-A-1/status; do
  [ -w "$f" ] && echo on > "$f"
done
exit 0
