# Claude session transcript collection

**Open goal:** verify that the operator's Claude Code session history is
preserved across cloud sessions and configured workstations, then identify which
metrics Haku still needs. The former proposal to rsync every habitat's
`~/.claude/projects/**/*.jsonl` into a custom `transcript-sink` is superseded:
cloud events have a PostgreSQL mirror, and workstation transcript files already
have an encrypted Restic backup path. These are distinct stores with different
query and recovery properties.

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
- The workstation backup is enabled in the Home Manager configs for `wyrm2`,
  `rugged`, and `iguana`. Its systemd user timer runs every 15 minutes and backs
  up the Claude home tree, including `projects/` transcript files, to the
  encrypted append-only Restic service. The backup excludes runtime/cache paths
  such as `session-env/`, but not `projects/`. Its tests restore a sample
  `projects/...jsonl` file. This establishes source-level intent and fixture
  coverage, not that each live timer is running or has a restorable recent
  snapshot.
- The API mirror and Restic file archive cover different surfaces. The mirror
  supports querying cloud events; workstation transcripts are preserved as files
  for recovery. Confirm whether any local-only session source or operator host
  falls outside the three configured backups before claiming complete coverage.

## Remaining work

1. Verify the deployed cloud mirror has been paired and backfilled, including
   archived and routine-trigger sessions. Check sync lag and recovery behavior;
   add alerting and database backup coverage where still missing. Do not infer
   live health from the checked-in deployment or image pin.
2. Verify the workstation backup timer and recent successful snapshots on
   `wyrm2`, `rugged`, and `iguana`; prove a representative Claude transcript can
   be restored, and identify any operator machine not covered by these configs.
   Keep the existing Restic client as the file-archive path unless a concrete
   coverage or query requirement calls for another mechanism.
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
measured gaps show that the cloud API mirror or the configured workstation
Restic backups miss required data. Add another client only for a specific
missing source or query requirement; do not duplicate cloud sessions by default.

OpenTelemetry raw API-body capture is a separate data path from session event
history. The current Home Manager setting is `OTEL_LOG_RAW_API_BODIES=1`; inline
capture is truncated. File mode can preserve full bodies but increases exposure
of prompts and tool content. Consider changing modes only if a metric or replay
requirement needs those bodies and the consent, retention, and access policy is
explicit.

The current telemetry consumer is tracked in
[Haku's run-telemetry work](https://github.com/agentydragon/ducktape/pull/2932).
