#!/usr/bin/env python3
"""boxctl — one command for the RTX box: auth, tunnels, health, VS Code.

Standalone: does NOT depend on the box-mcp server. It mints its own 24h SSH
session key (same idea, own implementation) and can additionally install a
Touch ID / Secure Enclave key so day-to-day connections need no TOTP at all.

Auth methods
  passkey  Secure Enclave key (Secretive) — Touch ID, sliding 30-day authorization.
           Preferred once set up: nothing secret is stored on disk; use renews it.
  totp     password + TOTP -> mints a 24h key. Always kept as the bootstrap /
           recovery path (and the only way to install the passkey the first time).

Commands
  boxctl status              key/ssh/tunnel/serve health at a glance
  boxctl connect [--totp]    authenticate (auto-picks passkey if set up)
  boxctl setup-passkey       install Secretive + authorize a Touch ID key
  boxctl tunnel start|stop|status
  boxctl doctor              diagnose + auto-fix common breakage
  boxctl code [path]         open VS Code Remote-SSH on the box
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time

# Site config: env var > ~/.config/boxctl/config.json > placeholder.
# Keeping the real host/user OUT of the source means this repo can be public.
def _cfg() -> dict:
    try:
        return json.loads((pathlib.Path(os.path.expanduser("~/.config/boxctl/config.json"))).read_text())
    except Exception:
        return {}


_C = _cfg()
HOST = os.environ.get("BOX_HOST") or _C.get("host", "box.example.com")
USER = os.environ.get("BOX_USER") or _C.get("user", "youruser")
ALIAS = os.environ.get("BOX_ALIAS") or _C.get("alias", "box")
CLOUDFLARED = shutil.which("cloudflared") or "/opt/homebrew/bin/cloudflared"
CFG_DIR = pathlib.Path(os.path.expanduser("~/.config/boxctl"))
KEY = CFG_DIR / "session_key"
META = CFG_DIR / "session.json"
PASSKEY_PUB = CFG_DIR / "passkey.pub"
SSH_CONFIG = pathlib.Path(os.path.expanduser("~/.ssh/config"))
TTL_HOURS = int(os.environ.get("BOX_TTL_HOURS", "24"))
PASSKEY_DAYS = int(os.environ.get("BOX_PASSKEY_DAYS", "30"))
TUNNELS = [(8011, 8011), (11435, 11434)]        # local -> remote
LAN_HOST_DEFAULT = os.environ.get("BOX_LAN_HOST") or _C.get("lan_host", "")
SECRETIVE_SOCK = pathlib.Path(os.path.expanduser(
    "~/Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh"))
BEGIN, END = "# >>> boxctl managed >>>", "# <<< boxctl managed <<<"

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"
ok, bad, warn = f"{G}✓{X}", f"{R}✗{X}", f"{Y}!{X}"


# ---------------------------------------------------------------- helpers
def run(cmd, timeout=25, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def meta() -> dict:
    try:
        return json.loads(META.read_text())
    except Exception:
        return {}


def key_remaining_h() -> float | None:
    """Hours left on the minted session key (None if we have no metadata)."""
    m = meta()
    if not m.get("expires_at"):
        return None
    return (m["expires_at"] - time.time()) / 3600.0


def passkey_remaining_days() -> float | None:
    """Days left on the server-enforced Secure Enclave authorization."""
    expires = meta().get("passkey_expires_at")
    if not expires:
        return None
    return (expires - time.time()) / 86400.0


def passkey_authorization(pub: str, expires_at: float) -> str:
    """Build an authorized_keys line with an OpenSSH-enforced UTC expiry."""
    fields = pub.split()
    if len(fields) < 2:
        raise RuntimeError("invalid Secretive public key")
    stamp = time.strftime("%Y%m%d%H%M%SZ", time.gmtime(expires_at))
    return f'expiry-time="{stamp}" {fields[0]} {fields[1]} boxctl-passkey'


def session_authorization(pub: str, expires_at: float) -> str:
    """Build a server-expiring authorized_keys line for a silent session key."""
    stamp = time.strftime("%Y%m%d%H%M%SZ", time.gmtime(expires_at))
    return f'expiry-time="{stamp}" {pub}'


def passkey_ready() -> bool:
    return SECRETIVE_SOCK.exists()


def ssh_works(timeout=15) -> tuple[bool, str]:
    # Background checks must never reach Secretive and trigger Touch ID.
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
             "-o", f"ConnectTimeout={timeout}", ALIAS, "echo BOXCTL_OK"],
            timeout=timeout + 10)
    return ("BOXCTL_OK" in r.stdout), (r.stderr.strip().splitlines() or [""])[-1]


def port_open(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", p)) == 0


def serve_health() -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8011/health", timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# ---------------------------------------------------------------- ssh config
def write_ssh_alias(identity: str | None = None, agent_sock: str | None = None) -> None:
    """Idempotently (re)write our managed block with THREE aliases:

        box         AUTO — direct LAN when you're on the same network (fast,
                    no cloudflared), else the ssh domain. ssh takes the FIRST
                    value it obtains for a keyword, so the `Match exec` LAN
                    probe below wins when it succeeds and is ignored otherwise.
        box-lan     force local network
        box-remote  force ssh domain (use this from outside)

    Uses an ABSOLUTE cloudflared path — VS Code launched from Finder has a
    minimal PATH and cannot find a bare `cloudflared`."""
    m = meta()
    # Prefer the mDNS/Bonjour name (avahi runs on the box): it keeps working when
    # DHCP moves the box to a new IP, and resolves in ~10ms on the LAN.
    lan = m.get("lan_name") or m.get("lan_host") or LAN_HOST_DEFAULT
    proxy = f"{CLOUDFLARED} access ssh --hostname {HOST}"
    lines = [BEGIN,
             "# `box` picks the LAN automatically when reachable, else the domain.",
             f'Match host {ALIAS} exec "nc -z -G 1 -w 1 {lan} 22 >/dev/null 2>&1"',
             f"    HostName {lan}",
             "    ProxyCommand none",
             "",
             f"Host {ALIAS} {ALIAS}-remote",
             f"    HostName {HOST}",
             f"    ProxyCommand {proxy}",
             "",
             f"Host {ALIAS}-lan",
             f"    HostName {lan}",
             "",
             f"Host {ALIAS} {ALIAS}-lan {ALIAS}-remote",
             f"    User {USER}"]
    # ORDER MATTERS. The session key goes FIRST: it is silent, so background work
    # (tunnel keeper reconnects, scripts, VS Code) never triggers a biometric
    # prompt. The Secure Enclave key is the FALLBACK — used only when the session
    # key is missing/expired, and to renew it. Listing the passkey first caused a
    # Touch ID popup storm: every launchd-driven reconnect demanded a signature.
    if identity:
        lines += [f"    IdentityFile {identity}"]
    if agent_sock:
        lines += [f"    IdentityAgent {agent_sock}"]          # Touch ID key via Secretive
        # With IdentitiesOnly=yes ssh ignores arbitrary agent keys, so the enclave
        # key must be named explicitly by its PUBLIC key file — ssh then asks the
        # agent for the matching private key.
        if PASSKEY_PUB.exists():
            lines += [f"    IdentityFile {PASSKEY_PUB}"]
    if identity or agent_sock:
        lines += ["    IdentitiesOnly yes"]
    lines += ["    StrictHostKeyChecking accept-new",
              "    ControlMaster auto", "    ControlPath ~/.ssh/cm-%r@%h:%p",
              "    ControlPersist 12h", "    ServerAliveInterval 15",
              "    ServerAliveCountMax 3", "    TCPKeepAlive yes", END, ""]
    block = "\n".join(lines)
    SSH_CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
    cur = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
    if BEGIN in cur and END in cur:
        cur = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", cur, flags=re.S)
    # our block must precede any other `Host box` so ours wins (ssh: first match)
    SSH_CONFIG.write_text(block + cur)
    SSH_CONFIG.chmod(0o600)


def disable_foreign_box_blocks() -> int:
    """Comment out OTHER `Host box` definitions (e.g. box-mcp's) so they cannot
    silently point ssh at an expired key. Returns how many were disabled."""
    if not SSH_CONFIG.exists():
        return 0
    text = SSH_CONFIG.read_text()
    ours_start = text.find(BEGIN)
    ours_end = text.find(END)
    out, n, i = [], 0, 0
    for block in re.split(r"(?m)^(?=Host )", text):
        start = i
        i += len(block)
        if not block.startswith(f"Host {ALIAS}"):
            out.append(block)
            continue
        if ours_start <= start <= (ours_end if ours_end > 0 else start):
            out.append(block)                     # our own block
            continue
        out.append("".join("#boxctl-disabled " + ln + "\n" if ln.strip() and
                           not ln.startswith("#boxctl-disabled") else ln + "\n"
                           for ln in block.splitlines()))
        n += 1
    if n:
        SSH_CONFIG.write_text("".join(out))
    return n


# ---------------------------------------------------------------- auth: totp
def mint_session_key(password: str, totp: str, force_remote: bool = False) -> str:
    """Authenticate with password+TOTP, then install a FRESH local key into the
    box's authorized_keys with a TTL marker. Mirrors the 24h-session approach."""
    import pexpect

    CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # 1. fresh keypair (never reuse — a new one each mint)
    if KEY.exists():
        KEY.unlink()
    pub = KEY.with_suffix(".pub")
    if pub.exists():
        pub.unlink()
    r = run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
             f"boxctl-{int(time.time())}", "-f", str(KEY)], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {r.stderr.strip()[:200]}")
    KEY.chmod(0o600)
    pubtext = pub.read_text().strip()
    exp = time.time() + TTL_HOURS * 3600
    session_line = session_authorization(pubtext, exp)

    # 2. Use native OpenSSH for keyboard-interactive auth. Paramiko is rejected
    # before authentication by some newer OpenSSH server configurations.
    # Prefer direct LAN (including Bonjour/IPv6); use cloudflared outside.
    lan = meta().get("lan_name") or meta().get("lan_host") or LAN_HOST_DEFAULT
    ssh_args = ["-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "PreferredAuthentications=keyboard-interactive,password",
                "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=20"]
    if not force_remote and lan and lan_reachable(lan):
        target = f"{USER}@{lan}"
    else:
        target = f"{USER}@{HOST}"
        ssh_args += ["-o", f"ProxyCommand={CLOUDFLARED} access ssh --hostname {HOST}"]
    install_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && "
        "sed -i '/boxctl-[0-9][0-9]*$/d' ~/.ssh/authorized_keys && "
        f"printf '%s\\n' {json.dumps(session_line)} >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    passkey_expiry = None
    if PASSKEY_PUB.exists():
        passkey_expiry = time.time() + PASSKEY_DAYS * 86400
        passkey_line = passkey_authorization(PASSKEY_PUB.read_text().strip(), passkey_expiry)
        install_cmd += (" && sed -i '/boxctl-passkey/d' ~/.ssh/authorized_keys && "
                        f"printf '%s\\n' {json.dumps(passkey_line)} "
                        ">> ~/.ssh/authorized_keys")
    install_cmd += " && echo INSTALLED"
    child = pexpect.spawn("ssh", [*ssh_args, target, install_cmd],
                          encoding="utf-8", timeout=60)
    transcript = ""
    try:
        for _ in range(8):
            matched = child.expect([
                r"(?i)password[^:\r\n]*:",
                r"(?i)(verification|totp|token|code)[^:\r\n]*:",
                r"(?i)are you sure you want to continue connecting[^?]*\?",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ])
            transcript += child.before or ""
            if matched == 0:
                child.sendline(password)
            elif matched == 1:
                child.sendline(totp)
            elif matched == 2:
                child.sendline("yes")
            elif matched == 3:
                break
            else:
                raise RuntimeError("SSH authentication timed out")
        child.close()
        if "INSTALLED" not in transcript or child.exitstatus != 0:
            raise RuntimeError("authentication failed (check password / TOTP)")
    finally:
        if child.isalive():
            child.close(force=True)

    m = meta()
    m.update({"expires_at": exp, "minted_at": time.time(),
              "ttl_hours": TTL_HOURS, "host": HOST, "user": USER,
              "method": "totp", "pub": pubtext})
    if passkey_expiry is not None:
        m.update({"passkey": True, "passkey_expires_at": passkey_expiry,
                  "passkey_days": PASSKEY_DAYS})
    META.write_text(json.dumps(m, indent=2))
    META.chmod(0o600)
    write_ssh_alias(identity=str(KEY),
                    agent_sock=str(SECRETIVE_SOCK) if passkey_expiry is not None else None)
    disable_foreign_box_blocks()
    return f"session key installed, valid {TTL_HOURS}h"


