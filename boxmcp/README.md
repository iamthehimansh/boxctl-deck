# boxmcp

A local MCP server that exposes the box managed by `boxctl` to Codex. It uses the
existing `ssh box` alias, so LAN/remote routing, session keys, and the Secretive
Touch ID fallback stay in one place. No password, TOTP, or private key is stored
by the MCP server.

## Tools

| tool | purpose |
|---|---|
| `box_info` | identity, OS, uptime, disks, memory, and GPU |
| `box_exec` | arbitrary non-interactive remote shell command |
| `box_list` | bounded remote directory traversal |
| `box_read` | read a remote text file |
| `box_write` | atomically replace a remote text file |
| `box_service` | inspect/control user-level systemd services |

`box_exec` intentionally has the same authority as the configured SSH user. The
server forces non-interactive SSH so an expired key fails instead of hanging on a
hidden password prompt.

## Install for Codex

From this directory:

```bash
uv sync
codex mcp add box -- "$PWD/.venv/bin/boxmcp"
codex mcp list
```

Restart Codex after adding the server. In a new session, ask it to use the `box`
MCP tools. Override the SSH alias when needed:

```bash
codex mcp add box --env BOX_MCP_SSH_ALIAS=box-remote -- "$PWD/.venv/bin/boxmcp"
```

## Test

```bash
uv run python -m unittest discover -s tests -v
```

