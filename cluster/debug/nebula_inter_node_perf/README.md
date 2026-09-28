# Nebula inter-node performance investigation (2026-07-07)

## Conclusion

Nebula is not the primary throttle for the OVH same-DC paths measured here.
The live OVH API reports `OvhToOvh` bandwidth limits of **500 Mbps** for the
KS-5 nodes and **300 Mbps** for the KS-GAME nodes, with 1 Gbps physical
connection speed and no vRack bandwidth. The direct public `eno1` path is
already near those class limits; Nebula and Cilium mostly inherit that ceiling
plus overlay overhead.

This explains why the SQLite/SeaweedFS SSD result looked suspicious: the bench
client pods were on HDD node `ovh-ns103656`, while the SSD SeaweedFS volume
servers live on `ovh-ns104952` and `ovh-ns104963`. The SSD class therefore
forced inter-node OVH/Nebula traffic, while the HDD class could hit a local
HDD volume server on `ovh-ns103656`.

This report records the reduced evidence; raw command dumps are intentionally
omitted from the PR.

## OVH Limits

The public Kimsufi catalog at
<https://eco.us.ovhcloud.com/?display=list&range=kimsufi> lists:

- `KS-5 | Intel Xeon-E3 1270 v6`: no private bandwidth; public bandwidth
  300-500 Mbps depending on variant.
- `KS-GAME | Intel Core i7-7700K`: no private bandwidth; public bandwidth
  300 Mbps.

The authenticated live API query
(`/dedicated/server/{serviceName}/specifications/network`) is more precise for
the purchased services:

| Node           | Commercial range | `OvhToOvh` | `OvhToInternet` | `InternetToOvh` | Connection | vRack |
| -------------- | ---------------- | ---------: | --------------: | --------------: | ---------: | ----- |
| `ovh-ns102453` | KS-5             |   500 Mbps |        500 Mbps |        500 Mbps |  1000 Mbps | none  |
| `ovh-ns103656` | KS-5             |   500 Mbps |        500 Mbps |        500 Mbps |  1000 Mbps | none  |
| `ovh-ns103711` | KS-5             |   500 Mbps |        500 Mbps |        500 Mbps |  1000 Mbps | none  |
| `ovh-ns104952` | KS-GAME          |   300 Mbps |        300 Mbps |        300 Mbps |  1000 Mbps | none  |
| `ovh-ns104963` | KS-GAME          |   300 Mbps |        300 Mbps |        300 Mbps |  1000 Mbps | none  |

The OVH API distinguishes `OvhToOvh`, `OvhToInternet`, and `InternetToOvh`;
for these services same-provider traffic is explicitly not an unmetered/private
1 Gbps path.

## Method

Temporary `nicolaka/netshoot:v0.13` pods were pinned in namespace
`nebula-perf-debug`:

- `hostNetwork` client/server pods measured the public underlay (`eno1`) and
  Nebula host route (`10.42.0.0/16 dev nebula1`).
- normal pod client/server pairs measured the pod overlay path
  (`pod -> Cilium VXLAN -> nebula1 -> eno1`).
- `iperf3 -J` captured TCP throughput/retransmits and UDP loss.
- host-network diagnostics captured `ip route`, `tracepath`, `ethtool -S`,
  `ip -s link`, `tc -s qdisc`, and selected Talos `/proc`/`/sys` counters.

## Key Measurements

### KS-5 pair: `ovh-ns102453 <-> ovh-ns103711`

| Path             | Direction          | TCP receive throughput | Retransmits |
| ---------------- | ------------------ | ---------------------: | ----------: |
| public host path | `102453 -> 103711` |             489.8 Mbps |      36,102 |
| public host path | `103711 -> 102453` |             430.5 Mbps |      28,833 |
| Nebula host path | `102453 -> 103711` |             468.8 Mbps |      33,165 |
| Nebula host path | `103711 -> 102453` |             432.3 Mbps |      26,559 |
| pod overlay      | `102453 -> 103711` |             453.4 Mbps |      29,837 |
| pod overlay      | `103711 -> 102453` |             430.6 Mbps |      24,779 |

