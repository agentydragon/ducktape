# Rough edges, knowns and unknowns

## Rough edges worth knowing

- **Two `OpenClawInstance`s with the same agent id share one sandbox.** The
  derived name collides, so the first lab instance silently attached to
  production's sandbox. Distinct agent ids are mandatory for isolation.
- **`sandbox.mode: off` must be quoted in YAML.** Unquoted `off` parses as
  boolean `false` and the gateway crash-loops with
  `Invalid input (allowed: "off", "non-main", "all")`.
- **`kubectl auth can-i` returns false positives here.** It answered `yes` for
  `pods/log` and `pods/exec` where the real calls returned 403. Verify with the
  real call.
- **`pods/exec` needs `get`, not just `create`** — kubectl tries the WebSocket
  transport first, which authorizes as `get`.
- **The memory tools are silently stripped.** `memory_get`/`memory_search` in
  `tools.allow` are removed again by the _sandbox_ tool policy, which falls back
  to `DEFAULT_TOOL_ALLOW` when `tools.sandbox.tools.allow` is unset. The
  effective surface was `exec`, `process`, `session_status`. Only the gateway log
  shows this.
- **Three measurement artifacts nearly became findings.** Each looked like a
  result and was a bug in the probe. Written down because the pattern repeats:
  `${VAR:-NO}` in a presence check prints the value (leaked a token prefix);
  unique-per-word probe filler tokenizes several tokens per word, so a "360k"
  context probe was much larger and failed for the wrong reason; and testing a
  _renamed_ instance for the old variable name returned a clean `/proc` scan that
  meant nothing. Before believing a negative result, check that the probe could
  have produced a positive one.
- **Always verify an OpenClaw config's shape before shipping it.** The config is
  opaque JSON/YAML to every tool in this repo, so an invalid value is accepted by
  the file, passes `kubeconform` and `pre-commit`, and is rejected only when the
  gateway loads it -- one deploy cycle per typo. Three have bitten so far:
  `bind: "all"` (not in `{auto, lan, loopback, custom, tailnet}`), `mode: off`
  unquoted in YAML (boolean `false`, not the string), and inventing field names
  generally. The harness ships the means to check:

  ```bash
  openclaw config schema     # full JSON schema for openclaw.json
  openclaw config validate   # check the active config, no gateway start
  ```

  Read the schema for the field you are setting rather than inferring the value
  from prose or from another config. Every one of the three was avoidable that way.

- **The driving CLI never reaches the gateway.** Every
  `openclaw agent …` run from inside the pod reports
  `"transport":"embedded","fallbackFrom":"gateway"`, because
  `openclaw gateway status` probes `ws://127.0.0.1:18789` and is refused with
  `device identity required`. So these results describe the _embedded_ runner.
  Pod-scoped properties (S3 storage, S4 egress) are unaffected — same pod, same
  PVC, same NetworkPolicy — but the gateway's own agent path is not covered by
  any of this, and real channel traffic uses it.
- **Never `kubectl exec` into `openshell-sandboxes`.** It looks like a
  read-only diagnostic and is in fact destructive (F1).
- **First contact costs a turn.** A fresh agent id lands in bootstrap and asks
  who you are instead of running the task; and destructive-looking commands
  (`rm -rf`) draw a confirmation turn.

## Knowns and unknowns

**Known:**

- The wedged state is understood: the sandbox's SSH Unix socket gets re-bound
  and orphaned, so the gateway's relay gets `ECONNREFUSED` (F1). Recovery is
  sandbox recreation. `kubectl exec` into the sandbox pod is a proven trigger;
  production reached the same state by some other route, still unidentified.
- Multi-turn repo work is possible in mirror mode for repos that clone fast; a
  yielding clone did **not** reproduce the retention bug in the lab, so that
  model needs refining.
- **S3 memory works, verified end to end** on `oc-plain`: written to `MEMORY.md`
  in one session, recalled verbatim in a fresh one. The chain that findings.md
  reasoned out from source is real.
- **S4 is achievable** (`oc-plain`), and requires leaving the operator behind.
- **All four hard requirements pass together on one configuration**: `oc-plain`
  clears S1, S2, S3 and S4, plus the S5 want. That is the answer to "is there a
  setup that meets the bar" — yes, at the cost of the harness/execution split.
- Memory survives a pod restart: after the Deployment rolled, a new pod still
  had `MEMORY.md` with the stored fact.
- Operator shape cannot meet S4 (F3). Domain allowlisting itself is solved (F4).
- Sandbox confinement is process-scoped, not pod-scoped (F2).

**Unknown:**

- Whether the `--mode=process` re-bind of `/run/openshell/ssh.sock` is exactly
  the mechanism, or merely correlated with it. The causation is settled (F1);
  the last step is not directly observable without root inside the sandbox,
  which the supervisor denies.
- Whether anything _other_ than `kubectl exec` triggers the same re-bind — a
  liveness probe, an operator sidecar, or a second gateway attaching would all
  be worth checking before trusting the OpenShell shape unattended.
- Whether `oc-plain` stays stable over days rather than hours. At 8h+ uptime it
  still completed an `exec` round-trip and recalled a stored memory in a fresh
  session, but that is spot-checking, not a soak test.
- What exactly the agent runs as inside the harness container, and what that
  means for a personal-data agent: with `sandbox.mode: "off"` there is no
  isolation _within_ the boundary, which is the acknowledged cost of this shape.
- Whether the split topology (W2) can be had without the OpenShell relay —
  cloud workers being the candidate (findings.md, C8).
- Whether OpenClaw-inside-OpenShell (whole harness, NemoClaw-style) is
  declarable here: the `agents.x-k8s.io` `Sandbox` CRD has no network-policy
  field (`operatingMode`, `podTemplate`, `service`, `shutdownPolicy`,
  `shutdownTime`, `volumeClaimTemplates`), so that shape would still need its
  egress boundary from elsewhere.
- Cloud workers as a topology — OpenClaw's mature git-based split (findings.md,
  C8) — untested, and its bundled provider leases cloud VMs.
