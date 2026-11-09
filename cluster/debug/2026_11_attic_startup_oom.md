# Attic restart and database-read investigation (November 2026)

The `nix-cache/attic-next` container has repeatedly hit its 4 GiB memory limit.
The cause is not established. Before this change, the container logged startup
and listener messages but no garbage-collection lifecycle or request activity.
Kubernetes reports that the previous container was OOM-killed at
2026-11-09 02:10:09 UTC; its logs contain no final diagnostic message.

At the 2026-11-09 01:47:07.897 UTC cAdvisor scrape, the container used about
4,080 MiB (1,769 MiB RSS and 2,019 MiB cache); it was OOM-killed at 01:47:37.
Its replacement reached about 2,481 MiB at 01:48:04.512 and was killed at
01:48:23. PostgreSQL's `pg_stat_database.tup_fetched` rose sharply during the
restart period, by approximately 1.4 million, 2.26 million, and 2.26 million
rows in the minute buckets ending at 01:48:15.513, 01:49:15.513, and
01:50:15.513 UTC. These are database-wide counters, not query or caller
attribution. `pg_stat_statements` was unavailable, and existing gateway
telemetry did not expose Attic request methods or routes.

The upstream Attic server's monolithic mode starts its GC task immediately
after migrations; the configured `30 days` interval is a delay _between_ runs,
not an initial delay. GC emits `info` messages when it starts and after its
time-based object and orphan-NAR phases. The proposed deployment sets
`RUST_LOG=error,attic_server::gc=info`, preserving error logs from all targets
while surfacing those GC messages. The upstream implementation does not always
emit a completion message when the orphan-chunk phase finds no chunks, so the
logs cannot precisely time the end of every GC run. The image is digest-pinned;
its registry `latest` tag resolved to the same digest on 2026-11-09, but the image
has no source-revision label. No rollout or GC marker has been verified for
this proposed filter; verify the expected markers after rollout.

For the next restart, align timestamped `attic-next` pod logs (or the retained
Loki pod-log stream) with per-container cAdvisor RSS/cache/working-set series,
the PostgreSQL `tup_fetched` and transaction rates, and the CI Attic push job
timestamps. `kubectl -n nix-cache logs deploy/attic-next --previous --timestamps`
only covers the immediately preceding container. A GC start followed by heavy
reads with no concurrent CI push is evidence for startup GC, but it does not
exclude requests from other clients. If the read spike follows the GC phase
messages instead, investigate request traffic and query plans separately.

HTTP request tracing is deliberately not enabled. Attic's existing Tower trace
layer logs the full URI, including any client-supplied query string; its default
configuration omits headers and bodies, but arbitrary query text on the public
endpoint is not safe to retain. Aggregate gateway/network metrics and CI logs
cannot attribute every lookup or upload to a route or client. A sanitized
route-level counter or an upstream server change is needed for that distinction
when GC and uploads overlap.
