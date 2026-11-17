#!/bin/bash
# Daily alarm sweep — put this in cron. Idempotent: safe to re-run.
set -uo pipefail

V380SD=${V380SD:-v380sd}
ARCHIVE=${ARCHIVE:-/mnt/media/camera-data/ALARM}
LOGFILE=${LOGFILE:-/var/log/v380.log}

mkdir -p "$ARCHIVE"
# The SD card usually keeps only 2-3 days, so always re-scan the last 3.
"$V380SD" --days 3 --events \
          --out "$ARCHIVE" \
          --template "{day}/{kind}_{day}_{time}_{id}.mp4" >> "$LOGFILE" 2>&1
