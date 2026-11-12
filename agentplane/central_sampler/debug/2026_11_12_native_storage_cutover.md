# Native sampler storage: staging cutover and growth measurements

## Cutover, 2026-11-12

[PR #15718](https://github.com/agentydragon/ducktape/pull/15718) merged as
`5d68c0f7c8278c1ca92e523ec3ba6b616e04a79b`. Staging was cut over to
`devel-20261112092451-5d68c0f`, image digest
`sha256:d9097eb654c38205f06f5d1842c30151b7adf639ffae323d04ee8e466d00f79b`.

Old sampler replicas were stopped before migration 13 ran using the serving binary's
`--migrate-only` mode. The operator explicitly approved deleting the 27 existing sampler
sessions and related requests, journal events and export checkpoints. History's database and
existing executor storage were not reset. The migration succeeded at approximately 09:30 UTC.

`pg_database_size('central_sampler')` fell from **3,673,585,343 to 9,008,831 bytes** immediately
after migration. This reclaimed old data; it is not a measurement of deduplication efficiency.
The primary filesystem's available space increased from 11,234,123,776 to 14,893,506,560 bytes.

At 09:42 UTC, both sampler replicas were ready on the new image, the temporary Deployment
reconciliation hold had been removed, and Flux reported a successful reconciliation. CNPG had
two ready instances. The replica independently reported schema version 13 and the two new test
sessions with all 34 provider requests.

## Live acceptance

Two isolated sessions used `chatgpt-sub/oai-responses/gpt-6-luna` and
`anthropic-api/ant-messages/claude-haiku-5-5`, both at low reasoning effort. Each completed:

- A real tool-call turn executing `printf 'storage-smoke\n'`; all associated processes exited 0.
- Twelve appended user-input turns, each with roughly 3 KB of inert reference text.
- Explicit compaction followed by a successful turn.
- Another successful turn after replacing the sampler Pod.

All **32 pre-restart requests** reconstructed to identical JSON structures after restart.
Earlier Anthropic requests included signed thinking blocks. The post-restart turns followed
compaction, so this does not independently test restarting with an uncompacted signed-thinking
continuation. All 34 final requests reconstructed successfully. History export checkpoints
matched the last event cursors for both sessions: 1,685 and 682.

The temporary executor Sandbox was suspended to `stopStage=Stopped`, then deleted with a UID
precondition. Its PVCs were gone afterward. The completed migration Job, local port forwards
and temporary diagnostic token were also removed. The two small sampler test sessions remain
as evidence; no user workspace was used for the checks.

## Append-growth measurements

These snapshots precede compaction. Counts combine both sessions; turns are per session.

| Turns | Requests | Sum of reconstructed request JSON bytes | Shared content, context and request bytes | Database bytes |
| ----: | -------: | --------------------------------------: | ----------------------------------------: | -------------: |
|     1 |        4 |                                 163,865 |                                    89,986 |     10,467,007 |
|     5 |       12 |                                 552,559 |                                   129,273 |     11,916,991 |
|     9 |       20 |                               1,026,069 |                                   167,017 |     13,235,903 |
|    13 |       28 |                               1,585,375 |                                   205,153 |     14,636,735 |

For equal four-turn additions to each session, the selected shared representation grew by
**39,287, 37,744 and 38,136 bytes**. Keeping every full request would add **388,694, 473,510
and 559,306 JSON bytes**. Physical database growth over the same batches was **1,449,984,
1,318,912 and 1,400,832 bytes**. This bounded sample supports shared-prefix growth for these
provider paths; it is not a production-scale benchmark or a proof about every conversation.

The shared-representation column sums UTF-8 payload lengths for unique reachable
`sampler_content` rows and `pg_column_size` of their unique context rows and request rows.
Reachability includes source and projected contexts, request prefixes/suffixes and provider
profile/message references. At turn 13 this was 116 content rows (173,193 bytes), 87 context
rows (21,208 bytes), and 28 request rows (10,752 bytes).

This column excludes indexes, content-row overhead, intents and journals. Full-request JSON is
also uncompressed. Neither column predicts the physical saving from a hypothetical old-format
database, whose JSONB/TOAST compression would differ. `pg_database_size` includes all sampler
relations and allocation overhead, and therefore measures a different quantity.

## Remaining duplication

After 15 turns per provider, the largest sampler relations were `sampler_event` (4,456,448
bytes), `sampler_intent` (1,474,560), and `sampler_content` (262,144), including their indexes
and TOAST. The replica's database size was 15,775,423 bytes.

The new intent requests contain content/context/profile references rather than inline request
bodies. Intent **results** still retain provider responses and stream events. Summing sizes of
individually extracted JSONB fields across the 34 provider results gave 1,860,680 bytes for
`events` and 695,018 for `response`. Within responses, the 17 OpenAI results retained 370,243
bytes of `instructions` and 150,535 bytes of `tools`. These field sums are logical measurements,
not additive physical relation sizes.

The next focused investigation is provider response and stream-event retention, especially
repeated instructions/tool definitions that request profiles already represent. Trace recovery,
History replay and evidence readers before removing or replacing those copies. This rollout
changed sampler request/context storage; it did not rewrite History or optimize response
evidence, and it does not establish that acknowledged sampler events are disposable.
