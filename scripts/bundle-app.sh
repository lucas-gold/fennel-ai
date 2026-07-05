#!/usr/bin/env bash
# Build a self-contained Fennel.app and a .dmg.
# The Python runtime and backend source go inside the bundle; models do not,
# and are fetched on first launch.
#
# Signing is ad-hoc unless DEVELOPER_ID is set to a "Developer ID Application:"
# identity, which is what a distributable build needs (then scripts/notarize.sh).
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
rm -rf "$ROOT/dist" || true   # Finder may recreate .DS_Store mid-delete
mkdir -p "$APP/Contents/MacOS" "$RES"
cp "$BIN" "$APP/Contents/MacOS/Fennel"
cp app/Resources/Info.plist "$APP/Contents/Info.plist"
cp app/Resources/Fennel.icns "$RES/Fennel.icns"
cp LICENSE "$RES/LICENSE.txt"
cp THIRD-PARTY.md "$RES/THIRD-PARTY.md"
cp licenses/APACHE-2.0.txt "$RES/APACHE-2.0.txt"
cp licenses/PERMISSIVE.txt "$RES/PERMISSIVE.txt"

echo "==> python runtime"
# uv's CPython is relocatable and can be copied; the venv can't, since it only
# symlinks to the interpreter. So copy the interpreter and its site-packages.
UV_PYTHON=$(ls -d "$HOME/.local/share/uv/python/cpython-3.12"*-macos-aarch64-none 2>/dev/null | sort | tail -1)
[ -n "$UV_PYTHON" ] || { echo "no uv CPython 3.12 found — run scripts/setup-venv.sh"; exit 1; }
mkdir -p "$RES/runtime"
cp -R "$UV_PYTHON"/. "$RES/runtime"/
cp -R backend/.venv/lib/python3.12/site-packages/. "$RES/runtime/lib/python3.12/site-packages"/
# Hard link so Activity Monitor shows "Fennel" instead of "python3.12".
# It stays in bin/ so Python still resolves its prefix.
ln -f "$RES/runtime/bin/python3.12" "$RES/runtime/bin/Fennel"

echo "==> backend source"
mkdir -p "$RES/backend"
cp backend/*.py "$RES/backend"/
cp -R backend/voice "$RES/backend"/
cp -R backend/models "$RES/backend"/
# Drop __pycache__ — the .pyc files reference build paths that won't exist.
find "$RES/backend" "$RES/runtime/lib/python3.12/site-packages" \
     -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> signing"
if [ -n "$DEVELOPER_ID" ]; then
  # Every Mach-O has to be signed innermost first under the hardened runtime.
  # Python needs the JIT and unsigned-memory entitlements to run under it.
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
