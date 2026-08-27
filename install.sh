#!/bin/bash
# One-command BoxDeck enrollment for a new Apple-silicon Mac.
set -euo pipefail

REPO="iamthehimansh/boxctl-deck"
DMG_URL="${BOXDECK_DMG_URL:-https://github.com/$REPO/releases/latest/download/BoxDeck.dmg}"
HOST="${BOX_HOST:-}"
USER_NAME="${BOX_USER:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --user) USER_NAME="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = Darwin ] || { echo "BoxDeck requires macOS" >&2; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "This release currently requires Apple silicon" >&2; exit 1; }

if [ -z "$HOST" ]; then read -r -p "Public SSH hostname: " HOST; fi
if [ -z "$USER_NAME" ]; then read -r -p "Box username: " USER_NAME; fi
[ -n "$HOST" ] && [ -n "$USER_NAME" ] || {
  echo "host and user are required" >&2; exit 2;
}

TMP="$(mktemp -d)"
MOUNT="$TMP/mount"
cleanup() {
  hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
mkdir -p "$MOUNT"
echo "==> downloading BoxDeck"
curl -fL --retry 3 "$DMG_URL" -o "$TMP/BoxDeck.dmg"
hdiutil attach "$TMP/BoxDeck.dmg" -nobrowse -readonly -mountpoint "$MOUNT" -quiet
[ -d "$MOUNT/BoxDeck.app" ] || { echo "invalid BoxDeck image" >&2; exit 1; }
rm -rf /Applications/BoxDeck.app
ditto "$MOUNT/BoxDeck.app" /Applications/BoxDeck.app
xattr -dr com.apple.quarantine /Applications/BoxDeck.app 2>/dev/null || true

mkdir -p "$HOME/.local/bin"
ln -sf /Applications/BoxDeck.app/Contents/Helpers/boxctl "$HOME/.local/bin/boxctl"
BOXCTL=/Applications/BoxDeck.app/Contents/Helpers/boxctl
"$BOXCTL" init --host "$HOST" --user "$USER_NAME" --lan-host "$HOST"
"$BOXCTL" connect --totp --remote
"$BOXCTL" server sync
open /Applications/BoxDeck.app
echo "==> ready: BoxDeck is installed and this Mac is enrolled"
