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

The first VM diagnostic failed during the synthetic owner's initial COSI reconciliation, before
the restart: marker creation under `/run/agentplane-proof-lifetime-*` returned `read-only file
system`. The packaged Runtime unit uses `ProtectSystem=strict` and allows writes only under
`/run/agentplane/execution-service` (and its other declared paths), so the fixture now places its
unique systemd-managed RuntimeDirectory under that writable subtree. This changes only the test
owner's path; it does not relax the Runtime sandbox.

The corrected fixture passed in [BuildBuddy invocation 48820de0](https://app.buildbuddy.io/invocation/48820de0-12c5-44bb-bd98-ad98ee4c0990) on PR head `ce73ee24`. The `test_vm` target passed 1/1 in 281.70 seconds. Its guest console reports systemd `257.9-1~deb13u1`, and records
`TestSystemdKernelFixture/held_old_invocation_is_fenced_after_successor_inventory` passing, along
with the complete supervision fixture. This proves the held-request behavior exercised here; it
does not prove delivery after the original D-Bus sender dies.

The first pair matches the system-test guest; the second pair is for the Nix-built environment VM:

- [systemd.unit.xml, v257.9](https://github.com/systemd/systemd/blob/v257.9/man/systemd.unit.xml)
- [systemd.exec.xml, v257.9](https://github.com/systemd/systemd/blob/v257.9/man/systemd.exec.xml)
- [systemd.unit.xml, v260.5](https://github.com/systemd/systemd/blob/v260.5/man/systemd.unit.xml)
- [systemd.exec.xml, v260.5](https://github.com/systemd/systemd/blob/v260.5/man/systemd.exec.xml)
