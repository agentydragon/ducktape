# Design

## Alternatives considered

This historical comparison records research completed on 2026-10-25 into Claude Code and Codex session collection on personal Linux workstations, initially `wyrm2`, `rugged`, and `iguana`. It captures the evidence and tradeoffs considered across collection, indexing, search, and tracing; it is not a current operations guide or active plan. The research favored raw, versioned file retention with a rebuildable SQL projection. Current importer and query behavior is documented in the [archive README](../README.md); workstation capture, exclusions, retention, and restore are owned by the [session-backup contract](../../cluster/k8s/session-backup/README.md).

### Historical first-upload measurement

A wyrm2 initial capture on 2026-10-26 contained **19,529 files / 27.204 GiB logical data**. Restic added **22.931 GiB of deduplicated data, compressed to 5.005 GiB**; repository disk usage was approximately **5.01 GiB**. The service took **9m 9s**, including SQLite staging; Restic itself took **8m 40s**. This is one initial-upload observation, not an estimate of incremental runtime or long-term growth.

### Requirements considered in the research

The research treated the source as the actual contents of configured harness directories, such as `~/.claude` and `~/.codex`: transcripts, tool results, subagent files, SQLite databases, configuration, and other recorded files. It called for preserving source files and directory structure, rather than selecting only fields a parser understands. The proposal did not assume credential/config/cache exclusions; indexing could be narrower than archival without deleting those source files. Its approved exceptions were Claude's `session-env/` (generated runtime files and Bazel output) and Codex's `worktrees/`; the current exclusion list is in the [capture contract](../../cluster/k8s/session-backup/README.md).

The research accepted a fifteen-minute collection delay while connected, with earlier captured versions recoverable and only the latest known version immediately queryable. SQL was sufficient; Postgres was an acceptable destination. Indexing was expected to read harness files without telemetry callbacks.

| Need                  | Required behavior                                                                                                                                           |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preserve sessions     | Collect existing history and subsequent changes, including sessions that never produce a Git commit. Retain collected data after source files disappear.    |
| Find past work        | Search across harnesses and machines; filter by date, project, and source host; open the matching conversation.                                             |
| Inspect operations    | Recover the recorded model messages, tool arguments/results, order, IDs, and available usage/timing. Follow subagent and fork relationships where recorded. |
| Independent retention | Harness rotation, workstation cleanup, and replacement of a synchronized folder must not delete archived content.                                           |
| Recover query state   | Rebuild or replace parsers, indexes, and UI without depending on files still existing on the workstations. Preserve unknown raw records for future parsers. |

“Attachments” means things such as image files referenced by a conversation. Files and embedded content inside the harness directories are included automatically. A reference to a file elsewhere does not copy that external file into the archive; discovering/copying external paths is additional scope, not a prerequisite for preserving the directories as requested. Exact provider request capture beyond what those files record is also outside this first scope.

“Nothing disappears” applies to captured data. A 15-minute polling scheme cannot promise to capture a file created and deleted entirely between successful captures, or work created offline and deleted before reconnecting. It does preserve all versions it actually captured, independently of later source GC. The research treated source-side capture/spooling as one possible way to reduce that window.

### Original PR assessment — historical

