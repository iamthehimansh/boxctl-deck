#!/bin/bash
# Launcher: put this on your PATH (e.g. ln -sf "$PWD/boxctl.sh" ~/.local/bin/boxctl)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/.venv/bin/python" "$HERE/boxctl.py" "$@"
