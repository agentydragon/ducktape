# Talos host logs through Alloy

The replacement uses `otelcol.receiver.tcplog` in Alloy **v1.21.1**, pinned by
multi-platform image digest to the image used by the Docker test. This component
requires `--stability.level=experimental`.

## Protocol and behavior

Talos v1.12.3 and v1.14.0's
[`jsonLinesSender`](https://github.com/siderolabs/talos/blob/v1.14.0/internal/app/machined/pkg/runtime/logging/sender_jsonlines.go)
JSON-encodes each event (`msg`, `talos-time`, `talos-level`, service fields and extra
tags), appends a newline for TCP and holds the connection open. This is not syslog.
It retries connection/write failures, but a successful TCP write is not an
application-level acknowledgement. A receiver restart can therefore lose in-flight
messages with either collector; neither this pipeline nor Vector guarantees
exactly-once delivery or durable buffering.

Alloy's
[`tcplog` wrapper](https://github.com/grafana/alloy/blob/v1.21.1/internal/component/otelcol/receiver/tcplog/tcplog.go)
uses OpenTelemetry stanza v0.162.0's
[TCP input](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.162.0/pkg/stanza/operator/input/tcp/input.go).
It frames by newline across arbitrary TCP reads, accepts a final line at EOF,
uses a 1 MiB maximum line size, and leaves idle connections open. No JSON fields
become Loki labels. `loki.format=raw` preserves the original JSON body through the
OTel-to-Loki conversion. The relabel step retains exactly `job="talos-node"` and
`node=NODE_NAME`.

Loki timestamps remain receipt time, as in the old Vector socket source. Talos's
own event timestamp remains available in the `talos-time` JSON field; the pipeline
does not parse it as Loki time. The raw line no longer contains Vector-injected
transport fields (`host`, `port`, `source_type`); all sender fields
remain intact. There are no repository queries depending on those injected fields.
The Loki writer retries transient errors indefinitely, including HTTP 429; its
queue is in memory, with no WAL or persistent volume, as for the old receiver.

### Why not `loki.source.syslog` with `syslog_format="raw"`?

Both
[v1.21.1](https://github.com/grafana/alloy/blob/v1.21.1/internal/component/loki/source/syslog/internal/syslogtarget/transport.go)
and
[v1.22.0](https://github.com/grafana/alloy/blob/v1.22.0/internal/component/loki/source/syslog/internal/syslogtarget/transport.go)
can frame raw JSON lines, but enforce a default 120-second idle timeout. Zero
selects that default rather than disabling it. The Talos versions above detect a
closed connection only after a write fails, so a write after an idle close can
succeed locally and lose the event. A very long timeout only defers that problem.
The native TCP receiver has no idle timeout configured and avoids introducing
routine disconnects.

## Rollout and verification

The namespace, DaemonSet, selector, ConfigMap base name, Flux Kustomization,
ArtifactGenerator entry and generated directory keep their `vector-talos-logs`
identities. Only the receiver image/config changes. The DaemonSet rolls one node
at a time without surge; old and new collectors cannot bind host port 13333
concurrently. No resource adoption, pruning transition, host files or Terraform
endpoint changes are needed. Host networking, `127.0.0.1:13333`, Talos placement,
tolerations, resource budget and Loki's host-network ingress allowance stay.
Alloy's own HTTP listener also binds loopback, on port 12347.

`//cluster/cdk8s/monitoring:test_talos_logs` sends split/coalesced writes, Unicode,
an escaped newline, a 70 kB event and a final line at EOF through the production
pipeline into a real Loki 3.6.0 container. It checks original JSON, exact labels,
receipt timestamps, reconnect and delivery after a temporary Loki outage. This is
an RBE Docker test; it does not establish production delivery.

After Flux applies the change, check the DaemonSet is Ready on each online Talos
node, the collector reports no parse/send failures, and recent Loki results for
`{job="talos-node"}` cover each node with expected service fields (`| json`).
Check delivery again after a quiet interval and after a collector restart. Restore
the previous image and configuration by reverting this change through Flux if
fresh logs stop arriving. No checkpoint migration is required.