This section describes the proposal-era versions examined in the 2026-10-25 research: [#8587 at `9d5dbb01`](https://github.com/agentydragon/ducktape/pull/8587) and [#8588 at `0b80f376`](https://github.com/agentydragon/ducktape/pull/8588). For current capture and recovery behavior, use the owner documents linked above.

The [server contract][pr-server] specifies append-only workstation Restic repositories, with **no automatic pruning of their snapshots**. The 7 daily / 4 weekly / 6 monthly retention belongs to the outer VolSync backup of the server repository. It does not expire the inner workstation snapshots. A file removed by harness GC remains recoverable from an earlier inner snapshot until deliberate maintenance removes that snapshot.

The [client][pr-client] uploads every 15 minutes, snapshots Codex SQLite databases through the online backup API, and excludes selected credential/config/cache files and live `state_*.sqlite*` files. Those exclusions and the limited SQLite selection do **not** satisfy whole-directory preservation. Useful pieces to retain are host opt-ins, scheduling, credential wiring, and consistent SQLite snapshots. Restic can supply the file archive; indexing is a separate job. A first extraction must consider historical snapshots, not only the latest folder tree. The two storage copies share SeaweedFS, so their recovery boundary excludes loss of that whole substrate.

Whole-directory capture includes SQLite files and their journals/WAL sidecars, but copying them separately while the harness writes does not establish a consistent restorable database. The [SQLite backup API](https://www.sqlite.org/backup.html) supplies an additional consistent database image; it does not preserve the exact layout/history of the original files. The research therefore treated a consistent image with source-path mapping, or an atomic filesystem snapshot, as a complement to the raw directory capture. It cautioned against substituting only the consistent image for the raw files. See SQLite's [backup/copying caveats](https://www.sqlite.org/howtocorrupt.html#_backup_or_restore_while_a_transaction_is_active). The right collection mechanism for each workstation filesystem was not established.

Repository evidence narrowed that investigation: [`rugged`](../../nix/nixos/hosts/rugged/hardware-configuration.nix) and [`iguana`](../../nix/nixos/hosts/iguana/hardware-configuration.nix) declare Btrfs `@home` subvolumes, making a read-only home snapshot a candidate stable source for Restic. The [Btrfs documentation](https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html#nested-subvolumes) warns that snapshots exclude nested subvolume contents, so a design relying on them would need the actual harness-root mount/subvolume layout established first. Live mounts and `wyrm2`'s home filesystem were not established in this research. A local filesystem snapshot would be staging for off-machine backup, not the sole retained archive.

At the research date, repository configuration set Claude's `cleanupPeriodDays = 9999` in [`nix/home/claude_code/default.nix`](../../nix/home/claude_code/default.nix). This reduces one source of local expiry; it is not an archive retention contract, nor proof that every running workstation has activated that setting.

### Separate the layers

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

### Collection components evaluated

As of the research date, these components were checked against their official documentation, rather than source-pinned or run locally. They were alternatives to writing a custom transfer protocol.

| Component                                                                                                                                                      | Useful existing behavior                                                                                                                | Fit and limitation                                                                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`rclone copy`](https://rclone.org/commands/rclone_copy/) with [`--backup-dir`](https://rclone.org/docs/#backup-dir-string)                                    | Copies changed files without deleting missing destination files; moves overwritten destination files into a backup hierarchy            | A concrete first archive prototype: per-host `current/` plus a unique backup directory for every run. Reusing a backup pathname can overwrite history. Requires destination support for server-side move/copy. It preserves whole versions, so repeated appends can have substantial storage cost.                                |
| [Syncthing file versioning](https://docs.syncthing.net/users/versioning.html)                                                                                  | Continuous folder synchronization; receiver can retain versions on remote replacement/deletion or invoke an external versioning command | Good transport into landing folders. Versioning is off by default; built-in strategies can discard prior versions by count, interval, or age. Staggered versioning still coalesces versions even with unlimited age. An external versioner could hand old files to an archive before replacement.                                 |
| [Vector file source](https://vector.dev/docs/reference/configuration/sources/file/) + [S3 sink](https://vector.dev/docs/reference/configuration/sinks/aws_s3/) | File checkpointing and provenance, configurable disk buffering, batched object uploads                                                  | Worth comparing for incremental JSONL event collection, especially to avoid repeated whole-file copies. Preserve each original line as data before parsing; retain source path, file generation and offset for reconstruction/deduplication. This is an event stream, so SQLite databases and attachments still need a file path. |

The `rclone` composition evaluated was workstation folders → per-host current/version directories → session indexer, with Langfuse conversion optional. Scheduling, manifests, error reporting, and index refresh were glue; existing tools could do the copying and version preservation. A separate archive service might not be necessary for an initial implementation.

For Vector, the evaluation flagged [end-to-end acknowledgements](https://vector.dev/docs/architecture/end-to-end-acknowledgements/) and disk buffering; the S3 sink defaults to a memory buffer. The file source's default `max_line_bytes = 102400` discards some larger lines, including potentially large embedded tool results/images. Checkpoint recovery, copy/truncate rotation, and replay ordering would need acceptance testing. A line tailer is not by itself a byte-exact file archive; a periodic file snapshot path could complement streaming capture.

The research noted that **a polling ingester can miss content replaced between scans** when it reads a synchronized current-folder tree. Receiver-side versioning, snapshotting before replacement, or source-side spooling would need to close that gap; parsing only the latest tree is insufficient.

### Existing pieces and their fit

The table describes the inspected versions, not an end-to-end deployment guarantee. Sources follow each option.

| Piece           | Existing capability                                                                                 | Role in a composed solution                                           | Work a composition would still have needed at the research date                                                                          |
| --------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `cass`          | SSH collection, source provenance, raw captures, lexical/semantic search, CLI and stdio MCP service | Broadest candidate for collection + archive + search                  | Nix packaging; capture-budget monitoring; retention configuration; verify raw recovery and parser coverage; custom license consideration |
| `sessions`      | Raw transcript vault, rebuildable search, CLI and MCP                                               | Smaller search/recall service over centrally preserved transcripts    | Transport; host separation; version preservation; subagent completeness                                                                  |
| `claudex`       | Additive SQLite history, full-text search, usage and session reports                                | Optional analytics over an independently retained archive             | Transport; raw preservation; explicit machine attribution; operation replay beyond aggregates                                            |
| Episodic Memory | Transcript copying, local embeddings, semantic/exact search, MCP                                    | Natural-language recall over archived conversations                   | Transport; host separation; version preservation; raw operation viewer                                                                   |
| Langfuse        | Central trace UI/API, Claude/Codex transcript integrations, session grouping                        | Operation inspection and a central query surface                      | Reliable replay/backfill from archive; source attribution; verify integration fidelity and deployed-version compatibility                |
| Entire          | Git-backed transcript checkpoints, remote synchronization, code attribution, historical import      | Useful if conversation-to-code provenance becomes a major requirement | Automatic coverage for sessions without commits; general workstation archival outside repository workflows                               |

#### `cass`: strongest broad fit, with operational caveats

**Observed:** [remote collection][cass-sync] uses rsync over SSH without `--delete`. Mirrors are separated by configured source; [raw manifests][cass-mirror] carry source ID, origin host, and original path. This directly addresses “which machine?” and keeps files that disappear upstream. Its [Codex connector][cass-codex] handles tool calls/results, but truncates indexed tool output beyond 128 Ki characters. The raw archive therefore matters for complete inspection.

**Preservation:** [raw-mirror retention documentation][cass-retention] describes content-addressed chunks and retained captures. Automatic retirement requires a newer capture to preserve the older bytes as an identical prefix; truncations and rewrites do not qualify. This is substantially closer to our requirement than a latest-file mirror.

**Important default:** the [capture policy][cass-policy] caps raw storage at 8 GiB and reserves 2 GiB of free space. If another capture cannot fit, raw capture can pause while indexing continues. A working search result is therefore insufficient proof of preservation. Explicit operator pruning can also remove divergent historical versions; the archive policy must constrain that separately from automatic prefix retirement.

**Composition:** a cluster worker could pull configured workstation sources into persistent storage, preserve the raw mirror/canonical database, and expose search through `cass serve --stdio --mcp` over a controlled connection. That service is documented in the [README][cass-readme]; it is not an authenticated HTTP application by itself. Alternatively, keep an independent archive and use `cass` primarily as its indexer. Rehydration into paths its connectors discover needs an experiment.

**Caveats:** upstream labels the project alpha. Its [license][cass-license] is MIT with an OpenAI/Anthropic restriction, not plain MIT; account for that before choosing it as a dependency. Source-build/Nix packaging and upgrade behavior remain unevaluated. Treat a successful raw-mirror-to-empty-database recovery as a required proof, rather than assuming every archive file is automatically rediscovered by ordinary indexing.

#### `sessions`: a useful small vault/search pattern

**Observed:** the [vault][sessions-vault] keeps raw Claude/Codex transcript copies and a manifest. The [indexer][sessions-cache] rediscovers vault-only sessions when source files disappear. Its search database can be rebuilt from those files; archiving failures warn and allow indexing to continue.

The archive is **one latest copy per original path**. `archiveFile` overwrites it when source size/mtime changes. The indexer falls back to the vault for unusable source files, but a still-parseable shortened/replaced transcript can become the new vault copy. This protects against disappearance, not every destructive rewrite. The manifest is atomically replaced; the raw-file copy itself uses `copyFileSync` to its destination.

Claude discovery in the inspected indexer selects project-level `*.jsonl` files. Subagent user text is folded into search, but that does not establish independent archival of nested subagent transcripts. Message search also intentionally omits tool-result noise; use raw data for operation inspection.

**Composition:** run the search/MCP component over a materialized view of a versioned archive. Host namespaces must survive materialization: a shared original path or a session resumed/copied between machines must not silently overwrite another source. This is a plausible smaller MIT-licensed alternative if we supply collection and stronger raw retention. See the [project overview][sessions-readme] for query interfaces.

#### `claudex`: useful analytics; its retained index is not the raw archive

**Observed:** [incremental sync][claudex-indexing] retains rows when source files disappear; schema migrations preserve them. `claudex index --force` deliberately wipes that retained history and rebuilds from files still present. Changed files are re-ingested, so additive session retention should not be mistaken for preservation of all earlier contents of a changed file.

The [schema][claudex-schema] stores searchable user/assistant messages and useful derived data. `tool_calls`, for example, contains per-session/tool counts, not an ordered log of every invocation and result. The [export implementation][claudex-export] filters indexed candidates to those still on disk before reading source records. A retained searchable session is consequently not equivalent to an exportable full transcript after source GC.

**Composition:** feed it preserved, materialized transcripts for reporting on models, costs, files, and PRs. The [public JSON command interface][claudex-readme] is preferable to coupling our service to its changing SQL schema. It contributes less to transport and durable archival than the other candidates; it is an optional analytics component rather than the first foundation to adopt.

#### Episodic Memory: stronger fit for recall than operation replay

**Observed:** [sync][episodic-sync] copies new/modified transcripts into an archive using a temporary file and rename. It keeps files that vanish from the source, but replaces a destination with a newer source version based on mtime. That has the same version-preservation gap as a latest-file vault.

Its [parser][episodic-parser] builds conversation exchanges, records some tool metadata, and intentionally excludes reasoning from searchable conversation text. The [README][episodic-readme] documents local embeddings, exact/semantic search, MCP, and oversize exchanges excluded from indexing. Search coverage and raw preservation therefore need separate checks. Summary generation can be disabled; summaries are an optional recall aid, not the retained evidence.

**Composition:** useful for “find the session where we discussed this,” operating on an independent raw archive. Collection, host identity, and a complete operation view remain separate components. Compared with `sessions`, its main reason to choose it would be demonstrated retrieval quality on our queries, rather than stronger retention.

#### Langfuse: attractive existing operation view, with ingestion work

At the research date, repository configuration declared a Langfuse deployment, external Postgres/ClickHouse/Valkey, SeaweedFS event/media storage, and Authentik access in [`cluster/cdk8s/langfuse.py`](../../cluster/cdk8s/langfuse.py). That makes reusing it attractive; this investigation did not check live readiness, project settings, or existing workstation coverage.

**Fidelity:** the [Claude integration][langfuse-claude] reconstructs generations and tool spans from transcripts. Its example code uses the latest user message or preceding tool results as generation input, rather than recreating the entire actual request. It also truncates some displayed fields. This is useful operation inspection, but not proof of the full prompt sent to a model.

The [Codex integration][langfuse-codex] documents richer capture including recorded system/developer context, tool I/O, interrupted turns, and subagents. That is an integration-specific claim, not a guarantee for every harness/version. The [generic coding-agent guide][langfuse-guide] and this integration differ on context coverage; test the exact plugin instead of treating either description as universal.

**Delivery:** in the [Codex hook][langfuse-codex-hook], turns are marked uploaded only after conversion and exporter shutdown return without error. Unmarked turns remain retryable on another hook invocation. This is useful retry state, but not an independently scheduled durable upload spool or proof of server persistence. The documented latest-turn finalization also needs checking for a session that never receives another turn.

The [Claude example][langfuse-claude] saves its advanced read offset even when `emit_turn` fails, then flushes in cleanup. That code path can skip a failed turn on the next invocation. Do not use that sample alone as the archival transport.

**Retention:** [self-hosted Langfuse retains event data indefinitely by default][langfuse-retention], unless a retention policy is configured. Verify the actual project, database TTLs, and object-store lifecycle before relying on that. No-retention configuration also does not make converted traces a replacement for the original bytes.

**Composition:** optional after file-derived SQL works: replay/backfill archived events into Langfuse with deterministic IDs and host/harness/session metadata. Reuse the upstream converters where practical. A worker with retries would supply the delivery guarantee missing from hook-only collection. This is additional glue; API/SDK version compatibility and conversion semantics remain to be demonstrated. Neither OTLP callbacks nor hooks determine which source files are retained or queryable.

#### Entire: Git-backed code provenance and historical import

**Observed:** [normal capture][entire-readme] condenses sessions into durable Git refs at commits and syncs checkpoints with Git pushes. A separate private checkpoint remote is supported. This provides useful ownership and code-to-conversation links, but automatic archival depends on repository workflow activity.

There is also `entire import`. The [importer][entire-import] supports historical sessions as read-only imported checkpoints with deterministic per-turn identities. Historical import is not limited to sessions that produced code commits. Scheduled import coverage of uncommitted sessions and all relevant project roots, including work outside existing repositories, remains to be demonstrated. First-time `--import-history` defaults to the last 30 days, so it is not a complete historical backfill by itself.

**Composition:** a viable Git-based archive direction if attribution to commits is central. For the stated requirements it introduces repository discovery, checkpoint synchronization, and Git storage policy where a general transcript collector is simpler. Keep it as an alternative, not a required layer.

### SQL-first fit and integration findings

The SQL query requirement reduces the value of adopting a complete recall product. The candidates' databases contain useful patterns, but none of the inspected implementations is a ready-made whole-directory-to-Postgres pipeline.

| Existing implementation                                                                       | SQL data actually available                                                                                                                        | Integration implication                                                                                                                                                                                                                           |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`cass` canonical storage][cass-sqlite] and [message model][cass-model]                       | SQLite conversation/source/workspace tables, ordered messages, content, and `extra_json`; connectors normalize tool records                        | Strongest existing operation-oriented model to evaluate. Feeding Postgres still requires an adapter and archive provenance mapping. Truncation/redaction/connector omissions mean canonical rows are a projection, not the authoritative archive. |
| [`sessions` index][sessions-cache]                                                            | SQLite session metadata plus FTS5 message text; intentionally excludes some tool-result content from message search                                | Easy to query its own SQLite database, but copying that schema does not produce complete operation records. Its MCP server adds little to the initial SQL requirement.                                                                            |
| [`claudex` provider interface][claudex-provider] and [schema][claudex-schema]                 | SQLite search text and derived reports; `ProviderRecord` carries tool names/counts and aggregate usage rather than a complete ordered event stream | Useful reporting/parser reference. Reconstructing operation detail still needs raw-file ingestion, so this is not a shortcut to the required Postgres model.                                                                                      |
| [Episodic Memory parser][episodic-parser]                                                     | Exchange-oriented SQLite/vector model and selected tool metadata                                                                                   | Retrieval features are optional now; adopting its exchange model would still leave raw event preservation and normalization work.                                                                                                                 |
| [DuckDB JSON reader](https://duckdb.org/docs/1.4/data/json/loading_json) over extracted files | SQL directly over JSONL through `read_ndjson_objects`, retaining whole JSON objects instead of inferred narrow columns                             | A low-glue way to explore the archived corpus and develop normalization queries. It does not supply archival, incremental import state, or a Postgres service.                                                                                    |

#### Composition favored by the research: Restic archive plus a file-to-Postgres worker

The research favored reusing the per-host append-only Restic transport, 15-minute scheduling, raw-file capture, and consistent SQLite images, while treating backup and indexing as separate success states. The proposed importer would enumerate completed snapshots with [Restic's JSON scripting interface](https://restic.readthedocs.io/en/stable/075_scripting.html), track file versions, and use [selective restore or `restic dump`](https://restic.readthedocs.io/en/stable/050_restore.html#printing-files-to-stdout) rather than requiring a privileged FUSE mount or restoring every cached plugin file. It would retain source and archive references, keep arbitrary non-event files in the archive inventory, and expose selected event data through SQL views. Ordinary queries would show each source path's latest known version while older versions remained recoverable from Restic.

This was an integration judgment, not a benchmark result: it reused the existing backup mechanism and kept the query projection separate from retained source files. The current importer and its access model are described in the archive README linked above.

### Other compositions considered

| Composition                                                                | Reused pieces                                                                          | New work / unresolved fit                                                                              | Assessment                                                                                                                  |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Whole-directory Restic + Postgres                                          | Existing transport, deduplicated retained snapshots, SQLite snapshot helper, SQL JSONB | Correct source capture; snapshot/file ingestion ledger; harness views                                  | Favored by the 2026-10-25 research.                                                                                         |
| Versioned `rclone` folders + Postgres                                      | File transfer and previous-version directories; SQL JSONB                              | Capture consistency, unique version namespaces, provenance/import worker; measure whole-version growth | Main alternative if simpler file access outweighs Restic's deduplication and snapshot machinery.                            |
| Whole-directory archive + `cass` SQLite, optionally replicated to Postgres | Existing normalized messages, source attribution, connectors, lexical search           | Keep whole-directory backup separately; schema mapping, fidelity checks, packaging and license fit     | Worth comparing for parser reuse. Its session-focused archive alone does not establish full harness-directory preservation. |
| Whole-directory archive + DuckDB JSON queries                              | Restic/rclone extraction; existing SQL JSON reader                                     | Materialized file set and harness views; later Postgres importer if needed                             | Smallest SQL exploration path; no obligation to adopt a recall product before useful queries exist.                         |
| Whole-directory archive + optional recall/trace interfaces                 | `sessions`, Episodic Memory, Langfuse, or Entire                                       | Connect preserved data to whichever UI or workflow proves useful                                       | Later additions; they do not replace the primary archive or file-derived SQL.                                               |

The research compared two transport designs for an independent archive:

- **Cluster pulls over SSH:** reuse `cass` collection or a small rsync job. Fewer workstation components; archive latency depends on workstation reachability. A no-delete mirror still needs version preservation before overwrite.
- **Workstations capture locally and push:** a watcher/timer spools changed files or chunks, then retries uploads to the cluster. Better fit for intermittent connectivity and immediate local rotation protection. Needs a small collector/receiver or an existing transfer tool plus an ingestion worker. Do not synchronize a mutable archive database between machines; synchronize source bytes with explicit identities.

The research treated periodic reconciliation and an initial backfill as complements to hooks/watchers: a hook alone can miss sessions when disabled or interrupted, while polling cannot recover bytes deleted before its first observation. Improving source retention narrows that capture gap but does not replace archive retention.

### Inspected source snapshots

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
