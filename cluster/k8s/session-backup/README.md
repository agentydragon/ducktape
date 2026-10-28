# AI session backup server

This namespace serves the workstations' Restic repositories over HTTPS at
`ai-session-backups.allegedly.works`. The Rest Server requires HTTP Basic authentication and runs
with `--append-only --private-repos`.

## Workstation contract

Use a repository URL with the source workstation's username as its first path segment:

```text
rest:https://ai-session-backups.allegedly.works/<username>
```

Restic can also use a subrepository below that path. Set
`RESTIC_REST_USERNAME=<username>` and that workstation's unique
`RESTIC_REST_PASSWORD`; do not put credentials in the URL. The usernames are `wyrm2`,
`rugged`, and `iguana`. Their credentials and the shared Restic encryption password are
in `secrets/shared/session-backup.yaml`, with keys
`restic_rest_username_<host>`, `restic_rest_password_<host>`, and `restic_password`.
That shared file is decryptable on all three workstations, so any of them can restore
another workstation's repository. Escrow `restic_password` in the user's password
manager before enabling the client backup timer.

The first workstation timer run captures the Claude and Codex directories,
including credentials, configuration, caches, transcripts, subagents, original SQLite
files, and their sidecars. Later runs add incremental snapshots every 15 minutes;
unchanged captures create no new snapshot. The roots respect `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, and the Home Manager XDG configuration. Captured history stays in the
append-only repository even after a harness deletes its local files.

The Claude root's `session-env/` and the Codex root's `worktrees/` are excluded from
both raw backup and SQLite discovery. The [Claude hook](../../../devinfra/claude/claude_hook/main.rs)
(`home_session_dir`, `write_session_bazelrc`) writes per-session runtime configuration
there and directs Bazel's build output into it. Previously staged SQLite images from
both excluded subtrees are also excluded from new snapshots; existing archived snapshots remain.

The client also scans both trees for SQLite file headers, regardless of filename or
subdirectory, and uses SQLite's online backup API to create consistent database images
under `${XDG_STATE_HOME}/ducktape/ai-session-backup-sqlite`. Each image is a consistent
copy of one database; the complete directory capture is not an atomic filesystem
snapshot. Original database files and WALs remain included as raw files and may be
inconsistent when captured during writes. The supplemental images provide the
consistent database restore path.

Staging persists across runs, and unchanged images and manifests retain their metadata.
`manifest.json` records each successfully captured database's source path, relative
image path, and SHA-256 digest, plus any capture errors. A disappeared or failed source
keeps its previous staged image, but is absent from the manifest's current successes.
If a SQLite capture fails, the raw-file backup still runs and the service exits with a
failure so the error remains visible. Such a run must not be treated as a complete
consistent database backup.

### Why Codex session files remain included

Preserve `sessions/**/*.jsonl` alongside SQLite originals and consistent images.
The SQLite history is a projection of the journal, and cannot replace it for complete
archival. Checked on wyrm2 with Codex CLI 0.162.1 on 2026-10-25 using read-only schema
inspection and a record-type/field comparison of one completed paginated session.

| Journal data                                       | Difference from the sampled SQLite history                                                                                                                                                                                                                                   |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `response_item` reasoning: `encrypted_content`     | The projected `reasoning` item contained `summary` and `content`, without the encrypted payload.                                                                                                                                                                             |
| `response_item` compaction: `encrypted_content`    | The journal contained an encrypted compaction record with no corresponding projected item in this session.                                                                                                                                                                   |
| `turn_context`                                     | Detailed per-turn model, effort, approval, filesystem sandbox, permission profile, and collaboration settings were absent from the projected items. Some current thread settings exist separately in `state_5.sqlite`; they do not preserve these original per-turn records. |
| `world_state`                                      | The journal's `full`/`state` record was absent from the projected items.                                                                                                                                                                                                     |
| `token_usage_record` and `event_msg`/`token_count` | Per-response/turn usage records and rate-limit details were absent from the projected items. `state_5.sqlite` has cumulative `threads.tokens_used`; that aggregate does not preserve these events.                                                                           |
| `session_meta` and raw response envelopes          | Base instructions, runtime workspace roots, context-window metadata, and original response-record fields/order are not represented by the projected reasoning and assistant-message items. Selected thread metadata is stored separately.                                    |

`state_5.sqlite` stores thread metadata and `threads.rollout_path` pointing at the
journal. `thread_history_1.sqlite` stores projected turns/items and projection offsets
(`thread_history_projection_state.next_rollout_byte_offset` and
`next_rollout_ordinal`). The inspected `logs_2.sqlite` schema stores diagnostic logs;
`memories_1.sqlite` stores generated memories/summaries and job state. Neither schema
establishes a complete journal copy.

These observations establish missing information in the sampled history projection,
not an exhaustive comparison of every session or every JSON/log value in every
database. Diagnostic logs may duplicate individual fields incidentally. SQLite
schemas and projection behavior can change between Codex versions; keep the raw
journals so future parsers can recover information the current projection omits.

### Restore

Select the source workstation's repository credentials, inspect snapshots with
`restic snapshots`, and inspect the selected snapshot with `restic ls <snapshot-id>`.
Restore into a temporary directory using
`restic restore <snapshot-id> --target <temporary-directory>` before replacing local
files. Paths below the workstation home are stored relative to that home: typically
`.claude`, `.codex`, and `.local/state/ducktape/ai-session-backup-sqlite`. Configured
roots outside the home retain their absolute hierarchy; confirm paths with `restic ls`.

For a database restore, stop the harness and preserve the destination's existing files
separately. Find its source-to-image mapping in the restored `manifest.json`, verify
the image digest and SQLite integrity, and copy the consistent image to the intended
database path. Keep raw captured or existing `-wal`, `-shm`, and `-journal` sidecars
separate from that replacement; replaying them against a different database image can
corrupt it. Restore the other original files from the raw trees. Review manifest errors
and historical snapshots if a desired database is absent from the selected manifest.

The client Restic encryption passphrase is `restic_password`. Keep it separate from
`restic_rest_password_<host>`, which authenticates to this server. The latter values
are unique per host and match the bcrypt entries in
`cluster/k8s/session-backup/rest-server-auth.sops.yaml`.

## Storage and recovery boundary

One Rest Server pod mounts a 100 GiB ReadWriteOnce PVC on
`piraeus-ssd-2-autodiskful`. Piraeus maintains two synchronous storage copies on
distinct OVH nodes. The server uses a Recreate deployment strategy, and the primary
filesystem is mounted directly through Piraeus CSI. Flux waits for the Piraeus cluster
before reconciling this component.

Rest Server's append-only mode prevents workstation clients from pruning snapshots or
deleting repository data. No automatic repository expiration or pruning is configured;
the repositories grow until an operator performs planned server-side maintenance.
The repository has two storage copies within the cluster and no separate recovery copy.

Before first deployment, complete the replicated-class failover, resync, and expansion
rehearsal recorded in the [Piraeus production plan](../../docs/plans/piraeus_production.md#replicated-class-and-seaweed-reduction).

TODO: Consider a separate recovery copy of the Restic repository if needed.

## Archive projection and browser

The separate `ai-session-archive` Flux Kustomization imports each host repository into a CNPG
database in this namespace and serves the owner-authenticated browser at
`https://ai-session-archive.allegedly.works`. It depends on this session-backup Kustomization,
CNPG, External Secrets, the Forgejo image credential and image policies, SSO providers, and the
SeaweedFS operator. The session-backup Kustomization depends only on Piraeus, so an unavailable
archive dependency does not suspend the Rest Server.

