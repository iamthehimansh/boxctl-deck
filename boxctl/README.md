# boxctl

One command for the RTX box: auth, routing, tunnels, health, VS Code.
Standalone — it does **not** depend on the box-mcp server (it mints its own 24 h
session key), and it adds a Touch ID option so daily use needs no TOTP.

```bash
boxctl status         # everything at a glance
boxctl doctor         # diagnose + auto-fix
boxctl connect        # auth (Touch ID if set up, else password+TOTP)
boxctl code           # open VS Code Remote-SSH on the box
```

## Two routes — automatic

| alias | path | when |
|---|---|---|
| `box` | **auto** | LAN when reachable, else the domain. Use this normally |
| `box-lan` | `himansh-raj-MS-7D88.local` direct | force local network (fastest, no cloudflared) |
| `box-remote` | `ssh.himansh.in` via cloudflared | force this when you're **outside** |

`ssh box` decides per-connection with a 1-second probe (`Match … exec` in
`~/.ssh/config`). Measured: LAN ~0.0 s, remote ~0.3 s to connect.

The LAN route uses the box's **mDNS/Bonjour name** (avahi runs on the box), not a
hardcoded IP — so it keeps working when DHCP moves the box. `boxctl route detect`
re-discovers the address if that ever fails.

## Auth — two methods, TOTP kept

| method | how it feels | expiry |
|---|---|---|
| **passkey** (`boxctl setup-passkey`) | Touch ID prompt | **never** |
| **totp** (`boxctl connect --totp`) | password + 6-digit code | 24 h |

`passkey` uses a **Secure Enclave** key via [Secretive]: the private key is
generated inside the enclave, cannot be exported, and every use requires Touch ID.
TOTP is kept as the bootstrap (it's how the passkey gets authorized the first
time) and as recovery if the Mac is unavailable.

> Setup is one-time and needs the GUI once: `boxctl setup-passkey` installs
> Secretive, you create a secret named `box` with *"Require authentication before
> use"* ticked, then re-run the command and it authorizes the key on the box.

## VS Code

`boxctl code [path]` opens a Remote-SSH window on the box (Remote-SSH extension
installed). Editing happens over SSH — **no mount needed**, no macFUSE, no
reduced-security boot.

`~/.ssh/config` uses an **absolute** cloudflared path on purpose: VS Code launched
from Finder/Dock has a minimal `PATH` and cannot find a bare `cloudflared`.

## Tunnels

`boxctl tunnel start|stop|status` keeps `:8011` (omni voice serve) and `:11435`
(box ollama) forwarded, reconnecting on drop. A stale link is detected in ~30 s
(`ServerAliveInterval=10 ×2`) instead of hanging — a stale tunnel is what made
Intern sit on "Thinking…" with no error.

## Files

| path | what |
|---|---|
| `~/.config/boxctl/session_key` | the minted key (0600) |
| `~/.config/boxctl/session.json` | expiry, method, LAN name/IP |
| `~/.ssh/config` | managed block between `>>> boxctl managed >>>` markers |

boxctl comments out any **other** `Host box` block (e.g. box-mcp's) so ssh can't
silently pick an expired key — `ssh` takes the first value it finds.

[Secretive]: https://github.com/maxgoedjen/secretive
