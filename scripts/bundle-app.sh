#!/usr/bin/env bash
# Build a self-contained Fennel.app and a .dmg to put online.
#
# Self-contained means the user opens the app and nothing else: the Python
# runtime and the backend source live inside the bundle, and the app starts the
# backend itself (BackendProcess.swift). Models are NOT bundled — they are ~3 GB
# and would triple the download; they fetch on first launch, which is the one
# moment Fennel needs the network.
#
# Signing: ad-hoc by default, which is fine locally but Gatekeeper will refuse it
# on someone else's Mac. Set DEVELOPER_ID to a "Developer ID Application: ..."
# identity for a distributable build, then notarize (see scripts/notarize.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

APP="$ROOT/dist/Fennel.app"
RES="$APP/Contents/Resources"
DEVELOPER_ID="${DEVELOPER_ID:-}"

echo "==> Swift binary"
swift build --package-path app -c release
BIN="$ROOT/app/.build/release/myai"

echo "==> assembling bundle"
# `|| true`: Finder recreates .DS_Store while the delete is in flight if the
# folder happens to be open, and that shouldn't fail a build.
rm -rf "$ROOT/dist" || true
mkdir -p "$APP/Contents/MacOS" "$RES"
cp "$BIN" "$APP/Contents/MacOS/Fennel"
cp app/Resources/Info.plist "$APP/Contents/Info.plist"
cp app/Resources/Fennel.icns "$RES/Fennel.icns"
cp LICENSE "$RES/LICENSE.txt"
cp THIRD-PARTY.md "$RES/THIRD-PARTY.md"
cp licenses/APACHE-2.0.txt "$RES/APACHE-2.0.txt"
cp licenses/PERMISSIVE.txt "$RES/PERMISSIVE.txt"

echo "==> python runtime"
# uv's CPython is built to be relocatable, so it can simply be copied. The venv
# itself cannot: it only symlinks to that interpreter.
UVPY=$(ls -d "$HOME/.local/share/uv/python/cpython-3.12"*-macos-aarch64-none 2>/dev/null | sort | tail -1)
[ -n "$UVPY" ] || { echo "no uv CPython 3.12 found — run scripts/setup-venv.sh"; exit 1; }
mkdir -p "$RES/runtime"
cp -R "$UVPY"/. "$RES/runtime"/
cp -R backend/.venv/lib/python3.12/site-packages/. "$RES/runtime/lib/python3.12/site-packages"/
# A second name for the interpreter, so Activity Monitor says "Fennel" rather
# than "python3.12". A hard link, not a copy: same 17 MB file, one more
# directory entry, and the process name comes from the path used to exec it.
# Python still finds its prefix because the name sits in the same bin/.
ln -f "$RES/runtime/bin/python3.12" "$RES/runtime/bin/Fennel"

echo "==> backend source"
mkdir -p "$RES/backend"
cp backend/*.py "$RES/backend"/
cp -R backend/voice "$RES/backend"/
cp -R backend/models "$RES/backend"/
# Caches and venvs must not travel: stale .pyc referencing build paths, and a
# venv whose symlinks point at this machine.
find "$RES/backend" "$RES/runtime/lib/python3.12/site-packages" \
     -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> signing"
if [ -n "$DEVELOPER_ID" ]; then
  # Every Mach-O inside has to be signed, innermost first, and the hardened
  # runtime is required for notarization. Python needs the JIT/unsigned-memory
  # entitlements or it will crash under it.
  cat > "$ROOT/dist/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.device.audio-input</key><true/>
  <key>com.apple.security.personal-information.calendars</key><true/>
</dict></plist>
PLIST
  find "$APP" \( -name "*.dylib" -o -name "*.so" -o -perm +111 -type f \) -print0 \
    | xargs -0 -I{} codesign --force --timestamp --options runtime \
        --entitlements "$ROOT/dist/entitlements.plist" --sign "$DEVELOPER_ID" {} 2>/dev/null || true
  codesign --force --timestamp --options runtime \
    --entitlements "$ROOT/dist/entitlements.plist" --sign "$DEVELOPER_ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
else
  echo "    ad-hoc (local only — Gatekeeper will block this on other Macs)"
  codesign --force --deep --sign - "$APP" 2>/dev/null || codesign --force --sign - "$APP"
fi

echo "==> dmg"
DMG="$ROOT/dist/Fennel.dmg"
STAGE="$ROOT/dist/stage"
mkdir -p "$STAGE"; cp -R "$APP" "$STAGE"/; ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Fennel" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "built $(du -sh "$APP" | cut -f1) app  ->  $(du -sh "$DMG" | cut -f1) dmg"
echo "  $DMG"
[ -z "$DEVELOPER_ID" ] && echo "  NOT distributable yet: set DEVELOPER_ID and notarize (scripts/notarize.sh)"
exit 0
