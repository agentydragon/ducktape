# Wayback cache follow-ups

The service is parked; see <README.md> for the implemented contract and
<../../gym/k8s/README.md> for the eval procedure.

## Reliability after revival

- Reprobe the CDX miss for `http://www.reuters.com/` with the `2026-01-10`
  as-of clamp and burst cold CDX and replay misses. Confirm
  `wayback_cache_limiter_in_flight{endpoint="cdx"}` returns to zero,
  `wayback_cache_acquisition_failures_total{endpoint,reason,status}` separates
  queue timeout from upstream retry/backoff, and input-side fill waits appear in
  `wayback_cache_input_fill_wait_duration_seconds`.
- Tune filler concurrency if cache-side `503` remains dominant.

## Hardening

- Add validated Availability fallback to CDX if timestamp selection should live
  in the service rather than the proxy.
- Add alias rows for Internet Archive canonicalization redirects and equivalent
  replay URL spellings, mapping them to one stored replay record.
- Decide whether filler pods should remain an OVH-spread Deployment, become a
  DaemonSet, or run on roaming nodes once node/IP policy is clearer.
- Decide `wayback-archive-db` backup/failover policy before moving from a single
  CNPG instance to the OVH-HA profile; verify metadata survives pod restarts
  and database failover.
- Rename remaining stateful `archive` names in a migration-safe follow-up: GHCR
  image / Flux image policy, CNPG database and generated secret, SeaweedFS bucket
  and S3 identity. Consider renaming internal Rust `Archive*` names separately.
- Add orphan-blob garbage collection.
- Decide refresh/TTL policy for Availability/CDX metadata and stable negatives.
- Add HEAD or Range support only if a direct client needs it.
