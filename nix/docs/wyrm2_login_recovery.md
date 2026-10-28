# Wyrm2: login returns to the greeter

Recovery for the `A graphical session is already running!` abort observed in
July 2026. Check the signature before terminating sessions; killing their
processes stops any work still running in them.

## Symptom

- Type password on the GDM greeter (either seat) → screen freezes / bounces
  back. Every compositor choice fails the same way: **sway and GNOME both**.
- `journalctl -u display-manager` (with GDM debug on, see below):

  ```text
  GdmSession: Emitting 'session-started' signal with pid 'NNN'
  .gnome-session-[MMM]: A graphical session is already running!
  GdmSession: session was killed with status 6        # SIGABRT
  GdmDisplay: Session never registered, failing
  ```

- Coredump present: `gnome-session-init-worker` SIGABRT, backtrace is
  `main → g_log → abort` (a fatal `g_error`).

## Check both stale-session layers

`gnome-session` enforces **one graphical session per user** and aborts (fatally)
if it thinks one already exists. A messy logout (compositor died without tearing
down) leaves two _independent_ leftovers, and either one trips the guard:

1. **Zombie logind session** — `loginctl` still shows the old session in
   `State=closing` / `Active (abandoned)` because long-lived daemons **spawned
   inside its `session-N.scope`** keep the scope alive. On wyrm2 the usual
   culprits are **bazel JVM servers and tmux** started from terminals in the
   dead session. `Type=wayland` + `Class=user` → logind still reports a
   graphical session for the user.

2. **Stuck `graphical-session.target`** in the **persistent `user@UID.service`
   systemd manager**. `user@UID` is a _single
   per-user manager_ that outlives individual login sessions, so the stale
   target survives logout, session kill, **and every `systemctl restart
display-manager`**.

### Restarting GDM is insufficient

**Restarting the display manager does nothing for either leftover** — they live
in the user slice / user manager, not in GDM. Restarting GDM "N times" is
restarting the wrong layer.

## Recovery (ordered — do all of it; the two leftovers are independent)

```bash
# 0. See the debris. Look for extra graphical sessions and the stuck target.
loginctl user-status <user>          # note any *extra* wayland/closing session
systemctl --user -M <user>@ is-active graphical-session.target   # 'active' == stuck

# 1. Kill whatever pins the zombie session's scope.
#    terminate-session sends SIGTERM, which idle bazel JVMs SURVIVE — so find the
#    pinning PIDs and SIGKILL them directly.
cat /sys/fs/cgroup/user.slice/user-<UID>.slice/session-<N>.scope/cgroup.procs
kill -9 <those pids>                 # bazel servers / tmux / etc.
loginctl list-sessions               # zombie session should now be GONE

# 2. Clear the stale target in the persistent user manager (the hidden one).
systemctl --user -M <user>@ stop graphical-session.target
systemctl --user -M <user>@ is-active graphical-session.target   # want: inactive

# 3. Retry the login. gnome-session should now register:
#    'GdmDisplay: session registered: yes'
```

Nuclear option if the pieces keep coming back (clears ALL persistent user-session
state; login-session SSH scopes survive, user services blip): `sudo systemctl
restart user@<UID>.service` — or just reboot.

## GDM diagnostics

Without GDM debug logging the daemon goes **silent after PAM** — you see the
session open and then nothing, which reads as an inscrutable freeze. Turn it on:

```nix
services.displayManager.gdm.debug = true;   # wyrm2/default.nix
```

Rebuild, reproduce, and the daemon prints the real cause (`A graphical session
is already running!` / `session registered: no`).

## Unresolved teardown leak

The teardown leak is the actual bug. Options, roughly in order of preference:

- Keep long-lived daemons (**bazel servers, tmux**) **out of the login-session
  scope** so they can't pin `session-N.scope` after the compositor dies. A
  `systemd-run --user --scope`/lingering-service home, or launching them under
  `user@UID` rather than the session, would stop the zombie from forming.
- Make logout actually stop `graphical-session.target` and reap the session
  scope.

Until one of those lands, every messy logout re-arms the trap.

The separate July non-`seat0` handoff failure is described in
[the display guide](wyrm2_display.md#constraints-from-the-multiseat-investigation).
