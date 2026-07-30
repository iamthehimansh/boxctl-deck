#!/bin/bash
# Build BoxDeck.app and package it as a distributable .dmg
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/build.sh"
APP="$HERE/BoxDeck.app"
DMG="$HERE/BoxDeck.dmg"
STAGE="$(mktemp -d)/BoxDeck"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"          # drag-to-install
rm -f "$DMG"
echo "==> creating dmg"
hdiutil create -volname "BoxDeck" -srcfolder "$STAGE" -ov -format ULFO "$DMG" >/dev/null
rm -rf "$(dirname "$STAGE")"
echo "==> $DMG  ($(du -h "$DMG" | cut -f1))"
