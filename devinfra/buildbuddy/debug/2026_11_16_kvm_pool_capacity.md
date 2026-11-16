# KVM pool capacity, 16 November 2026

The pool can grow from three to four workers without required host anti-affinity.
Each worker reserves 2 CPU instead of 4, retaining four guest vCPUs, a 4-CPU limit,
8 GiB RAM, an 80 GiB local SSD work disk, and one BuildBuddy action at a time.
At the new ceiling, VM requests total 8 CPU and 32 GiB guest RAM, plus KubeVirt
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

The inspected source base was `ce5a9a6fc5`. All six HIL nodes were Ready with no
memory or disk pressure. The SSD storage class uses one local copy and
`WaitForFirstConsumer`; SSD pools are on three HIL hosts. At inspection:

| Host          | Unreserved CPU | Unreserved RAM, GiB | SSD free, GiB |
| ------------- | -------------: | ------------------: | ------------: |
| ovh-ns1001419 |           5.54 |               42.58 |         349.1 |
| ovh-ns104952  |           2.46 |               46.16 |         312.9 |
| ovh-ns104963  |           1.80 |               44.12 |         325.2 |

Unreserved values are allocatable less active pod requests, including the three
existing workers. SSD values are LINSTOR thin-pool free capacity, not reserved
space or an I/O performance guarantee. Other workloads can consume this headroom.
Only HIL placement and hardware virtualization eligibility remain required.

This is a capacity and reservation change, not a measured CI speedup. Co-located
busy workers still need comparison for queue time, execution time, CPU steal,
SSD latency, registration and graceful draining under concurrent load.
