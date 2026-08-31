# ActivityWatch transport hardening

The current topology and operating contract are the single source of truth in
[`README.md`](README.md). The importer rollout is complete; this page keeps only the
remaining transport-boundary decision and a short historical record.

## Remaining

- **Make the write route ingest-only.** Incremental runs still read the destination for
  their bounded reconciliation window. Once the importer has a separate cursor or
  another way to avoid destination reads, restrict the write route to write methods;
  until then, a leaked write token can read history as well as ingest.

Agent credential hygiene (rotator-issued short-lived read tokens) and moving the central
DB off `local-path-proxmox` remain storage/deployment debt in the README, not blockers
for ingestion.

## Historical rollout

- 2026-08-26: replaced the mutating, non-idempotent `aw-sync`/Syncthing transport with
  the repo-owned REST importer, preserving source provenance and add-only reconciliation.
- 2026-08-26: enabled rugged, wyrm2, iguana, and atlas as importer devices behind the
  shared bearer-gated write route.
