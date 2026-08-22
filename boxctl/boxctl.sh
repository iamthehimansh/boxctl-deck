#!/bin/bash
# Launcher: put this on your PATH (e.g. ln -sf "$PWD/boxctl.sh" ~/.local/bin/boxctl)
# Resolve symlinks first, since the virtualenv lives beside the real launcher,
# not beside a link such as ~/.local/bin/boxctl.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
HERE="$(cd -P "$(dirname "$SOURCE")" && pwd)"
exec "$HERE/.venv/bin/python" "$HERE/boxctl.py" "$@"
