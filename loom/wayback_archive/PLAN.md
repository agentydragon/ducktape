# Wayback write-through archive service plan

Status: plan drafted 2026-06-12; v0 replacement PR in progress. The PR
replaces nginx/PVC `wayback-cache` in place with one Rust archive-service pod,
one CNPG instance, and SeaweedFS S3 replay bodies. After merge, wait for image
publish and Flux reconcile, smoke-test the live service, then run the eval
comparison.
Companion to
<../plans/wayback_ia_throttling.md>, <../plans/wayback_proxy.md>, and
<../plans/archive_org_apis.md>.

## Summary

Build an Internet-Archive-shaped service in the cluster. From the frontend it
accepts the Wayback paths our eval already uses:

- `GET /wayback/available?url=...&timestamp=...`
- `GET /cdx/search/cdx?...`
- `GET /web/<timestamp><modifier>/<original-url>`

On a local hit, it serves from our stored archive records. On a miss, it talks
to real IA on the backend, validates the result against loom's as-of semantics,
writes the result into durable cluster storage, and then serves from that stored
record. This is a write-through archive cache, not a prefetch pipeline and not
a bulk IA mirror.

The material reliability lever is the miss-acquisition path: cold replay/CDX
misses see very high IA `5xx` rates, so v0 must apply adaptive per-endpoint
concurrency and backpressure. Semantic keys and canonicalization keep the store
clean, but they are not the part expected to fix a roughly half-failing upstream.

The per-agent `wayback_proxy` remains the policy layer for intercepted live-web
URLs: it enforces `WAYBACK_AS_OF`, rejects future captures, emits evidence
manifests, and points at this cluster service as its upstream.

## Non-goals

- Do not try to enumerate or prefetch the eval URL universe. Agents discover
  URLs dynamically, so the scalable invariant is online miss fill.
- Do not call Save Page Now from the eval path.
- Do not bulk-mirror IA. `web.archive.org` crawl WARCs are not available through
  an open replication protocol.
- Do not start with full `pywb`/`OutbackCDX` unless we later need broad
  Wayback compatibility. A purpose-built subset is enough for the current eval.

## Current reality

Before this PR, `cluster/k8s/wayback-cache/` was nginx `proxy_cache`. The v0
replacement keeps the `wayback-cache` Service/hostname but swaps the backend to
the Rust archive service. The nginx behavior below is the baseline being
replaced.

What it does well:

- PVC-backed hits never touch IA.
- `proxy_cache_lock` collapses concurrent identical request URIs.
- Availability, CDX, and replay requests are cached and rate-limited separately.
- Origin status and IA signal headers are logged/exported.

What it does not do:

- It does not understand Wayback objects. The cache key is still essentially
  origin plus request URI, not `(capture_ts, modifier, original_url)`.
- It cannot canonicalize aliases. Partial timestamps, IA canonicalization
  redirects, percent-encoding variants, direct replay URLs, and Availability/CDX
  resolution can converge on the same capture while occupying separate cache
  entries.
- It cannot persist semantic metadata: capture timestamp, original URL,
  modifier, archive status, memento headers, source URL, body hash, fetch time,
  aliases, and error classification.
- It cannot distinguish stable negative facts from transient IA failures.
  `no capture <= as_of`, takedown/exclusion, and archived 404 are not the same
  as IA `502/503/504`.
- It retries IA `502/503/504` with `proxy_next_upstream_tries 3`, which can
  amplify overload during the exact failure mode hurting the eval.
- It applies static request rates, not adaptive concurrency. A fixed `30r/m` or
  `60r/m` limit cannot react when IA's CDX/replay backend starts returning
  `50%`-scale `5xx`.
- It cannot tell the per-agent proxy to wait. A failing miss comes back as a
  fetch failure, and the LLM often loops until it exhausts its message budget.

## Target behavior

### Pinned v0 decisions

- Max acquisition queue wait is configurable; default to 60 seconds. If a miss
  cannot start IA acquisition within that budget, return `503 + Retry-After`.
- Start with one archive-service replica. Still implement Postgres-backed fill
  leases before increasing replicas; the first implementation slice can use
  in-process single-flight because there is only one archive-service pod.
- Start with one CNPG instance on `local-path-ovh` for v0. Move to a replicated
  CNPG profile after the service has proven useful in eval and the app has the
  cross-replica fill coordination needed to scale archive pods safely.