# ---------------------------------------------------------------- auth: passkey
def setup_passkey() -> int:
    """Install Secretive (Secure Enclave SSH agent) and authorize its key on the
    box for a sliding 30-day period."""
    print(f"{B}== Touch ID / Secure Enclave key setup =={X}")
    if not pathlib.Path("/Applications/Secretive.app").exists():
        print("Secretive not found — installing (Homebrew)…")
        r = run(["brew", "install", "--cask", "secretive"], timeout=600)
        if r.returncode != 0:
            print(f"{bad} brew install failed:\n{r.stderr[-500:]}")
            print("   Install manually: https://github.com/maxgoedjen/secretive/releases")
            return 1
        print(f"{ok} Secretive installed")

    if not SECRETIVE_SOCK.exists():
        print(f"\n{warn} Finish this in the Secretive app (one time, needs the GUI):")
        print("   1. Secretive is opening now")
        print("   2. Create a new secret  →  name it 'box'  →  tick "
              "'Require authentication before use' (that is the Touch ID prompt)")
        print("   3. Re-run:  boxctl setup-passkey")
        run(["open", "-a", "Secretive"], timeout=20)
        return 2

    env = dict(os.environ, SSH_AUTH_SOCK=str(SECRETIVE_SOCK))
    keys = run(["ssh-add", "-L"], timeout=15, env=env)
    pubs = [ln for ln in keys.stdout.splitlines() if ln.startswith(("ssh-", "ecdsa-"))]
    if not pubs:
        print(f"{bad} Secretive agent has no keys yet — create one in the app, then re-run.")
        run(["open", "-a", "Secretive"], timeout=20)
        return 2
    pub = pubs[0]
    print(f"{ok} Secure Enclave key: {pub.split()[0]} …{pub.split()[1][-12:]}")

    good, err = ssh_works()
    if not good:
        print(f"{bad} Need a working connection to authorize the key. Run "
              f"`boxctl connect --totp` first.\n   ssh said: {err}")
        return 1
    passkey_expiry = time.time() + PASSKEY_DAYS * 86400
    passkey_line = passkey_authorization(pub, passkey_expiry)
    cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
           "sed -i '/boxctl-passkey/d' ~/.ssh/authorized_keys; "
           f"printf '%s\\n' {json.dumps(passkey_line)} >> ~/.ssh/authorized_keys; "
           "chmod 600 ~/.ssh/authorized_keys; echo DONE")
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
             ALIAS, cmd], timeout=40)
    if "DONE" not in r.stdout:
        print(f"{bad} authorize failed: {r.stderr.strip()[:200]}")
        return 1
    PASSKEY_PUB.write_text(pub + "\n")          # named identity for IdentitiesOnly
    PASSKEY_PUB.chmod(0o644)
    write_ssh_alias(identity=str(KEY) if KEY.exists() else None,
                    agent_sock=str(SECRETIVE_SOCK))
    disable_foreign_box_blocks()
    m = meta(); m.update({"passkey": True, "passkey_pub": pub,
                          "passkey_expires_at": passkey_expiry,
                          "passkey_days": PASSKEY_DAYS})
    META.write_text(json.dumps(m, indent=2))
    META.chmod(0o600)
    print(f"{ok} Touch ID key authorized on the box for {PASSKEY_DAYS} days.")
    print("   Each successful Touch ID renewal extends that authorization.")
    return 0


