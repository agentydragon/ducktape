# SeaweedFS trial — baseline performance

Initial latency/throughput numbers captured on the two-kimsufi-worker
SeaweedFS deployment described in <kimsufi_provisioning.md> and the trial
plan at `~/.claude/plans/ns103656-ip-147-135-39-us-...md`.

The point of this file: a written-down "what we got on day one" so a future
decision about (a) enabling the filer for PVCs, or (b) promoting the
storage class to non-trial use, has a recorded baseline to compare against.

## Hardware

|            |                                                                                          |
| ---------- | ---------------------------------------------------------------------------------------- |
| Nodes      | `talos-kimsufi-worker-0` (`147.135.39.162`), `talos-kimsufi-worker-1` (`147.135.39.176`) |
| Hardware   | KS-5: Intel Xeon-E3 1270 v6 (4c/8t), 32 GB RAM, 2× 2 TB SATA HDD JBOD                    |
| Datacenter | OVH `hil1`, rack `H109B04` (same rack, same AZ `us-west-hil-a`)                          |
| Talos      | v1.12.3                                                                                  |
| Network    | 1 Gbps                                                                                   |

## SeaweedFS layout

- 1 master, 2 volume servers (1 per node), 1 filer (leveldb2 metadata), 2 S3 gateways
- Replication: `001` (1 copy on a different node, same rack)
- Data on `/dev/sdb` (separate from the Talos system disk), XFS, ~1.95 TB usable per node
- Operator: chart `seaweedfs-operator-0.1.22` = operator v1.0.19
- SeaweedFS binary: `chrislusf/seaweedfs:3.93`

## Initial smoke-test numbers (2026-05-16, n=1 per row)

Single-run wall-clock timings from `aws s3 cp` (amazon/aws-cli image) in a
disposable pod against `seaweedfs-s3.seaweedfs.svc:8333`. These include
AWS CLI startup, TLS handshake, and bucket auth overhead — small-object
timings are dominated by overhead, not transfer.

| Operation | Object size | Wall clock | Effective throughput   |
| --------- | ----------- | ---------- | ---------------------- |
| PUT       | 1 KB        | 1.17 s     | ≈ overhead-dominated   |
| PUT       | 1 MB        | 0.84 s     | ≈ 1.2 MB/s w/ overhead |
| PUT       | 100 MB      | 4.20 s     | ≈ 24 MB/s              |
| GET       | 1 KB        | 1.86 s     | ≈ overhead-dominated   |
| GET       | 1 MB        | 0.78 s     | ≈ 1.3 MB/s w/ overhead |
| GET       | 100 MB      | 5.61 s     | ≈ 18 MB/s              |

Byte-identity verified by SHA256 on all three sizes.

These are spaghetti numbers; for a serious baseline replace with `warp` or
`s3-benchmark` percentile output once the monitoring pipeline is back up
(Alloy → Mimir → Grafana, blocked on a pre-existing Alloy config drift
unrelated to this work).

## Latency histograms (when Alloy is fixed)

Prometheus metric `SeaweedFS_s3_request_seconds` is a histogram per
operation type — that's the canonical source. Per-component endpoints
all live and serving Prometheus exposition:

| Component | Port | Endpoint                                                             |
| --------- | ---- | -------------------------------------------------------------------- |
| master    | 9321 | `seaweedfs-master:9321/metrics`                                      |
| volume    | 9325 | `seaweedfs-volume-0:9325/metrics`, `seaweedfs-volume-1:9325/metrics` |
| filer     | 9326 | `seaweedfs-filer:9326/metrics`                                       |
| s3        | 9327 | `seaweedfs-s3:9327/metrics`                                          |

(SeaweedFS binds these to the pod IP, not `localhost` — kubelet/Alloy
scrape via the Service ClusterIP, which is fine.)

## How to reproduce

In-cluster S3 endpoint: `http://seaweedfs-s3.seaweedfs.svc:8333`.
Credentials in SOPS at `cluster/k8s/seaweedfs/secrets/s3-config.sops.yaml`.

```sh
# admin creds (from SOPS)
sops -d cluster/k8s/seaweedfs/secrets/s3-config.sops.yaml |
  yq '.stringData["seaweedfs_s3_config.json"]' | fromjson |
  jq '.identities[0].credentials[0]'

# from a one-shot pod in the cluster:
kubectl run -n default --rm -it --image=amazon/aws-cli s3test -- \
  --endpoint-url http://seaweedfs-s3.seaweedfs.svc:8333 s3 ls
```
