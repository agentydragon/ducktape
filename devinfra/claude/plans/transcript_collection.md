# Claude session transcript collection

Status: **plan; the OTel leg is implemented** (PR #2930). Goal: every Claude Code
session's transcript (`~/.claude/projects/**/*.jsonl`) and derived metrics land in
one operator-owned store **automatically** — web sessions, routines, and the
operator's own machines — with no per-session manual step and no reliance on agent
cooperation. Primary consumer today: Haku's run telemetry
([wake_model_and_eval.md](../../../haku/plans/wake_model_and_eval.md) → _Logging_,
PR #2932); the collector itself is
agent-agnostic.

## Facts this design rests on (probed live 2026-07-05/06)

- **No export API exists.** Claude Code web session transcripts cannot be downloaded
  after the fact: the enterprise Compliance API covers claude.ai chats/files/projects
  only, and the account data export has no documented Code-session coverage. The
  transcript exists only inside the container while it lives.
- **Env-var delivery in hosted sessions splits by mechanism** (verified via
  `/proc/<claude-pid>/environ`, claude 2.1.42): the web UI "Environment Variables"
  knob reaches the `claude` process (12 `OTEL_*` vars confirmed); `startup_env_script`
  outputs reach Bash subprocesses only. The subprocess scrub also hides `OTEL_*` from
  Bash — **inspect `/proc/<claude-pid>/environ`, never `env` from a shell**, when
  checking what claude sees.
- **Repo `.claude/settings.json` applies in remote sessions — even from `--add-dir`**
  (verified via the claude `--debug` diagnostics log: 4 settings sources, 0 errors;
  both the harness launcher hook and ducktape's `claude-hook` SessionStart hook
  spawned). `SessionEnd` also observed firing. Caveat (operator recollection,
  unverified): multi-repo web sessions may not load repo settings — verify before
  relying on settings.json in multi-source environments.
- **The SessionStart → hook daemon → background-commands chain works in
  routine-fired sessions**: run 39 (2026-07-05) was fired by the Haku routine and its
  bootstrap — a profile background command behind that chain — ran normally. The
  run-32/35 "no hook daemon" incidents were transient harness flakes, not a
  systematic gap. (A follow-up self-report is queued via Haku's intake for explicit
  confirmation; a synthetic probe session fired 2026-07-06 produced no output —
  inconclusive, superseded by the in-run self-report.)
- **Claude Code's native OTel exporter runs in hosted sessions, but its direct
  egress doesn't arrive.** With the endpoint set to `127.0.0.1`, an in-session
  listener captured all three signals (metrics ~10 s, logs ~5 s, traces); with the
  endpoint set to the public Alloy host, nothing ever reached Alloy's receiver,
  while the same authenticated POST from a shell (via the egress proxy) returned 200. Exact network-layer cause not pinpointed — possibly only the environment's
  domain allowlist (untested); the localhost relay sidesteps it either way.
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

| Habitat                   | Trigger                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------- |
| Web envs (incl. routines) | bootstrap background loop: `while :; do rsync …; sleep 120; done`                            |
| Operator machines         | home-manager systemd user timer (the `nix/TODO.md` OTel item's sibling)                      |
| Hooks                     | `Stop` → same rsync one-liner, as a low-latency extra on top of the loop; never load-bearing |

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

## OTel leg (dashboards) — IMPLEMENTED (PR #2930)

Hosted sessions export Claude Code's native telemetry through a **localhost
forwarder** (the exporter can't egress directly; see Facts):

- <../otlp_forwarder.py> — stdlib relay, `127.0.0.1:4318` →
  `alloy-otlp.allegedly.works` via the egress proxy; bearer re-read per request from
  `~/.cache/ducktape/otel-bearer` (rotation-safe); 503 until the token exists, 502 on
  upstream failure; chunked bodies handled. Functionally verified in a live web
  container.
- <../ensure_otel_forwarder.sh> — idempotent starter, run as a profile background
  command on **every claude launch** (fresh container and resume into a recycled one;
  the init script only runs at container creation, so it is deliberately not used).
  Wired into the web, home-manager, and haku profiles. Token sources:
  `DUCKTAPE_OTEL_BEARER_TOKEN` env, else the mirrored `alloy-otlp-bearer` k8s Secret
  (with retry — the kubeconfig materializes in a sibling background command). The
  haku age key is a recipient of the bearer SOPS file, so both the web and haku
  envs normally take the env path; the k8s mirror covers SOPS-outage/bootstrap
  windows.
- Cluster: the `authentik-jwt-rotation` job publishes the bearer as
  `flux-system/alloy-otlp-bearer` (`k8s_secret` output); a `ClusterExternalSecret`
  (<../../../cluster/k8s/agents/alloy-otlp-bearer/>) mirrors it into
  `claude-sandbox` and `haku-sandbox` (openclaw-sandbox deliberately excluded —
  nothing there runs the forwarder, and its Kustomization is suspended).
- **Operator step — paste into each environment's UI env vars** (Haku env + default
  web env; content knobs per env sensitivity; OTel events truncate at 60 KB, so the
  rsync path remains the lossless record):

  ```text
  CLAUDE_CODE_ENABLE_TELEMETRY=1
  OTEL_METRICS_EXPORTER=otlp
  OTEL_LOGS_EXPORTER=otlp
  OTEL_TRACES_EXPORTER=otlp
  CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
  OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
  OTEL_LOG_USER_PROMPTS=1
  OTEL_LOG_TOOL_DETAILS=1
  OTEL_LOG_TOOL_CONTENT=1
  OTEL_LOG_RAW_API_BODIES=1
  ```

- CLI / operator machines need no forwarder: direct export is wired in
  <../../../nix/home/claude_code/default.nix> with `otelHeadersHelper` (a script
  emitting headers JSON, re-run every ~29 min; HTTP protocols only) reading the
  sops-nix materialized `secrets/alloy-otlp-bearer-token.yaml` bearer.
- Possible later simplification (untested): if the hosted-egress block was only the
  environment domain allowlist, allowlisting `alloy-otlp.allegedly.works` +
  `otelHeadersHelper` would remove the forwarder. Test before assuming.

## Remaining build order

1. **Sink**: `agents-infra` Deployment + PVC + Role/RoleBindings (`haku`,
   sandbox-users group, operator). Manifests under `cluster/k8s/agents/transcript-sink/`.
2. **Clients**: `kexec-rsh` + rsync loop in the web bootstrap
   (<../claude_hook/> profile or `web_setup.sh`); home-manager timer for machines.
3. **Sink processor**: summary.json per session; wire Haku's run-manifest rows to it.
4. **Grafana**: claude-code `GrafanaDashboard` CR once data flows; `web_selfcheck`
   check for the forwarder (4318 bound, token file fresh).
5. Grafana dashboard and alerting around native Claude Code telemetry.