# ---------------------------------------------------------------- tunnels
def tunnel_proc() -> list[str]:
    r = run(["pgrep", "-f", "boxctl-tunnel"], timeout=10)
    return [p for p in r.stdout.split() if p]


def tunnel_start() -> int:
    # Require a live KEEPER, not just an open port: an orphaned ssh child keeps
    # the port open with nothing to restart it when the link drops.
    if all(port_open(l) for l, _ in TUNNELS) and tunnel_proc():
        print(f"{ok} tunnels already up (keeper alive)")
        return 0
    if all(port_open(l) for l, _ in TUNNELS) and not tunnel_proc():
        print(f"{warn} ports open but no keeper — adopting (restarting cleanly)")
        tunnel_stop()
        time.sleep(1)
    fwd = []
    for lp, rp in TUNNELS:
        fwd += ["-L", f"{lp}:127.0.0.1:{rp}"]
    # keeper loop: reconnects on drop; stale links die in ~30s via ServerAlive
    script = (f"while :; do /usr/bin/ssh -N -o ExitOnForwardFailure=yes "
              f"-o ServerAliveInterval=10 -o ServerAliveCountMax=2 -o TCPKeepAlive=yes "
              f"-o BatchMode=yes -o IdentityAgent=none "
              f"{' '.join(fwd)} {ALIAS} >/dev/null 2>&1; sleep 3; done")
    subprocess.Popen(["/bin/bash", "-c", f"exec -a boxctl-tunnel /bin/bash -c {json.dumps(script)}"],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(1)
        if all(port_open(l) for l, _ in TUNNELS):
            print(f"{ok} tunnels up: " + ", ".join(f":{l}" for l, _ in TUNNELS))
            return 0
    print(f"{bad} tunnels did not come up — try `boxctl doctor`")
    return 1


def tunnel_stop() -> int:
    n = 0
    for pid in tunnel_proc():
        try:
            os.kill(int(pid), 9); n += 1
        except Exception:
            pass
    run(["pkill", "-f", f"ssh -N .*{ALIAS}"], timeout=10)
    print(f"{ok} stopped {n} tunnel keeper(s)")
    return 0


# ---------------------------------------------------------------- commands
def lan_reachable(host: str | None = None, timeout=1.5) -> bool:
    host = host or meta().get("lan_host") or LAN_HOST_DEFAULT
    if not host:
        return False
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except OSError:
        return False


def detect_lan_host() -> str | None:
    """Ask the box (over whatever route works) for its current LAN address and
    remember it — DHCP can move it."""
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
             f"{ALIAS}-remote", "hostname -I"], timeout=40)
    if r.returncode != 0:
        r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                 ALIAS, "hostname -I"], timeout=40)
    for ip in r.stdout.split():
        if ip.startswith(("192.168.", "10.")) and lan_reachable(ip):
            m = meta(); m["lan_host"] = ip
            META.write_text(json.dumps(m, indent=2))
            return ip
    return None


