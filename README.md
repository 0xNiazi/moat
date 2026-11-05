# moat

Keep the VPN in the moat, not on your host.

`moat` runs your lab OpenVPN client inside an isolated Docker network
namespace, then lets you run normal host applications (`ssh`, `xfreerdp`,
browsers, whatever) *only inside that namespace* — nothing else on your
machine gets routed, no VM, no full-tunnel VPN client hijacking your real
network.

---

## How it works

- `moat up` starts a minimal Alpine container running `openvpn`, owning its
  own tunnel interface and routes. Your host's network is untouched.
- `moat run -- <command>` uses `nsenter` to drop a host process into that
  container's network namespace, so e.g. your normal `ssh` binary — running
  as your own user, on your own filesystem — sees only what the moat's
  tunnel can reach.
- If you pass `--dns` on `up` (handy for AD labs that need their own DNS
  server), `moat run` also creates a private, throwaway mount namespace for
  that one command and bind-mounts a custom `resolv.conf` into it. Your
  host's real `/etc/resolv.conf` is never modified.
- `moat term` opens an independent terminal window (defaults to `alacritty` if
  none is set) running your native shell inside the moat's network namespace —
  completely detached from the calling terminal.
- `moat browser` manages isolated browser profiles so you can browse through
  the moat without conflicting with any browser already open on your host.
- `moat down` tears the whole thing down.

---

## Install

```bash
git clone https://gitlab.com/0xNiazi/moat
# mirror: https://github.com/0xNiazi/moat
cd moat

# make the CLI executable and put it on your PATH as `moat`
chmod +x moat.py
sudo ln -s "$(pwd)/moat.py" /usr/local/bin/moat

# build the image moat's container runs from
docker build -t moat:latest .

# so `moat` works without sudo -- log out/in (or `newgrp docker`) after this
sudo usermod -aG docker $USER
newgrp docker
```

Symlinking (rather than copying) `moat.py` into `/usr/local/bin` means a
`git pull` in this directory updates the installed command immediately, with
nothing to reinstall.

Verify it's on your PATH:
```bash
moat -h
```

---

## Usage

### VPN

```bash
# raise the moat
moat up --ovpn htb-lab.ovpn --dns "10.10.10.1"

# run anything through it
moat run -- ssh htb-student@10.10.10.5
moat run -- xfreerdp /v:10.10.10.5 /u:admin /p:password

moat status
moat logs
moat down
```

### Terminal

Launch an independent terminal window whose shell runs entirely inside the moat's network namespace:

```bash
# opens your default terminal (defaults to alacritty) with an interactive shell
moat term

# specify an alternate terminal or run a specific command
moat term -t alacritty
moat term -- htop
```

The new terminal window runs in its own detached session. You can continue using or close your original terminal window without terminating the moat terminal.

### Browsers

Browsers cannot be launched directly via `moat run` because they lock their
profile directories and will refuse to start a second instance that shares a
profile with one already running on your host.

`moat browser` solves this by creating a dedicated, isolated profile for each
browser under `~/.moat/browsers/<name>/` — completely separate from your
normal host profiles.

```bash
# Step 1 — create an isolated profile (once per browser)
moat browser --create firefox
moat browser --create chrome
moat browser --create chromium

# Step 2 — launch through the moat (moat must be up first)
moat browser --launch firefox
moat browser --launch chrome
moat browser --launch chromium

# shorthand — same as --launch
moat browser firefox
moat browser chrome
```

Supported browsers: `firefox`, `chrome`, `chromium`

To reset a browser profile (start fresh):
```bash
rm -rf ~/.moat/browsers/firefox
moat browser --create firefox
```

---

## Requirements

- Linux host
- `docker` (with your user in the `docker` group — see Install above)
- `sudo`, `nsenter`, `unshare`, `setpriv` (all part of `util-linux`, present
  on essentially every distro by default)

---

## Why

VPN clients for CTF/lab platforms usually assume they own your whole
network. `moat` scopes that assumption down to a single container's network
namespace, so the only things that ever see lab traffic are the commands you
explicitly run through `moat run` or `moat browser`.

---

## State

moat stores all its state under `~/.moat/`:

```
~/.moat/
  state.json          active container name, DNS file path, tun interface name
  vpn/
    client.ovpn       copy of your ovpn config
    auth.txt          copy of your auth file (if provided)
  resolv.conf         custom DNS config (if --dns was passed)
  browsers/
    firefox/          isolated Firefox profile
    chrome/           isolated Chrome profile
    chromium/         isolated Chromium profile
```

---

## Troubleshooting

**`Permission denied` on docker socket**
```bash
sudo usermod -aG docker $USER
newgrp docker   # or log out and back in
```

**Tunnel never comes up**
```bash
moat logs       # see the full openvpn output
```

**Browser says profile is locked / already running**
You likely ran `moat run -- firefox` directly without using `moat browser`.
Kill that instance and use `moat browser --launch firefox` instead.

**`moat browser --create` says profile already exists**
```bash
rm -rf ~/.moat/browsers/<name>
moat browser --create <name>
```

**Browser opens but can't reach lab IPs**
Check the tunnel is actually up:
```bash
moat status
```
If the tun interface shows no IP, the VPN handshake failed — check `moat logs`.
