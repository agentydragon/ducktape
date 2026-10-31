# Wayback cache

The parked Wayback cache is a write-through Internet Archive cache for Loom's
archive-backed evals. Its Flux Kustomization is suspended; its replay bucket was
emptied and deleted. The source and deployment manifests remain under
<../cache/> and <../deploy/> for a future revival.

The service supports Availability, CDX, and replay paths. Input pods serve
cached records and enqueue misses; filler pods fetch and persist them. The
<../proxy/README.md> enforces the agent's `WAYBACK_AS_OF` policy and emits
evidence manifests.

Replay identity is capture timestamp, modifier, and canonical original URL.
Postgres owns replay records, Availability/CDX metadata, and fill queue rows;
replay bodies are stored by content hash in S3. A metadata row must not commit
before its blob exists; an S3 write followed by a failed database commit can
leave an orphan blob. Oversize replay bodies are classified as `body_too_large`
rather than retried as transient failures.

Filler pods claim queue rows and perform Internet Archive egress. Postgres
deduplicates cold misses and uses `LISTEN/NOTIFY` to wake fillers and waiters.
Runtime settings are mounted as YAML; database and S3 credentials and the
optional bearer token are env-backed.

Availability, CDX, and replay have separate adaptive in-flight limiters per pod,
so throttling one node's egress IP need not stop other nodes. Queue waits are
bounded and over-budget requests return `503` with `Retry-After`. Archived error
responses carrying `Memento-Datetime` are historical content; upstream errors
without it are transient or stable-negative signals. Metrics distinguish
limiter waits, acquisition failures, upstream status, and cache hits/misses.

**Gotcha:** Cold CDX misses have saturated the endpoint limiter and exhausted the
queue budget even when Availability and replay records were already present.
Investigate queue waits and upstream timeouts separately before raising filler
concurrency. Internet Archive API details are in <../../docs/archive_org_apis.md>.