def route_timing() -> list[tuple[str, bool, float]]:
    """(alias, works, seconds) for each explicit route — so you can see which is fast."""
    out = []
    for alias in (f"{ALIAS}-lan", f"{ALIAS}-remote"):
        t0 = time.time()
        r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                 "-o", "ConnectTimeout=8",
                 alias, "echo OK"], timeout=45)
        out.append((alias, "OK" in r.stdout, time.time() - t0))
    return out


def cmd_route(args) -> int:
    if args.action == "detect":
        ip = detect_lan_host()
        print(f"{ok} LAN host = {ip}" if ip else f"{bad} could not detect a LAN address")
        if ip:
            write_ssh_alias(identity=str(KEY) if KEY.exists() else None,
                            agent_sock=str(SECRETIVE_SOCK) if passkey_ready() else None)
        return 0 if ip else 1
    print(f"{B}== routes =={X}")
    lan = meta().get("lan_host") or LAN_HOST_DEFAULT
    print(f"  lan     {lan}  (direct, fast — same network only)")
    print(f"  remote  {HOST}  (cloudflared — use from outside)")
    print(f"  auto    `ssh {ALIAS}` picks LAN when reachable, else remote\n")
    for alias, good, secs in route_timing():
        print(f"  {alias:12s}{ok if good else bad} {secs:.1f}s")
    return 0