Interpretation: the public path is already at the same rough ceiling as
Nebula/pod overlay. This is not a Nebula-specific cap.

UDP loss on the same public path:

| Target rate | Public loss | Nebula loss |
| ----------: | ----------: | ----------: |
|    300 Mbps |       1.35% |       1.46% |
|    450 Mbps |       1.51% |       2.48% |
|    650 Mbps |      24.03% |      27.65% |

Repeating with 1200-byte UDP payloads did not remove loss, so this is not a
simple MTU/fragmentation artifact.

### SSD pair: `ovh-ns104963 <-> ovh-ns104952`

| Path             | Direction          | TCP receive throughput | Retransmits |
| ---------------- | ------------------ | ---------------------: | ----------: |
| public host path | `104963 -> 104952` |             270.9 Mbps |      23,276 |
| public host path | `104952 -> 104963` |             848.7 Mbps |          90 |
| Nebula host path | `104963 -> 104952` |             261.4 Mbps |      16,426 |
| Nebula host path | `104952 -> 104963` |             803.8 Mbps |       1,714 |
| pod overlay      | `104963 -> 104952` |             255.5 Mbps |      17,026 |
| pod overlay      | `104952 -> 104963` |             755.7 Mbps |       1,159 |

Interpretation: the slow direction is close to the live 300 Mbps `OvhToOvh`
limit before Nebula enters the stack. The high reverse direction shows the OVH
limit is not a simple hard bidirectional shaper over short tests, but the
published/live included bandwidth is still 300 Mbps for this class.

UDP with 1200-byte payloads:

| Target rate | Public loss | Nebula loss |
| ----------: | ----------: | ----------: |
|    300 Mbps |       7.73% |      12.75% |
|    650 Mbps |      56.15% |      58.88% |

### Benchmark-topology path: `ovh-ns103656 <-> ovh-ns104952`

This approximates "client on HDD node, SSD volume server remote":

| Path             | Direction          | TCP receive throughput | Retransmits |
| ---------------- | ------------------ | ---------------------: | ----------: |
| public host path | `103656 -> 104952` |             492.3 Mbps |      26,398 |
| public host path | `104952 -> 103656` |             842.3 Mbps |         210 |
| Nebula host path | `103656 -> 104952` |             390.2 Mbps |      11,644 |
| Nebula host path | `104952 -> 103656` |             764.6 Mbps |         231 |
| pod overlay      | `103656 -> 104952` |             439.6 Mbps |      17,887 |
| pod overlay      | `104952 -> 103656` |             696.7 Mbps |       1,324 |

Interpretation: the benchmark's SSD tier was testing a cross-class inter-node
path where the destination KS-GAME service has 300 Mbps `OvhToOvh` included
bandwidth, not just faster media.

## Cap Sweep

Additional UDP sweeps tested rates around the live OVH limits with 1200-byte
payloads:

| Path                              | Target payload rate | Observed payload rate |   Loss |
| --------------------------------- | ------------------: | --------------------: | -----: |
| KS-GAME `104963 -> 104952` public |            250 Mbps |            250.0 Mbps |  7.17% |
| KS-GAME `104963 -> 104952` public |            280 Mbps |            280.0 Mbps | 13.41% |
| KS-GAME `104963 -> 104952` public |            300 Mbps |            300.0 Mbps | 16.72% |
| KS-GAME `104963 -> 104952` public |            330 Mbps |            330.0 Mbps | 22.77% |
| KS-GAME `104952 -> 104963` public |            280 Mbps |            280.0 Mbps |  2.22% |
| KS-GAME `104952 -> 104963` public |            300 Mbps |            300.0 Mbps |  1.76% |
| KS-GAME `104952 -> 104963` public |            650 Mbps |            551.1 Mbps |  4.20% |
| KS-GAME `104952 -> 104963` public |            850 Mbps |            575.8 Mbps |  5.16% |
| KS-5 `102453 -> 103711` public    |            430 Mbps |            430.0 Mbps |  7.31% |
| KS-5 `102453 -> 103711` public    |            470 Mbps |            469.5 Mbps |  9.19% |
| KS-5 `102453 -> 103711` public    |            500 Mbps |            499.9 Mbps | 10.35% |
| KS-5 `102453 -> 103711` public    |            540 Mbps |            539.9 Mbps | 16.33% |