- Do not require Valkey for v0.
- Mildly prefer Rust for the archive service. It is a long-lived async proxy
  that may stream many response bodies and maintain endpoint limiters, so
  resource efficiency and tight backpressure are useful. Python remains a
  reasonable fallback if reuse of the existing `wayback_proxy` models/tests
  becomes the dominant implementation concern.
- Store only sane-size replay bodies. Make the cap configurable; default to
  10 MiB. A capture over the cap is a stable local policy outcome
  (`body_too_large`), not a transient IA failure to retry forever.

### Frontend contract

The service should look enough like IA for our clients:

- `wayback_proxy` can use it as `WAYBACK_UPSTREAM` and
  `WAYBACK_AVAILABILITY_UPSTREAM`.
- Direct archive URLs from agents remain clamped by `wayback_proxy`, then are
  served by this service.
- Response status, `Content-Type`, `Location`, `Memento-Datetime`, and raw body
  semantics should match what the eval relies on today.
- Public access, if kept, remains bearer-authed. The unauthenticated service
  stays ClusterIP-only.

### Storage model

Use explicit archive records instead of opaque HTTP cache files.

Replay body key:

```text
sha256/<first-byte>/<second-byte>/<content-sha256>
```

Store:

- body bytes in blob storage;
- status;
- selected response headers needed for replay semantics;
- `capture_ts`;
- `modifier`;
- canonical original URL;
- observed request URL aliases;
- source IA URL;
- content hash;
- fetch timestamp;
- classification: `served_capture`, `archived_error`, `stable_negative`,
  `body_too_large`, or `transient_failure`.

Use CNPG Postgres as the authoritative metadata store in v0. This service is a
long-lived cluster cache, not per-pod scratch state, so the metadata should have
application-level replication and failover from the beginning.

Implementation preference: use normal client libraries instead of handwritten
protocol clients. In Rust, use SeaORM/SeaQuery for metadata DDL and upserts, and
the `object_store` crate for S3-compatible SeaweedFS access. Avoid raw SQL in the
application path unless we later need a database feature the query layer cannot
express cleanly.

Recommended split:

- **CNPG Postgres**: replay records, Availability/CDX metadata records,
  canonical keys, observed aliases, stable negatives, fill attempts, endpoint
  limiter state snapshots, and audit/debug history.
- **SeaweedFS S3 blob storage**: replay bodies, keyed by content hash or replay
  record key. The authoritative metadata still lives in Postgres.
- **Valkey, future optional**: short-lived coordination and hot-path counters:
  leases, queue depth, in-flight tokens, cooldown deadlines, and
  metrics-friendly rolling windows. Anything needed for correctness must also be
  recoverable from Postgres. Do not require this for v0.

For v0, run a single CNPG instance on `local-path-ovh` in the same OVH zone as
the archive service and SeaweedFS-backed storage. The HA follow-up is to move to
the cluster's OVH-HA profile: two CNPG instances, anti-affinity by hostname, and
the archive service pinned to the same region. If a Valkey is added later, use
the existing operator-managed replicated Valkey pattern in the same region.

Prefer the SeaweedFS S3 API over a mounted durable PVC for replay bodies. Replay
bytes are immutable objects, so S3 matches the model better than POSIX files and
lets multiple archive-service pods read/write the same bucket without RWO mount
constraints. A small `emptyDir` or scratch PVC is still useful for in-progress
downloads before hashing/classification, but it should not be the durable body
store.

Write order:

1. Fetch IA response into a bounded buffer or scratch file while computing
   `sha256`.
2. Classify the response.
3. Put the body to SeaweedFS S3 under a content-addressed key such as
   `sha256/ab/cd/<hash>`.
4. Commit the CNPG metadata row referencing that blob key.
5. Serve the buffered/scratch body for the current request.

If the S3 write succeeds and the DB commit fails, the result is an orphan blob
that can be garbage-collected by scanning for unreferenced keys. A DB row must
not be committed before its blob exists.

### Miss flow

1. Normalize the incoming path into a semantic lookup key when possible.
2. Serve a local hit without contacting IA.
3. On miss, acquire a single-flight lease for that semantic key.
4. If another request already holds the lease, wait for the fill or return
   `503 + Retry-After` if the wait budget is exceeded.
