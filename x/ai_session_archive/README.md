# Local AI session archive index

This package imports transcript file versions from one machine's Restic repository into a fresh Postgres index. `SnapshotIngestor.ingest()` enumerates snapshots in archive order, uses a diff only when its exact parent is already inventoried and completely processed, and otherwise performs an independent full inventory. The `restic_snapshots` and `snapshot_files` tables form the durable snapshot/file ledger, so failed extraction is visible and a later run retries it. Snapshot processing is complete only after all supported changes are either imported or recorded as terminally unclassified/not-indexed/deleted states.

Configure the snapshot paths of the harness roots explicitly. The values must match paths returned by Restic, which can differ from the importing process's home directory:

```sh
DATABASE_URL='postgresql+psycopg://…' \
RESTIC_REPOSITORY='/archive/repositories/host' \
RESTIC_PASSWORD_FILE='/run/secrets/restic-password' \
MACHINE_ID='host-id' \
MACHINE_DISPLAY_NAME='Host name' \
bb run //x/ai_session_archive:ingest_cli -- \
  --claude-root '/home/user/.claude' \
  --codex-root '/home/user/.codex'
```

`DATABASE_URL` is read only from the environment so credentials do not appear in process arguments. Archive roots must be configured explicitly with `--claude-root` / `--codex-root` or `CLAUDE_ARCHIVE_ROOT` / `CODEX_ARCHIVE_ROOT`. These absolute paths must match the paths in the Restic snapshots; the importer does not infer them from its own home directory or harness environment. Repeat the root flags to map additional roots. The CLI runs the repository's pinned Restic binary from Bazel runfiles. Restic reads its normal environment settings; use `RESTIC_PASSWORD_FILE` for the repository password.

The first schema initializer is explicitly opt-in with `--initialize-fresh-database`. It uses SQLAlchemy `create_all` for a brand-new prototype database and does not upgrade an existing database. Production upgrades need versioned migrations before deploying schema changes. No cluster CronJob or secret provisioning is included here.

Claude JSONL under `projects/` is indexed, including nested `subagents/`; Codex JSONL under `sessions/` and `archived_sessions/` is indexed. Other `.jsonl` files are recorded as unclassified inventory rows and are not presented as conversations. Claude subagent parent paths are exposed as `parent_session_id` with `parent_link_kind="claude_path_inferred"`; `identity_inferred` marks a parent placeholder until its own transcript is captured. Native and path-derived relationships can be extended in a later parser.

Each physical JSONL line has its own ordinal and byte offset. Repeated identical lines at different positions remain separate events. The raw line bytes are content-addressed once and reused by version/ordinal references; malformed UTF-8, invalid JSON, or JSONB-incompatible values retain their raw bytes and a record-level parse/storage issue, while subsequent lines continue importing. Events expose a replacement-decoded `raw` display string and lossless base64 bytes. A file version is `complete` when extraction succeeded and all records were parseable/storeable. `capture_status` remains `unknown`: Restic snapshot existence does not prove the workstation's backup read every source file.

Growing transcript versions share their unchanged record prefix and store only their divergent suffix, with a full checkpoint at least every 64 distinct versions. Import parses in batches capped at 500 records and approximately 4 MiB of source bytes; one very large physical line can exceed that bound. The exact file hash/size is computed from a temporary spool, so the full transcript is not copied into Postgres per snapshot. Restic remains the source of exact file bytes; Postgres retains deduplicated raw record bytes for all imported JSONL lines and snapshot/path provenance.

The first parser is fixed at `jsonl-v1`. A parser-version change needs an explicit migration/reparse workflow; changing the constant alone does not reinterpret already imported file versions. Binary/config/cache files remain available from Restic but are not copied into this initial SQL index. Search, normalized operation tables, archive-pruning policy, live backup completeness, and an archive restore rehearsal remain outside this slice.
