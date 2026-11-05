#!/usr/bin/env python3
"""
moat - run OpenVPN inside a Docker container's isolated network namespace,
then launch host applications (ssh, xfreerdp, browsers, etc.) so they see ONLY
that namespace's network -- your host's real interfaces/routes/DNS are never
touched.

Setup (once):
  docker build -t moat:latest .

Usage:
  moat up --ovpn lab.ovpn [--auth creds.txt] [--dns "10.10.10.1,10.10.10.2"] [--name moat]
  moat run -- ssh user@10.10.10.5
  moat run -- xfreerdp /v:10.10.10.5 /u:admin
  moat term [-t TERMINAL] [-- <command>]
  moat browser --create firefox
  moat browser --launch firefox
  moat status
  moat logs [-n 100]
  moat down

Requires on host: docker, sudo, nsenter, unshare, setpriv (all in util-linux,
already present on basically every Linux distro).

How the DNS override works:
  `run` enters the container's *network* namespace (nsenter --net) and ALSO
  creates a private, throwaway *mount* namespace just for that one command
  (unshare --mount). Only inside that throwaway mount namespace do we bind-mount
  your custom resolv.conf over /etc/resolv.conf. Your host's real
  /etc/resolv.conf is never modified. Once the command exits, the private
  mount namespace and its bind-mount vanish.

Why browsers need special handling:
  Browsers lock their profile directories and refuse to launch a second
  instance against the same profile. `moat browser --create <name>` sets up
  a dedicated moat profile under ~/.moat/browsers/<name>/ so moat can launch
  a completely isolated browser instance that never conflicts with whatever
  is already running on your host.
"""

import argparse
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Browsers moat knows about.
# Each entry describes:
#   binary   - the executable to look for on PATH
#   create   - callable(profile_dir) -> list[str] command to initialise the
#              profile (run on the host, NOT through moat).
#              None means "just mkdir; the browser self-initialises on first
#              launch".
#   launch   - callable(profile_dir, home) -> list[str] full command that
#              moat will exec inside the network namespace.
#   suppress - extra substrings to drop from stderr beyond the global list.
# ---------------------------------------------------------------------------

def _firefox_create(profile_dir: Path):
    return [
        "firefox",
        "--CreateProfile",
        f"moat {profile_dir}",
    ]

def _firefox_launch(profile_dir: Path, home: str):
    return [
        "env", f"HOME={home}",
        "firefox",
        "--profile", str(profile_dir),
        "--no-remote",
    ]