5. Queue the exact needed IA resource through the endpoint-specific acquisition
   controller.
6. Validate the IA response against the Wayback/as-of contract.
7. Persist metadata and bytes atomically.
8. Serve from the stored record.

## Better IA client behavior

These are the protocol/client improvements that matter for the eval.

### Use Availability for normal timestamp selection

For intercepted live-web URLs, prefer:

```text
/wayback/available?url=<url>&timestamp=<as_of_ts>
```

Then validate:

- `closest.available` is truthy;
- `closest.timestamp` is a valid IA timestamp;
- `closest.timestamp <= WAYBACK_AS_OF`;
- `closest.url` is a Wayback replay URL with the same timestamp.

If Availability returns unavailable, future, malformed, or too lossy results,
fall back to clamped CDX. Do not trust "closest" blindly; IA can mean nearest in
time, including after the requested timestamp.

### Keep CDX, but narrow its role

CDX remains necessary for:

- direct agent CDX requests, after clamping `to <= WAYBACK_AS_OF`;
- diagnostics and richer capture lists;
- Availability fallback, especially for archived non-200 captures.

The service should cache CDX metadata by a normalized query key, not raw query
string spelling. It should reject or normalize parameters that would reveal
post-as-of captures.

### Store replay bytes by capture identity

Always acquire raw replay bytes with `id_` for normal page fetches:

```text
/web/<capture_ts>id_/<original-url>
```

Send `Accept-Encoding: identity` so evidence hashes are stable. Preserve the
body exactly as stored by IA.

### Handle redirects as archive protocol, not generic HTTP

Archive-internal redirects can canonicalize partial or inexact timestamps. The
service should:

- follow archive-internal redirects only while re-validating every timestamp
  against `WAYBACK_AS_OF`;
- store aliases from the requested replay URL to the canonical capture record;
- treat off-archive redirects as historical replay responses and store their
  `Location`, so the next client request re-enters the proxy and is clamped.

### Treat Memento headers as the archived-error boundary

Replay `4xx/5xx` with `Memento-Datetime` is historical content and should be
stored/served as an archived error page.

Replay `4xx/5xx` without `Memento-Datetime` is IA/cache failure and must not be
cached as content. Classify it as transient unless there is a specific stable IA
exclusion/takedown signal.

### Negative-cache stable facts

Cache stable negative outcomes separately from failures:

- no capture at or before `WAYBACK_AS_OF`;
- IA exclusion/takedown, if we can identify it reliably;
- archived 404 with `Memento-Datetime`.

Do not negative-cache IA overload, connection reset, timeout, `502`, `503`, or
`504`.

### Stop retry amplification

Replace blind `proxy_next_upstream` retry behavior with an acquisition policy:

- respect `Retry-After` when present;
- record IA signal headers such as `x-rl`, `x-na`, `x-app-server`, `x-ts`, and
  `x-tr`;
- maintain separate adaptive budgets for Availability, CDX, and replay;
- use retry budgets, not fixed "try three times" behavior;
- circuit-break per endpoint when transient failures spike.

When the service is backing off, return `503 + Retry-After` to `wayback_proxy`.
The proxy should enforce that wait so the agent cannot hammer a missing URL in a
tight loop.

### Adapt concurrency per IA endpoint

The controller should limit **in-flight IA requests**, not just request start
rate. Separate controllers are needed because Availability, CDX, and replay have
different latency and failure behavior:

- Availability: usually fast/healthy; keep enough concurrency to avoid making
  timestamp selection the bottleneck, but still honor `Retry-After`/`x-rl`.
- CDX: slow and failure-prone; use the most conservative controller.
- Replay: content-size-dependent and currently high-`5xx`; adapt separately
  from CDX so large content responses do not starve index lookups.

Use a simple controller first:

- maintain `current_limit` per endpoint;
- each acquisition must hold one endpoint token while the IA request is in
  flight;
- on success with acceptable latency, increase slowly;
- on `502/503/504`, timeout, connection reset, or explicit rate-limit signal,
  decrease quickly and set a short backoff window;
- on `Retry-After`, set the endpoint limit to zero until the retry time;
- bound limits with conservative min/max values so bugs cannot create an
  unbounded stampede.

