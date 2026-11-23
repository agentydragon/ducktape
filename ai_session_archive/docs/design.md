# Design

Decision record for collecting Claude Code and Codex sessions from personal Linux workstations (research of 2026-10-25, first `wyrm2`, `rugged`, `iguana`). It is not an operations guide: importer and query behavior is in the [archive README](../README.md); capture, exclusions, retention and restore are in the [session-backup contract](../../cluster/k8s/session-backup/README.md).

## Decision

Raw, versioned file retention with a rebuildable SQL projection: per-host append-only Restic repositories (the session-backup transport, its 15-minute schedule, raw-file capture and consistent SQLite images) plus a file-to-Postgres worker. Backup and indexing are separate success states. The worker lists completed snapshots with [Restic's JSON scripting interface](https://restic.readthedocs.io/en/stable/075_scripting.html), tracks file versions, reads files with [selective restore or `restic dump`](https://restic.readthedocs.io/en/stable/050_restore.html#printing-files-to-stdout) (no privileged FUSE mount, no restore of every cached plugin file), keeps non-event files in the archive inventory, and exposes event data as SQL views of each path's latest version; older versions stay recoverable from Restic. This was an integration judgment, not a benchmark.

One initial `wyrm2` capture (2026-10-26): 19,529 files, 27.204 GiB logical; Restic added 22.931 GiB deduplicated, 5.005 GiB compressed; the service took 9m 9s, 8m 40s of it Restic, with SQLite staging. It says nothing about incremental runtime or growth.

## Requirements

The source is the whole content of the harness directories (`~/.claude`, `~/.codex`: transcripts, tool results, subagent files, SQLite, config), not only the fields a parser understands; indexing may be narrower than archival. Approved exclusions: Claude `session-env/` and Codex `worktrees/` (the current list is in the session-backup contract).

- **Preserve sessions:** existing history and later changes, including sessions that never produce a Git commit; keep data after source files disappear.
- **Find past work:** search across harnesses and machines; filter by date, project and host; open the conversation.
- **Inspect operations:** recorded messages, tool arguments and results, order, IDs, usage and timing; follow subagent and fork links where recorded.
- **Independent retention:** harness rotation, workstation cleanup or replacing a synchronized folder must not delete archived content.
- **Recover query state:** rebuild parsers, indexes and UI without the workstation files; keep unknown raw records for future parsers.

Accepted: a 15-minute collection delay while connected, only the latest version immediately queryable, SQL and Postgres as enough, indexing that reads files without telemetry callbacks. Out of scope: copying files referenced from outside the harness directories, and provider request capture beyond what the files record. "Nothing disappears" covers captured data: polling cannot see a file created and deleted between captures, or created offline and deleted before reconnecting. Source-side spooling would narrow that window.

## Constraints that shaped it

- **Layers.** Archive the bytes before parsing. The receipt is "bytes and provenance durably archived", not "rsync finished" or "indexed". A parser break must not fail archiving; indexing and trace conversion retry from preserved input.
- **Versions.** A landing folder, even one without delete propagation, shows the latest state, and the same path can be truncated or overwritten. The archive must keep every accepted version; content-addressed chunks or versioned objects avoid storing a whole transcript per append. A polling ingester that reads only the latest synchronized tree can miss content replaced between scans.
- **SQLite.** Copying a database and its journal or WAL while the harness writes does not give a restorable database. The [SQLite backup API](https://www.sqlite.org/backup.html) gives a consistent image, not the original layout and history ([caveats](https://www.sqlite.org/howtocorrupt.html#_backup_or_restore_while_a_transaction_is_active)): keep it as a complement to the raw directory, never instead of it. [`rugged`](../../nix/nixos/hosts/rugged/hardware-configuration.nix) and [`iguana`](../../nix/nixos/hosts/iguana/hardware-configuration.nix) declare Btrfs `@home`, so a read-only home snapshot is a candidate stable source, but [snapshots exclude nested subvolumes](https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html#nested-subvolumes): establish the real mount layout first (not established for `wyrm2`). A filesystem snapshot is staging for off-machine backup, not the archive.
- **Source expiry.** At the research date `cleanupPeriodDays = 9999` in [`nix/home/claude_code/default.nix`](../../nix/home/claude_code/default.nix) reduced one source of local expiry. It is not a retention contract, and nothing proves every workstation had activated it.
- **Capture gaps.** A hook alone misses sessions when disabled or interrupted, and polling cannot recover bytes deleted before its first observation, so use periodic reconciliation and an initial backfill together. Improving source retention narrows the gap but does not replace archive retention.
- **Recovery boundary.** At the research date the inner Restic repository and its outer VolSync backup shared SeaweedFS, so losing that whole substrate was outside the recovery boundary.

## Options not taken, and why

Findings come from the pinned commits at the end, or from the tools' documentation on the research date; none was run.

- **`cass`** ([sync][cass-sync], [retention][cass-retention], [policy][cass-policy]): the strongest broad fit (rsync over SSH without `--delete`, per-source mirrors with host and path manifests, content-addressed raw chunks), but raw storage is capped at 8 GiB with a 2 GiB free-space reserve, so raw capture can pause while search keeps working. Automatic retirement keeps older bytes only when a newer capture extends them as a prefix. Indexed Codex tool output is cut at 128 Ki characters ([connector][cass-codex]). The project is alpha, its [license][cass-license] is MIT with an OpenAI/Anthropic restriction, Nix packaging and upgrades were not evaluated, and recovery from the raw mirror into an empty database was unproven. `cass serve --stdio --mcp` ([README][cass-readme]) is not an authenticated HTTP service.
- **`sessions`** ([vault][sessions-vault], [index][sessions-cache]): one latest copy per original path. `archiveFile` overwrites it when size or mtime changes, so a shortened or replaced transcript can replace the vault copy. Nested subagent transcripts are not independently archived, and message search omits tool results.
- **`claudex`** ([indexing][claudex-indexing], [schema][claudex-schema], [export][claudex-export]): the retained index is not the raw archive. `claudex index --force` wipes retained history, changed files are re-ingested, `tool_calls` holds counts rather than an ordered log, and export reads only sessions still on disk. Use it for analytics through its [JSON interface][claudex-readme], not through its changing SQL schema.
- **Episodic Memory** ([sync][episodic-sync], [parser][episodic-parser], [README][episodic-readme]): replaces a destination with a newer-mtime source (the same version gap as a latest-file vault); its parser leaves out reasoning and oversize exchanges. A recall aid, not retention.
- **Langfuse** ([Claude][langfuse-claude], [Codex][langfuse-codex], [Codex hook][langfuse-codex-hook], [retention][langfuse-retention]): it reconstructs generations from transcripts (the Claude example uses the latest user message or preceding tool results as input, not the full request), and that example saves its read offset even when `emit_turn` fails, which can skip a turn. Hooks give no scheduled durable upload spool. The deployment in `cluster/cdk8s/langfuse.py` makes reuse attractive, but only as an optional replay target after file-derived SQL works: deterministic IDs, host, harness and session metadata. Self-hosted retention is indefinite by default, yet converted traces never replace the original bytes.
- **Entire** ([README][entire-readme], [import][entire-import]): a Git-checkpoint model, so archival follows repository workflow and needs repository discovery and checkpoint sync. First `--import-history` covers 30 days; coverage of uncommitted sessions and of work outside repositories is unproven. A fit only if commit attribution becomes central.
- **`rclone copy --backup-dir`** ([copy](https://rclone.org/commands/rclone_copy/), [backup-dir](https://rclone.org/docs/#backup-dir-string)): the main alternative if simpler file access outweighs Restic's deduplication and snapshots. It stores whole versions, so repeated appends cost storage, needs a unique backup directory per run (reuse overwrites history) and needs server-side move or copy.
- **Syncthing versioning** ([docs](https://docs.syncthing.net/users/versioning.html)): off by default, built-in strategies discard versions by count, interval or age, and staggered versioning coalesces them. Usable only as transport into landing folders, optionally with an external versioner.
- **Vector file source and S3 sink** ([file](https://vector.dev/docs/reference/configuration/sources/file/), [S3](https://vector.dev/docs/reference/configuration/sinks/aws_s3/)): a line tailer, not a byte-exact file archive. The default `max_line_bytes = 102400` discards larger lines (tool results, images), the S3 sink buffers in memory by default, and SQLite files and attachments still need a file path. Checkpoint recovery, copy-truncate rotation and replay order need acceptance tests.
- **DuckDB JSON reader** ([docs](https://duckdb.org/docs/1.4/data/json/loading_json)): `read_ndjson_objects` gives low-glue SQL over extracted JSONL, useful for prototyping normalization queries. It supplies no archival, import state or Postgres service.
- **Cluster pulls over SSH versus workstation push.** Pull needs fewer workstation parts but archive latency depends on reachability, and a no-delete mirror still needs version preservation before overwrite. Push (a watcher or timer spools changed files, with retries) fits intermittent connectivity and protects against local rotation, but needs a small collector or transfer tool plus an ingestion worker. Never synchronize a mutable archive database between machines; synchronize source bytes with explicit identities.

None of the tools is a ready whole-directory-to-Postgres pipeline: their SQLite databases are projections (truncation, redaction, connector omissions), so each would still need raw-file ingestion for operation detail.

Pinned commits inspected: `cass` `21e93cbd`, `sessions` `e617512a`, `claudex` `595e130c`, Episodic Memory `7e065193`, Entire CLI `68c520d8`, Langfuse documentation `a78bd88e`, Langfuse Codex plugin `5ca5c6ce`; repository context `e23189776a23`. The full hashes are in the link targets below.

[cass-readme]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/README.md
[cass-sync]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/sources/sync.rs
[cass-codex]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/connectors/codex/implementation.rs
[cass-retention]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/docs/raw-mirror-retention.md
[cass-policy]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/src/raw_mirror/policy.rs
[cass-license]: https://github.com/Dicklesworthstone/coding_agent_session_search/blob/21e93cbd188481b6a85bb252cfbed626c6f5d0b3/LICENSE
[sessions-vault]: https://github.com/nicknisi/sessions/blob/e617512a242c26221011a79606e57c9093852ca5/src/vault/archive.ts
[sessions-cache]: https://github.com/nicknisi/sessions/blob/e617512a242c26221011a79606e57c9093852ca5/src/cache.ts
[claudex-readme]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/README.md
[claudex-indexing]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/website/guide/indexing.md
[claudex-schema]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/website/reference/schema.md
[claudex-export]: https://github.com/utensils/claudex/blob/595e130c62933f407c9119ed88755bba66f523e4/crates/claudex-cli/src/commands/export.rs
[episodic-readme]: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/README.md
[episodic-sync]: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/sync.ts
[episodic-parser]: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/parser.ts
[langfuse-claude]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/integrations/developer-tools/claude-code.mdx
[langfuse-codex]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/integrations/developer-tools/codex.mdx
[langfuse-codex-hook]: https://github.com/langfuse/codex-observability-plugin/blob/5ca5c6ce3f13ed142db63a97f1304819e7d07a7d/plugins/tracing/src/index.ts
[langfuse-retention]: https://github.com/langfuse/langfuse-docs/blob/a78bd88e34d91b893bd3a85707f9dab72d6d3a22/content/docs/administration/data-retention.mdx
[entire-readme]: https://github.com/entireio/cli/blob/68c520d8956d273fb14176379822c43a341d6172/README.md
[entire-import]: https://github.com/entireio/cli/blob/68c520d8956d273fb14176379822c43a341d6172/cmd/entire/cli/agentimport/agentimport.go
