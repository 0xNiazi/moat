# moat

Keep the VPN in the moat, not on your host.

`moat` runs your HTB / THM / lab OpenVPN client inside an isolated Docker
network namespace, then lets you run normal host applications (`ssh`,
`xfreerdp`, a browser, whatever) *only inside that namespace* — nothing
else on your machine gets routed, no VM, no full-tunnel VPN client hijacking
your real network.

## How it works

- `moat up` starts a minimal container running `openvpn`, owning its own
  `tun0` interface and routes. Your host's network is untouched.
- `moat run -- <command>` uses `nsenter` to drop a host process into that
  container's network namespace, so e.g. your normal `ssh` binary — running
  as your own user, on your own filesystem — sees only what the moat's
  `tun0` can reach.
- If you pass `--dns` on `up` (handy for AD labs that need their own DNS
  server), `moat run` also creates a private, throwaway mount namespace for
  that one command and bind-mounts a custom `resolv.conf` into it. Your
  host's real `/etc/resolv.conf` is never modified.
- `moat down` tears the whole thing down.

## Setup

```bash
git clone <this repo>
cd moat
docker build -t moat:latest .
```

## Usage

```bash
# raise the moat
moat up --ovpn htb-lab.ovpn --dns "10.10.10.1"

# run things through it
moat run -- ssh htb-student@10.10.10.5
moat run -- xfreerdp /v:10.10.10.5 /u:admin

moat status
moat down
```

## Requirements

- Linux host
- `docker`
- `sudo`, `nsenter`, `unshare`, `setpriv` (all part of `util-linux`, present
  on essentially every distro by default)

## Why

VPN clients for CTF/lab platforms usually assume they own your whole
network. `moat` scopes that assumption down to a single container's netns,
so the only things that ever see lab traffic are the commands you
explicitly run through `moat run`.
