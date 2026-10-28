# Local AI session archive index

This package imports transcript file versions from one machine's Restic repository into a fresh Postgres index. `SnapshotIngestor.ingest()` alternates a 32-file freshness batch from the newest snapshot with a 128-file batch from the earliest unfinished snapshot, then lists Restic snapshots again. Within a freshness batch, recently modified files run first. A new snapshot can therefore appear during a long historical replay without waiting for that replay to finish. The importer keeps running until all currently visible work is complete or a file/inventory failure remains for the next job's retry.

The freshness pass stages only paths changed from the newest snapshot's available Restic parent. It may use that Restic diff even when the parent has not been imported, but it does not mark the new snapshot's inventory complete or inherit an unfinished parent ledger. If the newest imported file came from a snapshot at least one hour older than the current snapshot (or no file has been imported), it also seeds up to 64 recently modified transcripts. That persisted freshness check brings currently active but unchanged transcripts into a new or stale index. A snapshot with no available parent stages at most 64 recently modified transcript candidates. The historical pass later inventories the entire snapshot, using a diff as a complete ledger only when its exact parent is still available and already inventoried and completely processed; otherwise it performs an independent full inventory. This bounds the rows created for each new snapshot during backfill while preserving full historical coverage. The `restic_snapshots` and `snapshot_files` tables form the durable snapshot/file ledger, so failed extraction is visible and a later run retries it. Snapshot processing is complete only after all supported changes are either imported or recorded as terminally unclassified/not-indexed/deleted states. Identical file content encountered out of order retains its earliest snapshot as `first_snapshot_id` and its latest capture as `last_seen_at`.

Configure the Restic node roots explicitly. The repository's `session_backup.sh` changes to the workstation home and backs up relative `.claude` and `.codex` paths, so `restic ls` exposes the roots as `/.claude` and `/.codex`. Configure those paths even if `restic snapshots --json` lists the physical home paths in its `Paths` metadata:

```sh
DATABASE_URL='postgresql+psycopg://…' \
RESTIC_REPOSITORY='/archive/repositories/host' \
RESTIC_PASSWORD_FILE='/run/secrets/restic-password' \
MACHINE_ID='host-id' \
MACHINE_DISPLAY_NAME='Host name' \
bb run //x/ai_session_archive:ingest_cli -- \
  --claude-root '/.claude' \
  --codex-root '/.codex'
```

`DATABASE_URL` is read only from the environment so credentials do not appear in process arguments. Archive roots must be configured explicitly with `--claude-root` / `--codex-root` or `CLAUDE_ARCHIVE_ROOT` / `CODEX_ARCHIVE_ROOT`. These absolute paths must match file-node paths returned by `restic ls`; the importer does not infer them from its own home directory or harness environment. Repeat the root flags to map additional roots. The CLI runs the repository's pinned Restic binary from Bazel runfiles. Restic reads its normal environment settings; use `RESTIC_PASSWORD_FILE` for the repository password.

Deployments that mount the canonical session-backup YAML source may pass it with
`--credentials-file`. The importer selects the REST username, REST password and repository
password for `--machine-id`, and passes those values to the Restic child process. This mode cannot
be combined with `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, `RESTIC_PASSWORD` or
`RESTIC_PASSWORD_FILE` in the importer environment.

Before the first import, create or upgrade the schema with the versioned migration runner:

```sh
DATABASE_URL='postgresql+psycopg://…' bb run //x/ai_session_archive:migrate_database_bin
```

The migration runner applies a versioned Alembic history and uses a PostgreSQL advisory lock to serialize concurrent runs. Run it before starting the importer or API; those processes do not create or upgrade tables. This initial revision targets an empty database. It will fail without dropping data if the earlier prototype `create_all` schema already exists without a migration stamp; adopting an existing prototype database needs a separate schema comparison and explicit stamp.

Claude JSONL under `projects/` is indexed, including nested `subagents/`; Codex JSONL under `sessions/` and `archived_sessions/` is indexed. Other `.jsonl` files are recorded as unclassified inventory rows and are not presented as conversations. Claude subagent parent paths are exposed as `parent_session_id` with `parent_link_kind="claude_path_inferred"`; `identity_inferred` marks a parent placeholder until its own transcript is captured. Codex `session_meta.payload.parent_thread_id` creates a `parent_session_id` link only when it matches exactly one captured Codex session's native metadata ID on the same machine. Native identity is read only from `session_meta.payload.id`. Conflicting native or parent IDs are exposed as candidate lists and conflict flags; such links are marked inconsistent rather than guessed. Claude subagent `sessionId` can refer to the root session, so Claude identity remains path-based. Physical files with duplicate native IDs remain separate sessions and make parent resolution ambiguous.

Each physical JSONL line has its own ordinal and byte offset. Repeated identical lines at different positions remain separate events. The raw line bytes are content-addressed once and reused by version/ordinal references; malformed UTF-8, invalid JSON, or JSONB-incompatible values retain their raw bytes and a record-level parse/storage issue, while subsequent lines continue importing. Events expose a replacement-decoded `raw` display string and lossless base64 bytes. A file version is `complete` when extraction succeeded and all records were parseable/storeable. `capture_status` remains `unknown`: Restic snapshot existence does not prove the workstation's backup read every source file.

Growing transcript versions share their unchanged record prefix and store only their divergent suffix, with a full checkpoint at least every 64 distinct versions. Import parses in batches capped at 500 records and approximately 4 MiB of source bytes; one very large physical line can exceed that bound. The exact file hash/size is computed from a temporary spool, so the full transcript is not copied into Postgres per snapshot. Restic remains the source of exact file bytes; Postgres retains deduplicated raw record bytes for all imported JSONL lines and snapshot/path provenance.

The first parser is fixed at `jsonl-v1`. A parser-version change needs an explicit migration/reparse workflow; changing the constant alone does not reinterpret already imported file versions. Binary/config/cache files remain available from Restic but are not copied into this initial SQL index. Search, normalized operation tables, archive-pruning policy, live backup completeness, and an archive restore rehearsal remain outside this slice.

## Read API and browser

`//x/ai_session_archive:web_image` packages the owner-authenticated read API with the frontend bundle in its Bazel runfiles; `//x/ai_session_archive:web_bin` does the same for local runs. Set `AI_SESSION_ARCHIVE_DATABASE_URL`, `AI_SESSION_ARCHIVE_PUBLIC_BASE_URL`, `AI_SESSION_ARCHIVE_OIDC_ISSUER`, `AI_SESSION_ARCHIVE_OIDC_CLIENT_ID`, `AI_SESSION_ARCHIVE_OIDC_CLIENT_SECRET`, `AI_SESSION_ARCHIVE_OIDC_SESSION_SECRET`, and `AI_SESSION_ARCHIVE_OIDC_ALLOWED_SUBJECT`. `AI_SESSION_ARCHIVE_FRONTEND_DIR` is an optional override for a separately provisioned frontend directory. The only unauthenticated endpoint is `/healthz`.

The API lists machines and sessions, supports filtering child sessions by `parent_session_id`, and serves captured versions and paginated records. Each event response includes replacement-decoded text and lossless base64 bytes. Claude path-derived parent links are explicitly marked as inferred. Codex metadata links distinguish resolved, unresolved, ambiguous, and conflicting parent IDs, with conflicting native-ID candidates available in the session summary. The web process does not ingest data or run migrations; the cluster deployment supplies separate importer CronJobs and migration init containers.