def _chrome_launch(profile_dir: Path, home: str):
    return [
        "env", f"HOME={home}",
        "google-chrome",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

def _chromium_launch(profile_dir: Path, home: str):
    return [
        "env", f"HOME={home}",
        "chromium",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

BROWSERS = {
    "firefox": {
        "binary":   "firefox",
        "create":   _firefox_create,
        "launch":   _firefox_launch,
        "suppress": ["dconf", "IBUS", "Fontconfig", "shader cache",
                     "mesa_glthread", "org.a11y", "pulse", "GLib-GIO"],
    },
    "chrome": {
        "binary":   "google-chrome",
        "create":   None,           # chrome self-initialises on first run
        "launch":   _chrome_launch,
        "suppress": ["Fontconfig", "mesa_glthread", "MESA", "libva",
                     "NSS", "gpu_process"],
    },
    "chromium": {
        "binary":   "chromium",
        "create":   None,
        "launch":   _chromium_launch,
        "suppress": ["Fontconfig", "mesa_glthread", "MESA", "libva",
                     "NSS", "gpu_process"],
    },
}

SUPPORTED_BROWSERS = list(BROWSERS.keys())


def real_home() -> Path:
    """
    Resolve the *actual* invoking user's home directory, even when moat is
    run under sudo. Without this, `sudo moat up` writes state to /root/.moat
    while a plain `moat status` reads from ~/.moat -- two different state
    files that silently disagree with each other.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


STATE_DIR   = real_home() / ".moat"
STATE_FILE  = STATE_DIR / "state.json"
BROWSER_DIR = STATE_DIR / "browsers"
IMAGE       = "moat:latest"
DEFAULT_NAME = "moat"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def check_docker_access():
    if os.geteuid() == 0:
        return

    r = subprocess.run(["docker", "info"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode == 0:
        return

    stderr = (r.stderr or "").lower()
    if "permission denied" in stderr:
        print("=" * 60)
        print("  YOU DON'T HAVE THE CORRECT PERMS")
        print("=" * 60)
        print("Your user can't talk to the Docker daemon socket.")
        print()
        print("Fix it with:")
        print("  sudo usermod -aG docker $USER")
        print("  newgrp docker      # or just log out and back in")
        print()
        print("Then run moat normally, WITHOUT sudo.")
        sys.exit(1)
    elif "cannot connect" in stderr or "daemon running" in stderr:
        sys.exit("[!] Docker daemon doesn't seem to be running.\n"
                 "    Try: sudo systemctl start docker")
    else:
        sys.exit(f"[!] docker isn't usable right now:\n{r.stderr}")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def run_out(cmd):
    return subprocess.check_output(cmd, text=True).strip()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_pid(container):
    return run_out(["docker", "inspect", "-f", "{{.State.Pid}}", container])


def _build_nsenter_cmd(pid, uid, gid, dns_file, user_cmd, cwd=None, container=None):
    """
    Build the full sudo nsenter … unshare … setpriv … <user_cmd> invocation
    that drops a command into the moat's network namespace as the real user.
    """
    home = str(real_home())
    mount_dns = (
        f"mount --bind {shlex.quote(str(dns_file))} /etc/resolv.conf"
        if dns_file else ":"
    )
    change_dir = f"cd {shlex.quote(str(cwd))}\n" if cwd else ""
    moat_env = (
        f"export MOAT=1\n"
        f"export MOAT_CONTAINER={shlex.quote(str(container))}\n"
        if container else ""
    )
    inner = (
        "set -e\n"
        f"{mount_dns}\n"
        f"export HOME={shlex.quote(home)}\n"
        f"{moat_env}"
        f"{change_dir}"
        f'exec setpriv --reuid={uid} --regid={gid} --init-groups -- "$@"\n'
    )
    return [
        "sudo", "nsenter", "--target", pid, "--net", "--",
        "unshare", "--mount", "--",
        "bash", "-c", inner, "moat-run",
    ] + list(user_cmd)


def detect_terminal(preferred=None):
    """
    Determine the terminal emulator to launch.
    Priority:
      1. Explicit preference / -t / --terminal argument
      2. $TERMINAL environment variable
      3. Modern xdg-terminal-exec (if available)
      4. Default to 'alacritty' (if installed)
      5. x-terminal-emulator (Debian/Ubuntu default alternative)
      6. Common terminal emulators list
    """
    if preferred:
        path = shutil.which(preferred)
        if path:
            return path
        sys.exit(f"[!] requested terminal '{preferred}' not found on PATH.")

    env_term = os.environ.get("TERMINAL")
    if env_term:
        path = shutil.which(env_term)
        if path:
            return path

    if shutil.which("xdg-terminal-exec"):
        try:
            cmd = subprocess.check_output(
                ["xdg-terminal-exec", "--print-cmd"],
                text=True, stderr=subprocess.DEVNULL
            ).strip().splitlines()
            if cmd and shutil.which(cmd[0]):
                return cmd[0]
        except Exception:
            pass

    # If default is not already set / resolved, prioritize alacritty
    if shutil.which("alacritty"):
        return "alacritty"

    if shutil.which("x-terminal-emulator"):
        return "x-terminal-emulator"

    fallbacks = [
        "alacritty",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "kitty",
        "foot",
        "wezterm",
        "tilix",
        "terminator",
        "xterm",
    ]
    for term in fallbacks:
        if shutil.which(term):
            return term

    return None


def _format_terminal_exec(term_bin: str, cmd: list[str], title: str = "moat", hold: bool = False) -> list[str]:
    """
    Build the command line for launching `cmd` inside the given terminal emulator.
    Handles symlinks like x-terminal-emulator to detect the underlying terminal.
    """
    real_path = os.path.realpath(term_bin)
    base = os.path.basename(real_path).lower()
    bin_name = os.path.basename(term_bin).lower()

    def matches(name):
        return name in base or name in bin_name

    if matches("alacritty"):
        args = [term_bin, "-T", title]
        if hold:
            args.append("--hold")
        return args + ["-e"] + cmd
    elif matches("gnome-terminal"):
        return [term_bin, "--title", title, "--"] + cmd
    elif matches("konsole"):
        args = [term_bin, "-p", f"tabtitle={title}"]
        if hold:
            args.append("--hold")
        return args + ["-e"] + cmd
    elif matches("xfce4-terminal"):
        args = [term_bin, "-T", title]
        if hold:
            args.append("-H")
        return args + ["-x"] + cmd
    elif matches("kitty"):
        args = [term_bin, "-T", title]
        if hold:
            args.append("--hold")
        return args + cmd
    elif matches("foot"):
        args = [term_bin, "-T", title]
        if hold:
            args.append("-H")
        return args + cmd
    elif matches("wezterm"):
        return [term_bin, "start", "--"] + cmd
    elif matches("tilix"):
        return [term_bin, "-t", title, "-e"] + cmd
    elif matches("terminator"):
        return [term_bin, "-T", title, "-x"] + cmd
    elif matches("xterm"):
        args = [term_bin, "-title", title]
        if hold:
            args.append("-hold")
        return args + ["-e"] + cmd
    else:
        # Generic terminal fallback (supports -e)
        return [term_bin, "-e"] + cmd


def _filter_stderr(proc, suppress_patterns):
    """
    Stream proc's stderr to our stderr, dropping lines that match any of the
    suppress patterns (all those harmless dconf / IBus / fontconfig warnings).
    Lines that look like real errors are always shown.
    """
    for raw in proc.stderr:
        line = raw.rstrip("\n")
        if any(pat in line for pat in suppress_patterns):
            continue
        print(line, file=sys.stderr)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_up(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    ovpn_path = Path(args.ovpn).resolve()
    if not ovpn_path.exists():
        sys.exit(f"[!] ovpn file not found: {ovpn_path}")

    vpn_dir = STATE_DIR / "vpn"
    vpn_dir.mkdir(exist_ok=True)
    run(["cp", str(ovpn_path), str(vpn_dir / "client.ovpn")])

    auth_arg = []
    if args.auth:
        auth_path = Path(args.auth).resolve()
        if not auth_path.exists():
            sys.exit(f"[!] auth file not found: {auth_path}")
        run(["cp", str(auth_path), str(vpn_dir / "auth.txt")])
        auth_arg = ["--auth-user-pass", "/vpn/auth.txt"]

    dns_file = None
    if args.dns:
        dns_file = STATE_DIR / "resolv.conf"
        lines = "".join(
            f"nameserver {ip.strip()}\n"
            for ip in args.dns.split(",") if ip.strip()
        )
        dns_file.write_text(lines)
        print(f"[*] custom DNS written to {dns_file}:\n{lines}")

    subprocess.run(["docker", "rm", "-f", args.name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"[*] raising the moat ({args.name}) ...")
    run([
        "docker", "run", "-d",
        "--name", args.name,
        "--cap-add=NET_ADMIN",
        "--device=/dev/net/tun",
        "-v", f"{vpn_dir}:/vpn:ro",
        IMAGE,
    ])

    print("[*] launching openvpn inside the moat ...")
    run([
        "docker", "exec", "-d", args.name,
        "sh", "-c",
        "openvpn --config /vpn/client.ovpn "
        + " ".join(auth_arg)
        + " --log /var/log/openvpn.log",
    ])

    print("[*] waiting for the tunnel interface to come up ", end="", flush=True)
    up_ok    = False
    tun_name = None
    for _ in range(60):
        r = subprocess.run(
            ["docker", "exec", args.name, "sh", "-c",
             "ip -o link show type tun 2>/dev/null"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            tun_name = r.stdout.split(":", 2)[1].strip()
            print(f" up ({tun_name}).")
            up_ok = True
            break

        alive = subprocess.run(
            ["docker", "exec", args.name, "sh", "-c", "pgrep openvpn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if alive.returncode != 0:
            print(" openvpn exited early.")
            break

        print(".", end="", flush=True)
        time.sleep(1)

    if not up_ok:
        print(f"\n[!] tunnel never came up. Last lines of openvpn.log:\n")
        subprocess.run(["docker", "exec", args.name, "sh", "-c",
                        "tail -n 30 /var/log/openvpn.log 2>/dev/null || echo '(no log yet)'"])
        print(f"\n[!] run `moat logs` any time to see this again, or "
              f"`docker exec -it {args.name} openvpn --config /vpn/client.ovpn` "
              f"to watch it live.")
        sys.exit(1)

    save_state({
        "container": args.name,
        "dns_file":  str(dns_file) if dns_file else None,
        "tun_name":  tun_name,
    })
    print(f"[+] moat is up. Try: moat run -- ssh user@<lab-ip>")


def cmd_run(args):
    state = load_state()
    if not state.get("container"):
        sys.exit("[!] no moat is up. Run `moat up` first.")

    container = state["container"]
    pid = get_pid(container)
    if pid in ("0", ""):
        sys.exit(f"[!] container {container} is not running.")

    user_cmd = args.cmd
    if user_cmd and user_cmd[0] == "--":
        user_cmd = user_cmd[1:]
    if not user_cmd:
        sys.exit("[!] usage: moat run -- <command> [args...]")

    uid, gid   = os.getuid(), os.getgid()
    dns_file   = state.get("dns_file")
    full_cmd   = _build_nsenter_cmd(pid, uid, gid, dns_file, user_cmd, cwd=os.getcwd(), container=container)
    os.execvp("sudo", full_cmd)


def cmd_term(args):
    state = load_state()
    if not state.get("container"):
        sys.exit("[!] no moat is up. Run `moat up` first.")

    container = state["container"]
    pid = get_pid(container)
    if pid in ("0", ""):
        sys.exit(f"[!] container {container} is not running.")

    user_cmd = args.cmd
    if user_cmd and user_cmd[0] == "--":
        user_cmd = user_cmd[1:]

    uid, gid = os.getuid(), os.getgid()
    if not user_cmd:
        user_shell = (
            os.environ.get("SHELL")
            or pwd.getpwuid(uid).pw_shell
            or shutil.which("bash")
            or "/bin/sh"
        )
        user_cmd = [user_shell]

    term_bin = detect_terminal(preferred=getattr(args, "terminal", None))
    if not term_bin:
        sys.exit(
            "[!] could not detect any installed terminal emulator.\n"
            "    Install alacritty (recommended) or set $TERMINAL."
        )

    dns_file = state.get("dns_file")
    cwd = os.getcwd()
    full_cmd = _build_nsenter_cmd(pid, uid, gid, dns_file, user_cmd, cwd=cwd, container=container)

    title = f"moat ({container})"
    hold = getattr(args, "hold", False)
    term_exec = _format_terminal_exec(term_bin, full_cmd, title=title, hold=hold)

    proc = subprocess.Popen(
        term_exec,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=cwd,
    )
    print(f"[+] launched {os.path.basename(term_bin)} (PID {proc.pid}) inside moat ({container}).")


def cmd_browser(args):
    """
    moat browser --create <name>   set up an isolated profile for a browser
    moat browser --launch <name>   launch that browser through the moat
    moat browser <name>            shorthand for --launch
    """
    # resolve which browser and which action
    browser_name = (args.create or args.launch or args.name or "").lower().strip()

    if not browser_name:
        sys.exit(
            "[!] specify a browser.\n"
            f"    Supported: {', '.join(SUPPORTED_BROWSERS)}\n"
            "    Examples:\n"
            "      moat browser --create firefox\n"
            "      moat browser --launch firefox\n"
            "      moat browser firefox"
        )

    if browser_name not in BROWSERS:
        sys.exit(
            f"[!] '{browser_name}' is not a supported browser.\n"
            f"    Supported: {', '.join(SUPPORTED_BROWSERS)}"
        )

    info = BROWSERS[browser_name]

    # check the binary exists on the host
    if not shutil.which(info["binary"]):
        sys.exit(
            f"[!] '{info['binary']}' not found on PATH.\n"
            f"    Install {browser_name} first, then re-run this command."
        )

    profile_dir = BROWSER_DIR / browser_name

    # ---- CREATE ----
    if args.create:
        if profile_dir.exists():
            print(f"[*] profile for {browser_name} already exists at {profile_dir}")
            print(f"    To reset it: rm -rf {profile_dir}  then re-run --create")
            return

        profile_dir.mkdir(parents=True, exist_ok=True)

        if info["create"] is not None:
            print(f"[*] initialising {browser_name} profile ...")
            create_cmd = info["create"](profile_dir)
            result = subprocess.run(
                create_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode not in (0, 1):
                # Firefox --CreateProfile exits 1 even on success sometimes
                print(f"[!] profile creation returned {result.returncode} "
                      f"(this may be harmless — try --launch anyway)")
        else:
            # chrome/chromium self-initialise; just having the directory is enough
            print(f"[*] {browser_name} will initialise its own profile on first launch.")

        print(f"[+] profile created at: {profile_dir}")
        print(f"    Launch with: moat browser --launch {browser_name}")
        return

    # ---- LAUNCH (and bare `moat browser <name>`) ----
    if not profile_dir.exists():
        sys.exit(
            f"[!] no moat profile found for {browser_name}.\n"
            f"    Create one first: moat browser --create {browser_name}"
        )

    state = load_state()
    if not state.get("container"):
        sys.exit("[!] no moat is up. Run `moat up` first.")

    container = state["container"]
    pid = get_pid(container)
    if pid in ("0", ""):
        sys.exit(f"[!] container {container} is not running.")

    uid, gid = os.getuid(), os.getgid()
    dns_file = state.get("dns_file")
    home     = str(real_home())

    browser_cmd = info["launch"](profile_dir, home)
    full_cmd    = _build_nsenter_cmd(pid, uid, gid, dns_file, browser_cmd)

    print(f"[*] launching {browser_name} through the moat ...")

    # run with stderr piped so we can filter the noise
    proc = subprocess.Popen(
        full_cmd,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _filter_stderr(proc, info["suppress"])
    except KeyboardInterrupt:
        proc.terminate()
    proc.wait()


def cmd_status(args):
    state = load_state()
    if not state.get("container"):
        print("moat is down")
        return
    container = state["container"]
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    running = r.stdout.strip() == "true"
    print(f"container: {container}  running: {running}")
    if state.get("dns_file"):
        print(f"dns override file: {state['dns_file']}")
    if running:
        tun_name = state.get("tun_name") or "tun0"
        subprocess.run(["docker", "exec", container, "sh", "-c",
                        f"ip -4 addr show {shlex.quote(tun_name)}; echo; ip route"])

    # show which browser profiles exist
    if BROWSER_DIR.exists():
        profiles = [p.name for p in BROWSER_DIR.iterdir() if p.is_dir()]
        if profiles:
            print(f"browser profiles: {', '.join(profiles)}")


def cmd_logs(args):
    state = load_state()
    if not state.get("container"):
        sys.exit("[!] moat is down.")
    container = state["container"]
    subprocess.run(["docker", "exec", container, "sh", "-c",
                    f"tail -n {args.lines} /var/log/openvpn.log "
                    f"2>/dev/null || echo '(no log yet)'"])


def cmd_down(args):
    state = load_state()
    container = state.get("container")
    if container:
        subprocess.run(["docker", "rm", "-f", container])
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    print("[+] moat drained.")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="moat",
        description="Keep the VPN in the moat, not on your host",
    )
    sub = p.add_subparsers(dest="action", required=True)

    # up
    up = sub.add_parser("up", help="raise the moat (start the VPN container)")
    up.add_argument("--ovpn", required=True, help="path to .ovpn config file")
    up.add_argument("--auth", help="optional file with username/password (2 lines)")
    up.add_argument("--dns",  help='optional comma-separated DNS IPs, e.g. "10.10.10.1"')
    up.add_argument("--name", default=DEFAULT_NAME)
    up.set_defaults(func=cmd_up)

    # run
    runp = sub.add_parser("run", help="run a host command inside the moat's network namespace")
    runp.add_argument("cmd", nargs=argparse.REMAINDER,
                      help="-- ssh user@ip   |   -- xfreerdp /v:ip /u:admin")
    runp.set_defaults(func=cmd_run)

    # term
    term = sub.add_parser(
        "term",
        help="open an independent terminal window inside the moat",
        description=(
            "Opens an independent terminal window (defaulting to alacritty if none is set) "
            "running inside the moat's network namespace."
        ),
    )
    term.add_argument(
        "-t", "--terminal",
        help="terminal emulator to launch (default: system default or alacritty)",
    )
    term.add_argument(
        "--hold", action="store_true",
        help="keep terminal window open after command exits",
    )
    term.add_argument(
        "cmd", nargs=argparse.REMAINDER,
        help="optional command to run inside the terminal (default: user interactive shell)",
    )
    term.set_defaults(func=cmd_term)

    # browser
    br = sub.add_parser(
        "browser",
        help="manage and launch browsers through the moat",
        description=(
            "Browsers need isolated profiles to run alongside an existing host "
            "instance. Use --create once, then --launch (or just `moat browser "
            "<name>`) every time."
        ),
    )
    br_action = br.add_mutually_exclusive_group()
    br_action.add_argument(
        "--create", metavar="BROWSER",
        help=f"create an isolated moat profile  [{', '.join(SUPPORTED_BROWSERS)}]",
    )
    br_action.add_argument(
        "--launch", metavar="BROWSER",
        help="launch a browser that already has a moat profile",
    )
    br.add_argument(
        "name", nargs="?", default="",
        help="shorthand: moat browser firefox  (same as --launch firefox)",
    )
    br.set_defaults(func=cmd_browser)

    # status
    st = sub.add_parser("status", help="show moat and tunnel state")
    st.set_defaults(func=cmd_status)

    # logs
    lg = sub.add_parser("logs", help="show the openvpn log from inside the moat")
    lg.add_argument("-n", "--lines", type=int, default=50)
    lg.set_defaults(func=cmd_logs)

    # down
    dn = sub.add_parser("down", help="drain the moat (stop and remove the container)")
    dn.set_defaults(func=cmd_down)

    args = p.parse_args()
    check_docker_access()
    args.func(args)


if __name__ == "__main__":
    main()
