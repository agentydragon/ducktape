# Remote desktop to wyrm2 — investigation (2026-07-18)

Goal: graphical remote access to **wyrm2** (NixOS VM on atlas/Proxmox; the gaming
seat, 2x RTX 5090) from **rugged** and the other laptops, over the Nebula mesh.

Network baseline (verified): rugged `10.42.0.30` ↔ wyrm2 `10.42.0.20` over Nebula,
~74 ms; `ssh wyrm2.nebula.allegedly.works` works; root SSH is key-authed.

## Path A — gnome-remote-desktop system RDP ("Remote Login") — BLOCKED on NixOS

The "intended" post-SPICE design (`nix/nixos/hosts/wyrm2/default.nix`): a headless
GNOME session over system RDP, reached via an SSH-key tunnel on Nebula
(`ssh -L 3390:localhost:3389 <host>` + an RDP client). One real access layer — the
SSH key gates the tunnel, PAM authenticates the login via GRD's GDM handover.

PR #3424 made it fully declarative: a SOPS TLS pair (`secrets/hosts/wyrm2-rdp-tls.sops.{crt,key}`,
recipients admin + wyrm2-host) rendered to the daemon's paths, and a `grd.conf`
(`pkgs.writeText`) symlinked into `$XDG_DATA_HOME` via `systemd.tmpfiles.rules`. No
`grdctl`, no oneshot, no `/etc` writes.

### Why it never listens (root cause)

- The daemon reads `grd.conf` at `$XDG_DATA_HOME/gnome-remote-desktop/grd.conf` =
  `/var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop/grd.conf`
  (path/schema from `src/grd-settings-system.c`). It picks up cert/key/port from it
  — confirmed live: `grdctl --system status` shows the right cert path + fingerprint
  - key + `Port: 3389`.
- But `rdp-enabled=true` in the file does **not** flip the listener. `grdctl --system
rdp enable` is what flips it, and the daemon-side handler **self-systemd-enables**
  into `/etc/systemd/system/graphical.target.wants/gnome-remote-desktop.service` —
  read-only on NixOS → `GDBus.Error:System.Error.EROFS` (even as root) → RDP stays
  `Status: disabled`. The unit is `linked` but not `enabled` (no wants symlink).
- `grdctl --system status` authoritatively shows `Status: disabled`; the
  `org.gnome.RemoteDesktop.Rdp.Server` `Enabled` D-Bus property is `false`. Nothing
  binds 3389.

### Known, unsolved NixOS problem (not just us)

- nixpkgs **#504490** — "Handover daemon never starts in GDM greeter session —
  system-level RDP fails" (the failure right after the enable step).
- nixpkgs **#535360** — "Cannot enable remote login…" (the EROFS enable blocker).
- nixpkgs **#266774** — "How to configure GNOME Remote Desktop" (no declarative surface).
- nixpkgs `services.gnome.gnome-remote-desktop` exposes only `enable`.

### Status

PR #3424 is **merged but dormant**: the daemon runs (`active`), no listener, inert
(RDP isn't exposed, so it's safe). Revisit if nixpkgs merges a declarative
Remote-Login surface or fixes the GDM handover.

## Path B — Sunshine/Moonlight — WORKS (with a caveat)

Sunshine is already on wyrm2 (`services.sunshine`, CUDA/NVENC build). Verified live:

