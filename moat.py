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
"""

import argparse
import json
import os
import pwd
import shlex
import subprocess
import sys
import time
from pathlib import Path


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


STATE_DIR = real_home() / ".moat"
STATE_FILE = STATE_DIR / "state.json"
IMAGE = "moat:latest"
DEFAULT_NAME = "moat"


def check_docker_access():
    """
    Fail loudly and early if the invoking user can't talk to the Docker
    daemon, instead of letting docker itself produce a cryptic error deep
    inside a subprocess call.
    """
    if os.geteuid() == 0:
        # root (or sudo) always has access to the docker socket -- nothing
        # to check, though it's no longer necessary to run moat with sudo
        # at all now that state paths are unified (see real_home()).
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
        lines = "".join(f"nameserver {ip.strip()}\n" for ip in args.dns.split(",") if ip.strip())
        dns_file.write_text(lines)
        print(f"[*] custom DNS written to {dns_file}:\n{lines}")

    # clean up any previous container with the same name
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
    # log to a file inside the container so we can actually see failures,
    # instead of `docker exec -d` output vanishing into nowhere.
    run([
        "docker", "exec", "-d", args.name,
        "sh", "-c",
        "openvpn --config /vpn/client.ovpn "
        + " ".join(auth_arg)
        + " --log /var/log/openvpn.log",
    ])

    print("[*] waiting for the tunnel interface to come up ", end="", flush=True)
    up_ok = False
    tun_name = None
    for _ in range(60):
        r = subprocess.run(
            ["docker", "exec", args.name, "sh", "-c",
             "ip -o link show type tun 2>/dev/null"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            # e.g. "2: exploitad: <POINTOPOINT,...> ..." -- pull the name out
            tun_name = r.stdout.split(":", 2)[1].strip()
            print(f" up ({tun_name}).")
            up_ok = True
            break

        # if the openvpn process has already died, no point waiting the
        # full timeout -- bail early and show the log.
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
        "dns_file": str(dns_file) if dns_file else None,
        "tun_name": tun_name,
    })
    print(f"[+] moat is up. Try: moat run -- ssh user@<lab-ip>")


def get_pid(container):
    return run_out(["docker", "inspect", "-f", "{{.State.Pid}}", container])


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

    uid, gid = os.getuid(), os.getgid()
    dns_file = state.get("dns_file")

    mount_dns = (
        f"mount --bind {shlex.quote(dns_file)} /etc/resolv.conf" if dns_file else ":"
    )
    inner = (
        "set -e\n"
        f"{mount_dns}\n"
        f'exec setpriv --reuid={uid} --regid={gid} --init-groups -- "$@"\n'
    )

    full_cmd = [
        "sudo", "nsenter", "--target", pid, "--net", "--",
        "unshare", "--mount", "--",
        "bash", "-c", inner, "moat-run",
    ] + user_cmd

    os.execvp("sudo", full_cmd)


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


def cmd_logs(args):
    state = load_state()
    if not state.get("container"):
        sys.exit("[!] moat is down.")
    container = state["container"]
    subprocess.run(["docker", "exec", container, "sh", "-c",
                     f"tail -n {args.lines} /var/log/openvpn.log 2>/dev/null || echo '(no log yet)'"])


def cmd_down(args):
    state = load_state()
    container = state.get("container")
    if container:
        subprocess.run(["docker", "rm", "-f", container])
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    print("[+] moat drained.")


def main():
    p = argparse.ArgumentParser(prog="moat", description="Keep the VPN in the moat, not on your host")
    sub = p.add_subparsers(dest="action", required=True)

    up = sub.add_parser("up", help="raise the moat (start the VPN container)")
    up.add_argument("--ovpn", required=True, help="path to .ovpn config file")
    up.add_argument("--auth", help="optional file with username/password (2 lines)")
    up.add_argument("--dns", help='optional comma-separated DNS IPs, e.g. "10.10.10.1"')
    up.add_argument("--name", default=DEFAULT_NAME)
    up.set_defaults(func=cmd_up)

    runp = sub.add_parser("run", help="run a host command inside the moat's network namespace")
    runp.add_argument("cmd", nargs=argparse.REMAINDER,
                       help="-- ssh user@ip   |   -- xfreerdp /v:ip /u:admin")
    runp.set_defaults(func=cmd_run)

    st = sub.add_parser("status")
    st.set_defaults(func=cmd_status)

    lg = sub.add_parser("logs", help="show the openvpn log from inside the moat")
    lg.add_argument("-n", "--lines", type=int, default=50)
    lg.set_defaults(func=cmd_logs)

    dn = sub.add_parser("down", help="drain the moat (stop and remove the container)")
    dn.set_defaults(func=cmd_down)

    args = p.parse_args()
    check_docker_access()
    args.func(args)


if __name__ == "__main__":
    main()
