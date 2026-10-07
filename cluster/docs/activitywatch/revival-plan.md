# ActivityWatch transport hardening

The current topology and operating contract are the single source of truth in [`README.md`](README.md). This page tracks
the remaining transport-boundary decision.

## Remaining

- **Make the write route ingest-only.** Incremental runs still read the destination for their bounded reconciliation
  window. Once the importer has a separate cursor or another way to avoid destination reads, restrict the write route to
  write methods; until then, a leaked write token can read history as well as ingest.

Agent credential hygiene (rotator-issued short-lived read tokens) and moving the central DB off `local-path-proxmox`
remain storage/deployment debt in the README, not blockers for ingestion.