def cmd_status(args) -> int:
    print(f"{B}== box status =={X}")
    lan = meta().get("lan_host") or LAN_HOST_DEFAULT
    on_lan = lan_reachable(lan)
    print(f"  route       {ok} " + (f"LAN {lan} (direct, fast)" if on_lan
                                    else f"remote {HOST} (cloudflared)"))
    rem = key_remaining_h()
    if passkey_ready() and meta().get("passkey"):
        days = passkey_remaining_days()
        if days is None:
            print(f"  auth        {warn} Touch ID needs 30-day expiry migration")
        elif days > 0:
            print(f"  auth        {ok} Touch ID {days:.1f}d left (renews on use)")
        else:
            print(f"  auth        {bad} Touch ID EXPIRED → password + TOTP required")
    if rem is None:
        print(f"  session key {warn} none minted by boxctl")
    elif rem > 0:
        print(f"  session key {ok} {rem:.1f}h left")
    else:
        print(f"  session key {bad} EXPIRED {abs(rem):.1f}h ago  → `boxctl connect`")
    good, err = ssh_works()
    print(f"  ssh {ALIAS:8s}{ok if good else bad} " + ("reachable" if good else err[:70]))
    for lp, _ in TUNNELS:
        print(f"  tunnel :{lp:<5}{ok if port_open(lp) else bad} "
              + ("open" if port_open(lp) else "closed"))
    if not args.quick:
        h = serve_health()
        print(f"  omni serve  {ok if h.get('ok') else bad} "
              + (f"ok, {h.get('vram_gb')}GB VRAM" if h.get("ok") else "unreachable"))
    if good and not args.quick:
        g = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                 "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader"],
                timeout=25)
        if g.stdout.strip():
            print(f"  gpu         {ok} {g.stdout.strip()}")
    return 0


