#!/bin/bash
# Compile l'app « Tapis Garmin » et l'installe dans ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
APP="$HOME/Applications/Tapis Garmin.app"
BUILD="$(mktemp -d)/Tapis Garmin.app"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
swiftc -O -parse-as-library -swift-version 5 -target arm64-apple-macos14 \
  TapisGarmin.swift -o "$BUILD/Contents/MacOS/TapisGarmin"
cp Info.plist "$BUILD/Contents/Info.plist"
cp ../tapis.py "$BUILD/Contents/Resources/tapis.py"
codesign --force --sign - "$BUILD"
pkill -x TapisGarmin 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$APP"
mv "$BUILD" "$APP"
open "$APP"
echo "Installée : $APP"
