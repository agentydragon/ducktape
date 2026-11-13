# Successful sampler audit deduplication

The writer change in [PR #16148](https://github.com/agentydragon/ducktape/pull/16148) merged and its
staging rewrite and `VACUUM FULL` completed on 2026-11-13. Sampler migration 14 rewrote 38 successful
audits across 3 Sessions; History migration 17 rewrote 1,623 audits across 26 Sessions. Every removed
array matched the separately retained ordered provider stream under the same Session/correlation ID.
Raw transport, terminal content/metadata, signed continuation and failure diagnostics remain.

## Integrity and live checks

- All 3,337 overlapping sampler/History EventEntry records matched after decoding source compression.
  Only hashes left the PostgreSQL container for this check.
- Event counts, Session counts, packed blocks and cursor-watermark digests were unchanged. Result
  fragments kept their cursors, origin, record ID, indices/count, parent/correlation ID and source links.
- Fresh OpenAI and Anthropic turns, tool calls, compaction and post-compaction continuation passed.
  Afterwards all 48 successful audits retained responses without embedded `events`; all three
  sampler Sessions' History export checkpoints matched their source cursors.
- Both services returned to two Ready replicas. Flux reconciliation and its original annotations,
  Deployment pause states and `max_slot_wal_keep_size=512MB` were restored. Physical and logical
  replication slots stayed active and valid.

## Physical reclamation

Decimal bytes, measured before new canary traffic:

| Database/relation |        Before | After rewrite and full vacuum |
| ----------------- | ------------: | ----------------------------: |
| `central_sampler` |    16,209,599 |                    14,440,127 |
| `sampler_intent`  |     1,712,128 |                       557,056 |
| `sampler_event`   |     5,545,984 |                     4,931,584 |
| `sandbox_service` | 2,718,668,479 |                 2,693,904,063 |
| `session_event`   | 1,009,467,392 |                   984,694,784 |

This reclaimed about 26.5 MB across the two databases. It removes the successful audit arrays;
other response encodings and projection copies remain separate work in the storage plan.

## Baseline adoption

The cleanup baselines are sampler 15 and History 18. They create the final schema on empty
databases and adopt only the completed rewrite predecessors (14 and 17), preserving deployed
Goose history and advisory-lock identities. Earlier databases must run the preceding image's
rewrite before adoption. The converter, superseded migrations, staging reset path and old
compatibility fixtures are retired; startup does not scan or normalize legacy audit JSON.

For maintenance requiring temporary Flux holds, disabling direct Deployment rollouts alone is
insufficient because Flux restores declared pause settings. Suspend the owning Kustomization and
protect that hold from its parent with the documented
[resource reconciliation annotation](https://fluxcd.io/flux/components/kustomize/kustomizations/#suspending-and-resuming),
then restore both settings after maintenance. This cutover used `ducktape-flux/agentplane-staging`.
