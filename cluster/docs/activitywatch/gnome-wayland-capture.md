# ActivityWatch window bucket empty on rugged (GNOME Wayland)

[ActivityWatch project overview](README.md).

Investigation/RCA — 2026-07-07. rugged = ThinkPad, GNOME Shell 49.4, Wayland-only.

## TL;DR

The `aw-watcher-window_rugged` bucket (local and the synced
`aw-watcher-window_rugged-synced-from-rugged` on cluster) has **zero events**. AFK and
Chrome-tab buckets flow fine, so presence + browser-focus are captured; the missing
signal is "which non-browser app has focus" — the richest prioritization input.

Root cause is on-device, watcher-side (not sync): the stock `aw-watcher-window` cannot
see windows on GNOME Wayland. **A maintained, maintainer-endorsed solution exists and is
packaged in nixpkgs**: `awatcher` (Rust binary) + the `focused-window-d-bus` GNOME Shell
extension. Both ActivityWatch co-founders redirect GNOME-Wayland users to this combo, and
it's verified on GNOME Shell 49.0.

## Evidence (verified on rugged, 2026-07-07)

Local aw-server (`127.0.0.1:5600`) bucket event counts:

```text
aw-watcher-window_rugged/events/count   → 0      ← empty
aw-watcher-afk_rugged/events/count      → 3      ← control, working
aw-watcher-web-chrome_localhost         → populated
```

The window bucket exists (created at aw-qt startup) but `metadata.start`/`end` are
`null` and `events` is `null` — the watcher registered the bucket and never wrote a
sample.

Session + watcher process:

```text
XDG_SESSION_TYPE=wayland   XDG_CURRENT_DESKTOP=GNOME   ShellVersion=49.4
aw-watcher-window-0.13.2 running as a child of aw-qt (PID via .aw-watcher-window-wrapped)
```

The watcher throws on every poll (journal, once per second):

```text
aw-qt.desktop[…]: [ERROR]: Exception thrown while trying to get active window (aw_watcher_window.main:133)
  … aw_watcher_window/lib.py:64 get_current_window
  … aw_watcher_window/lib.py:17 get_current_window_linux
  … aw_watcher_window/xlib.py:85 get_window_name
```

Its only window-detection dependency is `python3.13-xlib-0.33` — no dbus/gtk/wnck
backend. It reads `_NET_ACTIVE_WINDOW` off the X root window, which Mutter-on-Wayland
does not maintain for native Wayland clients, so it throws and writes nothing.

## Why every out-of-process method fails on GNOME 49

Every method an _external_ process can use to read the focused window is closed off on
GNOME 49 (all probed on rugged):

| Method                                                                  | Result                                                                                              |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| stock `aw-watcher-window` (xlib `_NET_ACTIVE_WINDOW`)                   | throws every poll                                                                                   |
| `aw-watcher-window-wayland` (wlroots `wlr-foreign-toplevel-management`) | Mutter does not implement the protocol → nothing                                                    |
| `org.gnome.Shell.Eval` (JS `global.display.focus_window`)               | `(false, '')` — disabled in `Mode="user"` (gated to `unsafe-mode` since GNOME 41)                   |
| `org.gnome.Shell.Introspect.GetWindows()`                               | `org.freedesktop.DBus.Error.AccessDenied: GetWindows is not allowed` (whitelisted since GNOME 3.32) |

These closures all target _out-of-process_ introspection. The one path that works is code
running **in-process inside `gnome-shell`** (a Shell extension), which reads
`global.display.focus_window` directly via GJS and re-exports it on a custom session-bus
interface that an external watcher polls.

## The solution: `awatcher` + `focused-window-d-bus`

- **Extension** `focused-window-d-bus` (flexagoon, e.g.o #5592,
  <https://github.com/flexagoon/focused-window-dbus>) — runs in-process, exports
  `org.gnome.shell.extensions.FocusedWindow.Get()` at
  `/org/gnome/shell/extensions/FocusedWindow`, returning the focused window's
  `{app, title}`.
- **Watcher** `awatcher` (2e3s, Rust, <https://github.com/2e3s/awatcher>) — polls the
  extension's D-Bus method once per second and POSTs heartbeats to aw-server at
  `127.0.0.1:5600`. Also handles AFK via `org.gnome.Mutter.IdleMonitor` (no extension
  needed for AFK). Default `--poll-time-window 1s`, `--host 127.0.0.1 --port 5600`.
- Writes bucket `aw-watcher-window_<hostname>`, type `currentwindow`, data `{app, title}`
  — **the same shape as the stock watcher** (contract below). This was originally
  compared with the aw-sync/Syncthing pipeline; the current importer reads the local
  aw-server directly.

**Maintainer endorsements**: ErikBjare (`aw-watcher-window#46`, 2024-11-26) and
johan-bjareholt (forum #3947, 2025-05-24) both redirect GNOME-Wayland users to
`awatcher` + this extension. Verified working on GNOME Shell 49.0 / Wayland in
`ActivityWatch/activitywatch#1218` (2026-03-19). The master Wayland tracking issue
`activitywatch#92` (open since 2017-08-02) documents why no first-party watcher exists:
the project refuses compositor-specific watchers and is waiting for a freedesktop portal
protocol Mutter will likely never adopt.

### nixpkgs packaging (verified 2026-07-07)

```text
pkgs.awatcher                              → awatcher-0.3.1          (upstream 0.3.3; minor lag)
pkgs.gnomeExtensions.focused-window-d-bus  → …-focused-window-d-bus-9
  metadata.json: shell-version ["49"], uuid focused-window-dbus@flexagoon.com   ← loads on GNOME 49
```

awatcher CLI (`nix run nixpkgs#awatcher -- --help`) confirms default
`--host 127.0.0.1 --port 5600`, `--poll-time-window 1s`, window + idle, `--no-server`.

## aw-server contract (reference)

Confirmed from the stock watcher source (`aw_watcher_window/main.py`, `lib.py`) and a
local throwaway-bucket round-trip; `awatcher` writes the same shape:

- Bucket id: `aw-watcher-window_<hostname>` → `aw-watcher-window_rugged`
- Bucket type: `currentwindow`, client: `aw-watcher-window`, hostname: `rugged`
- Create (idempotent): `POST /api/0/buckets/aw-watcher-window_rugged` body
  `{"id":"aw-watcher-window_rugged","type":"currentwindow","client":"aw-watcher-window","hostname":"rugged","data":{}}`
- Heartbeat: `POST /api/0/buckets/aw-watcher-window_rugged/heartbeat?pulsetime=2` body
  `{"timestamp":"<iso8601 utc>","data":{"app":"<wm_class>","title":"<title>"}}`
- `app`/`title` fall back to `"unknown"` when unresolved.

## Current state

The recommended `awatcher` plus `focused-window-d-bus` implementation is deployed from
`nix/home/services/activitywatch.nix`; it owns window and AFK capture and emits the same
bucket shapes as the stock watchers. Cluster ingestion is documented in
`cluster/docs/activitywatch/README.md`. The remaining device-specific question is only
whether another host needs a compositor-specific watcher configuration.
