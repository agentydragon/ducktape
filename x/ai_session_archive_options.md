# Workstation AI session archive: options and composition

Research date: 2026-10-25. Scope: Claude Code and Codex on personal Linux workstations, initially `wyrm2`, `rugged`, and `iguana`.

**Recommendation:** retain whole harness directories in an independently retained file archive, then ingest archived files into a rebuildable SQL database. The strongest starting composition is the proposed append-only Restic archive plus a snapshot-to-Postgres worker. Compare a versioned `rclone` file tree if extraction becomes inconvenient. Session-search tools contribute prior art and optional parsers; NL search, MCP, Langfuse, and OTLP are not required for the first useful result.

This is a source and documentation investigation. No candidate was installed, benchmarked, or exercised against workstation data. Findings below distinguish observed implementation from proposed integration; the acceptance experiments remain unrun.

## Confirmed requirements

The intended source is the actual contents of the configured harness directories, such as `~/.claude` and `~/.codex`: transcripts, tool results, subagent files, SQLite databases, configuration, and other recorded files. Preserve source files and directory structure, rather than selecting only fields a parser understands. The initial archive scope has no assumed credential/config/cache exclusions; indexing can be narrower than archival without deleting those source files.

Fifteen-minute collection delay while connected is acceptable; continuous capture is an improvement. Earlier captured versions must remain recoverable, but only the latest known versions need to be immediately queryable. SQL is sufficient; Postgres is an acceptable destination. Indexing must read harness files and work without telemetry callbacks.

| Need                  | Required behavior                                                                                                                                           |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preserve sessions     | Collect existing history and subsequent changes, including sessions that never produce a Git commit. Retain collected data after source files disappear.    |
| Find past work        | Search across harnesses and machines; filter by date, project, and source host; open the matching conversation.                                             |
| Inspect operations    | Recover the recorded model messages, tool arguments/results, order, IDs, and available usage/timing. Follow subagent and fork relationships where recorded. |
| Independent retention | Harness rotation, workstation cleanup, and replacement of a synchronized folder must not delete archived content.                                           |
| Recover query state   | Rebuild or replace parsers, indexes, and UI without depending on files still existing on the workstations. Preserve unknown raw records for future parsers. |

“Attachments” means things such as image files referenced by a conversation. Files and embedded content inside the harness directories are included automatically. A reference to a file elsewhere does not copy that external file into the archive; discovering/copying external paths is additional scope, not a prerequisite for preserving the directories as requested. Exact provider request capture beyond what those files record is also outside this first scope.

“Nothing disappears” applies to captured data. A 15-minute polling scheme cannot promise to capture a file created and deleted entirely between successful captures, or work created offline and deleted before reconnecting. It does preserve all versions it actually captured, independently of later source GC. Immediate source-side capture/spooling can improve that window later.

## What the existing PRs actually provide

