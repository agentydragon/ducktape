# Wayback Archive Service Plan

Last trimmed: 2026-06-12.

Status: v0 is live. The old nginx/PVC `wayback-cache` backend was replaced in
place by the Rust archive service: CNPG metadata, SeaweedFS S3 replay bodies,
Prometheus metrics, and a public bearer-auth `:8090` listener. The archive
service now runs one replica per OVH node and uses shared fill leases so
identical cold misses do not stampede IA across replicas.

Companions:

- <../wayback_proxy/README.md>: per-agent policy proxy and local demo.
- <../docs/archive_org_apis.md>: Internet Archive API behavior notes.
- <../gym/TODO.md>: active eval reliability TODOs.
- <../gym/k8s/README.md>: in-cluster eval run procedure.

## Service Contract

The archive service is an Internet-Archive-shaped, write-through archive cache.
It serves the Wayback paths the eval already uses:

- `GET /wayback/available?url=...&timestamp=...`
- `GET /cdx/search/cdx?...`
- `GET /web/<timestamp><modifier>/<original-url>`

On a local hit, it serves stored metadata and replay bodies. On a miss, it
acquires the needed IA object, validates it against Loom's as-of semantics,
stores the result, and serves from that stored record.

The per-agent `wayback_proxy` remains the policy layer. It intercepts natural
web URLs, enforces `WAYBACK_AS_OF`, rejects future captures, emits evidence
manifests, and points at this service as `WAYBACK_UPSTREAM` /
`WAYBACK_AVAILABILITY_UPSTREAM`.

Non-goals:

- no Save Page Now from eval traffic;
- no bulk IA mirror;
- no attempt to enumerate or prefetch the agent URL universe;
- no full `pywb` / `OutbackCDX` dependency unless future clients need broader
  compatibility.

## Current Architecture

Storage is semantic, not opaque HTTP cache files.

- Replay identity is `(capture_ts, modifier, canonical_original_url)`.
- Replay bodies live in SeaweedFS S3, keyed by content hash.
- CNPG Postgres is authoritative for replay records, Availability/CDX metadata,
  cross-replica fill leases, aliases, stable negatives, fill attempts, limiter
  snapshots, and audit/debug history.
- A DB metadata row must not commit before its referenced blob exists. If an S3
  write succeeds and the DB commit fails, the result is an orphan blob that can
  be garbage-collected later.
- Replay bodies over the configured cap, default 10 MiB, are classified as
  `body_too_large`; they are not retried as transient IA failures.

The v0 deployment runs multiple archive-service pods, spread across OVH nodes
so cold IA fills can use distinct node egress IPs. In-process single-flight
deduplicates identical misses inside a pod. Postgres-backed fill leases
deduplicate identical misses across pods, keyed by endpoint and semantic
request key. Lease waiters poll the shared store today; `LISTEN/NOTIFY` would
be a cleaner wakeup path once this coordination becomes hot enough to matter.

IA acquisition is endpoint-aware:

- Availability, CDX, and replay have separate adaptive in-flight limiters in
  each pod. This is intentional while pod placement is tied to node egress IP:
  one node/IP getting throttled should not globally stop other node/IPs.
- Queue waits are bounded by `WAYBACK_ARCHIVE_MAX_QUEUE_WAIT_SECONDS`
  (default 60s).
- Over-budget waits return `503 + Retry-After`.
- Transient IA failures (`502`, `503`, `504`, timeouts, connection resets,
  explicit rate-limit signals) lower endpoint concurrency and set backoff.
- Archived `4xx/5xx` responses with `Memento-Datetime` are historical content;
  IA/cache `4xx/5xx` without `Memento-Datetime` are transient or stable-negative
  signals, not replay content.
- Metrics expose endpoint limit, in-flight count, queue length/wait, backoff
  seconds, acquisition attempts/failures, upstream status, and hit/miss shape.

The limiter implementation now uses cancellation-safe RAII permits. Dropped or
wedged acquisitions release `in_flight` without recording a false health sample.
Failures are counted by endpoint, reason, and upstream status so queue timeout
can be separated from upstream timeout/backoff.

## First Eval Finding

First cold eval against the replacement service:

- date: 2026-06-12;
- job: `claude-sandbox/loom-gym-eval`;
- target: `http://wayback-cache.wayback-cache.svc.cluster.local:8080`;
- model/config: `glm-4.5`, `--task-filter manifold-`, `--max-samples 8`,
  old `message_limit=80`;