This can be AIMD to start (`+1` per healthy window, halve on overload). Envoy's
adaptive concurrency filter can replace this later if it gives better
operational behavior, but the first implementation should keep the signal
classification in the archive service because it needs Wayback-specific context.

Initial v0 guesses from the eval data:

| endpoint     | initial | min | max | timeout | reasoning                                                                                                                                  |
| ------------ | ------- | --- | --- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Availability | 8       | 1   | 32  | 10s     | Recent eval saw Availability stay healthy; keep it from becoming the bottleneck, but still back off on signal.                             |
| CDX          | 2       | 1   | 6   | 120s    | CDX was around half `5xx` under the current static setup and has 10-25s latency, so start much lower than the implied old in-flight level. |
| Replay       | 2       | 1   | 8   | 60s     | Replay was also high-`5xx`; allow modest parallelism for independent content fetches but adapt separately from CDX.                        |

Window rules for v0:

- evaluate health over a rolling 30s window, or at least 10 completed requests;
- if transient failure rate is under 5% and latency is acceptable, increase
  limit by 1;
- if transient failure rate is at least 20%, halve the limit and enter a short
  jittered cooldown;
- if transient failure rate is at least 50%, drop to the endpoint minimum and
  cool down for 30-60s;
- if `Retry-After` is present, set the endpoint limit to 0 until that deadline;
- if a request waits more than the configured queue budget, return
  `503 + Retry-After` rather than letting the agent spin.

Expose metrics per endpoint:

- current concurrency limit;
- in-flight acquisitions;
- queue length and wait seconds;
- IA status counts;
- timeout/connect failure counts;
- backoff seconds remaining;
- cache/store hit rate before acquisition.

## Components

### Archive service

New service, mildly preferably Rust. Python/aiohttp has the lowest integration
cost because `wayback_proxy` already uses aiohttp, Pydantic, and the same IA
response models; Rust is the better default if we optimize for a tight
long-lived async proxy with explicit backpressure. It owns:

- route parsing for Availability/CDX/replay paths;
- canonicalization;
- semantic lookup;
- single-flight fill leases;
- response reconstruction from stored records;
- metrics and structured logs.

### Store

CNPG Postgres metadata plus SeaweedFS S3 blobs for bodies. Tables should model:

- replay records;
- metadata records for Availability/CDX responses;
- aliases from observed request URLs to semantic keys;
- fill attempts and transient failures.

Use database uniqueness and row-level locking for correctness:

- unique replay record key:
  `(capture_ts, modifier, canonical_original_url)`;
- unique normalized metadata keys for Availability/CDX queries;
- aliases as many-to-one rows pointing at replay/metadata records;
- fill leases as rows with `locked_until`, `owner`, `attempt`, and endpoint;
- `SELECT ... FOR UPDATE SKIP LOCKED` or equivalent to claim fill work.

Valkey can mirror lease/counter state later for speed, but Postgres remains the
source of truth after a pod restart or Valkey failover.

### IA acquisition client

One code path for all IA backend calls. It owns:

- contact `User-Agent`;
- `Accept-Encoding: identity` for replay;
- per-endpoint concurrency limits;
- adaptive backoff;
- retry budgets;
- IA signal-header capture;
- response classification.

Envoy can sit between this client and IA later, but it is not the core
invariant. Envoy can help with adaptive concurrency and circuit breaking; it
cannot classify Wayback semantics or persist archive records.

### `wayback_proxy` integration

Keep `wayback_proxy` focused on sandbox policy:

- intercept natural `http://` and `https://` URLs;
- enforce `WAYBACK_AS_OF`;
- validate future timestamps from Availability or IA redirects;
- emit evidence manifests;
- map `503 + Retry-After` into an enforced wait/retry instead of an immediate
  LLM-visible failure loop.

## Rollout plan

Current implementation slice:

- Rust archive-service binary with `/web/<timestamp><modifier>/<url>`,
  `/wayback/available?...`, and `/cdx/search/cdx?...` miss fill.
- In-process single-flight for identical replay misses, suitable for the
  one-replica v0.
- Adaptive per-endpoint acquisition limiters with configurable queue wait and
  cooldown defaults.
- SeaORM-managed CNPG replay/Availability/CDX metadata and `object_store` S3
  replay bodies.
