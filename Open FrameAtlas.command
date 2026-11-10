#!/bin/sh
set -eu
cd "$(dirname "$0")"
echo "FrameAtlas · http://127.0.0.1:7318"
echo "Keep this window open while using your catalogue. Ctrl-C stops it safely."
if curl -fsS --max-time 2 http://127.0.0.1:7318/api/session >/dev/null 2>&1; then
  open http://127.0.0.1:7318
  exit 0
fi
./run.sh serve &
atlas_pid=$!
trap 'kill "$atlas_pid" 2>/dev/null || true' EXIT HUP INT TERM
atlas_attempt=0
while [ "$atlas_attempt" -lt 30 ]; do
  if curl -fsS --max-time 1 http://127.0.0.1:7318/api/session >/dev/null 2>&1; then
    open http://127.0.0.1:7318
    break
  fi
  kill -0 "$atlas_pid" 2>/dev/null || exit 1
  atlas_attempt=$((atlas_attempt + 1))
  sleep 0.2
done
wait "$atlas_pid"