- result: 15 submitted answers out of 33, 18 no-answer / `nan`;
- mean proper loss over submitted samples: 1.218;
- driver summary: 1,084 served-fetch mentions, `855x 503`, `5x 403`;
- wall time: 2:22:15.

This proved the write-through plumbing but did not improve the eval. The visible
failure mode shifted from direct IA replay refusals to archive-service `503`
backpressure and slow cold acquisition.

Important diagnosis: the high-error URLs generally were archived. In the worst
sample (`scotus-upholds-trump-tariffs`), the archive DB already had
Availability `200` and replay `200` rows for all six unique fetched sites at or
before the task's `2026-01-10` clamp. Availability for
`http://www.reuters.com/` returned quickly, while the equivalent CDX miss waited
about 60s and returned `503 archive acquisition is backing off`. At that point
CDX limit and in-flight were both 1, so fresh CDX misses could spend the whole
queue budget waiting for the single slot.

The run predates the current eval harness defaults: `--message-limit 1000` and
Inspect summary compaction via `--compaction-threshold-tokens 115000`.
Rerunning with that config is still needed, but cache warmth alone is unlikely
to fix the core issue because agents choose new URLs dynamically.

## Active Next Steps

1. Verify the deployed multi-replica lease + limiter behavior in the live
   service.
   - Reprobe the known bad CDX path.
   - Burst cold CDX and replay misses.
   - Confirm `wayback_archive_limiter_in_flight{endpoint="cdx"}` returns to 0.
   - Confirm
     `wayback_archive_acquisition_failures_total{endpoint,reason,status}`
     separates limiter queue timeout from upstream retry/backoff.
2. Rerun the 33-task `glm-4.5` panel with:
   - `--message-limit 1000`
   - `--compaction-threshold-tokens 115000`
3. Record archive-service endpoint counters in the final eval notes, not just
   driver-level aggregate `503` counts.
4. Tune CDX acquisition if cache-side `503` remains dominant. CDX staying at
   concurrency 1 protects IA but serializes cold misses enough to hurt the agent
   loop.
5. Keep `wayback_proxy` retry/wait behavior covered: `503 + Retry-After` should
   be an enforced wait while the proxy has budget, and an agent-visible failure
   only after waiting would exceed that budget.
6. Decide whether to split the service into two binaries/containers:
   - `archive-input`: accepts agent/proxy HTTP, serves cache hits, owns request
     parsing and cache policy.
   - `archive-filler`: node-pinned egress workers with per-node IA
     limiter/backoff state.
     This would separate input routing from output egress-IP selection. Postgres
     has the primitives for the middle: lease rows, queue rows with
     `FOR UPDATE SKIP LOCKED`, and `LISTEN/NOTIFY` for wakeups.

## Hardening Backlog

- Add validated Availability fallback to CDX inside the archive service if we
  want timestamp selection to live in the service rather than in
  `wayback_proxy`.
- Add alias rows for IA canonicalization redirects and equivalent replay URL
  spellings.
- Replace cross-replica lease polling with Postgres `LISTEN/NOTIFY` so waiters
  wake immediately when a peer fills or releases a semantic key.
- If random Service input routing still sends requests to pods whose node/IP is
  backed off while another node/IP is healthy, split input and filler/egress as
  above or add an output-side scheduler/Envoy layer that understands per-egress
  health.
- Decide `wayback-archive-db` backup/failover policy before moving from a
  single CNPG instance to the OVH-HA profile.
- Add orphan-blob garbage collection.
- Decide refresh/TTL policy for Availability/CDX metadata and stable negatives.
- Add HEAD or Range support only if a direct client needs it; the eval does not.

## Acceptance Criteria

- Multiple replay URL spellings for the same capture map to one stored replay
  record.
- Concurrent identical semantic misses produce one IA backend fetch per service
  replica; cross-replica duplication is blocked before replica count exceeds
  one.
- A repeated eval run reuses captures filled by previous runs without touching
  IA for those captures.
- IA `502/503/504`, connection resets, and timeouts are not cached as archive
  content.
- Archived `404/500` pages with `Memento-Datetime` are cached and served as
  historical content.
- Availability results after `WAYBACK_AS_OF` are rejected or fall back to CDX.
- Direct CDX requests cannot reveal captures after `WAYBACK_AS_OF`.
- Backoff state is visible in metrics and propagated as `503 + Retry-After`.
- Metadata survives archive-service pod restarts and CNPG failover.
- Durable replay bodies live in SeaweedFS S3, not an RWO-mounted cache PVC.
- Queue wait is configurable and defaults to 60 seconds.