def renew_via_passkey() -> str:
    """Mint a fresh silent 24h session key while authenticated by the Secure
    Enclave key. ONE Touch ID prompt, then a day of prompt-free background work
    (tunnels, VS Code, scripts) — and no TOTP ever."""
    CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for f in (KEY, KEY.with_suffix(".pub")):
        if f.exists():
            f.unlink()
    r = run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
             f"boxctl-{int(time.time())}", "-f", str(KEY)], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {r.stderr.strip()[:200]}")
    KEY.chmod(0o600)
    pubtext = KEY.with_suffix(".pub").read_text().strip()
    session_expiry = time.time() + TTL_HOURS * 3600
    session_line = session_authorization(pubtext, session_expiry)
    passkey_pub = PASSKEY_PUB.read_text().strip()
    passkey_expiry = time.time() + PASSKEY_DAYS * 86400
    passkey_line = passkey_authorization(passkey_pub, passkey_expiry)
    # authenticate with the passkey (Touch ID) and install the new key
    cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
           "sed -i '/boxctl-/d;/boxctl-passkey/d' ~/.ssh/authorized_keys 2>/dev/null; "
           f"printf '%s\\n' {json.dumps(session_line)} >> ~/.ssh/authorized_keys && "
           f"printf '%s\\n' {json.dumps(passkey_line)} >> ~/.ssh/authorized_keys && "
           "chmod 600 ~/.ssh/authorized_keys && echo INSTALLED")
    env = dict(os.environ, SSH_AUTH_SOCK=str(SECRETIVE_SOCK))
    r = run(["ssh", "-o", "ConnectTimeout=25", "-o", f"IdentityFile={PASSKEY_PUB}",
             "-o", "IdentitiesOnly=yes", ALIAS, cmd], timeout=120, env=env)
    if "INSTALLED" not in r.stdout:
        raise RuntimeError(f"install failed: {(r.stderr or r.stdout).strip()[:200]}")
    m = meta()
    m.update({"expires_at": session_expiry, "minted_at": time.time(),
              "ttl_hours": TTL_HOURS, "method": "passkey", "pub": pubtext,
              "passkey_expires_at": passkey_expiry, "passkey_days": PASSKEY_DAYS})
    META.write_text(json.dumps(m, indent=2))
    META.chmod(0o600)
    write_ssh_alias(identity=str(KEY), agent_sock=str(SECRETIVE_SOCK))
    return (f"renewed via Touch ID — silent for {TTL_HOURS}h; "
            f"Touch ID valid {PASSKEY_DAYS}d")


