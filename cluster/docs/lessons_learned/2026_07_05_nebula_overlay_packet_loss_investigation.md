# Nebula overlay packet loss / low single-stream throughput (2026-07-05, OPEN)

**Status: unresolved investigation.** Tracking issue: agentydragon/ducktape#2917.

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

Not a hard bandwidth cap (hit 730). Not MTU (pod MTU ~1370, TCP clamps; DF-ping confirms
small packets pass). It's **~5-6% packet loss on the overlay**, which collapses single-stream
TCP and makes single-stream gRPC (SeaweedFS moves) flaky.

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

## Next (measure the loss source before more config)

- UDP-mode iperf3 (`-u`) to quantify one-way loss without TCP retransmits confounding it.
- `nebula1` tun-device drop counters — `tun.tx_queue` overflow is a _separate_ drop path from
  the socket buffer (and `tun.tx_queue` is currently unset/default).
- `/proc/net/snmp` UDP `InErrors`/`RcvbufErrors` on both ends.
- Rule out genuine OVH-underlay loss between these hosts (nebula-off `eno1`-direct iperf), and
  whether the path is direct-punched vs relayed (all OVH nodes are lighthouse+relay+use_relays).