The examined versions are [#8587 at `9d5dbb01`](https://github.com/agentydragon/ducktape/pull/8587) and [#8588 at `0b80f376`](https://github.com/agentydragon/ducktape/pull/8588).

The [server contract][pr-server] specifies append-only workstation Restic repositories, with **no automatic pruning of their snapshots**. The 7 daily / 4 weekly / 6 monthly retention belongs to the outer VolSync backup of the server repository. It does not expire the inner workstation snapshots. A file removed by harness GC remains recoverable from an earlier inner snapshot until deliberate maintenance removes that snapshot.

The [client][pr-client] uploads every 15 minutes, snapshots Codex SQLite databases through the online backup API, and excludes selected credential/config/cache files and live `state_*.sqlite*` files. Those exclusions and the limited SQLite selection do **not** satisfy whole-directory preservation. Useful pieces to retain are host opt-ins, scheduling, credential wiring, and consistent SQLite snapshots. Restic can supply the file archive; indexing is a separate job. A first extraction must consider historical snapshots, not only the latest folder tree. The two storage copies share SeaweedFS, so their recovery boundary excludes loss of that whole substrate.

Whole-directory capture includes SQLite files and their journals/WAL sidecars, but copying them separately while the harness writes does not establish a consistent restorable database. The [SQLite backup API](https://www.sqlite.org/backup.html) supplies an additional consistent database image; it does not preserve the exact layout/history of the original files. Preserve the original directory capture as requested and retain a consistent image with its source-path mapping, or capture the directory from an atomic filesystem snapshot where available. Do not silently substitute only the consistent image for the raw files. See SQLite's [backup/copying caveats](https://www.sqlite.org/howtocorrupt.html#_backup_or_restore_while_a_transaction_is_active). The collection mechanism for each workstation filesystem remains to be determined.

Repository evidence narrows that investigation: [`rugged`](../nix/nixos/hosts/rugged/hardware-configuration.nix) and [`iguana`](../nix/nixos/hosts/iguana/hardware-configuration.nix) declare Btrfs `@home` subvolumes, making a read-only home snapshot a candidate stable source for Restic. The [Btrfs documentation](https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html#nested-subvolumes) warns that snapshots exclude nested subvolume contents; enumerate the actual harness-root mount/subvolume layout before relying on that. Live mounts and `wyrm2`'s home filesystem were not established here. A local filesystem snapshot is staging for off-machine backup, not the sole retained archive.

Current repository configuration already sets Claude's `cleanupPeriodDays = 9999` in [`nix/home/claude_code/default.nix`](../nix/home/claude_code/default.nix). This reduces one source of local expiry; it is not an archive retention contract, nor proof that every running workstation has activated that setting.

## Separate the layers

```text
Workstations: live harness folders + local capture/spool
                 |
                 | retryable transfer, host identity, no propagated deletions
                 v
Cluster landing folders / transfer staging
                 |
                 | archive bytes before parsing; record durable acknowledgement
                 v
Archive: retained file versions/chunks + provenance manifest
                 |
                 +--> JSONL/file ingestion ----------> Postgres SQL tables/views
                 |
                 +--> optional later conversion ----> trace UI / search / MCP
                 |
                 +--> recovery backup with its own failure boundary
```

The landing folders may reflect the latest workstation state. The archive must independently retain every accepted version needed to preserve history. A synchronized tree using no delete propagation can be a useful landing area, but the same pathname can still be truncated or overwritten. Preserve that prior content before replacing it. Content-addressed chunks or versioned objects can avoid storing a whole large transcript on every append.

The key receipt is “these bytes and their provenance are durably archived,” not “rsync finished” or “the search tool indexed this session.” Archiving should succeed even when a new harness record type breaks a parser. Index and trace conversion can then retry from preserved input.

## Mature collection components to compose with the session tools

These components were checked against current official documentation, rather than source-pinned or run locally. They offer alternatives to writing a custom transfer protocol.

| Component                                                                                                                                                      | Useful existing behavior                                                                                                                | Fit and limitation                                                                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`rclone copy`](https://rclone.org/commands/rclone_copy/) with [`--backup-dir`](https://rclone.org/docs/#backup-dir-string)                                    | Copies changed files without deleting missing destination files; moves overwritten destination files into a backup hierarchy            | A concrete first archive prototype: per-host `current/` plus a unique backup directory for every run. Reusing a backup pathname can overwrite history. Requires destination support for server-side move/copy. It preserves whole versions, so repeated appends can have substantial storage cost.                                |
| [Syncthing file versioning](https://docs.syncthing.net/users/versioning.html)                                                                                  | Continuous folder synchronization; receiver can retain versions on remote replacement/deletion or invoke an external versioning command | Good transport into landing folders. Versioning is off by default; built-in strategies can discard prior versions by count, interval, or age. Staggered versioning still coalesces versions even with unlimited age. An external versioner could hand old files to an archive before replacement.                                 |
| [Vector file source](https://vector.dev/docs/reference/configuration/sources/file/) + [S3 sink](https://vector.dev/docs/reference/configuration/sinks/aws_s3/) | File checkpointing and provenance, configurable disk buffering, batched object uploads                                                  | Worth comparing for incremental JSONL event collection, especially to avoid repeated whole-file copies. Preserve each original line as data before parsing; retain source path, file generation and offset for reconstruction/deduplication. This is an event stream, so SQLite databases and attachments still need a file path. |

For `rclone`, the concrete composition is workstation folders → per-host current/version directories → session indexer, with Langfuse conversion optional. Scheduling, manifests, error reporting, and index refresh are glue; existing tools can do the copying and version preservation. A separate archive service may not be necessary for the first useful implementation.

For Vector, evaluate [end-to-end acknowledgements](https://vector.dev/docs/architecture/end-to-end-acknowledgements/) and disk buffering explicitly; the S3 sink defaults to a memory buffer. The file source discards lines beyond its default `max_line_bytes = 102400`, which is unsuitable for large embedded tool results/images without adjustment. Checkpoint recovery, copy/truncate rotation, and replay ordering must pass the acceptance cases. A line tailer is not by itself a byte-exact file archive. A periodic file snapshot path can complement streaming capture.

If we synchronize current folders and run an ingester afterwards, **a polling ingester can miss content replaced between scans**. Receiver-side versioning, snapshotting before replacement, or source-side spooling must close that gap. Simply running a parser over the latest synchronized tree does not.

## Existing pieces and their fit

The table describes the inspected versions, not an end-to-end deployment guarantee. Sources follow each option.

| Piece           | Existing capability                                                                                 | Role in a composed solution                                           | Work still needed                                                                                                                        |
| --------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `cass`          | SSH collection, source provenance, raw captures, lexical/semantic search, CLI and stdio MCP service | Broadest candidate for collection + archive + search                  | Nix packaging; capture-budget monitoring; retention configuration; verify raw recovery and parser coverage; custom license consideration |
| `sessions`      | Raw transcript vault, rebuildable search, CLI and MCP                                               | Smaller search/recall service over centrally preserved transcripts    | Transport; host separation; version preservation; subagent completeness                                                                  |
| `claudex`       | Additive SQLite history, full-text search, usage and session reports                                | Optional analytics over an independently retained archive             | Transport; raw preservation; explicit machine attribution; operation replay beyond aggregates                                            |
| Episodic Memory | Transcript copying, local embeddings, semantic/exact search, MCP                                    | Natural-language recall over archived conversations                   | Transport; host separation; version preservation; raw operation viewer                                                                   |
| Langfuse        | Central trace UI/API, Claude/Codex transcript integrations, session grouping                        | Operation inspection and a central query surface                      | Reliable replay/backfill from archive; source attribution; verify integration fidelity and deployed-version compatibility                |
| Entire          | Git-backed transcript checkpoints, remote synchronization, code attribution, historical import      | Useful if conversation-to-code provenance becomes a major requirement | Automatic coverage for sessions without commits; general workstation archival outside repository workflows                               |

### `cass`: strongest broad fit, with operational caveats

**Observed:** [remote collection][cass-sync] uses rsync over SSH without `--delete`. Mirrors are separated by configured source; [raw manifests][cass-mirror] carry source ID, origin host, and original path. This directly addresses “which machine?” and keeps files that disappear upstream. Its [Codex connector][cass-codex] handles tool calls/results, but truncates indexed tool output beyond 128 Ki characters. The raw archive therefore matters for complete inspection.

**Preservation:** [raw-mirror retention documentation][cass-retention] describes content-addressed chunks and retained captures. Automatic retirement requires a newer capture to preserve the older bytes as an identical prefix; truncations and rewrites do not qualify. This is substantially closer to our requirement than a latest-file mirror.

**Important default:** the [capture policy][cass-policy] caps raw storage at 8 GiB and reserves 2 GiB of free space. If another capture cannot fit, raw capture can pause while indexing continues. A working search result is therefore insufficient proof of preservation. Explicit operator pruning can also remove divergent historical versions; the archive policy must constrain that separately from automatic prefix retirement.

**Composition:** a cluster worker could pull configured workstation sources into persistent storage, preserve the raw mirror/canonical database, and expose search through `cass serve --stdio --mcp` over a controlled connection. That service is documented in the [README][cass-readme]; it is not an authenticated HTTP application by itself. Alternatively, keep an independent archive and use `cass` primarily as its indexer. Rehydration into paths its connectors discover needs an experiment.

**Caveats:** upstream labels the project alpha. Its [license][cass-license] is MIT with an OpenAI/Anthropic restriction, not plain MIT; account for that before choosing it as a dependency. Source-build/Nix packaging and upgrade behavior remain unevaluated. Treat a successful raw-mirror-to-empty-database recovery as a required proof, rather than assuming every archive file is automatically rediscovered by ordinary indexing.

### `sessions`: a useful small vault/search pattern

**Observed:** the [vault][sessions-vault] keeps raw Claude/Codex transcript copies and a manifest. The [indexer][sessions-cache] rediscovers vault-only sessions when source files disappear. Its search database can be rebuilt from those files; archiving failures warn and allow indexing to continue.

The archive is **one latest copy per original path**. `archiveFile` overwrites it when source size/mtime changes. The indexer falls back to the vault for unusable source files, but a still-parseable shortened/replaced transcript can become the new vault copy. This protects against disappearance, not every destructive rewrite. The manifest is atomically replaced; the raw-file copy itself uses `copyFileSync` to its destination.

Claude discovery in the inspected indexer selects project-level `*.jsonl` files. Subagent user text is folded into search, but that does not establish independent archival of nested subagent transcripts. Message search also intentionally omits tool-result noise; use raw data for operation inspection.

**Composition:** run the search/MCP component over a materialized view of a versioned archive. Host namespaces must survive materialization: a shared original path or a session resumed/copied between machines must not silently overwrite another source. This is a plausible smaller MIT-licensed alternative if we supply collection and stronger raw retention. See the [project overview][sessions-readme] for query interfaces.

### `claudex`: useful analytics; its retained index is not the raw archive

**Observed:** [incremental sync][claudex-indexing] retains rows when source files disappear; schema migrations preserve them. `claudex index --force` deliberately wipes that retained history and rebuilds from files still present. Changed files are re-ingested, so additive session retention should not be mistaken for preservation of all earlier contents of a changed file.

The [schema][claudex-schema] stores searchable user/assistant messages and useful derived data. `tool_calls`, for example, contains per-session/tool counts, not an ordered log of every invocation and result. The [export implementation][claudex-export] filters indexed candidates to those still on disk before reading source records. A retained searchable session is consequently not equivalent to an exportable full transcript after source GC.

**Composition:** feed it preserved, materialized transcripts for reporting on models, costs, files, and PRs. The [public JSON command interface][claudex-readme] is preferable to coupling our service to its changing SQL schema. It contributes less to transport and durable archival than the other candidates; it is an optional analytics component rather than the first foundation to adopt.

### Episodic Memory: stronger fit for recall than operation replay

**Observed:** [sync][episodic-sync] copies new/modified transcripts into an archive using a temporary file and rename. It keeps files that vanish from the source, but replaces a destination with a newer source version based on mtime. That has the same version-preservation gap as a latest-file vault.

Its [parser][episodic-parser] builds conversation exchanges, records some tool metadata, and intentionally excludes reasoning from searchable conversation text. The [README][episodic-readme] documents local embeddings, exact/semantic search, MCP, and oversize exchanges excluded from indexing. Search coverage and raw preservation therefore need separate checks. Summary generation can be disabled; summaries are an optional recall aid, not the retained evidence.

**Composition:** useful for “find the session where we discussed this,” operating on an independent raw archive. Collection, host identity, and a complete operation view remain separate components. Compared with `sessions`, its main reason to choose it would be demonstrated retrieval quality on our queries, rather than stronger retention.

### Langfuse: attractive existing operation view, with ingestion work

The repository already declares a Langfuse deployment, external Postgres/ClickHouse/Valkey, SeaweedFS event/media storage, and Authentik access in [`cluster/cdk8s/langfuse/app.py`](../cluster/cdk8s/langfuse/app.py). That makes reusing it attractive; this investigation did not check live readiness, project settings, or existing workstation coverage.

**Fidelity:** the [Claude integration][langfuse-claude] reconstructs generations and tool spans from transcripts. Its example code uses the latest user message or preceding tool results as generation input, rather than recreating the entire actual request. It also truncates some displayed fields. This is useful operation inspection, but not proof of the full prompt sent to a model.

The [Codex integration][langfuse-codex] documents richer capture including recorded system/developer context, tool I/O, interrupted turns, and subagents. That is an integration-specific claim, not a guarantee for every harness/version. The [generic coding-agent guide][langfuse-guide] and this integration differ on context coverage; test the exact plugin instead of treating either description as universal.

**Delivery:** in the [Codex hook][langfuse-codex-hook], turns are marked uploaded only after conversion and exporter shutdown return without error. Unmarked turns remain retryable on another hook invocation. This is useful retry state, but not an independently scheduled durable upload spool or proof of server persistence. The documented latest-turn finalization also needs checking for a session that never receives another turn.

The [Claude example][langfuse-claude] saves its advanced read offset even when `emit_turn` fails, then flushes in cleanup. That code path can skip a failed turn on the next invocation. Do not use that sample alone as the archival transport.

**Retention:** [self-hosted Langfuse retains event data indefinitely by default][langfuse-retention], unless a retention policy is configured. Verify the actual project, database TTLs, and object-store lifecycle before relying on that. No-retention configuration also does not make converted traces a replacement for the original bytes.

**Composition:** optional after file-derived SQL works: replay/backfill archived events into Langfuse with deterministic IDs and host/harness/session metadata. Reuse the upstream converters where practical. A worker with retries would supply the delivery guarantee missing from hook-only collection. This is additional glue; API/SDK version compatibility and conversion semantics remain to be demonstrated. Neither OTLP callbacks nor hooks determine which source files are retained or queryable.

### Entire: Git-backed code provenance and historical import

**Observed:** [normal capture][entire-readme] condenses sessions into durable Git refs at commits and syncs checkpoints with Git pushes. A separate private checkpoint remote is supported. This provides useful ownership and code-to-conversation links, but automatic archival depends on repository workflow activity.

There is also `entire import`. The [importer][entire-import] supports historical sessions as read-only imported checkpoints with deterministic per-turn identities. Historical import is not limited to sessions that produced code commits. Scheduled import coverage of uncommitted sessions and all relevant project roots, including work outside existing repositories, remains to be demonstrated. First-time `--import-history` defaults to the last 30 days, so it is not a complete historical backfill by itself.

**Composition:** a viable Git-based archive direction if attribution to commits is central. For the stated requirements it introduces repository discovery, checkpoint synchronization, and Git storage policy where a general transcript collector is simpler. Keep it as an alternative, not a required layer.

## SQL-first fit and integration findings

The SQL query requirement reduces the value of adopting a complete recall product. The candidates' databases contain useful patterns, but none of the inspected implementations is a ready-made whole-directory-to-Postgres pipeline.

| Existing implementation                                                                       | SQL data actually available                                                                                                                        | Integration implication                                                                                                                                                                                                                           |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`cass` canonical storage][cass-sqlite] and [message model][cass-model]                       | SQLite conversation/source/workspace tables, ordered messages, content, and `extra_json`; connectors normalize tool records                        | Strongest existing operation-oriented model to evaluate. Feeding Postgres still requires an adapter and archive provenance mapping. Truncation/redaction/connector omissions mean canonical rows are a projection, not the authoritative archive. |
| [`sessions` index][sessions-cache]                                                            | SQLite session metadata plus FTS5 message text; intentionally excludes some tool-result content from message search                                | Easy to query its own SQLite database, but copying that schema does not produce complete operation records. Its MCP server adds little to the initial SQL requirement.                                                                            |
| [`claudex` provider interface][claudex-provider] and [schema][claudex-schema]                 | SQLite search text and derived reports; `ProviderRecord` carries tool names/counts and aggregate usage rather than a complete ordered event stream | Useful reporting/parser reference. Reconstructing operation detail still needs raw-file ingestion, so this is not a shortcut to the required Postgres model.                                                                                      |
| [Episodic Memory parser][episodic-parser]                                                     | Exchange-oriented SQLite/vector model and selected tool metadata                                                                                   | Retrieval features are optional now; adopting its exchange model would still leave raw event preservation and normalization work.                                                                                                                 |
| [DuckDB JSON reader](https://duckdb.org/docs/1.4/data/json/loading_json) over extracted files | SQL directly over JSONL through `read_ndjson_objects`, retaining whole JSON objects instead of inferred narrow columns                             | A low-glue way to explore the archived corpus and develop normalization queries. It does not supply archival, incremental import state, or a Postgres service.                                                                                    |

### Preferred composition: Restic archive plus a file-to-Postgres worker

1. Keep the existing per-host append-only Restic transport and 15-minute scheduling, revise capture scope to whole configured directories, and explicitly retain all snapshots. Solve live SQLite consistency as described above. Backup and indexing success are separate statuses.
2. Have a cluster worker enumerate completed snapshots using [Restic's JSON scripting interface](https://restic.readthedocs.io/en/stable/075_scripting.html). Inventory all file entries, not just transcript paths. Track which captures and file versions have been processed.
3. Read changed JSONL and JSON files with [selective restore or `restic dump`](https://restic.readthedocs.io/en/stable/050_restore.html#printing-files-to-stdout), rather than requiring a privileged FUSE mount or restoring every cached plugin file for each indexing run. Deduplicate repeated file contents; record source and archive references. Use documented CLI interfaces before considering dependence on Restic's internal tree/blob format.
4. Insert complete source event JSON into Postgres, then add harness-specific SQL views or projections for sessions, messages, tool invocations/results, and parent/subagent relationships. Keep arbitrary non-event files in the file archive and its inventory; there is no need to force every binary/config/cache file into the event schema.
5. Publish latest-known archived session versions through the normal views. When a session leaves the workstation, retain its last known data in the archive and query projection. Superseded contents remain recoverable by archive version; indexing every historical version for ordinary search is unnecessary.

This recommendation is an integration judgment, not a benchmark result. It reuses the proposed backup mechanism and makes its data convenient to query without requiring another search product. Restic's encryption also means the importer needs repository decryption access; no currently configured cluster-side importer is assumed. Host-separated repositories can be ingested by one worker while retaining distinct source identities.

### Runtime and secret ownership

**Workstations:** Home Manager declares a systemd user oneshot service and a persistent timer on each opted-in host, initially every 15 minutes as in [#8588][pr-client]. Restic backs up whole configured harness roots into that host's append-only repository. The timer depends on `sops-nix.service`; it must also run without an interactive shell, and unattended operation depends on the user manager being active. If stable filesystem capture needs privilege, declare a narrow NixOS capture helper rather than running all indexing or backup logic as root. Backup status must distinguish complete captures from snapshots with unreadable/changing files.

**Passwords:** the existing proposal has one shared repository encryption passphrase and separate per-host REST authentication credentials. SOPS stores their ciphertext in Git; Home Manager's `sops-nix` decrypts the selected keys into restricted runtime files using the user's configured SSH identity. Use `RESTIC_PASSWORD_FILE` for the encryption passphrase; REST authentication values are loaded for the backup process, not exported to every user shell. Restic documents these inputs in its [repository configuration guide](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html). Retain an independent recovery copy of the encryption passphrase as the existing server contract requires.

**Cluster access:** the importer needs the decryption passphrase and backend read access for each source repository, plus its own Postgres credential. The current session-backup SOPS rule includes workstation users and admin, not Flux's cluster recipient; no importer access exists yet. Follow the existing shared-secret pattern: one SOPS-encrypted Kubernetes Secret source with both intended workstation recipients and the Flux recipient, consumed through `sops-nix` key paths on workstations and Flux decryption in the importer namespace. Avoid a manually maintained second copy. The importer pod receives mounted Secret values, not a workstation SSH key or the Flux age private key. Repository read access does not require granting the worker snapshot-pruning authority.

**Ingestion:** declare a Kubernetes CronJob with `concurrencyPolicy: Forbid`, initially every few minutes. Poll the retained repositories rather than depending on callbacks or asking clients to notify the job. A persistent ledger in Postgres makes restarts/retries safe; the job's local extraction directory can be disposable. Inventory historical snapshots on first run, select the latest known version of every source path, and populate current event projections. Subsequent runs inventory new snapshots, extract changed supported files, and atomically publish each parsed file version with its current rows. File/version identity, not content equality between individual messages, prevents duplicate imports while preserving repeated operations. Retain references to older versions in the archive; ordinary SQL need not retain a full duplicate event set for every appended-file snapshot.

If a source disappears, mark it absent on the workstation and keep its last archived projection. If parsing fails, retain the bytes and error, keep the last successfully parsed projection labelled stale, and retry after adapter repair; never mark the failed version successfully indexed. Report last completed archive and ingestion positions separately. None of these runtime additions are implemented in the design PR.

### Minimal SQL boundary

This is a proposed schema boundary, not implemented tables or a schema-migration commitment:

| Relation                             | Purpose                                                                                                                                                                                                                          |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `captures`                           | Source host, harness root, snapshot/archive reference, source capture order/time, completeness and file-read errors.                                                                                                             |
| `file_versions`                      | Original relative path, file type/metadata, content and metadata fingerprints, archive location, and parse status. Covers every captured file, including ones never indexed semantically.                                        |
| `file_heads`                         | Latest known version per source path, plus whether the source still has that path. Deletion upstream does not clear the retained head.                                                                                           |
| `raw_events`                         | File-version identity, record number/byte location, and full decoded source JSON for complete JSONL records. Preserve unknown event types. Parse errors and incomplete trailing bytes remain recorded against the archived file. |
| `sessions`, `messages`, `tool_calls` | Normalized views/projections of selected raw events, with native IDs and source links. Model responses and tool results retain their recorded ordering and content.                                                              |

Postgres [`jsonb`](https://www.postgresql.org/docs/18/datatype-json.html) gives SQL access and indexes over varying harness fields, but does not preserve original formatting, key order, or duplicate keys. The original files remain authoritative. Conversion failures, including values Postgres cannot represent, must become visible ingestion failures with raw archive references, not dropped source records.

Importing the same snapshot twice must be idempotent. Importing successive versions of an appended transcript must not multiply the current operation count. Do not identify events by content alone: two identical tool calls are two operations. Nor is a native session ID alone a sufficient physical source key when sessions are copied or resumed on another machine. Those rules, current-version selection, and harness-specific interpretation are the substantive new code.

SQLite state is included in archival regardless of query schema. A first SQL projection can index JSONL session operations and inventory the SQLite images; extracting selected harness database tables later does not change whether those files were preserved.

### Relative integration work

| Work item                            | Existing piece to reuse                                                       | New work still required                                                                                                                                  |
| ------------------------------------ | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Directory backup and transfer        | Restic, current host/timer/credential wiring                                  | Correct exclusions, preserve original layout, consistent live database capture, explicit completeness reporting.                                         |
| Ingestion from retained snapshots    | Restic JSON listing and selective extraction                                  | Capture/file-version ledger, retries, deduplication and archive-to-row provenance. This is the main new service.                                         |
| Initial SQL access                   | Postgres JSONB or DuckDB's JSON readers                                       | Import whole event objects and expose source/host/date information; no embeddings, MCP, or UI needed.                                                    |
| Consistent operation model           | Existing connector/parser implementations as references or reusable libraries | Check exact tool input/output handling, subagent/fork links, record ordering, and format-version support. Borrowing a parser still requires this review. |
| Latest-known history after source GC | Retained snapshots and SQL source-status metadata                             | Process deletions as source status changes; keep previously ingested sessions queryable.                                                                 |
| Richer search/trace UI               | Optional `cass`, `sessions`, or Langfuse                                      | Connect them to preserved files/SQL later if useful. Callback delivery is never the only source of session history.                                      |

No runtime estimate is claimed: extraction cost, file counts, directory size, parsing coverage, and existing package availability have not been measured. That affects whether to use `cass` connectors, write small harness adapters directly, or begin with SQL-only JSON views. It does not change the archive/query separation.

## Other compositions worth comparing

| Composition                                                                | Reused pieces                                                                          | New work / unresolved fit                                                                              | Assessment                                                                                                                  |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Whole-directory Restic + Postgres                                          | Existing transport, deduplicated retained snapshots, SQLite snapshot helper, SQL JSONB | Correct source capture; snapshot/file ingestion ledger; harness views                                  | Preferred starting point under the clarified requirements.                                                                  |
| Versioned `rclone` folders + Postgres                                      | File transfer and previous-version directories; SQL JSONB                              | Capture consistency, unique version namespaces, provenance/import worker; measure whole-version growth | Main alternative if simpler file access outweighs Restic's deduplication and snapshot machinery.                            |
| Whole-directory archive + `cass` SQLite, optionally replicated to Postgres | Existing normalized messages, source attribution, connectors, lexical search           | Keep whole-directory backup separately; schema mapping, fidelity checks, packaging and license fit     | Worth comparing for parser reuse. Its session-focused archive alone does not establish full harness-directory preservation. |
| Whole-directory archive + DuckDB JSON queries                              | Restic/rclone extraction; existing SQL JSON reader                                     | Materialized file set and harness views; later Postgres importer if needed                             | Smallest SQL exploration path; no obligation to adopt a recall product before useful queries exist.                         |
| Whole-directory archive + optional recall/trace interfaces                 | `sessions`, Episodic Memory, Langfuse, or Entire                                       | Connect preserved data to whichever UI or workflow proves useful                                       | Later additions; they do not replace the primary archive or file-derived SQL.                                               |

For an independent archive, two transport designs are plausible:

- **Cluster pulls over SSH:** reuse `cass` collection or a small rsync job. Fewer workstation components; archive latency depends on workstation reachability. A no-delete mirror still needs version preservation before overwrite.
- **Workstations capture locally and push:** a watcher/timer spools changed files or chunks, then retries uploads to the cluster. Better fit for intermittent connectivity and immediate local rotation protection. Needs a small collector/receiver or an existing transfer tool plus an ingestion worker. Do not synchronize a mutable archive database between machines; synchronize source bytes with explicit identities.

In either design, a periodic reconciliation scan complements hooks/watchers. A hook alone misses sessions when disabled or interrupted; a periodic scan alone cannot recover bytes deleted before its first observation. Existing folders should be backfilled immediately when a collector is introduced. Improving source retention helps the capture window but does not replace archive retention.

## Suggested boundary for code we might write

Build only what the chosen existing pieces do not provide:

1. **Capture and transfer:** resolve actual `CLAUDE_CONFIG_DIR`/`CODEX_HOME` and additional roots; preserve whole directory trees, including metadata and symlinks. Do not silently follow symlinks to add external trees. Keep original database files and additional consistent images or use filesystem snapshots. Include archived session directories. Reuse Restic's snapshot/transfer machinery before writing a new spool or protocol.
2. **Archive envelope:** retain original bytes/versions with checksum, host, harness, source path, capture time, native session ID when available, and collector version. Preserve parse failures. Source deletion changes source-status metadata, not retention. The first implementation can use persistent files plus an append-only manifest; object storage is another backend, not a requirement to invent a new format.
3. **SQL ingestion:** retain full event JSON and source/byte locations, then normalize selected fields. Preserve physical source identity separately from logical session identity so resumed/copied sessions do not collide. Latest-known views retain sessions after harness deletion; earlier file versions remain recoverable without requiring historical versions to appear in ordinary queries.
4. **Optional query adapters:** materialize layouts that an additional indexer expects, or convert archived events into trace views. Such conversion has separate progress and can retry from source files; it is not required for initial SQL queries.
5. **Coverage reporting:** distinguish last observed, transferred, archived, indexed, and trace-converted positions. Surface missed machines, raw capture pauses, parse errors, and growing retry backlogs. Search working is not the acceptance signal for preservation.

Avoid building a new search UI or semantic recall service for the initial requirement. A thin file-to-SQL importer and a small set of harness-specific views may be less integration work than adapting an entire recall product. Reuse existing parser code where it actually saves work and preserves the required detail.

## Acceptance experiments before selecting components

These are proposed checks, not results. Use synthetic fixtures first, then a representative private sample covering both harnesses and all three hosts. The spike should produce commands, counts/checksums, example query results, resource use, and exact failure observations.

| Experiment                 | Pass condition                                                                                                                                                                                                                     |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Existing-history import    | Every discovered source file is archived or has an explicit actionable failure; old dates and original host/project metadata remain queryable.                                                                                     |
| Whole-directory fidelity   | Restore a capture and compare its file inventory, bytes, relevant metadata and symlinks against the captured source view. Include non-JSON files, nested subagents, config/cache files and SQLite originals/consistent images.     |
| Connected freshness        | Measure source-to-durable-archive delay on representative updates against the accepted approximately 15-minute window. Report archive-to-SQL delay separately; a timer interval alone is not a measured freshness result.          |
| Landing-folder replacement | Replace a file twice between ingester runs. Every version accepted by the transfer/versioning layer remains recoverable, even if the parser never saw it live.                                                                     |
| Harness GC                 | After ingestion, remove the source session, reconcile, and rebuild the query database from archive alone. The conversation and recorded tool operations remain available.                                                          |
| Rewrite/truncation         | Replace a transcript with a shorter valid one. Both versions remain recoverable; normal SQL selects the latest known archived version without silently destroying earlier bytes.                                                   |
| Long live session          | Repeated appends preserve earlier records without quadratic retained storage. Partial trailing JSON does not lose completed records or block raw archival.                                                                         |
| Offline destination        | Generate work while the cluster is unreachable, close the harness, then restore connectivity without sending another prompt. Collection and file-derived SQL catch up for retained source data.                                    |
| SQL examples               | Find a known session by host, date, project and text; list its recorded model/tool operations and results using SQL. Native JSON and normalized views link back to exact archived source records. No NL search or MCP is required. |
| Operation fidelity         | Compare raw source with SQL rows: tool arguments/results, repeated identical calls, reasoning summaries where present, model/usage, subagents, forks, interrupted/final turns, and large outputs. Report intentional omissions.    |
| Callback independence      | Disable hooks/OTLP exports and import old retained files. All expected SQL events remain discoverable; a session absent from telemetry is not absent from file ingestion.                                                          |
| Identity                   | The same path on two hosts stays distinct; the same session copied/resumed elsewhere preserves source provenance without accidental record loss or unexplained duplicate results.                                                  |
| Index replacement          | Delete derived state in the isolated spike and rebuild using no workstation access. Counts and selected complete transcripts match; unknown raw records remain recoverable.                                                        |
| Capture/storage pressure   | Exceed an isolated capture budget or fail a write. Preservation becomes visibly unhealthy; indexing cannot falsely acknowledge archival success.                                                                                   |

Start by demonstrating whole-directory Restic capture plus snapshot-to-SQL ingestion. Use DuckDB over extracted JSONL for low-cost schema exploration or populate Postgres directly. Compare versioned `rclone` directories only if extraction cost or operational complexity is material. Evaluate `cass` parser/model reuse against the exact missing normalization work, rather than adopting its search stack by default. Langfuse and richer recall remain optional follow-ups.

## Inspected source snapshots

The agent-tool source references below use pinned commits. General collection-tool documentation was checked on the research date and is not pinned. Local repository context was read at `e23189776a23fedd9dd717a282bbc3abcd147e55`; the PR versions were inspected separately. These snapshots establish what was read, not deployed or tested compatibility.

| Project                | Commit inspected                           |
| ---------------------- | ------------------------------------------ |
| `cass`                 | `21e93cbd188481b6a85bb252cfbed626c6f5d0b3` |
| `sessions`             | `e617512a242c26221011a79606e57c9093852ca5` |
| `claudex`              | `595e130c62933f407c9119ed88755bba66f523e4` |
| Episodic Memory        | `7e06519357777badd7a115d2014a7ef845904310` |
| Entire CLI             | `68c520d8956d273fb14176379822c43a341d6172` |
| Langfuse documentation | `a78bd88e34d91b893bd3a85707f9dab72d6d3a22` |
| Langfuse Codex plugin  | `5ca5c6ce3f13ed142db63a97f1304819e7d07a7d` |

[pr-server]: https://github.com/agentydragon/ducktape/blob/9d5dbb01e0e0198d4d10a1be41f29ac7551521e9/cluster/k8s/session-backup/README.md
[pr-client]: https://github.com/agentydragon/ducktape/blob/0b80f3762cb42dfafd1420d951011b3bf0935730/nix/home/services/session-backup.nix
[cass-readme]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/README.md
[cass-sync]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/sources/sync.rs
[cass-mirror]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/raw_mirror/store.rs
[cass-sqlite]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/storage/sqlite.rs
[cass-model]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/model/types.rs
[cass-codex]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/connectors/codex/implementation.rs
[cass-retention]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/docs/raw-mirror-retention.md
[cass-policy]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/raw_mirror/policy.rs
[cass-license]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/LICENSE
[sessions-readme]: https://github.com/nicknisi/sessions/blob/e617512a242c26221011a79606e57c9093852ca5/README.md
[sessions-vault]: https://github.com/nicknisi/sessions/blob/e617512a242c26221011a79606e57c9093852ca5/src/vault/archive.ts
[sessions-cache]: https://github.com/nicknisi/sessions/blob/e617512a242c26221011a79606e57c9093852ca5/src/cache.ts
[claudex-readme]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/README.md
[claudex-indexing]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/website/guide/indexing.md
[claudex-schema]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/website/reference/schema.md
[claudex-provider]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/crates/claudex/src/providers/mod.rs
[claudex-export]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/crates/claudex-cli/src/commands/export.rs
[episodic-readme]: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/README.md
[episodic-sync]: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/sync.ts
[episodic-parser]: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/parser.ts
[langfuse-claude]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/integrations/developer-tools/claude-code.mdx
[langfuse-codex]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/integrations/developer-tools/codex.mdx
[langfuse-guide]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/resources/engineering/coding-agent-tracing.mdx
[langfuse-codex-hook]: https://github.com/langfuse/codex-observability-plugin/blob/5ca5c6ce3f13ed142db63a97f1304819e7d07a7d/plugins/tracing/src/index.ts
[langfuse-retention]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/docs/administration/data-retention.mdx
[entire-readme]: https://github.com/entireio/cli/blob/68c520d8956d273fb14176379822c43a341d6172/README.md
[entire-import]: https://github.com/entireio/cli/blob/68c520d8956d273fb14176379822c43a341d6172/cmd/entire/cli/agentimport/agentimport.go
