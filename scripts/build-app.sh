#!/usr/bin/env bash
# Build the SwiftUI app and wrap it in a proper .app bundle.
#
# Why a bundle (not `swift run`): microphone access (and later EventKit) needs an
# Info.plist usage string + a code signature, which a bare SwiftPM binary lacks.
# Ad-hoc signing (`-`) is enough for local dev; Developer ID comes at Stage 5.
set -euo pipefail
cd "$(dirname "$0")/../app"

CONFIG="${1:-release}"
swift build -c "$CONFIG"

BIN=".build/$CONFIG/myai"
APP=".build/MyAI.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/MyAI"
cp Resources/Info.plist "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"

echo "built $APP"
echo "run:  open $APP        # windowed app; grants the mic prompt on first launch"
echo "      $APP/Contents/MacOS/MyAI   # same, but with console logs in the terminal"
