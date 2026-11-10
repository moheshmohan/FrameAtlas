#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-serve}" in
  serve) if [ "$#" -gt 0 ]; then shift; fi; exec python3 -m frameatlas.server "$@" ;;
  mcp) exec python3 -m frameatlas.mcp ;;
  worker) exec python3 -m frameatlas.worker ;;
  import-native) exec python3 -m frameatlas.cli import-native ;;
  test) exec python3 -m unittest discover -s tests -v ;;
  build-cloud) exec python3 scripts/build_cloud.py ;;
  build-ocr) mkdir -p native-helper/bin; exec xcrun swiftc -O native-helper/vision-ocr.swift -o native-helper/bin/vision-ocr ;;
  build-video-gps) mkdir -p native-helper/bin; exec xcrun swiftc -O -parse-as-library native-helper/video-gps.swift -o native-helper/bin/video-gps ;;
  *) echo "Usage: ./run.sh [serve|worker|mcp|import-native|test|build-cloud|build-ocr|build-video-gps]"; exit 2 ;;
esac
