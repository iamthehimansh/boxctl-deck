#!/bin/bash
# Build BoxDeck.app (Command Line Tools only — no Xcode project needed).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
echo "==> swift build (release)"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/BoxDeck"
APP="$HERE/BoxDeck.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/BoxDeck"
[ -f "$HERE/BoxDeck.icns" ] && cp "$HERE/BoxDeck.icns" "$APP/Contents/Resources/"
for f in menubar.png "menubar@2x.png"; do [ -f "$HERE/$f" ] && cp "$HERE/$f" "$APP/Contents/Resources/"; done
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
codesign --force --deep --sign - "$APP" 2>/dev/null || true
echo "==> built $APP"
echo "Run it with:  open \"$APP\""

# `./build.sh --install` refreshes /Applications so you never end up running two
# copies (two copies = two menu-bar icons, and login-at-startup only works
# reliably for an app in /Applications).
if [ "$1" = "--install" ]; then
  rm -rf /Applications/BoxDeck.app
  cp -R "$APP" /Applications/BoxDeck.app
  codesign --force --deep --sign - /Applications/BoxDeck.app 2>/dev/null || true
  echo "==> installed to /Applications/BoxDeck.app"
fi
