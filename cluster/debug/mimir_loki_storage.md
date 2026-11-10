# Mimir and Loki storage attribution

Read-only investigation on 2026-11-10, 21:12–21:28 UTC. No logs, credentials, stored data, settings, or retention were changed. Mimir 3.2.3 and Loki 3.6.11 use SeaweedFS; the relevant HDD volume claims are single-copy at the Piraeus layer and Seaweed uses two object copies. Snapshots below are non-atomic and use decimal bytes.

## Mimir

An authenticated paginated S3 listing found 963 objects totaling 239.718 GB logical in `mimir-blocks`. Independently, the filer read replica's metadata recorded 239.927 GB including bookkeeping files. The Bucket status reported 272.026 GB and the S3 exporter logical gauge 211.729 GB; do not use those gauges interchangeably. The exporter physical gauge was 545.171 GB, not a verified accounting of the live listing.

Fetched all 171 TSDB `meta.json` objects and joined each block ID to its filer object sizes. Blocks span 2026-06-02 22:00 through 2026-11-10 20:00 UTC. Using `maxTime` to classify blocks:

| Block age   | Blocks | Logical GB | Index GB |
| ----------- | -----: | ---------: | -------: |
| 0–30 days   |     40 |     58.237 |   10.266 |
| 30–90 days  |     60 |     81.242 |   13.502 |
| 90–161 days |     71 |    100.238 |   13.631 |

159 blocks cover 24 hours, nine cover two hours and three have other spans. The classification assigns a block crossing a cutoff to the newer side; overlap, deletion markers, compactor processing, Seaweed vacuum and lower-layer discard mean this is a physical-reclaim upper bound, not guaranteed freed bytes. A 90-day cutoff would put roughly 100.2 GB logical / 200.5 GB at two object copies at risk; a 180-day cutoff would remove none of these observed blocks. The declared retention is 365 days but actual observed history begins in June. Reducing retention does not reduce PVC limits.

At one current instant, `count({__name__!=""})` returned 1,073,249 series. `hubble_flows_processed_total` accounted for 290,331 (27.1%), including 207,086 L7 HTTP series and 133,686 source-less L7 HTTP series (12.5% of all series). These are series counts, not exact sample rates or per-metric compressed bytes; the Hubble ServiceMonitor's default scrape interval is 60 seconds. Source-less series still have destination data. Hubble flow history is an intentional diagnostic feature (`cluster/terraform/main/cilium-values.yaml`), and the UI's live flow buffer is distinct from Mimir's retained series. We found no exact reference to this metric in repository dashboards/rules or the current PrometheusRule expressions, but ad-hoc queries and externally provisioned dashboards remain unknown. Test whether reducing source-less HTTP detail preserves needed diagnosis before filtering it, and do not simply drop a label: collapsing series can create duplicate samples.

Other current top families include API-server request-duration buckets (43,740), request-body-size buckets (37,152), etcd request-duration buckets (30,710), and API-server SLI buckets (25,352). The API-server scrape is already filtered after `scrape_samples_post_metric_relabeling`, so that scrape counter is not the accepted downstream total. The live ControlPlaneLeasePutLatency alerts use the request-duration histogram. Historical all-series queries at 7 and 30 days timed out under the 35-second bound; current rankings cannot be assumed for older blocks.

## Loki

The Bucket status reported 120.654 GB logical; the exporter logical gauge was 95.890 GB and physical gauge 241.336 GB. These disagree and do not establish a precise per-source physical share. Loki's index-volume API reports approximate **uncompressed** chunk bytes, and overlapping chunks can be counted at query-window boundaries. The volume query must be split into at most 30 days plus one hour on this deployment; adjacent windows can overlap. Streams without a `job` label are excluded below.

| Indexed job                     |  0–30 days | 30–60 days | 60–90 days |
| ------------------------------- | ---------: | ---------: | ---------: |
| `talos-node`                    | 286.384 GB |  44.288 GB | 122.822 GB |
| `monitoring/kube-state-metrics` |   0.272 GB |   0.038 GB | 182.569 GB |

The first job is not just application output: Vector forwards Talos service logs with only `job` and `node` stream labels, keeping `talos-service` and `talos-level` in each JSON line. Four five-minute LogQL byte aggregations 2, 12, 48, and 168 hours ago attributed 74.9–85.5% of Talos line bytes to `auditd`. Corresponding short chunk-query samples compressed to 5.9–7.3% of decompressed bytes; the non-Talos samples were 14.4–15.7%. These are small time samples, not a source-weighted inventory of the whole bucket.

A separate sample of 3,000 AVC lines (200 each from three nodes at five different ages) had `permissive=1` for all 3,000; 2,943 also named DRBD and `unlabeled_t`, with source `pod_t`, and 2,996 reported directory-write denials. `permissive=1` means those sampled denials were not enforced; it does not establish cluster-wide SELinux policy. In nine 1,000-line audit samples, correlating by audit event ID attributed 86.4% of sampled audit-line bytes to permissive DRBD/unlabeled AVC events, with per-window shares from 18.4% to 99.9%. Adjacent PATH, SYSCALL, CWD, PROCTITLE and EOE records multiply the log volume. Sampling is time- and node-stratified, not byte-representative. Logs can contain security-relevant events; do not discard all AVCs or relabel production volumes without understanding policy and the existing Piraeus runbook.

A deliberately broad scenario: if every auditd line disappeared, 286.384 GB indexed raw over 30 days × 75–86% sampled audit share × 5.9–7.3% sampled Talos compression × two Seaweed copies is roughly 25–36 GB physical per 30 days. This is a low-confidence projection, not measured audit bytes or a recommended deletion. A targeted fix or filter would save less; filtering only affects future ingestion. Per-stream Loki retention cannot select old audit records by `talos-service` because that field is not currently a stream label. Before filtering, evaluate labeling or policy configuration and preserve denial/security coverage; avoid raw label removal that changes stream cardinality unexpectedly.

The kube-state-metrics spike on 2026-08-12–09-10 was repeated Flux `spec.suspend` nil-path logging. All 1,500 sampled lines from three dates contained the nil-path vocabulary. The [September 9 fix](https://github.com/agentydragon/ducktape/pull/5864) added `nilIsZero`; live HelmRelease and ConfigMap now contain it. Four short old chunk scans compressed to 4.1–5.9% of raw bytes, so the indexed 182.6 GB should not be presented as 182.6 GB of disk. Those logs should age out under the configured 90-day retention; a query just beyond 90 days still returned a small 19.7 MB index estimate, so neither complete deletion nor physical reclaim is proven.

## Useful read-only queries

With local forwards to `svc/mimir-gateway` port 80 and `svc/loki-read` port 3100, the following queries return aggregates. Time ranges and selectors should remain bounded; never print raw log lines or credentials.

```promql
count({__name__!=""})
count(hubble_flows_processed_total)
count(hubble_flows_processed_total{type="L7",subtype="HTTP",source=""})
topk(30, count by (__name__) ({job="apiserver"}))
```

```logql
sum by (talos_service, talos_level) (bytes_over_time({job="talos-node"} | json [5m]))
```

The Loki `/loki/api/v1/index/volume` endpoint can group `{job=~".+"}` by `targetLabels=job` over explicit 30-day intervals. Mimir's TSDB block `meta.json` `minTime`/`maxTime` should be joined to object sizes; file modification time is not a reliable substitute for data age after compaction. A [Loki 3.6.11 source check](https://github.com/grafana/loki/blob/v3.6.11/pkg/storage/stores/shipper/indexshipper/tsdb/store.go) confirms its indexed chunk size is derived from uncompressed size.