- Stable `body_too_large` policy results and non-caching IA transient replay
  failures.
- Cluster deployment on the existing `wayback-cache` service/hostname, replacing
  the nginx/PVC implementation instead of running as a parallel v2.
- Public bearer-auth compatibility through a dedicated `:8090` archive-service
  listener using the existing `wayback-cache-token`.
- Prometheus metrics exported directly by the archive service.
- `wayback_proxy` retry/wait handling for `503 + Retry-After`.

Follow-up hardening:

- Validated Availability fallback to CDX inside the archive service, if we want
  the service itself to make timestamp-selection decisions rather than leaving
  that to `wayback_proxy`.
- TODO before scaling replicas above one: add Postgres-backed fill leases or
  advisory locks keyed by endpoint and semantic request key so identical misses
  across pods cannot duplicate IA fetches.
- TODO before HA database rollout: decide the backup/failover policy for
  `wayback-archive-db`, then move from one CNPG instance to the OVH-HA profile.
- Archive URL alias rows for IA canonicalization redirects.

1. Build the archive service against `fake_ia.py`-style tests.
2. Add CNPG metadata schema plus SeaweedFS S3 blob storage wiring.
3. Implement replay record storage and aliasing first. This directly targets
   the eval's `web.archive.org` replay `502` volume.
4. Add Availability metadata storage and validated fallback to CDX.
5. Add CDX passthrough/storage for direct clamped CDX requests and fallback.
6. Add adaptive per-endpoint acquisition concurrency/backoff and remove blind
   retry amplification.
7. Replace the current nginx/PVC `wayback-cache` Deployment with one archive
   service replica backed by CNPG metadata and SeaweedFS S3 replay bodies.
8. Point one eval job at the replacement service and compare:
   - IA requests per run;
   - transient `502/503/504` count;
   - cache/shard hit rate;
   - `nan` samples;
   - scored mean proper loss;
   - second-run reuse of objects filled by the first run.

Post-merge rollout checklist:

1. Confirm GHCR image publish and Flux image automation update the
   `wayback-cache` Deployment image.
2. Confirm Flux reconciles `wayback-cache-namespace`, `wayback-archive-db`,
   `seaweedfs-wayback-archive-bucket`, `seaweedfs-secrets`, and `wayback-cache`.
3. Smoke-test in-cluster `/healthz`, `/metrics`, `/wayback/available`, `/cdx`,
   and replay miss fill.
4. Smoke-test public `wayback-cache.allegedly.works` through the bearer-auth
   `:8090` listener.
5. Run the eval comparison against the replacement service and compare it to the
   prior approaches.

## Acceptance criteria

- The same capture requested through multiple replay URL spellings maps to one
  stored replay record.
- Concurrent identical semantic misses produce one IA backend fetch.
- A repeated eval run reuses captures filled by the previous run without
  touching IA for those captures.
- IA `502/503/504` is not cached as archive content.
- Archived 404/500 pages with `Memento-Datetime` are cached and served as
  historical content.
- Availability `closest` after `WAYBACK_AS_OF` is rejected or falls back to CDX.
- Direct CDX requests cannot reveal captures after `WAYBACK_AS_OF`.
- Backoff state is visible in metrics and is propagated as `503 + Retry-After`.
- When replay/CDX `5xx` rises, the service lowers IA in-flight concurrency
  without changing config or redeploying.
- Metadata survives archive-service pod restarts and a single CNPG instance
  failover without losing filled capture records or stable negatives.
- Durable replay bodies live in SeaweedFS S3, not an RWO-mounted cache PVC; any
  archive-service pod can serve a filled blob once its CNPG metadata row exists.
- Acquisition queue wait is configurable and defaults to 60 seconds; over-budget
  waits return `503 + Retry-After`.
- Replay bodies over the configured cap, default 10 MiB, are classified as
  `body_too_large` and are not retried as transient IA failures.

## Open questions

- Canonical URL rules: how aggressively can we normalize original URLs without
  merging distinct captured resources? Host case and default ports are safe;
  query ordering is usually not safe.
- Storage TTLs: replay bytes can be effectively permanent; Availability/CDX
  metadata and negative facts may need refresh windows for backfills/takedowns.
- HEAD and Range support: likely unnecessary for the eval, but direct clients
  may eventually expect them.