def cmd_connect(args) -> int:
    rem = key_remaining_h()
    pass_days = passkey_remaining_days()
    if (not args.totp and passkey_ready() and meta().get("passkey")
            and pass_days is not None and pass_days > 0):
        if not args.touch_id and rem is not None and rem > 0.5:
            good, err = ssh_works()
            if good:
                print(f"{ok} already connected (session key: {rem:.1f}h left, no prompt)")
                return 0
        try:
            print("   renewing session key with Touch ID…")
            print(f"{ok} {renew_via_passkey()}")
            good, err = ssh_works()
            print(f"   verify: {ok if good else bad} " + ("ssh box works silently" if good else err[:70]))
            return 0 if good else 1
        except Exception as e:  # noqa: BLE001
            if args.touch_id:
                print(f"{bad} Touch ID renewal failed ({e}) — use password + TOTP")
                return 1
            print(f"{warn} passkey renew failed ({e}) — falling back to TOTP")
    elif not args.totp and meta().get("passkey"):
        if args.touch_id:
            print(f"{bad} Touch ID authorization expired — use password + TOTP")
            return 1
        print(f"{warn} Touch ID authorization expired or needs migration — use TOTP")
    print(f"{B}== box auth (password + TOTP) =={X}")
    route = HOST if args.remote else (meta().get("lan_name") or
                                      meta().get("lan_host") or LAN_HOST_DEFAULT or HOST)
    print(f"   host {USER}@{route}   ttl {TTL_HOURS}h")
    if args.stdin_json:
        try:
            credentials = json.load(sys.stdin)
            pw = str(credentials["password"])
            code = str(credentials["totp"]).strip()
        except Exception as e:
            print(f"{bad} invalid credentials input: {e}")
            return 2
    else:
        pw = getpass.getpass("   password: ")
        code = input("   TOTP code: ").strip()
    try:
        print("   authenticating…")
        msg = mint_session_key(pw, code, force_remote=args.remote)
    except Exception as e:  # noqa: BLE001
        print(f"{bad} {e}")
        return 1
    print(f"{ok} {msg}")
    good, err = ssh_works()
    print(f"   verify: {ok if good else bad} " + ("ssh box works" if good else err[:70]))
    if good and not meta().get("passkey"):
        print(f"\n{Y}tip{X}: run `boxctl setup-passkey` once — Touch ID then renews "
              f"its 30-day authorization whenever you use it.")
    return 0 if good else 1


