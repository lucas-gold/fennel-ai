#!/usr/bin/env bash
# Build the SwiftUI app into a .app bundle for local use.
# Microphone and EventKit access need an Info.plist and a signature, so a bare
# SwiftPM binary won't do. Ad-hoc signing is enough here; see bundle-app.sh for
# a distributable build.
set -euo pipefail
cd "$(dirname "$0")/../app"

CONFIG="${1:-release}"
swift build -c "$CONFIG"

BIN=".build/$CONFIG/myai"
APP=".build/Fennel.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Fennel"
cp Resources/Info.plist "$APP/Contents/Info.plist"
[ -f Resources/Fennel.icns ] && cp Resources/Fennel.icns "$APP/Contents/Resources/Fennel.icns"

# GPL-3.0 and Apache-2.0 both require their text to ship with the binary.
# The app reads these from Resources at runtime.
cp ../LICENSE                 "$APP/Contents/Resources/LICENSE.txt"
cp ../THIRD-PARTY.md          "$APP/Contents/Resources/THIRD-PARTY.md"
cp ../licenses/APACHE-2.0.txt "$APP/Contents/Resources/APACHE-2.0.txt"
cp ../licenses/PERMISSIVE.txt "$APP/Contents/Resources/PERMISSIVE.txt"
codesign --force --sign - "$APP"

echo "built $APP"
echo "  open $APP                     # windowed"
echo "  $APP/Contents/MacOS/Fennel    # with console logs"
