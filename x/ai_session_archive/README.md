# Local AI session archive index

This package imports transcript file versions from one machine's Restic repository into a fresh Postgres index. `SnapshotIngestor.ingest()` first commits a pending `restic_snapshots` row for every snapshot returned by Restic, so the database shows the total discovered, complete, and remaining snapshots while an import is still working on the oldest file. It then processes snapshots and paths in archive order. It uses a diff only when its exact parent is still present in Restic and already inventoried and completely processed, and otherwise performs an independent full inventory. The `restic_snapshots` and `snapshot_files` tables form the durable snapshot/file ledger, so failed extraction is visible and a later run retries it. Snapshot processing is complete only after all supported changes are either imported or recorded as terminally unclassified/not-indexed/deleted states. A new import run discovers snapshots added since the previous listing; it does not interrupt the current run to relist Restic. Import reports count only snapshots in that run's Restic listing. Previously discovered snapshots remain in the ledger if later pruned from Restic; the schema does not mark their current availability, so database-only counts include that history.

`record_payloads.raw_bytes` is the exact JSONL source. The event API decodes each readable record from those bytes; the database stores only the raw record and compact kind, timestamp, and parse-error projections. JSON `null` has no parse error; invalid UTF-8 or JSON retains its raw bytes with a parse error, and later lines continue importing.

`usage_observations` stores the token counters explicitly present in each unique readable payload. Separate rows retain direct response/message counters and cumulative turn, thread, session, result, and per-model result counters. Codex `payload.info.last_token_usage` is labeled `last_response`, separately from direct response `payload.usage`, because those source snapshots can overlap. The `session` kind labels Codex `payload.info.total_token_usage`; its reset and scope semantics are not validated here, so treat it as a source observation rather than an additive request count. A missing source counter is `NULL`; repeated occurrences of one raw payload share the same observation rows. Model and response identifiers are recorded only when that source record contains them, so surrounding transcript context is not inferred.

Existing archive Postgres indexes may be discarded and reimported. Verify that retained Restic snapshots cover the transcripts you need before doing so. This rebuild affects only the derived Postgres index; it does not delete or recreate source data, including native Codex SQLite databases.

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

Before the first import, initialize an empty index database with the versioned migration runner:

```sh
DATABASE_URL='postgresql+psycopg://…' bb run //x/ai_session_archive:migrate_database_bin
```

The migration runner applies the schema and uses a PostgreSQL advisory lock to serialize concurrent runs. Run it before starting the importer or API; those processes do not create tables. This revision changes the initial schema and has no upgrade migration or backfill. Rebuild an existing derived Postgres index by reimporting from retained Restic snapshots after verifying their coverage. This does not delete source data, including native Codex SQLite databases.

Claude JSONL under `projects/` is indexed, including nested `subagents/`; Codex JSONL under `sessions/` and `archived_sessions/` is indexed. Other `.jsonl` files are recorded as unclassified inventory rows and are not presented as conversations. Claude subagent parent paths are exposed as `parent_session_id` with `parent_link_kind="claude_path_inferred"`; `identity_inferred` marks a parent placeholder until its own transcript is captured. Codex `session_meta.payload.parent_thread_id` creates a `parent_session_id` link only when it matches exactly one captured Codex session's native metadata ID on the same machine. Native identity is read only from `session_meta.payload.id`. Conflicting native or parent IDs are exposed as candidate lists and conflict flags; such links are marked inconsistent rather than guessed. Claude subagent `sessionId` can refer to the root session, so Claude identity remains path-based. Physical files with duplicate native IDs remain separate sessions and make parent resolution ambiguous.

Each physical JSONL line has its own ordinal and byte offset. Repeated identical lines at different positions remain separate events. The raw line bytes are content-addressed once and reused by version/ordinal references; malformed UTF-8 and invalid JSON retain their raw bytes and a record-level parse issue, while subsequent lines continue importing. Events expose a replacement-decoded `raw` display string and lossless base64 bytes. A file version is `complete` when extraction succeeded and every record is valid UTF-8/JSON. `capture_status` remains `unknown`: Restic snapshot existence does not prove the workstation's backup read every source file.

Growing transcript versions share their unchanged record prefix and store only their divergent suffix, with a full checkpoint at least every 64 distinct versions. Import parses in batches capped at 500 records and approximately 4 MiB of source bytes; one very large physical line can exceed that bound. The exact file hash/size is computed from a temporary spool, so the full transcript is not copied into Postgres per snapshot. Restic remains the source of exact file bytes; Postgres retains deduplicated raw record bytes for all imported JSONL lines and snapshot/path provenance.

The first parser is fixed at `jsonl-v1`. A parser-version change needs an explicit migration/reparse workflow; changing the constant alone does not reinterpret already imported file versions. Binary/config/cache files remain available from Restic but are not copied into this initial SQL index. Search, normalized operation tables, archive-pruning policy, live backup completeness, and an archive restore rehearsal remain outside this slice.

