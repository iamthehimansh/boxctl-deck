#!/bin/bash
# Build BoxDeck.app (Command Line Tools only — no Xcode project needed).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
INSTALL=false
BUNDLE=false
for arg in "$@"; do
  [ "$arg" = "--install" ] && INSTALL=true
  [ "$arg" = "--bundle" ] && BUNDLE=true
done
[ "$INSTALL" = true ] && BUNDLE=true
echo "==> swift build (release)"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/BoxDeck"
APP="$HERE/BoxDeck.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/BoxDeck"
[ -f "$HERE/BoxDeck.icns" ] && cp "$HERE/BoxDeck.icns" "$APP/Contents/Resources/"
for f in menubar.png "menubar@2x.png"; do [ -f "$HERE/$f" ] && cp "$HERE/$f" "$APP/Contents/Resources/"; done
cp "$HERE/THIRD_PARTY.md" "$APP/Contents/Resources/"

if [ "$BUNDLE" = true ]; then
  XPRA_SOURCE="${BOXDECK_XPRA_APP:-/Applications/Xpra.app}"
  CLOUDFLARED_SOURCE="${BOXDECK_CLOUDFLARED:-$(command -v cloudflared || true)}"
  [ -d "$XPRA_SOURCE" ] || { echo "Xpra.app missing: $XPRA_SOURCE" >&2; exit 1; }
  [ -x "$CLOUDFLARED_SOURCE" ] || { echo "cloudflared missing" >&2; exit 1; }
  "$HERE/../boxctl/build-standalone.sh"
  mkdir -p "$APP/Contents/Helpers"
  ditto "$XPRA_SOURCE" "$APP/Contents/Helpers/Xpra.app"
  # Xpra is the transport for remote windows, not a separate user-facing Mac
  # application. Agent mode prevents one duplicate Dock tile per launched app.
  /usr/libexec/PlistBuddy -c 'Delete :LSUIElement' \
    "$APP/Contents/Helpers/Xpra.app/Contents/Info.plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c 'Add :LSUIElement bool true' \
    "$APP/Contents/Helpers/Xpra.app/Contents/Info.plist"
  # Xpra 6.5's Darwin backend does not declare a logical cursor canvas. GTK
  # consequently treats Retina backing pixels as points and doubles I-beams and
  # other cursors. Patch only our copied bundle, preserving dynamic cursor types.
  CURSOR_GUI="$(find "$APP/Contents/Helpers/Xpra.app/Contents/Resources/lib" \
    -path '*/xpra/platform/gui.py' -print -quit)"
  [ -n "$CURSOR_GUI" ] || { echo "Xpra cursor backend missing" >&2; exit 1; }
  perl -0pi -e 's/(def get_fixed_cursor_size\(\) -> tuple\[int, int\]:\n)    return -1, -1/$1    return 16, 16/' "$CURSOR_GUI"
  grep -A8 'def get_fixed_cursor_size' "$CURSOR_GUI" | grep -q 'return 16, 16' || {
    echo "Xpra cursor patch did not apply" >&2; exit 1;
  }
  # Xpra forwards macOS smooth-scroll deltas at full strength, which is much
  # faster than native Mac scrolling in many Linux GTK/Electron applications.
  # Add a BoxDeck-only percentage scale controlled by the helper environment.
  POINTER_GUI="$(find "$APP/Contents/Helpers/Xpra.app/Contents/Resources/lib" \
    -path '*/xpra/client/gtk3/window/pointer.py' -print -quit)"
  [ -n "$POINTER_GUI" ] || { echo "Xpra pointer backend missing" >&2; exit 1; }
  perl -0pi -e 's/(SMOOTH_SCROLL_NORM = envint\([^\n]+\)\n)/$1SMOOTH_SCROLL_SCALE = envint("XPRA_SMOOTH_SCROLL_SCALE", 100)\n/; s/return value\n/return value * SMOOTH_SCROLL_SCALE \/ 100.0\n/; s/return math\.copysign\(smoothed, value\)/return math.copysign(smoothed, value) * SMOOTH_SCROLL_SCALE \/ 100.0/' "$POINTER_GUI"
  grep -q 'SMOOTH_SCROLL_SCALE = envint' "$POINTER_GUI" || {
    echo "Xpra scroll scale patch did not apply" >&2; exit 1;
  }
  cp "$HERE/../boxctl/dist/boxctl" "$APP/Contents/Helpers/boxctl"
  cp "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CLOUDFLARED_SOURCE")" \
     "$APP/Contents/Helpers/cloudflared"
  chmod 755 "$APP/Contents/Helpers/boxctl" "$APP/Contents/Helpers/cloudflared"
  echo "==> bundled Xpra, boxctl and cloudflared"
fi
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>BoxDeck</string>
  <key>CFBundleDisplayName</key><string>BoxDeck</string>
  <key>CFBundleIdentifier</key><string>in.himansh.boxdeck</string>
  <key>CFBundleExecutable</key><string>BoxDeck</string>
  <key>CFBundleIconFile</key><string>BoxDeck</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
</dict></plist>
PLIST
# Do not use --deep here: it rewrites Xpra's nested GStreamer/Python binaries
# and breaks audio/plugin loading. Keep the upstream nested app signatures.
if [ "$BUNDLE" = true ]; then
  # We changed only Xpra's plist (agent mode) and cursor Python source. A shallow
  # re-sign validates that nested bundle without rewriting its media binaries.
  codesign --force --sign - "$APP/Contents/Helpers/Xpra.app" 2>/dev/null || true
fi
codesign --force --sign - "$APP" 2>/dev/null || true
echo "==> built $APP"
echo "Run it with:  open \"$APP\""

# `./build.sh --install` refreshes /Applications so you never end up running two
# copies (two copies = two menu-bar icons, and login-at-startup only works
# reliably for an app in /Applications).
if [ "$INSTALL" = true ]; then
  rm -rf /Applications/BoxDeck.app
  cp -R "$APP" /Applications/BoxDeck.app
  codesign --force --sign - /Applications/BoxDeck.app 2>/dev/null || true
  echo "==> installed to /Applications/BoxDeck.app"
  # The installed copy is the only one that should remain runnable. Keeping the
  # development bundle beside the sources makes Spotlight expose two identical
  # apps and can lead to two tunnel keepers when the wrong copy is launched.
  rm -rf "$APP"
fi
