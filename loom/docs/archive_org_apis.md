# Archive.org APIs for Loom Wayback Access

Reference notes from official Internet Archive developer docs checked on
2026-06-11, plus loom-specific observations from the `wayback-cache`,
`wayback/proxy`, and write-through cache-service work.

The short version for loom: use the **Availability API** for the normal
"pick a capture near `as_of`" path if it validates cleanly, use **Wayback
replay URLs** for bytes, keep **CDX** as a compatibility/debug/fallback path,
and treat the rest of archive.org's item/catalog APIs as out of scope for the
date-clamped browsing proxy.

## Sources

- Internet Archive developer portal:
  <https://archive.org/developers/>
- Availability API tutorial:
  <https://archive.org/developers/tutorial-get-snapshot-wayback.html>
- CDX tutorial:
  <https://archive.org/developers/tutorial-compare-snapshot-wayback.html>
- Automated access guidance:
  <https://archive.org/developers/bots.html>
- Item metadata API:
  <https://archive.org/developers/metadata.html>
- S3-like item storage API:
  <https://archive.org/developers/ias3.html>
- Python/CLI wrapper docs:
  <https://archive.org/developers/internetarchive/index.html>

## Wayback APIs

### Availability API

Endpoint:

```text
GET https://archive.org/wayback/available?url=<url>
```

Observed/expected timestamp form for loom to validate:

```text
GET https://archive.org/wayback/available?url=<url>&timestamp=<YYYYMMDDhhmmss>
```

Officially documented response shape:

```json
{
  "url": "http://example.com/",
  "archived_snapshots": {
    "closest": {
      "status": "200",
      "available": true,
      "url": "http://web.archive.org/web/20180427130634/https://example.com/",
      "timestamp": "20180427130634"
    }
  }
}
```

Why it matters here:

- It queries `archive.org`, not `web.archive.org/cdx/search/cdx`, so it may avoid
  the cold-CDX failure mode seen in the eval.
- It returns one chosen snapshot instead of the whole capture list, which matches
  the proxy's hot path if the chosen snapshot is correct.
- It should become the default resolver only after tests confirm the timestamp
  parameter returns a capture at or before `WAYBACK_AS_OF`, not simply the
  nearest capture on either side.

Validation questions before replacing CDX:

- Does `timestamp=<as_of_ts>` always return `closest.timestamp <= as_of_ts`?
  If not, the proxy must reject future timestamps and either return 404 or fall
  back to CDX.
- Does the API return archived non-200 captures, or only captures it considers
  "available"? Current replay behavior treats archived 404/500 pages as valid
  historical content when replay carries `Memento-Datetime`.
- Does `closest.url` already point at the desired replay path, or does the proxy
  need to normalize it to `/web/<timestamp>id_/<original-url>` to get raw bytes
  without Wayback rewriting?
- What headers appear under load (`x-rl`, `retry-after`, `x-na`, status 429 vs
  502/504)? Capture them in `wayback-cache` logs before tuning.

Recommended loom use:

- Normal URL lookup: Availability API -> validate timestamp -> fetch replay
  bytes.
- Cache it with a long but not infinite TTL. Like CDX, availability answers can
  drift if IA backfills older captures or applies takedowns.
- Keep enough response metadata in the proxy's upstream-error manifest to debug
  `available=false`, 429, 502, and malformed responses.

### CDX Server API

Endpoint:

```text
GET https://web.archive.org/cdx/search/cdx?url=<url>
```

Current proxy hot-path shape:

```text
GET /cdx/search/cdx?url=<url>&to=<as_of_ts>&output=json&limit=-1
```

The documented default row fields are:

- `urlkey`
- `timestamp` (`YYYYMMDDhhmmss`)
- `original`
- `mimetype`
- `statuscode`
- `digest`
- `length`

Why it matters here:

- It is the most expressive Wayback index API: complete capture lists, filtering,
  debug comparison between captures, and exact fallback when Availability is too
  lossy.
- It is also the endpoint that dominated the eval's cold-miss failures
  (`/cdx/search/cdx` 502/504 volume), so it should leave the hot path if
  Availability validates.

Recommended loom use:

- Keep direct agent CDX requests clamped: any user-supplied `to=` must be bounded
  by `WAYBACK_AS_OF`, and requests without `to=` get `to=WAYBACK_AS_OF`.
- Keep CDX in tests/fake IA until Availability semantics are covered by tests.
- Use CDX for diagnostics, richer offline analysis, and fallback if Availability
  cannot preserve the proxy's current semantics.

### Wayback replay URLs

Replay URL shape:

```text
https://web.archive.org/web/<timestamp><modifier>/<url>
https://web.archive.org/web/<timestamp>id_/<url>
```

The `id_` modifier is the important one for loom: it asks Wayback for raw replay
bytes instead of rewritten/bannered HTML. The proxy already fetches with
`Accept-Encoding: identity` so evidence hashes are stable.

