#!/usr/bin/env python3
"""boxctl — one command for the RTX box: auth, tunnels, health, VS Code.

Standalone: does NOT depend on the box-mcp server. It mints its own 24h SSH
session key (same idea, own implementation) and can additionally install a
Touch ID / Secure Enclave key so day-to-day connections need no TOTP at all.

Auth methods
  passkey  Secure Enclave key (Secretive) — Touch ID, sliding 30-day authorization.
           Preferred once set up: nothing secret is stored on disk; use renews it.
  password password plus TOTP when configured -> mints a 24h key. Kept as the bootstrap /
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
import base64
import getpass
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
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
_HELPERS = pathlib.Path(sys.executable).resolve().parent
CLOUDFLARED = (os.environ.get("BOX_CLOUDFLARED") or shutil.which("cloudflared") or
                (str(_HELPERS / "cloudflared") if (_HELPERS / "cloudflared").exists()
                 else "/opt/homebrew/bin/cloudflared"))
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
GUI_REGISTRY = CFG_DIR / "gui-sessions.json"
FORWARDS_FILE = CFG_DIR / "forwards.json"
GUI_LOG_LIMIT = 1_000_000
REMOTE_GUI_STATE = ".local/state/boxdeck/xpra"

G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"
ok, bad, warn = f"{G}✓{X}", f"{R}✗{X}", f"{Y}!{X}"


# ---------------------------------------------------------------- helpers
def run(cmd, timeout=25, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def rotate_log(path: pathlib.Path, limit: int = GUI_LOG_LIMIT, backups: int = 2) -> None:
    """Bound a log without unlinking a file that a live helper may still hold."""
    try:
        if path.stat().st_size <= limit:
            return
    except FileNotFoundError:
        return
    for n in range(backups, 0, -1):
        old = path.with_name(path.name + f".{n}")
        newer = path.with_name(path.name + f".{n + 1}")
        if old.exists():
            if n == backups:
                old.unlink()
            else:
                old.replace(newer)
    # Copy the useful tail, then truncate the original inode in case it is open.
    tail = path.read_bytes()[-limit:]
    path.with_name(path.name + ".1").write_bytes(tail)
    with path.open("r+b") as stream:
        stream.truncate(0)


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


def preferred_lan_host() -> str:
    """Return a reachable IPv4 LAN endpoint when Bonjour also advertises IPv6.

    OpenSSH may select a broken link-local AAAA record first and never retry the
    healthy A record.  Resolve configured names ourselves and pin the reachable
    IPv4 address in the generated alias; keep the configured name as fallback.
    """
    m = meta()
    configured = [m.get("lan_host"), m.get("lan_name"), LAN_HOST_DEFAULT]
    seen: set[str] = set()
    for host in filter(None, configured):
        try:
            candidates = [item[4][0] for item in socket.getaddrinfo(
                host, 22, socket.AF_INET, socket.SOCK_STREAM)]
        except OSError:
            candidates = []
        for candidate in candidates:
            if candidate not in seen and lan_reachable(candidate):
                return candidate
            seen.add(candidate)
    return next((host for host in configured if host), "")


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


def saved_forwards() -> list[dict]:
    try:
        value = json.loads(FORWARDS_FILE.read_text())
        return value if isinstance(value, list) else []
    except Exception:
        return []


def tunnel_pairs() -> list[tuple[int, int]]:
    return TUNNELS + [(int(x["local_port"]), int(x["remote_port"]))
                      for x in saved_forwards()]


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
    lan = preferred_lan_host()
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
    lan = preferred_lan_host()
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
    pairs = tunnel_pairs()
    # Require a live KEEPER, not just an open port: an orphaned ssh child keeps
    # the port open with nothing to restart it when the link drops.
    if all(port_open(l) for l, _ in pairs) and tunnel_proc():
        print(f"{ok} tunnels already up (keeper alive)")
        return 0
    if all(port_open(l) for l, _ in pairs) and not tunnel_proc():
        print(f"{warn} ports open but no keeper — adopting (restarting cleanly)")
        tunnel_stop()
        time.sleep(1)
    fwd = []
    for lp, rp in pairs:
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
        if all(port_open(l) for l, _ in pairs):
            print(f"{ok} tunnels up: " + ", ".join(f":{l}" for l, _ in pairs))
            return 0
    print(f"{bad} tunnels did not come up — try `boxctl doctor`")
    return 1


def cmd_forward(args) -> int:
    forwards = saved_forwards()
    if args.action == "list":
        for item in forwards:
            item["active"] = port_open(int(item["local_port"]))
            item["url"] = f"http://127.0.0.1:{int(item['local_port'])}"
        print(json.dumps(forwards))
        return 0
    local, remote = int(args.local_port or 0), int(args.remote_port or 0)
    if args.action == "add":
        if not 1024 <= local <= 65535 or not 1 <= remote <= 65535:
            print(f"{bad} Mac port must be 1024-65535 and box port 1-65535")
            return 2
        reserved = {p for p, _ in TUNNELS}
        if local in reserved or any(int(x["local_port"]) == local for x in forwards):
            print(f"{bad} Mac port {local} is already configured")
            return 2
        forwards.append({"local_port": local, "remote_port": remote})
    else:
        before = len(forwards)
        forwards = [x for x in forwards if int(x["local_port"]) != local]
        if len(forwards) == before:
            print(f"{bad} Mac port {local} is not configured")
            return 1
    CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    FORWARDS_FILE.write_text(json.dumps(forwards, indent=2) + "\n"); FORWARDS_FILE.chmod(0o600)
    tunnel_stop(); time.sleep(.3)
    result = tunnel_start()
    if result == 0:
        verb = "forwarding" if args.action == "add" else "removed"
        print(f"{ok} {verb} 127.0.0.1:{local}" + (f" → box:{remote}" if args.action == "add" else ""))
    return result


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
    lan = preferred_lan_host()
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
            print(f"  auth        {bad} Touch ID EXPIRED → password login required")
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
                print(f"{bad} Touch ID renewal failed ({e}) — use password login")
                return 1
            print(f"{warn} passkey renew failed ({e}) — falling back to TOTP")
    elif not args.totp and meta().get("passkey"):
        if args.touch_id:
            print(f"{bad} Touch ID authorization expired — use password login")
            return 1
        print(f"{warn} Touch ID authorization expired or needs migration — use TOTP")
    print(f"{B}== box auth (password + optional TOTP) =={X}")
    route = HOST if args.remote else (preferred_lan_host() or HOST)
    print(f"   host {USER}@{route}   ttl {TTL_HOURS}h")
    if args.stdin_json:
        try:
            credentials = json.load(sys.stdin)
            pw = str(credentials["password"])
            code = str(credentials.get("totp", "")).strip()
        except Exception as e:
            print(f"{bad} invalid credentials input: {e}")
            return 2
    else:
        pw = getpass.getpass("   password: ")
        code = input("   TOTP code (optional; press Enter if disabled): ").strip()
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


# ---------------------------------------------------------------- seamless GUI apps
def xpra_binary() -> str | None:
    """Find the native macOS Xpra client installed by its PKG/DMG or Homebrew."""
    # The Homebrew symlink cannot find its adjacent Contents/Resources bundle;
    # invoke the application executable by its real path on macOS.
    bundled = _HELPERS / "Xpra.app/Contents/MacOS/Xpra"
    candidates = [str(bundled), "/Applications/Xpra.app/Contents/MacOS/Xpra",
                  shutil.which("xpra")]
    return next((p for p in candidates if p and pathlib.Path(p).exists()), None)


def xpra_client_env() -> dict[str, str]:
    """Use linear, Mac-like trackpad deltas at a tunable remote-app scale."""
    env = dict(os.environ)
    env["XPRA_SMOOTH_SCROLL_NORM"] = "100"
    env["XPRA_MOUSE_SCROLL_SQRT_SCALE"] = "0"
    env["XPRA_SMOOTH_SCROLL_SCALE"] = os.environ.get(
        "BOXCTL_SCROLL_PERCENT", str(_C.get("scroll_percent", 25)))
    return env


def gui_scroll_sensitivity(value: int | None = None) -> int:
    """Read or persist remote-app scroll sensitivity as a percentage."""
    config_path = CFG_DIR / "config.json"
    config = _cfg()
    if value is not None:
        value = max(5, min(200, int(value)))
        config["scroll_percent"] = value
        CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        config_path.chmod(0o600)
    current = int(config.get("scroll_percent", 25))
    print(json.dumps({"scroll_percent": current}))
    return 0


def gui_apps() -> tuple[int, str]:
    """Return launchable freedesktop entries as JSON without executing them."""
    server = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                  "~/.local/bin/boxserver apps"], timeout=45)
    if server.returncode == 0 and server.stdout.lstrip().startswith("["):
        return 0, server.stdout
    # Compatibility fallback for boxes not enrolled with boxserver yet.
    script = r'''
import base64, configparser, glob, json, os
roots = ["/usr/share/applications", os.path.expanduser("~/.local/share/applications")]
apps = {}
icon_files = []
for icon_root in (os.path.expanduser("~/.local/share/icons"), "/usr/share/icons/hicolor", "/usr/share/icons", "/usr/share/pixmaps"):
    for folder, _, files in os.walk(icon_root):
        icon_files += [os.path.join(folder, f) for f in files if f.rsplit(".", 1)[-1].lower() in ("png", "svg", "xpm")]
def icon_data(value):
    if not value: return ""
    candidates = [value] if os.path.isabs(value) else []
    candidates += [p for p in icon_files if os.path.splitext(os.path.basename(p))[0] == value]
    candidates.sort(key=lambda p: ("/64x64/" not in p, "/48x48/" not in p, "/scalable/" not in p, len(p)))
    for path in candidates:
        try:
            data = open(path, "rb").read()
            if len(data) <= 512000: return base64.b64encode(data).decode()
        except OSError: pass
    return ""
for root in roots:
    for path in glob.glob(root + "/*.desktop"):
        c = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            c.read(path, encoding="utf-8")
            d = c["Desktop Entry"]
            if d.get("Type", "Application") != "Application": continue
            if d.getboolean("NoDisplay", fallback=False) or d.getboolean("Hidden", fallback=False): continue
            name, command = d.get("Name", "").strip(), d.get("Exec", "").strip()
            if not name or not command: continue
            ident = os.path.basename(path)
            icon = d.get("Icon", "").strip()
            apps[ident] = {"id": ident, "name": name,
                "detail": d.get("Comment", "").strip(), "icon": icon,
                "iconData": icon_data(icon)}
        except Exception:
            pass
print(json.dumps(sorted(apps.values(), key=lambda x: x["name"].casefold())))
'''
    payload = base64.b64encode(script.encode()).decode()
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
             f"echo {payload} | base64 -d | python3"], timeout=45)
    return r.returncode, r.stdout if r.returncode == 0 else r.stderr


def desktop_command(ident: str) -> tuple[int, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.desktop", ident):
        return 2, "invalid application id"
    # Resolve on the box, remove freedesktop field codes, and print only the Exec command.
    script = r'''
import configparser, os, re, sys
ident = sys.argv[1]
for root in (os.path.expanduser("~/.local/share/applications"), "/usr/share/applications"):
    path = os.path.join(root, ident)
    if not os.path.isfile(path): continue
    c = configparser.ConfigParser(interpolation=None, strict=False); c.read(path, encoding="utf-8")
    command = c.get("Desktop Entry", "Exec", fallback="")
    command = re.sub(r"\s*%[fFuUdDnNickvm]", "", command).replace("%%", "%").strip()
    print(command); raise SystemExit(0 if command else 2)
raise SystemExit(2)
'''
    payload = base64.b64encode(script.encode()).decode()
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
             f"echo {payload} | base64 -d | python3 - {ident}"], timeout=30)
    return r.returncode, r.stdout.strip() if r.returncode == 0 else (r.stderr.strip() or "application not found")


def _gui_registry() -> dict:
    try:
        return json.loads(GUI_REGISTRY.read_text())
    except Exception:
        return {}


def _save_gui_registry(value: dict) -> None:
    CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    GUI_REGISTRY.write_text(json.dumps(value, indent=2) + "\n")
    GUI_REGISTRY.chmod(0o600)


def _pid_command(pid: int) -> str:
    result = run(["ps", "-p", str(pid), "-o", "command="], timeout=5)
    return result.stdout.strip() if result.returncode == 0 else ""


def _stop_local_gui_clients(display: int) -> int:
    """Close every BoxDeck Xpra client attached to one remote display.

    Xpra's macOS client can leave its GTK window frozen after SIGTERM, and an
    app restart can leave an older client outside our registry.  Match both the
    bundled executable and exact display marker, then escalate only survivors.
    """
    marker = f"ssh://{ALIAS}/{int(display)}"
    executable = str(_HELPERS / "Xpra.app/Contents/MacOS/Xpra")
    rows = run(["ps", "-axo", "pid=,command="], timeout=10).stdout.splitlines()
    pids = []
    for row in rows:
        try: pid_text, command = row.strip().split(None, 1); pid = int(pid_text)
        except (ValueError, IndexError): continue
        if command.startswith(executable + " seamless ") and marker in command:
            pids.append(pid)
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    deadline = time.monotonic() + 0.75
    while time.monotonic() < deadline and any(_pid_command(pid) for pid in pids):
        time.sleep(0.05)
    for pid in pids:
        if _pid_command(pid):
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
    return len(pids)


def is_recorded_orphan_helper(pid: int, recorded_pids: list[int], command: str) -> bool:
    """Ownership guard used by cleanup: pid record plus an Xpra audio command."""
    return (pid in recorded_pids and "xpra" in command
            and ("_audio_meter" in command or "_audio_" in command))


def _remote_state_path() -> str:
    result = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                  ALIAS, "printf %s \"$HOME\""], timeout=15)
    if result.returncode or not result.stdout.startswith("/"):
        raise RuntimeError("could not resolve the box home directory")
    return result.stdout.strip() + "/" + REMOTE_GUI_STATE


REMOTE_GUI_CLEANER = r'''import json, os, shutil, signal, stat, sys, time
from pathlib import Path
state = Path(sys.argv[1]); mode = sys.argv[2]
sessions = state / "owned"; sessions.mkdir(parents=True, exist_ok=True)

def cmdline(pid):
    try: return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError: return ""

def children(root):
    parents = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit(): continue
        try:
            bits = (proc / "stat").read_text().split()
            parents[int(proc.name)] = int(bits[3])
        except (OSError, ValueError, IndexError): pass
    found, todo = [], [root]
    while todo:
        parent = todo.pop()
        for pid, ppid in list(parents.items()):
            if ppid == parent and pid not in found:
                found.append(pid); todo.append(pid)
    return found

removed=[]; active=[]
for record in sessions.glob("*.json"):
    try: data=json.loads(record.read_text()); pid=int(data["server_pid"]); marker=data["session_id"]
    except Exception:
        record.unlink(missing_ok=True); continue
    owned = f"BOXDECK_SESSION_ID={marker}" in cmdline(pid)
    display = str(data.get("display", ""))
    sockets = [state / "sessions" / display / "socket"]
    runtime_xpra = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "xpra"
    sockets += list(runtime_xpra.glob(f"*-{display}"))
    live = owned and any(p.exists() and stat.S_ISSOCK(p.stat().st_mode) for p in sockets)
    if live and mode != "all":
        helpers = children(pid)
        data["helper_pids"] = helpers
        data["helpers"] = {str(p): cmdline(p) for p in helpers if cmdline(p)}
        record.write_text(json.dumps(data))
        active.append(data.get("app", marker)); continue
    if owned:
        pids = children(pid) + [pid]
        for target in pids:
            try: os.kill(target, signal.SIGTERM)
            except ProcessLookupError: pass
        time.sleep(.25)
        for target in reversed(pids):
            try: os.kill(target, 0); os.kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
        removed.append(data.get("app", marker))
    elif not owned:
        # The server may have crashed and re-parented its audio meter to pid 1.
        # Kill only helpers whose exact pids were recorded while ownership was
        # live and whose command line is still recognizably Xpra.
        for target in data.get("helper_pids", []):
            command = cmdline(int(target))
            if "xpra" in command and ("_audio_meter" in command or "_audio_" in command):
                try: os.kill(int(target), signal.SIGTERM)
                except ProcessLookupError: pass
                removed.append(data.get("app", marker))
        for target_text, expected in data.get("helpers", {}).items():
            target = int(target_text); command = cmdline(target)
            if command and command == expected:
                try: os.kill(target, signal.SIGTERM)
                except ProcessLookupError: pass
                if data.get("app", marker) not in removed:
                    removed.append(data.get("app", marker))
    record.unlink(missing_ok=True)

usage=shutil.disk_usage(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
print(json.dumps({"removed":removed,"active":active,"free":usage.free,"total":usage.total}))
'''


def remote_gui_cleanup(all_sessions: bool = False) -> dict:
    state = _remote_state_path()
    payload = base64.b64encode(REMOTE_GUI_CLEANER.encode()).decode()
    mode = "all" if all_sessions else "stale"
    result = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                  f"echo {payload} | base64 -d | python3 - {shlex.quote(state)} {mode}"], timeout=35)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200] or "remote cleanup failed")
    return json.loads(result.stdout)


def gui_runtime_preflight() -> tuple[bool, str]:
    try:
        report = remote_gui_cleanup(False)
    except Exception as exc:
        return False, str(exc)
    minimum = 64 * 1024 * 1024
    if int(report.get("free", 0)) < minimum:
        return False, ("box runtime directory is full after stale-session cleanup "
                       f"({int(report.get('free', 0)) // 1048576} MiB free)")
    return True, ""


def _app_key(label: str, command: str) -> str:
    return hashlib.sha256((label + "\0" + command).encode()).hexdigest()[:16]


def _remote_start_app(command: str, label: str, key: str, microphone: bool) -> dict:
    state = _remote_state_path()
    session_id = f"{key}-{int(time.time())}"
    display = 200 + int(key[:8], 16) % 700
    script_data = base64.b64encode(("#!/bin/sh\nexec " + command + "\n").encode()).decode()
    # Probe forward in the unlikely event that another X server owns this display.
    for _ in range(20):
        probe = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                     ALIAS, f"timeout 3 xpra id :{display} >/dev/null 2>&1"], timeout=6)
        if probe.returncode != 0:
            break
        display += 1
    else:
        raise RuntimeError("no free BoxDeck Xpra display")
    app_script = f"{state}/commands/{session_id}.sh"
    pidfile = f"{state}/pids/{session_id}.pid"
    logfile = f"{state}/logs/{session_id}.log"
    owned = f"{state}/owned/{session_id}.json"
    mic = "on" if microphone else "disabled"
    start = (
        f"mkdir -p {shlex.quote(state)}/{{commands,pids,owned,sessions,logs}} && "
        f"echo {script_data} | base64 -d > {shlex.quote(app_script)} && chmod 700 {shlex.quote(app_script)} && "
        f"xpra start :{display} --daemon=yes --splash=no --start-child={shlex.quote(app_script)} "
        f"--exit-with-children=yes --exit-with-client=no --speaker=on --microphone={mic} "
        f"--audio-source=pulsesrc:device=Xpra-Speaker.monitor "
        f"--sessions-dir={shlex.quote(state + '/sessions')} --log-dir={shlex.quote(state + '/logs')} "
        f"--log-file={shlex.quote(logfile)} --pidfile={shlex.quote(pidfile)} "
        f"--env=BOXDECK_SESSION_ID={session_id} && "
        f"test -s {shlex.quote(pidfile)} && "
        # Truncate the same inode so an open helper cannot retain deleted data.
        f"nohup sh -c 'while kill -0 $(cat {shlex.quote(pidfile)}) 2>/dev/null; do "
        f"sleep 5; [ $(wc -c < {shlex.quote(logfile)} 2>/dev/null || echo 0) -le 1000000 ] || "
        f"truncate -s 0 {shlex.quote(logfile)}; done' >/dev/null 2>&1 &"
    )
    result = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS, start], timeout=35)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-500:] or "Xpra server did not start")
    pid_result = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                      f"cat {shlex.quote(pidfile)}"], timeout=8)
    server_pid = int(pid_result.stdout.strip())
    record = {"session_id": session_id, "app": label, "key": key, "display": display,
              "server_pid": server_pid, "logfile": logfile, "created": time.time()}
    encoded = base64.b64encode((json.dumps(record) + "\n").encode()).decode()
    saved = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                 f"echo {encoded} | base64 -d > {shlex.quote(owned)}"], timeout=8)
    if saved.returncode:
        run(["ssh", ALIAS, f"xpra stop :{display}"], timeout=10)
        raise RuntimeError("could not record BoxDeck session ownership")
    time.sleep(1)
    try: remote_gui_cleanup(False)  # capture audio/app helper pids while ownership is live
    except Exception: pass
    return record


def _stop_remote_record(record: dict) -> None:
    try:
        run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
             f"timeout 8 xpra stop :{int(record['display'])} >/dev/null 2>&1 || true"], timeout=15)
        remote_gui_cleanup(False)
    except Exception:
        pass


def gui_sessions() -> list[dict]:
    """Return live BoxDeck-owned remote sessions, including detached ones."""
    remote_gui_cleanup(False)
    state = _remote_state_path()
    result = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                  f"find {shlex.quote(state + '/owned')} -type f -name '*.json' "
                  "-maxdepth 1 -exec cat {} \\; -exec printf '\\n' \\;"], timeout=20)
    sessions = []
    registry = _gui_registry()
    clients = {str(r.get("session_id")): int(r.get("client_pid", 0) or 0)
               for r in registry.values()}
    for line in result.stdout.splitlines():
        try: record = json.loads(line)
        except json.JSONDecodeError: continue
        pid = clients.get(str(record.get("session_id")), 0)
        marker = f"ssh://{ALIAS}/{record.get('display')}"
        record["attached"] = bool(pid and marker in _pid_command(pid))
        record["client_pid"] = pid if record["attached"] else 0
        sessions.append(record)
    return sorted(sessions, key=lambda item: float(item.get("created", 0)), reverse=True)


def _find_gui_session(session_id: str) -> tuple[str, dict] | tuple[None, None]:
    for record in gui_sessions():
        if record.get("session_id") == session_id:
            return str(record.get("key", "")), record
    return None, None


def _xpra_client_args(record: dict, microphone: bool = False) -> list[str]:
    client = xpra_binary()
    if not client:
        raise RuntimeError("Xpra client is not installed on this Mac")
    return [client, "seamless", f"ssh://{ALIAS}/{record['display']}",
            "--ssh=ssh -o BatchMode=yes -o IdentityAgent=none",
            "--speaker=on", f"--microphone={'on' if microphone else 'disabled'}", "--av-sync=yes",
            "--audio-source=pulsesrc:device=Xpra-Speaker.monitor",
            "--clipboard=yes", "--clipboard-direction=both", "--notifications=yes",
            "--system-tray=yes", "--cursors=yes", "--splash=no", "--video=yes",
            "--opengl=auto", "--desktop-scaling=1", "--dpi=96"]


def gui_resume(session_id: str) -> int:
    key, record = _find_gui_session(session_id)
    if not record:
        print(f"{bad} session is no longer running")
        return 1
    if record.get("attached"):
        print(f"{ok} {record.get('app', 'application')} is already attached")
        return 0
    log = CFG_DIR / f"xpra-{key or session_id}.log"
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True); rotate_log(log)
    stream = open(log, "ab", buffering=0)
    proc = subprocess.Popen(_xpra_client_args(record), stdin=subprocess.DEVNULL,
                            stdout=stream, stderr=stream, env=xpra_client_env(),
                            start_new_session=True, close_fds=True)
    local = _gui_registry(); record["client_pid"] = proc.pid
    local[key or session_id] = record; _save_gui_registry(local)
    time.sleep(2)
    if proc.poll() is not None:
        print(f"{bad} could not resume {record.get('app', 'application')}")
        return 1
    print(f"{ok} resumed {record.get('app', 'application')}")
    return 0


def gui_detach(session_id: str) -> int:
    key, record = _find_gui_session(session_id)
    if not record:
        print(f"{bad} session is no longer running")
        return 1
    _stop_local_gui_clients(int(record["display"]))
    local = _gui_registry()
    if key in local: local[key]["client_pid"] = 0
    _save_gui_registry(local)
    print(f"{ok} detached {record.get('app', 'application')} — still running on box")
    return 0


def gui_terminate(session_id: str) -> int:
    key, record = _find_gui_session(session_id)
    if not record:
        print(f"{bad} session is no longer running")
        return 1
    _stop_local_gui_clients(int(record["display"]))
    _stop_remote_record(record)
    local = _gui_registry(); local.pop(key, None); _save_gui_registry(local)
    print(f"{ok} terminated {record.get('app', 'application')}")
    return 0


def gui_launch(command: str, label: str = "application", microphone: bool = False) -> int:
    client = xpra_binary()
    if not client:
        print(f"{bad} Xpra client is not installed on this Mac")
        return 3
    good, err = ssh_works()
    if not good:
        print(f"{bad} ssh not working ({err[:80]})")
        return 1
    check = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                 ALIAS, "command -v xpra"], timeout=25)
    if check.returncode != 0:
        print(f"{bad} Xpra server is not installed on the box")
        return 3
    ready, reason = gui_runtime_preflight()
    if not ready:
        print(f"{bad} cannot launch {label}: {reason}")
        return 1
    key = _app_key(label, command)
    registry = _gui_registry()
    existing = registry.get(key, {})
    existing_pid = int(existing.get("client_pid", 0) or 0)
    if existing_pid and f"ssh://{ALIAS}/{existing.get('display')}" in _pid_command(existing_pid):
        live = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                    f"timeout 4 xpra id :{int(existing.get('display', 0))} >/dev/null 2>&1"], timeout=7)
        if live.returncode == 0:
            print(f"{ok} {label} is already running")
            return 0
        try: os.kill(existing_pid, signal.SIGTERM)
        except ProcessLookupError: pass
    elif existing.get("session_id") and existing.get("display"):
        live = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                    f"timeout 4 xpra id :{int(existing['display'])} >/dev/null 2>&1"], timeout=7)
        if live.returncode == 0:
            return gui_resume(str(existing["session_id"]))
    registry.pop(key, None)
    try:
        record = _remote_start_app(command, label, key, microphone)
    except Exception as exc:
        print(f"{bad} cannot start {label}: {exc}")
        return 1
    args = _xpra_client_args(record, microphone)
    if os.environ.get("BOXCTL_GUI_HEADLESS") == "1":
        # CI/diagnostic mode attaches from a second remote Xvfb. This supplies
        # the monitor handshake GUI toolkits need without creating or activating
        # any macOS window, then tears both displays down.
        probe_display = 1000 + int(record["display"])
        probe_cmd = (
            f"setsid sh -c 'Xvfb :{probe_display} -screen 0 1512x982x24 >/dev/null 2>&1 & "
            f"xv=$!; trap \"kill $xv 2>/dev/null\" EXIT; sleep 1; "
            f"DISPLAY=:{probe_display} timeout 40 xpra attach :{record['display']} "
            "--speaker=off --microphone=disabled --clipboard=no --notifications=no "
            "--system-tray=no --splash=no >/dev/null 2>&1' >/dev/null 2>&1 & echo $!"
        )
        probe = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                     ALIAS, probe_cmd], timeout=8)
        try: probe_pid = int(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError): probe_pid = 0
        deadline = time.monotonic() + 35
        last = ""
        while time.monotonic() < deadline:
            info = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                        f"timeout 4 xpra info :{record['display']} 2>/dev/null | "
                        r"grep -E '^(clients=|windows\.[0-9]+\.title=)'"], timeout=7)
            last = info.stdout.strip()
            windows = re.search(r"^windows\.[0-9]+\.title=", info.stdout, re.MULTILINE)
            clients = re.search(r"^clients=([1-9][0-9]*)$", info.stdout, re.MULTILINE)
            if windows and clients:
                _stop_remote_record(record)
                if probe_pid:
                    run(["ssh", ALIAS, f"kill -TERM -- -{probe_pid} 2>/dev/null || true"], timeout=5)
                print(f"{ok} verified {label} headlessly")
                return 0
            time.sleep(1)
        _stop_remote_record(record)
        if probe_pid:
            run(["ssh", ALIAS, f"kill -TERM -- -{probe_pid} 2>/dev/null || true"], timeout=5)
        print(f"{bad} {label} did not create a remote window\n{last}")
        return 1
    log = CFG_DIR / f"xpra-{key}.log"
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rotate_log(log)
    rotate_log(CFG_DIR / "xpra-client.log")
    stream = open(log, "ab", buffering=0)
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                            env=xpra_client_env(), start_new_session=True, close_fds=True)
    record["client_pid"] = proc.pid
    registry[key] = record
    _save_gui_registry(registry)
    deadline = time.monotonic() + 35
    last_remote = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        info = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", ALIAS,
                    f"timeout 4 xpra info :{record['display']} 2>/dev/null | "
                    r"grep -E '^(clients=|windows\.[0-9]+\.title=)'"], timeout=7)
        last_remote = info.stdout.strip()
        windows = re.search(r"^windows\.[0-9]+\.title=", info.stdout, re.MULTILINE)
        clients = re.search(r"^clients=([1-9][0-9]*)$", info.stdout, re.MULTILINE)
        if info.returncode == 0 and windows and clients:
            try: remote_gui_cleanup(False)  # snapshot owned helper pids for crash cleanup
            except Exception: pass
            print(f"{ok} opened {label}")
            return 0
        time.sleep(1)
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill()
    _stop_remote_record(record)
    registry = _gui_registry(); registry.pop(key, None); _save_gui_registry(registry)
    try: tail = "\n".join(log.read_text(errors="replace").splitlines()[-8:])
    except OSError: tail = ""
    detail = tail or last_remote or "Xpra did not create a window"
    print(f"{bad} {label} failed to open\n{detail[-1200:]}")
    return 1


def prepare_gui_shell() -> int:
    """Keep one Xpra display attached so programs started by plain SSH can draw."""
    client = xpra_binary()
    if not client:
        print(f"{bad} Xpra client is not installed on this Mac")
        return 3
    remote = (
        "xpra id :100 >/dev/null 2>&1 || "
        "xpra start :100 --daemon=yes --exit-with-children=no "
        "--exit-with-client=no --speaker=on --microphone=disabled"
    )
    started = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                   ALIAS, remote], timeout=40)
    if started.returncode:
        print(f"{bad} could not prepare graphical shell: {(started.stderr or started.stdout).strip()[:160]}")
        return 1
    marker = f"ssh://{ALIAS}/100"
    processes = run(["ps", "-axo", "command="], timeout=10).stdout.splitlines()
    attached = any("/Xpra.app/Contents/MacOS/Xpra seamless " in command
                   and marker in command for command in processes)
    if not attached:
        args = [client, "seamless", marker,
                "--ssh=ssh -o BatchMode=yes -o IdentityAgent=none",
                "--speaker=on", "--microphone=disabled", "--av-sync=yes",
                "--clipboard=yes", "--clipboard-direction=both",
                # The tray keeps this windowless client alive between commands;
                # LSUIElement hides its Dock tile in the bundled application.
                "--notifications=yes", "--system-tray=yes", "--cursors=yes",
                "--video=yes", "--opengl=auto", "--desktop-scaling=1", "--dpi=96"]
        log = CFG_DIR / "xpra-shell.log"
        stream = open(log, "ab", buffering=0)
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                         env=xpra_client_env(), start_new_session=True, close_fds=True)
    print(f"{ok} graphical SSH environment ready on :100")
    return 0


def gui_cleanup(all_sessions: bool = False) -> tuple[int, int]:
    """Remove only tracked BoxDeck clients and owned remote sessions."""
    registry = _gui_registry()
    removed_local = 0
    kept = {}
    for key, record in registry.items():
        pid = int(record.get("client_pid", 0) or 0)
        marker = f"ssh://{ALIAS}/{record.get('display')}"
        alive = pid > 0 and marker in _pid_command(pid)
        if alive and not all_sessions:
            kept[key] = record
            continue
        if alive:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            removed_local += 1
    tracked = {int(r.get("client_pid", 0) or 0) for r in registry.values()}
    bundled_marker = str(_HELPERS / "Xpra.app/Contents/MacOS/Xpra")
    for line in run(["ps", "-axo", "pid=,command="], timeout=10).stdout.splitlines():
        try: pid_text, command = line.strip().split(None, 1); pid = int(pid_text)
        except (ValueError, IndexError): continue
        # Legacy clients predate the registry, but their executable path proves
        # BoxDeck ownership. RelayDesk and standalone Xpra use different paths.
        if (pid not in tracked and command.startswith(bundled_marker + " seamless ")
                and f"ssh://{ALIAS}/" in command):
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            removed_local += 1
    _save_gui_registry(kept)
    report = remote_gui_cleanup(all_sessions)
    return removed_local, len(report.get("removed", []))


def cmd_gui(args) -> int:
    if args.action == "apps":
        code, output = gui_apps(); print(output, end="" if output.endswith("\n") else "\n"); return code
    if args.action == "check":
        local = xpra_binary()
        remote = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                      ALIAS, "command -v xpra"], timeout=25)
        print(json.dumps({"client": bool(local), "server": remote.returncode == 0,
                          "client_path": local or ""}))
        return 0 if local and remote.returncode == 0 else 3
    if args.action == "sessions":
        try: print(json.dumps(gui_sessions()))
        except Exception as exc:
            print(f"{bad} cannot list sessions: {exc}"); return 1
        return 0
    if args.action == "sensitivity":
        return gui_scroll_sensitivity(args.percent)
    if args.action in ("resume", "detach", "terminate"):
        if not args.session:
            print(f"{bad} provide --session ID"); return 2
        return {"resume": gui_resume, "detach": gui_detach,
                "terminate": gui_terminate}[args.action](args.session)
    if args.action in ("clear", "cleanup"):
        try:
            local_count, remote_count = gui_cleanup(args.action == "clear")
        except Exception as exc:
            print(f"{bad} cleanup failed: {exc}")
            return 1
        total = max(local_count, remote_count)
        print(f"{ok} closed {total} remote app session{'s' if total != 1 else ''}")
        return 0
    if args.action == "shell":
        return prepare_gui_shell()
    if args.desktop:
        code, command = desktop_command(args.desktop)
        if code: print(f"{bad} {command}"); return code
        return gui_launch(command, args.desktop.removesuffix(".desktop"), args.microphone)
    command = " ".join(args.command or []).strip()
    if not command:
        print(f"{bad} provide --desktop ID or a command")
        return 2
    return gui_launch(command, command, args.microphone)


# ---------------------------------------------------------------- portable box profile
BOXSERVER = r'''#!/usr/bin/env python3
"""boxserver: the SSH-only BoxDeck endpoint (no daemon and no listening port)."""
import argparse, base64, glob, json, os, platform, shutil
from pathlib import Path

PROFILE = Path.home() / ".config/boxserver/profile.json"

def profile():
    try: data = json.loads(PROFILE.read_text())
    except Exception: data = {}
    data.update({"server_version": 2, "hostname": platform.node(),
                 "home": str(Path.home()), "user": os.environ.get("USER", ""),
                 "features": {"xpra": bool(shutil.which("xpra")),
                              "nvidia": bool(shutil.which("nvidia-smi")),
                              "audio": bool(shutil.which("pactl")),
                              "apps": True, "clipboard": True}})
    print(json.dumps(data))

def apps():
    import configparser
    found = {}
    icon_files = []
    for icon_root in (str(Path.home()/".local/share/icons"), "/usr/share/icons/hicolor", "/usr/share/icons", "/usr/share/pixmaps"):
        for folder, _, files in os.walk(icon_root):
            icon_files += [os.path.join(folder, f) for f in files if f.rsplit(".", 1)[-1].lower() in ("png", "svg", "xpm")]
    def icon_data(value):
        if not value: return ""
        candidates = [value] if os.path.isabs(value) else []
        candidates += [p for p in icon_files if os.path.splitext(os.path.basename(p))[0] == value]
        candidates.sort(key=lambda p: ("/64x64/" not in p, "/48x48/" not in p, "/scalable/" not in p, len(p)))
        for path in candidates:
            try:
                data = Path(path).read_bytes()
                if len(data) <= 512000: return base64.b64encode(data).decode()
            except OSError: pass
        return ""
    for root in ("/usr/share/applications", str(Path.home()/".local/share/applications")):
        for path in glob.glob(root + "/*.desktop"):
            c = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                c.read(path, encoding="utf-8"); d = c["Desktop Entry"]
                if d.get("Type", "Application") != "Application" or d.getboolean("NoDisplay", fallback=False) or d.getboolean("Hidden", fallback=False): continue
                name, command = d.get("Name", "").strip(), d.get("Exec", "").strip()
                if name and command:
                    ident = os.path.basename(path)
                    icon = d.get("Icon", "").strip()
                    found[ident] = {"id": ident, "name": name, "detail": d.get("Comment", "").strip(), "icon": icon, "iconData": icon_data(icon)}
            except Exception: pass
    print(json.dumps(sorted(found.values(), key=lambda x: x["name"].casefold())))

p = argparse.ArgumentParser(prog="boxserver")
p.add_argument("action", choices=("profile", "apps", "ping"))
a = p.parse_args()
if a.action == "profile": profile()
elif a.action == "apps": apps()
else: print(json.dumps({"ok": True, "server_version": 2}))
'''


def cmd_init(args) -> int:
    """Write the minimum profile needed before the first TOTP authentication."""
    CFG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg = {"host": args.host, "user": args.user, "alias": args.alias,
           "lan_host": args.lan_host}
    (CFG_DIR / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (CFG_DIR / "config.json").chmod(0o600)
    print(f"{ok} saved profile for {args.user}@{args.host}")
    print("   run `boxctl connect --totp --remote` next")
    return 0


def cmd_server(args) -> int:
    if args.action == "install":
        profile = {"host": HOST, "user": USER, "alias": ALIAS,
                   "lan_host": meta().get("lan_host") or LAN_HOST_DEFAULT,
                   "lan_name": meta().get("lan_name", ""), "profile_version": 1}
        source = base64.b64encode(BOXSERVER.encode()).decode()
        pdata = base64.b64encode((json.dumps(profile, indent=2) + "\n").encode()).decode()
        cmd = ("mkdir -p ~/.local/bin ~/.config/boxserver && "
               f"echo {source} | base64 -d > ~/.local/bin/boxserver && "
               "chmod 755 ~/.local/bin/boxserver && "
               f"echo {pdata} | base64 -d > ~/.config/boxserver/profile.json && "
               "chmod 600 ~/.config/boxserver/profile.json && ~/.local/bin/boxserver ping")
        r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                 ALIAS, cmd], timeout=45)
        if r.returncode:
            print(f"{bad} {(r.stderr or r.stdout).strip()[:200]}")
            return 1
        print(f"{ok} boxserver installed (SSH-only; no open port)")
        return 0
    if args.action == "sync":
        r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                 ALIAS, "~/.local/bin/boxserver profile"], timeout=45)
        try:
            profile = json.loads(r.stdout)
            required = ("host", "user", "alias", "lan_host")
            if r.returncode or not all(profile.get(k) for k in required):
                raise ValueError("incomplete server profile")
        except Exception as e:
            print(f"{bad} cannot import boxserver profile: {e}")
            return 1
        config = {k: profile[k] for k in required}
        (CFG_DIR / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        (CFG_DIR / "config.json").chmod(0o600)
        m = meta()
        m.update({k: profile[k] for k in ("lan_host", "lan_name") if profile.get(k)})
        META.write_text(json.dumps(m, indent=2) + "\n"); META.chmod(0o600)
        write_ssh_alias(identity=str(KEY) if KEY.exists() else None,
                        agent_sock=str(SECRETIVE_SOCK) if passkey_ready() else None)
        print(f"{ok} imported authoritative profile from {profile.get('hostname', 'boxserver')}")
        return 0
    remote = "~/.local/bin/boxserver " + args.action
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
             ALIAS, remote], timeout=45)
    print(r.stdout if r.returncode == 0 else r.stderr, end="")
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(prog="boxctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status")
    s.add_argument("--quick", action="store_true", help="skip service and GPU probes")
    s.set_defaults(fn=cmd_status)
    c = sub.add_parser("connect"); c.add_argument("--totp", action="store_true",
                                                  help="force password login (TOTP if configured)")
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
    pf = sub.add_parser("forward", help="manage saved box-to-Mac local port forwards")
    pf.add_argument("action", choices=["list", "add", "remove"])
    pf.add_argument("--local-port", type=int)
    pf.add_argument("--remote-port", type=int)
    pf.set_defaults(fn=cmd_forward)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    rt = sub.add_parser("route", help="show/measure LAN vs remote routes")
    rt.add_argument("action", nargs="?", default="show", choices=["show", "detect"])
    rt.set_defaults(fn=cmd_route)
    k = sub.add_parser("code"); k.add_argument("path", nargs="?"); k.set_defaults(fn=cmd_code)
    g = sub.add_parser("gui", help="discover and launch seamless GUI applications")
    g.add_argument("action", choices=["apps", "launch", "check", "sessions", "sensitivity", "resume",
                                      "detach", "terminate", "clear", "cleanup", "shell"])
    g.add_argument("--session", help="BoxDeck session id returned by `gui sessions`")
    g.add_argument("--percent", type=int, help="scroll sensitivity (5-200, default 25)")
    g.add_argument("--desktop", help="desktop entry id returned by `gui apps`")
    g.add_argument("--microphone", action="store_true",
                   help="share this Mac's microphone with the remote app")
    g.add_argument("command", nargs="*", help="custom GUI command")
    g.set_defaults(fn=cmd_gui)
    i = sub.add_parser("init", help="configure a box on this Mac")
    i.add_argument("--host", required=True)
    i.add_argument("--user", required=True)
    i.add_argument("--lan-host", required=True)
    i.add_argument("--alias", default="box")
    i.set_defaults(fn=cmd_init)
    bs = sub.add_parser("server", help="install or query the SSH-only boxserver")
    bs.add_argument("action", choices=["install", "sync", "profile", "apps", "ping"])
    bs.set_defaults(fn=cmd_server)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