Codex display names are imported separately from each retained snapshot's `/.codex/session_index.jsonl` and the consistent `state_5.sqlite` image listed in `/.local/state/ducktape/ai-session-backup-sqlite/manifest.json`. The image digest and SQLite integrity are checked before reading it. `thread_name` from the index takes precedence, followed by SQLite `threads.name`, then `threads.title`. The SQLite `rollout_path` identifies a physical transcript; an index-only ID is applied only when it matches one unambiguous, confirmed native session ID. Snapshot-specific observations remain in SQL when Restic snapshots are later pruned, and the latest captured observation supplies the browser's current title. Existing, fully imported snapshots are backfilled without reparsing their transcript versions. A malformed index line or damaged SQLite image leaves any other valid title source usable and records a partial metadata status and error on the snapshot.

The current-title projection selects the newest observation per native ID and
physical source path in PostgreSQL before loading candidates into Python. The
historical observation ledger stays intact; projection memory does not grow with
repeated captures of the same mapping. Path-specific and index-only observations
remain separate candidates until identity/ambiguity checks resolve them.

## Read API and browser

Claude display names prefer the last captured `custom-title` record, then the last
`summary`, then the first ordinary user prompt. Prompt fallback excludes `isMeta`
records and local-command markers; fork prompts use the directive following
`fork-boilerplate`. New transcript versions recompute the projected name so later
native titles and renames replace a prompt fallback. Already indexed versions are
not reparsed solely to refresh this projection.

The browser frontend lives in `//x/ai_session_archive/frontend:bundle`. Its Bazel output is the portable `dist/` directory (`index.html`, `main.js`, and source maps) mounted by the archive web server.

The browser selects a session at `/sessions/<session-id>` and keeps the machine, harness, and captured-version choices in the URL query string. It shows the latest capture by default; the conversation options menu provides older versions and capture metadata. Selecting an older version shows an Earlier capture indicator. Session badges describe exceptional states; a normal capture and its version count add no session-list chrome. The top bar shows compact progress beside the title only for machines with pending or failed import work; `/?view=import-progress` opens full snapshot, file-entry, and Codex metadata details for every registered machine. Session rows and related sessions are links that can be opened in a new tab; session and version changes participate in browser history. The page can be reloaded after sign-in. An unknown session ID stays on its URL and shows a load error; an unavailable version shows an explicit unavailable state without displaying another capture.

`//x/ai_session_archive:web_image` packages the owner-authenticated read API with the frontend bundle in its Bazel runfiles; `//x/ai_session_archive:web_bin` does the same for local runs. Set `AI_SESSION_ARCHIVE_DATABASE_URL`, `AI_SESSION_ARCHIVE_PUBLIC_BASE_URL`, `AI_SESSION_ARCHIVE_OIDC_ISSUER`, `AI_SESSION_ARCHIVE_OIDC_CLIENT_ID`, `AI_SESSION_ARCHIVE_OIDC_CLIENT_SECRET`, `AI_SESSION_ARCHIVE_OIDC_SESSION_SECRET`, and `AI_SESSION_ARCHIVE_OIDC_ALLOWED_SUBJECT`. `AI_SESSION_ARCHIVE_FRONTEND_DIR` is an optional override for a separately provisioned frontend directory. The only unauthenticated endpoint is `/healthz`.

The API lists machines and sessions, supports filtering child sessions by `parent_session_id`, and serves captured versions and paginated records. Each event response includes replacement-decoded text and lossless base64 bytes. Claude path-derived parent links are explicitly marked as inferred. Codex metadata links distinguish resolved, unresolved, ambiguous, and conflicting parent IDs, with conflicting native-ID candidates available in the session summary. The web process does not ingest data or run migrations; the cluster deployment supplies separate importer CronJobs and migration init containers.

## Transcript presentation

The browser uses the shared `util/frontend/transcript` messages, compact step lines,
bounded tool text and virtualized history. Archive-owned adapters leave user prompts
and assistant commentary/answers visible. Consecutive intermediate work folds into
semantic tool/reasoning/agent-message counts; bookkeeping stays inspectable under
Session details and does not inflate those counts.

Codex function/custom calls pair with results by `call_id` across loaded pages. A
paired step appears at its result's position and exposes both source records, with
original line ordinals; an unpaired result identifies its missing call, and a call
without a loaded result does not claim to be running or successful. Older prepends
can supply a missing call without resetting the result's expansion. Consecutive
readable reasoning summaries form a nested step. Encrypted-only reasoning adds no
placeholder step; its original record remains under Session details. Subagent
messages show their sender and readable content, or an unavailable-content label.
Compaction is a compact marker with source inspection.

Claude `user` records containing only tool results are activity; mixed text/content
stays visible. Unknown record types and parse failures remain standalone. Explicit
tool errors stay visible or mark the enclosing tool group as failed. Every original
record remains accessible through a compact inspection button and its exact-byte
view. Assistant replies use plain Markdown; user inputs use the shared right-aligned
bubble. Inspection actions appear on hover, keyboard focus or touch selection
without reserving a separate disclosure row.

Expansion choices survive virtualized row remounts and reset when the
selected session or captured version changes. Parsing and grouping belong to this
application; the shared viewport has no Claude, Codex or Agentplane protocol dependency.

The conversation opens at the latest captured records. Scrolling upward loads older
pages while retaining the reading position; “Load earlier records” also supports
keyboard navigation. Event API requests default to forward pagination;
`direction=backward` returns records before the exclusive cursor in ascending order.