This is consistent with being near class limits, not with a hidden 1 Gbps
same-DC path that Nebula is mysteriously failing to use. The packet loss below
the nominal payload rate is also plausible once wire overhead and concurrent
cluster traffic share the same included bandwidth.

## Route Evidence

The host routes are what the network model predicts:

- public `102453 -> 103711`: `147.135.39.176 via 147.135.37.254 dev eno1`
- Nebula `102453 -> 103711`: `10.42.0.14 dev nebula1 src 10.42.0.15 mtu 1420`
- pod `102453 -> 103711`: `10.244.2.123 via 10.244.4.35 dev eth0 mtu 1370`

Same-`/24` OVH peers still hairpin through OVH gateway/router infrastructure:

- `104963 -> 104952`: `147.135.104.5 via 147.135.104.254 dev eno1`
- `tracepath` shows PMTU 1500 and a 2-hop same-`/24` public path.

Cross-`/24` public traffic traverses several OVH internal hops with sub-ms RTT
and PMTU 1500.

## Counter Evidence

The slow public transfers into `ovh-ns104952` did not move local receiver NIC
drop counters:

- During `103656 -> 104952` public TCP, `104952` stayed at
  `rx_missed_errors=6258`, `rx_fifo_errors=6258`, `rx_no_buffer_count=421`,
  `rx_queue_*_drops=0`.
- During `104963 -> 104952` public TCP, the probe again did not attribute the
  retransmits to `104952` receiver queue drops.

That pushes the packet loss/shape upstream of the destination host NIC. The
`103656` NIC does have background `rx_missed_errors` growth, but it also grew
during tests where `103656` was not on the measured path, so it is a separate
host-health issue rather than the root cause of the `104952` inbound slowdown.

## Working Hypotheses

Most likely:

- Expected OVH Kimsufi/Eco included `OvhToOvh` limits: 500 Mbps for KS-5 and
  300 Mbps for KS-GAME.
- Soft/asymmetric OVH enforcement or path behavior around those limits,
  especially for traffic into `ovh-ns104952`.
- Overlay overhead and background cluster traffic consuming part of the same
  included public bandwidth.

Less likely after this run:

- Nebula `listen.read_buffer` / `write_buffer`: previously disproven, and the
  public path is already similarly lossy.
- Nebula `routines`: not relevant to a single TCP flow and does not explain
  public-path loss.
- MTU: route MTUs are 1500/1420/1370 as expected, and 1200-byte UDP payloads
  still lost packets.
- Receiver NIC overrun on `104952`: focused before/after `ethtool -S` snapshots
  did not move local RX drop counters during slow inbound tests.

## Next Actions

Do not ship another Nebula tuning knob without evidence. The useful next moves
are outside Nebula:

1. Open an OVH support/escalation ticket with the public-path iperf evidence,
   only if we need OVH to explain the asymmetry/loss below nominal payload
   rate. The overall throughput ceiling itself is close to the live service
   specs.
2. If same-DC storage traffic must be near line rate, move this workload to
   higher-bandwidth or vRack-capable hardware, or add topology awareness so
   storage benchmarks and workloads do not compare local HDD to remote SSD.
3. Separately investigate `ovh-ns103656` background `rx_missed_errors` growth;
   it is real host-health noise, but this run did not tie it to the bad SSD
   path.
