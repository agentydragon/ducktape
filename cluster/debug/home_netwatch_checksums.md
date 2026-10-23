# Home Netwatch checksum investigation — 2026-10-23

The switch-originated Netwatch loss graph is not a reliable measurement of LAN
packet delivery. At OptiPlex, malformed ICMP echo requests explain the observed
Netwatch loss exactly. Concurrency changes the fraction of malformed requests.
The separate host-to-gateway echo deficit was also observed in a packet capture;
this investigation does not establish whether that deficit is gateway ICMP
handling or an underlying connectivity fault.

## Controlled comparison

On CRS310 RouterOS 7.24.5, temporarily toggle only the two managed Netwatch
probes' `disabled` fields. Keep 200 packets, 150 ms spacing, one-minute interval,
and all thresholds unchanged. Run ten completed batches gateway-only, ten
OptiPlex-only, and ten with both enabled. Leave the Kubernetes host probes running.
Read completed counters directly from `/tool/netwatch` every five seconds.
Disabling/re-enabling clears probe counters; the experiment window must not be
interpreted as normal production failure frequency.

- Gateway-only, 05:51:39–06:01:17 UTC: ten batches, reported loss 54.5–56.5%.
- OptiPlex-only, 06:01:17–06:10:54 UTC: ten batches, each reported loss 50%.
  Every batch contained 200 requests at the receiver: 100 valid ICMP checksums,
  all answered, and 100 invalid ICMP checksums, all unanswered. Odd sequences
  were valid; even sequences were invalid.
- Both enabled, 06:10:54–06:20:32 UTC, ten batches each: completed OptiPlex batches' reported loss
  continued to match malformed requests exactly. The first two batches had
  200 valid requests and zero loss, alongside 99.5% and 100% reported gateway
  loss. Batch three had 128 malformed requests and 64% reported OptiPlex loss.
  Batch five had 89 malformed requests and 44.5% reported loss; batch seven
  had 45 malformed requests and 22.5% reported loss.

## Packet evidence

Capture OptiPlex's physical `eno1` with Talos's packet-capture API, directly
addressing endpoint/node `10.42.0.18`. Filter `icmp and host 192.168.1.100`.
Captured frames have source switch MAC `38:32:7a:34:e5:8b`, destination OptiPlex
MAC `e4:54:e8:85:9f:b2`, IPv4 source `192.168.1.100`, destination `192.168.1.10`,
and IP packet length 84 bytes. IPv4 header checksums are valid.

In the first OptiPlex-only train, request 1 stores ICMP checksum `0x8cb6`,
which is correct. Request 2 stores `0x8d09`, but its bytes require `0x8cb5`.
Request 3 stores the correct `0x8cb4`. Request 4 stores `0x8d07`, but requires
`0x8cb3`. Both tshark and an independent one's-complement checksum calculation
over the captured ICMP bytes agree.

The capture omits Ethernet FCS. Do not claim it was directly checked. OptiPlex's
NIC counters `rx_crc_errors`, `rx_frame_errors`, and `rx_errors` were all zero.
An Ethernet NIC can transmit a frame with a valid FCS around an incorrectly
constructed ICMP packet. Capture at the receiver does not prove the exact code
location where the malformed ICMP checksum originated; RouterOS's Netwatch
packet construction/shared state is the leading hypothesis, not a source-code
finding.

A separate two-minute capture on `eno1`, filtered to ICMP and gateway
`192.168.1.254`, recorded 599 valid host echo requests and 468 valid replies
(21.87% deficit). Missing replies recurred approximately once per second. This
sample supports the host probe counters, but does not distinguish loss from
intentional gateway ICMP policing. During an overlapping sample the ordinary
host-to-switch control had 3,920 requests and 3,920 replies, all valid.

## Artifacts

Local evidence is retained under `/tmp/home-netwatch-isolation/`:

- `results.jsonl`: phase transitions and raw completed-batch counters.
- `optiplex.pcap`: receiver-side switch ICMP capture, including host control pings.
- `host-gateway.pcap`: independent host/gateway capture.
- `check_checksums.py`: independent pcap/IPv4/ICMP checksum and reply matcher.
- `analyze_capture.py`: tshark-based request/reply grouping.
- `checksum-batches.jsonl`: checksum/reply counts per captured train.

The experiment runner uses the existing Tofu identity without printing its
credential and verifies the switch TLS certificate against the cluster CA.
Its cleanup restores the two original `disabled=false` values. It changes no
firewall, forwarding, management-service, or persistent repository settings.

## Completion

All three phases completed. Across 20 OptiPlex batches, all 4,000 requests
were captured; each batch independently matched the exported reply/loss counts.
Every valid request received a reply, and every invalid request went unanswered.
Both probes were restored and verified enabled with 150 ms spacing at
06:20:32 UTC. No Terraform approval or persistent network setting changed.