Recommended loom use:

- After timestamp selection, always fetch raw bytes via `/web/<ts>id_/<url>`.
- Follow archive-internal redirects only while re-validating every redirected
  timestamp against `WAYBACK_AS_OF`.
- Return off-archive captured redirects back to the client so the next request
  re-enters the proxy and is resolved under the clamp.
- Treat replay 4xx/5xx with `Memento-Datetime` as archived content; treat
  replay 4xx/5xx without `Memento-Datetime` as IA/cache failure.

### Save Page Now

Save Page Now captures current live pages for future citation. That is useful
for ingestion workflows, but not for the `loom/gym` as-of eval: creating a new
capture in 2026 must not make post-`as_of` information available to an agent.

Recommended loom use:

- Do not call Save Page Now from the proxy or eval.
- It could be useful only in a separate data-preparation pipeline that records
  present-day evidence for future tasks.

## Archive.org item/catalog APIs

These APIs operate on archive.org "items" (collections of files plus metadata),
not Wayback captures. They are useful for general archive.org automation, but
not for choosing replay captures in the proxy.

### Item Metadata API

Endpoint family:

```text
GET https://archive.org/metadata/<identifier>
```

It reads metadata and file listings for archive.org items. Writes require
authentication.

Use for loom:

- Not needed for date-clamped web browsing.
- Potentially useful if a future pipeline stores curated eval artifacts as
  archive.org items and needs metadata/file inventory.

### S3-like API (`ias3`)

Endpoint family:

```text
https://s3.us.archive.org/<item>/<file>
```

It maps archive.org items to S3-like buckets and files to S3-like keys; `PUT`
creates/uploads item files and ingests them through Archive's backend.

Use for loom:

- Not needed for the proxy/cache.
- Potentially useful for publishing or backing up generated artifacts, subject
  to IA's collection and authentication rules.

### Advanced search / item search

Endpoint:

```text
GET https://archive.org/advancedsearch.php?...&output=json
```

This searches archive.org item metadata, not the Wayback web crawl index.

Use for loom:

- Not a replacement for Wayback capture lookup.
- Could help discover archive.org-hosted datasets/items, but not ordinary web
  URL snapshots.

### Changes, tasks, views, relationships, reviews

The developer portal also documents:

- Changes API: discover changed archive.org item identifiers.
- Tasks API: inspect upload/derive/catalog tasks.
- Views API: item/collection engagement counts.
- Relationships and reviews APIs: item/user metadata surfaces.

Use for loom:

- Not relevant to the Wayback proxy/cache path.
- Keep them out of the proxy unless a separate artifact-publication workflow
  needs them.

## Automated access rules that matter here

The official automated-access guidance says automated callers should:

- Send a descriptive `User-Agent`.
- Add delays or concurrency limits for bulk work.
- Honor `429 Too Many Requests` and `Retry-After`.
- Cache responses.
- Handle errors gracefully with backoff.

The current `wayback-cache` already sends a descriptive User-Agent and caches
responses. Next cache changes should log enough upstream headers to distinguish:

- clean rate limiting: `429` and/or `Retry-After`;
- IA-specific signals: observed `x-rl`, `x-na`, `x-app-server`, `x-ts`, `x-tr`;
- overload/failure: `502`/`504` without retry hints.

Probe gotcha: agent-shell egress can be blocked differently from the cluster
egress used by the eval. In particular, direct `web.archive.org` probes from an
agent session have returned `403` with `x-block-reason: hostname_blocked`.
Measure IA behavior from the in-cluster Wayback service path when debugging eval
traffic.

## Proxy/cache division after the Availability change

Proxy responsibilities:

- Own all date policy.
- Hold one immutable `WAYBACK_AS_OF`.
- Resolve normal URLs through Availability first.
- Validate every returned or redirected timestamp itself.
- Fetch raw replay bytes.
- Emit served-evidence and upstream-error manifest records.
- Clamp or reject explicit archive URLs and CDX requests.

Cache responsibilities:

- Be policy-dumb: cache and pace upstream requests, do not decide dates.
- Support at least two upstream host classes:
  - `archive.org` for `/wayback/available`;
  - `web.archive.org` for `/web/...` and retained `/cdx/...`.
- Include the upstream host/class in the cache key once multiple origins share a
  cache, so `/path` collisions cannot cross hosts.
- Export metrics split by upstream host, path class, cache status, response
  status, upstream status, and selected IA signal headers.
- Avoid amplifying IA failures: do not blindly retry high-rate 502/504 bursts
  without a circuit breaker or adaptive backoff.

Open design choice: keep one nginx cache that routes by path to both IA hosts,
or split into two ClusterIP services/caches (`wayback-replay-cache` and
`wayback-availability-cache`) so cache keys, metrics, and rate limits are
naturally separated. Splitting is more YAML but less clever.
