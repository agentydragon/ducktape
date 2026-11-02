# Systemd invocation fence probe

The VM fixture now exercises the restart boundary with a dedicated transient owner service. It
does not restart or otherwise modify the production Runtime unit. The service uses systemd's
`RuntimeDirectory=` with `RuntimeDirectoryPreserve=no`, and the fixture reads A and B invocation
IDs from the real manager. The actual COSI `systemd-scope` reconciler prepares A's marker and later
performs B's successor inventory; readiness is observed through the existing COSI status.

Before restarting A, the fixture starts two Runtime-A `Process` objects and blocks both in the
`startTransient` seam after capturing their full systemd property lists. Their unit names are
absent from the manager. After the owner restarts as B, systemd removes A's marker and recreates the
stable RuntimeDirectory. The B scope reconciler then reaches Ready and its real inventory confirms
both captured unit names are still `not-found`. Only after that scan does the fixture submit the
captured A properties:

- The first request retains its A `ConditionPathExists` property. systemd skips execution; the
  process reports a failed start, publishes no PID and creates no effect file.
- The control request removes only `Conditions` from its captured A property list. It retains
  `BindsTo=owner.service` and `After=owner.service` with the same stable owner name. Its distinct
  effect file appears while owner B is active. This demonstrates that those stable-name
  dependencies by themselves bind the late request to B.

This is a bounded late-manager-arrival probe: the fixture process and its D-Bus connection remain
alive while a start is held locally, then the manager receives that request after the restart and
inventory. It does not prove that PID 1 delivers a request after the original sender process dies,
nor that a marker can undo external effects from a command which already began. COSI readiness and
inventory fence manager-visible units; they are not a distributed fence around arbitrary effects.

The system-test guest's console reports systemd `257.9-1~deb13u1`; this is the manager used by the
probe. Separately, the Nix-built `agentplane-environment-vm` package resolves systemd 260.5 from
the locked `nixos-26.05` set. The version-pinned upstream docs rule out the simple alternatives
considered here:

- `ConditionEnvironment=INVOCATION_ID=...` reads the service manager's environment block; the
  per-unit `$INVOCATION_ID` is passed to the service process environment. It does not compare a
  transient unit against the sender's invocation.
- `BindsTo=` with `After=` enforces a dependency on the referenced unit's active state. Runtime
  uses one stable packaged service name across restarts, which the positive control shows is
  insufficient for distinguishing an old A request from a new B owner.
- `RuntimeDirectoryPreserve=no` removes the RuntimeDirectory when a service stops, including for
  restart; the same configured directory is created again for B. The invocation-named marker
  supplies the distinction that the directory alone lacks.

Accordingly, this probe retains the marker. A per-invocation owner unit name could give systemd a
different dependency identity, but would require replacing the current stable Runtime service
ownership and coordinating those transient owner units. That is a larger lifecycle design, not a
simpler drop-in for this marker. Revisit only if a proposed native fence passes the same two-launch
probe with the no-condition control still demonstrating why stable-name dependencies are not
enough.

The first pair matches the system-test guest; the second pair is for the Nix-built environment VM:

- [systemd.unit.xml, v257.9](https://github.com/systemd/systemd/blob/v257.9/man/systemd.unit.xml)
- [systemd.exec.xml, v257.9](https://github.com/systemd/systemd/blob/v257.9/man/systemd.exec.xml)
- [systemd.unit.xml, v260.5](https://github.com/systemd/systemd/blob/v260.5/man/systemd.unit.xml)
- [systemd.exec.xml, v260.5](https://github.com/systemd/systemd/blob/v260.5/man/systemd.exec.xml)
