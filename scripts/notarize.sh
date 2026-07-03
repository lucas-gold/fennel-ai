#!/usr/bin/env bash
# Notarize the built DMG so Gatekeeper will open it on other Macs.
# Needs a paid Apple Developer account and a Developer ID certificate.
#
# One-time setup:
#   xcrun notarytool store-credentials fennel \
#     --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PASSWORD
#
# Then:
#   DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)" ./scripts/bundle-app.sh
#   ./scripts/notarize.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DMG="dist/Fennel.dmg"
PROFILE="${NOTARY_PROFILE:-fennel}"

[ -f "$DMG" ] || { echo "no $DMG — run scripts/bundle-app.sh first"; exit 1; }

echo "==> submitting (this takes a few minutes)"
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo "==> stapling"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

echo
echo "notarized: $DMG is ready to upload."
