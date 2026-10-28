# VM native-exit investigation

PR #12745, Runner head `197d4a47f882dbd4c65c17ac1f2b276e8c8ba1bb`.

## Evidence

- Hosted invocation `b057e426-1cd9-5317-a37e-e0a2368d7a6f`: Claude's initialize
  request was journaled, but the harness failed before a model request. Codex turn
  and tool-OOM cases passed. The native-only exporter discarded stderr and exit
  observations, so the exact launch failure cannot be recovered from its artifacts.
- Focused invocation `5f86d746-79d7-4c98-9ca9-b4d3f785943f`: Claude turn passed.
  Teardown failed because the VM was forcibly stopped without its shutdown marker.
  Runner stopped normally; `/state` unmounted; the console ended before the proof
  unit ran. This does not establish a Runner shutdown hang.
- Same-merge-tree retry `fd8c8c36-9529-5c71-8ff2-f2843a6bbe67`: ordinary Claude and
  Codex turns passed. The OOM victim was the intended `awk` process. Runner and
  app-server survived that kill, and a follow-up command completed in the same
  executor cgroup. Afterwards, the final Runner snapshot had no app-server and a
  failed Session at cursor 117. No global OOM or Runner restart was observed.

In the latter guest, the kernel marked TSC unstable and switched to `acpi_pm` at
21:03:39 UTC. An app-server item-completed frame emitted at approximately 21:03:37
was journaled at 21:03:57. Executor health probes timed out; native systemd fencing
logged a deadline failure at 21:03:58. Scheduling and event delivery were degraded,
but those observations alone do not identify the app-server's exit cause.

## Candidate mechanism

`systemdBackend.reap` uses a five-second deadline for manager status reads. Any
read error currently exits its observation loop with default exit code 125 and
actively fences the native domain. Thus a transient status timeout can kill a
live harness. The initiating read error is not logged.

Retrying a deadline on a still-connected manager must preserve the native
identity, stdin, pending Wait, and COSI ownership. Actual manager disconnection
must retain the existing fence-before-release behavior. A regression should
inject the deadline at the reaper boundary while launching and observing a real
systemd service; an intentional later exit must retain its true status.

The exporter now preserves all Session events, including stderr and lifecycle
observations. Subsequent runs can distinguish natural exit, Runner-requested
stop, and synthesized code 125. The archived events are needed to connect the
candidate mechanism to a particular CI failure.
