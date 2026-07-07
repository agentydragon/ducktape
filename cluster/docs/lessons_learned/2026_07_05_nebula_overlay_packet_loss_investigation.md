# OVH inter-node packet loss / low single-stream throughput (2026-07-05, updated 2026-07-07)

**Status: explained by OVH class limits plus underlay asymmetry, not Nebula-primary.**
Tracking issue: agentydragon/ducktape#2917.
The live follow-up is in <../../../debug/nebula_inter_node_perf/>.

## Symptom

Single-stream transfers over the pod network between OVH nodes are slow and lossy.
Surfaced during the OVH data-disk mount rename (`plans/ovh_storage_tiering.md`): SeaweedFS
`volume.move` between KS-5 nodes ran at **~32 MB/s (~256 Mbps)** and `volumeServer.evacuate`
kept aborting on transient gRPC errors mid-move (had to wrap it in a retry loop).

iperf3 pod-to-pod (the same path — `pod → Cilium VXLAN → nebula1 → eno1`):

| Path                                   | Throughput            | TCP retransmits          |
| -------------------------------------- | --------------------- | ------------------------ |
| KS-GAME↔KS-GAME (104952↔104963)        | 730 / 205 Mbps (asym) | 1.3k / 8.4k              |
| KS-5↔KS-5 (102453↔103711), post-tuning | ~450 Mbps             | ~10.8k / 5s (~5-6% loss) |

The first pass suspected overlay loss because the symptom was visible on pod-to-pod traffic.
The 2026-07-07 follow-up separated the direct public host path from Nebula and pod overlay;
the public path is already slow, lossy, and asymmetric.

## 2026-07-07 follow-up: public underlay is already near class limits

The reduced evidence report is in <../../../debug/nebula_inter_node_perf/>.

The authenticated OVH API reports these live network specs:

- KS-5 nodes (`102453`, `103656`, `103711`): `OvhToOvh=500 Mbps`,
  `OvhToInternet=500 Mbps`, `InternetToOvh=500 Mbps`, `connection=1000 Mbps`,
  no vRack bandwidth.
- KS-GAME nodes (`104952`, `104963`): `OvhToOvh=300 Mbps`,
  `OvhToInternet=300 Mbps`, `InternetToOvh=300 Mbps`, `connection=1000 Mbps`,
  no vRack bandwidth.

So same-DC OVH traffic on these Eco/Kimsufi nodes is explicitly not a private 1 Gbps path.

| Pair / topology                         | Public host path              | Nebula host path              | Pod overlay                   |
| --------------------------------------- | ----------------------------- | ----------------------------- | ----------------------------- |
| KS-5 `102453 -> 103711`                 | 489.8 Mbps, 36.1k retransmits | 468.8 Mbps, 33.2k retransmits | 453.4 Mbps, 29.8k retransmits |
| KS-5 `103711 -> 102453`                 | 430.5 Mbps, 28.8k retransmits | 432.3 Mbps, 26.6k retransmits | 430.6 Mbps, 24.8k retransmits |
| KS-GAME `104963 -> 104952`              | 270.9 Mbps, 23.3k retransmits | 261.4 Mbps, 16.4k retransmits | 255.5 Mbps, 17.0k retransmits |
| KS-GAME `104952 -> 104963`              | 848.7 Mbps, 90 retransmits    | 803.8 Mbps, 1.7k retransmits  | 755.7 Mbps, 1.2k retransmits  |
| Bench source `103656 -> 104952`         | 492.3 Mbps, 26.4k retransmits | 390.2 Mbps, 11.6k retransmits | 439.6 Mbps, 17.9k retransmits |
| Bench source reverse `104952 -> 103656` | 842.3 Mbps, 210 retransmits   | 764.6 Mbps, 231 retransmits   | 696.7 Mbps, 1.3k retransmits  |

UDP tests also lose packets on the direct public path. Example: `102453 -> 103711` lost
1.35% at 300 Mbps, 1.51% at 450 Mbps, and 24.03% at 650 Mbps on public `eno1`;
Nebula was close behind at 1.46%, 2.48%, and 27.65%. Repeating with 1200-byte UDP
payloads did not remove loss, so this is not an MTU artifact.

Focused receiver-NIC counter probes did not move `104952`'s local RX drop counters during
slow inbound public transfers, so the loss/shape is upstream of the destination host NIC.

Conclusion: Nebula has overhead, but the primary ceiling and asymmetry are already present on
the routed OVH public path and line up with the purchased service classes. Do not ship another
Nebula tuning knob for this symptom without new evidence.

## What was ruled OUT

- **UDP socket buffer size** (the first hypothesis, #2926: `listen.read_buffer`/`write_buffer`
  = 10 MiB, `routines = 2`). Applied to `103711` + `102453`, re-measured: **no improvement**,
  retransmits unchanged. Disproven because:
  - `net.core.rmem_max = 4 MiB` caps the request to 4 MiB anyway, and
  - at same-DC sub-ms RTT the bandwidth-delay product is ~30-60 KB, so **4 MiB is already
    ~60× more than the link can use.** The socket buffer was never the bottleneck.
  - `routines = 2` only helps aggregate multi-flow throughput; a single flow hashes to one
    routine, so it does nothing for the single-stream case.
  - #2926 is left in place (harmless) but is **not** a fix.

**Process lesson:** measure the hypothesis before shipping the "fix". The buffer change was
merged on plausibility, then measurement disproved it. Diagnose with real data first.

## Next

- Escalate the public-path asymmetry to OVH only if the below-nominal loss needs explanation;
  the overall ceiling is close to live service specs.
- Treat storage benchmarks across `local-path-ovh-{hdd,ssd}` as topology-sensitive: a client
  on `103656` can hit an HDD volume server locally, but SSD volume servers are only on
  `104952`/`104963`, so SSD results include remote inter-node network cost.
- Separately investigate `103656` background `rx_missed_errors` growth. It is real host-health
  noise, but this run did not tie it to the slow `104952` inbound path.
