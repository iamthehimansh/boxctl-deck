# box-suite

Control the RTX box (Ubuntu, i9-14900K + RTX 5060 Ti) from a Mac — auth, routing,
tunnels, services and live telemetry.

| | |
|---|---|
| **[boxctl](boxctl/)** | CLI: auth (Touch ID / TOTP), routing, tunnels, health, VS Code, seamless GUI apps |
| **[boxdeck](boxdeck/)** | macOS app: live telemetry, services, files, and a searchable remote app drawer |
| **[boxmcp](boxmcp/)** | MCP server: remote shell, files, services, and telemetry for Codex |

BoxDeck drives boxctl — one tunnel keeper, one source of truth.

## Install

On a new Apple-silicon Mac, the release bundle includes Xpra, cloudflared, and
boxctl. This single command installs the app, prompts for the box address and
one-time TOTP login, enrolls the SSH-only boxserver, and opens BoxDeck:

```bash
curl -fsSL https://raw.githubusercontent.com/iamthehimansh/boxctl-deck/main/install.sh | bash
```

For this box, the address can be supplied in the same command, leaving only the
password and TOTP authentication prompts:

```bash
curl -fsSL https://raw.githubusercontent.com/iamthehimansh/boxctl-deck/main/install.sh | \
  bash -s -- --host ssh.himansh.in --user himansh-raj
```

For development from a checkout:

```bash
# CLI
cd boxctl && uv venv .venv && uv pip install --python .venv/bin/python pexpect
ln -sf "$PWD/boxctl.sh" ~/.local/bin/boxctl        # or copy the launcher

# App
cd ../boxdeck && ./make-dmg.sh && open BoxDeck.dmg  # drag to /Applications

# Codex MCP
cd ../boxmcp && uv sync
codex mcp add box -- "$PWD/.venv/bin/boxmcp"
```

## Configure your box

Nothing is hardcoded — create `~/.config/boxctl/config.json`:

```json
{
  "host": "box.example.com",
  "user": "youruser",
  "alias": "box",
  "lan_host": "yourbox.local"
}
```

`BOX_HOST` / `BOX_USER` / `BOX_ALIAS` / `BOX_LAN_HOST` env vars override it.

## Quick use

```bash
boxctl status      # route, auth, key hours, tunnels, serve health, GPU
boxctl doctor      # diagnose + auto-fix
boxctl connect     # renew auth (Touch ID; TOTP fallback)
boxctl code [path] # VS Code Remote-SSH on the box
boxctl gui apps    # JSON list of launchable apps installed on the box
boxctl gui launch --desktop org.gnome.Calculator.desktop
boxctl gui launch xterm
```

## Design notes (the non-obvious bits)

- **Two routes, automatic.** `ssh box` probes the LAN (mDNS name, so DHCP can move
  the box) and falls back to the cloudflared domain. `box-lan` / `box-remote` force one.
- **Auth is layered on purpose.** A silent 24 h session key does day-to-day work;
  a Secure Enclave key (Touch ID) renews it. Listing the passkey *first* caused a
  biometric prompt on every background reconnect — order matters.
- **One tunnel keeper.** Two keepers = two ssh loops = a Touch ID popup storm.
  BoxDeck delegates to `boxctl tunnel start` rather than spawning its own.
- **One 16 GB GPU.** Services marked `gpu: true` in `~/services.json` (on the box)
  stop each other automatically.
- **Remote apps, not a remote desktop.** Xpra creates one seamless session per
  launched app over the existing SSH alias. No listening port is exposed, and
  its SSH client is forced to the silent session key so it cannot prompt Secretive.
- **Portable boxserver.** `boxctl server install` places a small standard-library
  command in `~/.local/bin` on the box. It publishes the machine profile and app
  catalog only to authenticated SSH clients; it is not a daemon and opens no port.
- **Full seamless integration.** Remote apps enable bidirectional clipboard,
  speakers, microphone, notifications, tray icons, video encoding and client
  OpenGL. A fixed 96 logical DPI and 1:1 scaling keep app sizing correct; remote
  cursor pixmaps are clamped to the normal macOS logical canvas, preserving
  I-beam, resize and hand cursor types without Xpra's oversized Retina cursor.
- **The menu-bar chart is an `NSView`, not an `NSImage`.** `MenuBarExtra` labels
  can't render `Canvas`, and `NSImage` + `lockFocus` draws at 1× and looks fuzzy
  on Retina.

## Requirements

macOS 13+, `cloudflared`, Python 3.12+ (`uv`), Command Line Tools for the app.
Seamless GUI apps require the native Xpra client on macOS and Xpra server on the box.
Optional: [Secretive](https://github.com/maxgoedjen/secretive) for the Touch ID key.
