from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Literal

from mcp.server.fastmcp import FastMCP

SSH_ALIAS = os.environ.get("BOX_MCP_SSH_ALIAS", "box")
MAX_OUTPUT_BYTES = int(os.environ.get("BOX_MCP_MAX_OUTPUT_BYTES", "200000"))
MAX_WRITE_BYTES = int(os.environ.get("BOX_MCP_MAX_WRITE_BYTES", "1000000"))
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")

mcp = FastMCP(
    "box",
    instructions=(
        "Operate the user's Ubuntu compute box through the boxctl-managed SSH alias. "
        "Commands run with the permissions of the configured remote SSH user. Inspect "
        "state before destructive operations and report material remote changes."
    ),
)


def _trim(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > MAX_OUTPUT_BYTES
    if truncated:
        data = data[:MAX_OUTPUT_BYTES]
    return data.decode("utf-8", errors="replace"), truncated


def _ssh(
    command: str,
    *,
    timeout_seconds: int = 120,
    stdin: bytes | None = None,
) -> dict[str, object]:
    timeout_seconds = max(1, min(timeout_seconds, 900))
    # OpenSSH joins arguments into a remote shell command. Quote the complete
    # payload as one bash -lc argument so spaces and shell syntax survive intact.
    remote = f"bash -lc {shlex.quote(command)}"
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                SSH_ALIAS,
                remote,
            ],
            input=stdin,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _trim(exc.stdout or b"")
        stderr, stderr_truncated = _trim(exc.stderr or b"")
        return {
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    stdout, stdout_truncated = _trim(result.stdout)
    stderr, stderr_truncated = _trim(result.stderr)
    return {
        "ok": result.returncode == 0,
        "timed_out": False,
        "exit_code": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    }


@mcp.tool()
def box_info() -> dict[str, object]:
    """Return remote identity, OS, uptime, storage, memory, and GPU status."""
    command = """
set -o pipefail
printf 'identity: '; id
printf 'hostname: '; hostname
printf 'kernel: '; uname -srmo
printf 'uptime: '; uptime
printf '\nfilesystems:\n'; df -hT / /mnt/winnvme /mnt/winsata 2>/dev/null || df -hT /
printf '\nmemory:\n'; free -h
printf '\ngpu:\n'; nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || true
""".strip()
    return _ssh(command, timeout_seconds=30)


@mcp.tool()
def box_exec(command: str, timeout_seconds: int = 120) -> dict[str, object]:
    """Run an arbitrary non-interactive shell command on the box.

    Use this for builds, Git, package inspection, logs, processes, and other remote
    administration. Commands run under `bash -lc` as the configured SSH user.
    Password or sudo prompts are not supported. Timeout is capped at 15 minutes.
    """
    if not command.strip():
        return {"ok": False, "error": "command must not be empty"}
    return _ssh(command, timeout_seconds=timeout_seconds)


@mcp.tool()
def box_list(path: str = "~", depth: int = 1) -> dict[str, object]:
    """List a remote file or directory with type, permissions, size, and timestamps."""
    depth = max(0, min(depth, 5))
    command = (
        f"target={shlex.quote(path)}; "
        "target=${target/#\\~/$HOME}; "
        "if [ ! -e \"$target\" ]; then echo 'path not found' >&2; exit 2; fi; "
        f"find \"$target\" -maxdepth {depth} -printf '%y\\t%m\\t%s\\t%TY-%Tm-%Td %TH:%TM:%TS\\t%p\\n' | sort"
    )
    return _ssh(command, timeout_seconds=60)


@mcp.tool()
def box_read(path: str, max_bytes: int = 200_000) -> dict[str, object]:
    """Read a remote text file, returning UTF-8 with replacement for invalid bytes."""
    max_bytes = max(1, min(max_bytes, MAX_OUTPUT_BYTES))
    command = (
        f"target={shlex.quote(path)}; target=${{target/#\\~/$HOME}}; "
        "test -f \"$target\" || { echo 'not a regular file' >&2; exit 2; }; "
        f'head -c {max_bytes} "$target"'
    )
    result = _ssh(command, timeout_seconds=60)
    if result.get("ok"):
        content = str(result["stdout"])
        result["content"] = content
        result["bytes"] = len(content.encode("utf-8"))
        result["stdout"] = ""
    return result


@mcp.tool()
def box_write(
    path: str,
    content: str,
    create_parents: bool = True,
) -> dict[str, object]:
    """Atomically replace a remote text file with the supplied content.

    The previous file is not retained. Inspect the target first when overwriting
    important data. Writes are capped by BOX_MCP_MAX_WRITE_BYTES (default 1 MB).
    """
    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        return {
            "ok": False,
            "error": f"content is {len(payload)} bytes; limit is {MAX_WRITE_BYTES}",
        }
    quoted_path = shlex.quote(path)
    mkdir = 'mkdir -p "$(dirname "$target")"; ' if create_parents else ""
    command = (
        f"target={quoted_path}; target=${{target/#\\~/$HOME}}; {mkdir}"
        'tmp=$(mktemp "${target}.boxmcp.XXXXXX") || exit; '
        'trap \'rm -f "$tmp"\' EXIT; cat >"$tmp"; '
        'if [ -e "$target" ]; then chmod --reference="$target" "$tmp"; fi; '
        'mv "$tmp" "$target"; trap - EXIT; '
        "stat -c 'wrote %s bytes to %n' \"$target\""
    )
    return _ssh(command, timeout_seconds=60, stdin=payload)


@mcp.tool()
def box_service(
    unit: str,
    action: Literal["status", "start", "stop", "restart", "enable", "disable"],
) -> dict[str, object]:
    """Inspect or control a user-level systemd service on the box."""
    if not UNIT_RE.fullmatch(unit):
        return {"ok": False, "error": "invalid systemd unit name"}
    if action == "status":
        command = f"systemctl --user status --no-pager -l {shlex.quote(unit)}"
    else:
        command = f"systemctl --user {action} {shlex.quote(unit)}"
    result = _ssh(command, timeout_seconds=120)
    # systemctl status uses exit code 3 for a known unit that is inactive or
    # activating. The inspection itself succeeded and its stdout is useful.
    if action == "status" and result.get("exit_code") == 3:
        result["ok"] = True
    return result


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
