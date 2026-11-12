# Claude session transcript collection

**Open goal:** keep the operator's Claude Code session history and the metrics
needed by Haku available automatically in an owner-controlled store. The former
proposal to rsync every habitat's `~/.claude/projects/**/*.jsonl` into a custom
`transcript-sink` is no longer the current design for Claude Code cloud sessions.

## Current sources and status

- [`session_export`](../session_export/) reads the full event history for Claude
  Code cloud sessions through the private `claude.ai/code` API. Its supported
  scope includes web, mobile, desktop-cloud, archived, and routine sessions. It
  can export an archive or keep a PostgreSQL mirror current; the sync includes
  routine-trigger sessions and follows recent sessions between polling cycles.
- The cluster service is described in
  [`session_export/docs/deploy.md`](../session_export/docs/deploy.md), with its
  desired resources in `cluster/cdk8s/claude_session_sync.py`. Pairing with the
  account is a one-time operator action before the first backfill. Source and
  GitOps configuration establish the intended deployment, not that the live
  service is paired, current, complete, or healthy; this plan keeps that
  operational acceptance open.
- The API path is private and undocumented, and may change without notice. Treat
  the session credential as sensitive and keep its single-writer refresh-token
  ownership as documented by `session_export`.
- `session_export` reads the cloud API; it does not read local Claude Code CLI
  transcript files from operator machines. Confirm whether local-only sessions
  appear in the cloud API before claiming that history is covered. Keep the cloud
  mirror and any local transcript archive distinct until their overlap and
  identity are understood.

## Remaining work

1. Verify the deployed cloud mirror has been paired and backfilled, including
   archived and routine-trigger sessions. Check sync lag and recovery behavior;
   add alerting and database backup coverage where still missing. Do not infer
   live health from the checked-in deployment or image pin.
2. Decide which operator-machine CLI transcripts must be centrally retained,
   which hosts may contribute them, and whether the source files are already
   covered by another archive. If collection is needed, implement a narrow,
   owner-managed client (the earlier home-manager timer is one candidate) with
   explicit source identity, incremental/retry behavior, and retention. Avoid
   building a hosted rsync loop for data already covered by the cloud API.
3. Compare Haku's required run telemetry with the fields already available in
   the session mirror and existing OTel pipeline. Define only the missing
   derived metrics, their source of truth, and their retention before adding a
   processor or dashboard. Keep raw transcript storage separate from metric
   publication.
4. Document and enforce who can read, export, and remove transcript data.
   Transcripts can contain secrets and tool output. Any local-file or raw-body
   collection must specify consent, retention, and access before it is enabled.

## Conditional coverage

The earlier design proposed a `transcript-sink`, Kubernetes-exec shippers from
web sessions, and home-manager rsync timers. Those remain options only if
measured gaps show that the API mirror and any approved local archive do not
supply required data. In particular, add a hosted transcript shipper only for a
specific missing source or event field; do not duplicate cloud sessions by
default.

OpenTelemetry raw API-body capture is a separate data path from session event
history. Inline capture is truncated; file mode can preserve full bodies but
increases exposure of prompts and tool content. Consider it only if the metric
or replay requirement needs those bodies and the consent, retention, and access
policy is explicit.

The current telemetry consumer is tracked in
[Haku's run-telemetry work](https://github.com/agentydragon/ducktape/pull/2932).