def cmd_doctor(_args) -> int:
    print(f"{B}== boxctl doctor =={X}")
    fixed = []
    if not pathlib.Path(CLOUDFLARED).exists():
        print(f"{bad} cloudflared missing at {CLOUDFLARED} — `brew install cloudflared`")
        return 1
    print(f"{ok} cloudflared {CLOUDFLARED}")
    txt = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
    if BEGIN not in txt:
        write_ssh_alias(identity=str(KEY) if KEY.exists() else None,
                        agent_sock=str(SECRETIVE_SOCK) if passkey_ready() else None)
        fixed.append("wrote boxctl ssh block")
    if re.search(r"^\s*ProxyCommand cloudflared ", txt, re.M):
        write_ssh_alias(identity=str(KEY) if KEY.exists() else None,
                        agent_sock=str(SECRETIVE_SOCK) if passkey_ready() else None)
        fixed.append("made cloudflared path absolute (VS Code needs this)")
    n = disable_foreign_box_blocks()
    if n:
        fixed.append(f"disabled {n} conflicting `Host {ALIAS}` block(s)")
    rem = key_remaining_h()
    if (rem is not None and rem <= 0) and not (passkey_ready() and meta().get("passkey")):
        print(f"{bad} session key expired → run `boxctl connect`")
    good, err = ssh_works()
    if not good:
        print(f"{bad} ssh failing: {err[:90]}")
        if "publickey" in err:
            print("   → key rejected/expired. `boxctl connect` (or `boxctl setup-passkey`)")
    else:
        print(f"{ok} ssh works")
        if not all(port_open(l) for l, _ in TUNNELS):
            tunnel_start(); fixed.append("restarted tunnels")
    for f in fixed:
        print(f"{ok} fixed: {f}")
    if not fixed:
        print(f"{ok} nothing to fix")
    return 0


def cmd_tunnel(args) -> int:
    if args.action == "start":
        return tunnel_start()
    if args.action == "stop":
        return tunnel_stop()
    for lp, _ in TUNNELS:
        print(f"  :{lp:<6}{ok if port_open(lp) else bad}")
    print(f"  keeper  {len(tunnel_proc())} process(es)")
    return 0


def cmd_code(args) -> int:
    good, err = ssh_works()
    if not good:
        print(f"{bad} ssh not working ({err[:60]}) — run `boxctl connect` first")
        return 1
    path = args.path or f"/home/{USER}"
    code = shutil.which("code") or os.path.expanduser("~/.local/bin/code")
    r = run([code, "--folder-uri", f"vscode-remote://ssh-remote+{ALIAS}{path}"], timeout=30)
    if r.returncode != 0:
        print(f"{bad} {r.stderr.strip()[:200]}")
        return 1
    print(f"{ok} VS Code opening {ALIAS}:{path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="boxctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status")
    s.add_argument("--quick", action="store_true", help="skip service and GPU probes")
    s.set_defaults(fn=cmd_status)
    c = sub.add_parser("connect"); c.add_argument("--totp", action="store_true",
                                                  help="force password+TOTP")
    c.add_argument("--remote", action="store_true",
                   help="bootstrap through cloudflared even when LAN is reachable")
    c.add_argument("--touch-id", action="store_true",
                   help="force Touch ID renewal; never fall back to an interactive TOTP prompt")
    c.add_argument("--stdin-json", action="store_true", help=argparse.SUPPRESS)
    c.set_defaults(fn=cmd_connect)
    sub.add_parser("setup-passkey").set_defaults(fn=lambda a: setup_passkey())
    t = sub.add_parser("tunnel"); t.add_argument("action", nargs="?", default="status",
                                                 choices=["start", "stop", "status"])
    t.set_defaults(fn=cmd_tunnel)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    rt = sub.add_parser("route", help="show/measure LAN vs remote routes")
    rt.add_argument("action", nargs="?", default="show", choices=["show", "detect"])
    rt.set_defaults(fn=cmd_route)
    k = sub.add_parser("code"); k.add_argument("path", nargs="?"); k.set_defaults(fn=cmd_code)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
