# Claude session transcript collection

Status: **plan** (2026-07-05/06, designed in the Haku architecture session). Goal:
every Claude Code session's transcript (`~/.claude/projects/**/*.jsonl`) and derived
metrics land in one operator-owned store **automatically** — web sessions, routines,
and the operator's own machines — with no per-session manual step and no reliance on
agent cooperation. Primary consumer today: Haku's run telemetry
(<../../../haku/plans/wake*model_and_eval.md> → \_Logging*); the collector itself is
agent-agnostic.

## Facts this design rests on (verified 2026-07-05)

- **No export API exists.** Claude Code web session transcripts cannot be downloaded
  after the fact: the enterprise Compliance API covers claude.ai chats/files/projects
  only, and the account data export has no documented Code-session coverage. The
  transcript exists only inside the container while it lives.
- **Env-var delivery in hosted sessions splits by mechanism** (verified via
  `/proc/<claude-pid>/environ` in a live remote session, claude 2.1.42): the web UI
  "Environment Variables" knob reaches the `claude` process; `startup_env_script`
  outputs reach Bash subprocesses only. (Relevant to the OTel leg below; matches
  <../docs/secrets_env_flow.md>.)
- **Hooks in routine-fired sessions: unverified.** `Stop`/`SessionEnd` hooks receive
  `transcript_path` and run from repo config in interactive web sessions, but Haku
  runs 32/35 came up without the hook daemon, so routine sessions may not fire hooks
  reliably. A diagnostic probe (fresh session in the routine's environment, fired
  2026-07-06) is pending — **the design below does not depend on the answer**; hooks
  are only a latency optimization.
- Claude Code transcripts are **append-only JSONL** during a session — rsync's
  `--append-verify` happy path.

## Architecture: dumb shippers, one smart sink

**Client side is literally `rsync`.** No bespoke shipper binary, no offset state:

```sh
rsync -rt --append-verify -e ./kexec-rsh \
  ~/.claude/projects/ sink:/data/${SOURCE_NAME}/
```

`kexec-rsh` is a ~3-line wrapper that drops rsync's hostname argument and execs
`kubectl -n agents-infra exec -i deploy/transcript-sink --`. **The kube API is the
one transport every habitat already has authenticated access to**: web containers
(session kubeconfig; exec via the `kubeapi-proxy` WebSocket path, already proven),
operator machines (personal kubeconfig; `rugged`/`wyrm2` are cluster nodes), and
anything remote via `kubeapi.allegedly.works`. No sshd, no new ingress/egress, no
new credential class — Kubernetes RBAC is the auth.

**Sink**: `transcript-sink` Deployment in `agents-infra` — a minimal image carrying
`rsync`, one PVC, per-source subdirectories (`$SOURCE_NAME` = hostname or environment
slug). All intelligence is sink-side, so clients never change:

- a processor (sidecar or CronJob) parses newly arrived JSONL into per-session
  `summary.json` (tokens in/out/cache-read/cache-write, per-model, tool-call counts,
  wall time — for Haku: orientation share, event→surface latency inputs);
- retention/compaction policy;
- optional later forwarding: summaries → Loki/Mimir for Grafana; raw files →
  seaweedfs S3.

**Triggers per habitat:**

| Habitat                            | Trigger                                                                                                   |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Web envs (incl. routines)          | bootstrap background loop: `while :; do rsync …; sleep 120; done` — works regardless of hook availability |
| Operator machines                  | home-manager systemd user timer (the `nix/TODO.md` OTel item's sibling)                                   |
| Hooks (if the probe confirms them) | `Stop` → same rsync one-liner, as a low-latency extra; never load-bearing                                 |

## Security note (accepted trade-off)

`pods/exec` cannot be restricted to a command: **every principal granted shipping
access can read the sink's entire PVC**, i.e. all sources' transcripts. Acceptable
within the current trust tier — all shippers are the operator's own Anthropic-harness
agents, and transcripts of any of them are operator-sensitive-class anyway. Two hard
lines: (1) worker-zone agents (zai/oai) get **no** kube credential by construction and
must never be granted this Role; (2) the sink pod carries no secrets and does nothing
but receive, so exec access ≈ transcript-read access, nothing more. If tier-splitting
is ever wanted, run per-tier sink pods in separate namespaces — additive, no client
redesign.

## Secondary leg: native Claude Code OTel (metrics dashboards)

Independent of transcripts: `CLAUDE_CODE_ENABLE_TELEMETRY=1` +
`OTEL_METRICS_EXPORTER=otlp` set as **UI env vars** (verified to reach the claude
process) pointed at `http://127.0.0.1:4318`, with a local forwarder attaching the
hourly-rotated `DUCKTAPE_OTEL_BEARER_TOKEN` and relaying to
`alloy-otlp.allegedly.works` (→ Mimir/Grafana). Smoke test first: UI env vars + a
dumb localhost listener started by bootstrap; one session tells us whether the remote
harness build emits at all. This leg is dashboards-only; the rsync path remains the
lossless record (OTel events truncate at 60 KB).

## Build order

1. **Sink**: `agents-infra` Deployment + PVC + Role/RoleBindings (`haku`,
   sandbox-users group, operator). Manifests under `cluster/k8s/agents/transcript-sink/`.
2. **Clients**: `kexec-rsh` + rsync loop in the web bootstrap
   (<../claude_hook/> profile or `web_setup.sh`); home-manager timer for machines.
3. **Sink processor**: summary.json per session; wire Haku's run-manifest rows to it.
4. **OTel smoke test**, then the local forwarder if it emits.
5. Revisit hooks as fast-path once the routine probe verdict is in.
