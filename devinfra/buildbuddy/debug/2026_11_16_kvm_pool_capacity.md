# KVM pool capacity, 16 November 2026

The pool can grow from three to eight workers without required host anti-affinity.
Each worker reserves 2 CPU instead of 4, retaining four guest vCPUs, a 4-CPU limit,
8 GiB RAM, an 80 GiB local SSD work disk, and one BuildBuddy action at a time.
At the new ceiling, VM requests total 16 CPU and 64 GiB guest RAM, plus KubeVirt
and sidecar overhead. Previously the ceiling reserved 12 CPU and 24 GiB guest RAM.
The autoscaling minimum, rate limits, readiness and drain behavior stay unchanged.

## CPU sizing evidence

Mimir window: **2026-11-13 11:43:45 through 2026-11-16 11:43:45 UTC**, evaluated
every 30 seconds. Active means a worker reported at least one executing task:

```promql
sum by (namespace, pod) (
  buildbuddy_remote_execution_tasks_executing{
    namespace="buildbuddy-executor", service="buildbuddy-kvm-worker-metrics", endpoint="metrics"
  }
) > 0
```

Host CPU use includes the worker's compute container and sidecars:

```promql
sum by (namespace, pod) (
  rate(container_cpu_usage_seconds_total{
    namespace="buildbuddy-executor", pod=~"virt-launcher-buildbuddy-kvm-worker-[0-9]+-.*",
    container!="", container!="POD"
  }[3m])
)
```

Join these expressions with `and on (namespace, pod)` before calculating the
following time-weighted sample statistics across worker incarnations:

| CPU cores                                                   | Samples |  Mean |   p50 |   p90 |   p95 |   p99 |   Max |
| ----------------------------------------------------------- | ------: | ----: | ----: | ----: | ----: | ----: | ----: |
| Host CPU, 3-minute rate                                     |   5,843 | 0.992 | 0.925 | 1.563 | 1.771 | 2.213 | 2.638 |
| Host CPU, last scrape interval (`irate`, 5-minute lookback) |   5,843 | 1.046 | 1.026 | 1.696 | 2.136 | 2.612 | 3.307 |

There were 46 worker pod incarnations with active samples. Adjacent samples share
rate windows and are not independent runs. One-minute rate windows omit many
observations at the actual scrape interval; they are unsuitable for this sizing.
Guest node-exporter CPU, excluding idle, iowait and steal, averaged 0.696 cores
(p95 1.200); host consumption is the sizing input because it includes VM overhead.
Guest memory (`MemTotal - MemAvailable`) averaged 2.049 GiB, p95 2.706 GiB, with
an observed 5.000 GiB maximum over 5,813 active samples. Keep the full RAM request.

[KubeVirt CPU requests](https://kubevirt.io/user-guide/compute/resources_requests_and_limits/)
can differ from vCPU topology and CPU limits. A 2-CPU request covers sustained
observed demand with room for bursts up to the existing 4-CPU limit. It lowers
CPU scheduling reservations and contention weight; it does not reserve four
physical cores. Shorter spikes and different workloads may exceed this sample.

## Placement and limits

Initial source base: `ce5a9a6fc5`; capacity refreshed after rebasing onto
`0e7c9a7010`. Only three of the six HIL hosts have the required local SSD pool.
The storage class uses one local copy and `WaitForFirstConsumer`.

The 11:50 UTC snapshot subtracts all non-pool pod requests from allocatable
resources, then accounts for **2.025 CPU and 8.460 GiB per worker pod**, including
KubeVirt overhead and the restartable guest-console sidecar. These are total
pool budgets, including the existing workers, not room for this many extra VMs.

| Host          | CPU budget for pool | RAM budget for pool, GiB | Workers at 2 CPU requested | Workers by RAM only | Current SSD free, GiB |
| ------------- | ------------------: | -----------------------: | -------------------------: | ------------------: | --------------------: |
| ovh-ns1001419 |               9.548 |                   50.983 |                          4 |                   6 |                 385.4 |
| ovh-ns104952  |               6.468 |                   54.566 |                          3 |                   6 |                 312.9 |
| ovh-ns104963  |               5.812 |                   52.522 |                          2 |                   6 |                 325.2 |
| Total         |              21.828 |                  158.071 |                          9 |                  18 |               1,023.5 |

The new **maximum of 8** leaves one worker below the current CPU scheduling
ceiling of 9. Eight pods request about 16.2 CPU and 67.7 GiB including overhead.
RAM alone could fit 18, but that is not the combined resource ceiling.
Reducing CPU requests to 1.5 would raise the CPU-only count to 13 (6 + 4 + 3),
before memory and local disk constraints. That more aggressive CPU weighting
and the resulting density are untested.

The 80 GiB guest disk currently needs an approximately **84.8 GiB PVC** after
CDI filesystem overhead. Eight such PVCs total about 678.4 GiB. LINSTOR reports
thin-pool free space, not reserved space or an I/O guarantee; host placement,
other volumes and future writes still matter. Four fully written worker PVCs
on ovh-ns1001419 would leave roughly 46 GiB of the current free space, assuming
no growth by other tenants. Increasing past eight should revisit local SSD
placement and headroom as well as CPU. Docker and containerd data live on the
work disk; guest root filesystem growth remains on node ephemeral storage.

A second Mimir window, 2026-11-13 11:51:10 through 2026-11-16 11:51:10 UTC, has 4,321 samples
per host at one-minute evaluation intervals. Host CPU excludes idle, iowait and
steal, with a three-minute rate; memory is node-exporter MemAvailable:

| Host          | Mean CPU cores | CPU p95 | CPU max | Lowest available RAM, GiB |
| ------------- | -------------: | ------: | ------: | ------------------------: |
| ovh-ns1001419 |          2.839 |   4.827 |   6.417 |                     33.72 |
| ovh-ns104952  |          1.797 |   3.038 |   5.347 |                     34.37 |
| ovh-ns104963  |          1.474 |   2.469 |   4.111 |                     38.86 |

Queries use `job="node-exporter",namespace="monitoring"` and the host's
internal IP as `instance` (`10.42.0.19:9100`, `.16:9100`, `.17:9100`):
`sum by (instance) (rate(node_cpu_seconds_total{...,mode!~"idle|iowait|steal"}[3m]))`
and `node_memory_MemAvailable_bytes{...}/1024^3`.
These measurements include the previous 1–3-worker pool; they do not measure
eight concurrent workers. Other workloads can consume this headroom.

This is a capacity and reservation change, not a measured CI speedup. Co-located
busy workers still need comparison for queue time, execution time, CPU steal,
SSD latency, registration and graceful draining under concurrent load.
The existing scale-up policy adds at most one VM per 15 minutes, so growing
from one to eight takes at least 90 minutes after the first addition. The
ceiling does not prewarm eight workers or guarantee burst admission latency.