- `sunshine-wrapp` (PID in agentydragon's user instance) listening on
  `0.0.0.0:47984/47989/47990`.
- Reachable from rugged over Nebula (`10.42.0.20`) — all three TCP ports open; web UI
  at `https://10.42.0.20:47990` returns 401 (auth prompt, expected).
- Captures the active seat0 session (`loginctl`: agentydragon, `tty2`).

### Caveat — "not logged in yet"

Sunshine **captures an existing graphical session**; it does not create one. So it
streams only while agentydragon is logged into seat0 on wyrm2. From the GDM greeter
(no session) there's nothing to capture, and the user service may not run. To cover
pre-login / anytime access, enable **GDM auto-login** on wyrm2 → always a session to
capture. Tradeoff: an always-unlocked local session (physical access = unlocked
desktop). GRD Remote Login would have done true headless login without that — but
it's the blocked Path A.

### Connecting from rugged

```bash
nix run --substituters https://cache.nixos.org nixpkgs#moonlight-qt
```

Add host `10.42.0.20`; pair via PIN (enter it in Sunshine's web UI,
`https://10.42.0.20:47990`, with the creds set at Sunshine setup). No SSH tunnel
needed — Nebula is the secure transport; UDP stream ports `47998–48000` are open via
`services.sunshine.openFirewall`. (Permanent install: add `moonlight-qt` to
`nix/home/hosts/rugged.nix`.)

## Path C — xrdp over Nebula — WORKING

Headless desktop RD without auto-login and without an SSH tunnel. `services.xrdp`

- Xfce (`nix/nixos/hosts/wyrm2/default.nix`, PR #3431). xrdp spawns its own X
  session on connect (PAM auth with the system password — no stored creds), so it
  works **pre-login** with **no auto-login**.

**Status: working** (verified 2026-07-19). Needed one fix beyond the base setup:
xrdp-sesman's pam line is `pam_env … readenv=0`, so the session env has no `HOME`,
and Xfce black-screened on connect. The `rdpSession` wrapper (PR #3442) exports
`HOME` then execs `startxfce4` (teeing output to `/tmp/xrdp-wm.log`). nixpkgs's own
xrdp test sidesteps this by using bare window managers that don't need HOME.

### Security model (why no SSH tunnel)

xrdp listens on `*:3389`, but wyrm2's firewall makes it **Nebula-only**:
`networking.firewall.trustedInterfaces` includes `nebula1` (so 3389 is reachable
over the mesh), and 3389 is NOT in `allowedTCPPorts`, so every other interface
(LAN `192.168.1.x`, docker, cilium) drops it. Access = **Nebula cert** (mesh
membership gates who reaches it) + **xrdp password** (PAM login). RDP runs over
TLS (xrdp's auto-generated self-signed cert — accept client-side). An SSH tunnel
was rejected here: it adds setup hoops and does NOT remove the xrdp password (PAM
auths every session regardless), so it bought nothing for the "connect without a
password" goal. (The `xrdp` NixOS module has no bind-address option, so the
firewall is the restriction mechanism; defense-in-depth would bind to the nebula
IP via an xrdp.ini override — not done.)

### How to connect (from a mesh peer, e.g. rugged)

```bash
xfreerdp /v:10.42.0.20 /u:agentydragon /cert:tofu
```

- `/cert:tofu` trusts xrdp's self-signed cert on first use.
- Username `agentydragon`, password = your system password (PAM).
- You get a separate X11 **Xfce** session (not wyrm2's Wayland/GNOME seat).
- ⚠ Don't use **gnome-connections** (50.0): it crashes on xrdp's drive-redirection
  channels (gtk-frdp bug — dies in `libgtk-frdp-0.2.so` `fuse_session_thread_func`).
  Use `xfreerdp`, or the "wyrm2 (RDP)" desktop entry on rugged (PR #3435) which
  launches `xfreerdp` in a terminal for the password prompt.

## Note — wyrm2 rebuilds must run as root (the attic token is fine)

Earlier "attic auth 401" was a red herring: the attic netrc
(`/run/secrets/rendered/attic-netrc`) is `0400` root-only, and
`builtins.fetchClosure` (for `drivefs`, via the `google-drive` HM module) is
evaluated by the nix process **as the invoking user**. So `nixos-rebuild` as
`agentydragon` 401s against `cache.allegedly.works/{main,gaffer}` (no netrc
access); **as root it auths fine** (the root build of the xrdp closure succeeded).
The token is valid — this is secret hygiene, not a rotation problem. Build/switch
wyrm2 as root.

## Decisions / status

- **xrdp over Nebula (Path C) is the working RD** — headless, pre-login, no
  auto-login, no tunnel. PRs #3431, #3442 (HOME fix). Connect via the "wyrm2 (RDP)"
  desktop entry on rugged or `xfreerdp /v:10.42.0.20 /u:agentydragon /cert:tofu`.

## Follow-ups (later, not now)

- **Graphical client (no terminal spawn) — DONE.** The desktop entry now opens
  `remmina` (connection list + password dialog) instead of `xfreerdp` in a terminal,
  so no terminal window spawns. Add the wyrm2 connection once in Remmina (RDP, host
  10.42.0.20, username agentydragon). (gnome-connections is out: gtk-frdp crashes on
  xrdp.)
- **Try GNOME instead of Xfce.** Reuse wyrm2's existing GNOME for a fuller/consistent
  desktop; `gnome-session` self-manages its env. Caveat: GNOME 50 is Wayland-primary,
  so an Xorg session under xrdp may not exist — check before assuming.
- **Visual density / HiDPI.** The Xfce session could use tuning (scale factor /
  resolution) for the client display.
- **Sunshine/Moonlight** (Path B) and **Guacamole via Authentik RAC** (`x/linux_rac/`)
  remain later passwordless options (Sunshine needs a logged-in seat; Guac is
  greenfield, and its SSH protocol is terminal-not-graphical until Kerberos/mTLS).
- GRD (#3424) stays merged-dormant (NixOS-blocked; inert).
