#!/usr/bin/env bash
# Build Spoke.app and a drag-to-Applications .dmg. Runs on macOS (CI: .github/workflows/macos-app.yml).
#   packaging/macos/build.sh            -> dist/Spoke.app, dist/Spoke-<version>-<arch>.dmg
# Needs: a Python 3.11+ with `pip install -r requirements.txt -r packaging/macos/requirements-build.txt`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
VERSION="$("$PY" -c 'import spoke; print(spoke.__version__)')"
ARCH="$(uname -m)"
ID="$("$PY" -c 'from spoke.app import BUNDLE_ID; print(BUNDLE_ID)')"

rm -rf build dist
mkdir -p build
"$PY" packaging/macos/make_icon.py build/Spoke.icns
SPOKE_ICON="$ROOT/build/Spoke.icns" "$PY" -m PyInstaller --noconfirm --clean \
  --distpath dist --workpath build/pyinstaller packaging/macos/Spoke.spec

APP="dist/Spoke.app"
# PyInstaller ad-hoc signs each binary; sign the bundle as a whole with its identifier so macOS
# sees one consistent app ("Spoke") for Microphone / Accessibility / Input Monitoring.
codesign --force --sign - --identifier "$ID" --timestamp=none "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist"

STAGE="build/dmg"
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="dist/Spoke-$VERSION-$ARCH.dmg"
hdiutil create -volname "Spoke" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "Built $APP and $DMG ($(du -sh "$APP" | cut -f1) app, $(du -h "$DMG" | cut -f1) dmg)"
