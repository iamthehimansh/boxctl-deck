#!/bin/bash
# Build a self-contained arm64 boxctl for bundling in BoxDeck.app.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
uv run --with pyinstaller --with pexpect pyinstaller \
  --onefile --clean --name boxctl \
  --distpath "$HERE/dist" --workpath "$WORK/work" --specpath "$WORK" \
  "$HERE/boxctl.py"
echo "==> $HERE/dist/boxctl"