Each workstation has a 15-minute CronJob that scans all retained snapshots. Failed or interrupted
imports retry on a later run, and newly uploaded snapshots appear without a one-time backfill
command. Each host has its own `Forbid` concurrency policy. Runs are offset by five minutes to
spread database and repository load. Imported history is durable application data: CNPG takes
weekly base backups and continuously archives WAL to SeaweedFS with a 30-day recovery window. The
database does not depend on replaying Restic snapshots as its only recovery path.

The importers use the workstation's existing REST credentials and the shared Restic encryption
password for snapshot inventory, diff, file listing, and extraction against the append-only
server. Those credentials are not read-only: the server permits appends, but rejects pruning and
deletion. The archive ArtifactGenerator copies the encrypted `secrets/shared/session-backup.yaml`
file into its own Kustomize directory. Kustomize's `secretGenerator` packages those encrypted file
contents under the `session-backup.yaml` Secret key; Flux then decrypts that generated Secret
value after the Kustomize build and before applying it. Each importer reads only the three keys
for its machine from the mounted file. Restic receives those values in its child process
environment; the importer does not log them. The separate `session-backup-rest-server-auth` Secret
continues to hold only `.htpasswd` for the Rest Server.

The deployment's SSO provider creates and reflects `ai-session-archive-oidc` into this namespace.
Its Authentik application policy and the API's numeric subject check both restrict access to the
owner. The CNPG app Secret supplies database credentials. Web and importer pods run the
versioned migration image as an init container before starting, using a database migration lock
to serialize concurrent runs. The CNPG database and reflected OIDC Secret must be ready before
the web process can start.

The import root arguments are `/.claude` and `/.codex`. The source service backs up relative
`.claude` and `.codex` paths from each workstation home, and these are the roots present in the
Restic tree. A snapshot's presence alone does not establish that all files were read successfully;
the importer reports incomplete snapshots or failed files as job failures, and those are retried.
