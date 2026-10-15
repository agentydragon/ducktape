# Rugged: partial Nebula connectivity recovered after network transitions

**Date:** 2026-10-14 (times below are America/Los_Angeles, UTC−07:00).
**Status:** connectivity recovered; root cause unresolved. Reliability investigation
is deferred. Restore inference routes for current use, without treating recovery as
proof of reliable roaming. Inference configuration and results belong in
[the inference report](../docs/inference/rugged_local_llm.md).

## Failure and scope

Rugged (`10.42.0.30`) remained Kubernetes Ready, with fresh leases and established
API connections to `ovh-ns1001419` (`10.42.0.19`). It was not disconnected from
Nebula altogether. Connections to other peers failed:

| Source node   | Nebula IP  | Host-network HTTP probe to rugged port 18080 |
| ------------- | ---------- | -------------------------------------------- |
| ovh-ns1001419 | 10.42.0.19 | HTTP 401, expected without bearer token      |
| ovh-ns104952  | 10.42.0.16 | Timeout                                      |
| ovh-ns104963  | 10.42.0.17 | Timeout                                      |

The same request from a LiteLLM pod on `.16` timed out. Cilium recorded forwarded
SYNs and SNAT to `.16`, with no replies. Failure from the source host network too
made a pod-only policy or SNAT explanation insufficient. The inference proxy was
listening, and local authentication checks had passed. Later LiteLLM placement
included replicas on `.19` and `.16`; do not assume one fixed source node.

## Packet evidence

- Rugged used Wi-Fi `wlp0s20f3`, address `10.0.0.66`, gateway `10.0.0.1`
  (Xfinity), to reach `.16`'s public endpoint `147.135.104.5:4242`.
- A peer-side capture around 18:18:36–18:18:40 showed 261-byte UDP requests
  arriving from rugged's NAT endpoint and 319-byte replies leaving toward it.
  Peer logs identified rugged's certificate and recorded stage-2 handshake replies.
- The user's rugged-side capture at 18:22:50–18:23:04 showed 21 outbound
  261-byte UDP packets to that endpoint, **no inbound packets**, and zero kernel
  capture drops. The capture used `-i any`; its promiscuous-mode warning was harmless.
- An API probe to `https://10.42.0.16:6443/livez` timed out after five seconds.

The captures were not simultaneous: they establish a repeated symptom, not an
end-to-end match of individual packets. They point to return-path loss before
rugged's capture point, but do not identify the dropping device. No router WAN/LAN
capture was available. A permanent Xfinity block was not established.

## Recovery sequence and confounder

1. The user disabled Wi-Fi. At 18:43:13 the route to `147.135.104.5` used
   cellular `wwan0`, source `100.86.200.98`, gateway `100.86.200.97`.
2. At 18:43:52 the API probe reached TLS and failed certificate verification
   (`curl: (60)`, cluster CA not trusted by plain curl). That proves transport
   reachability, not an authenticated API health result; TLS verification was not bypassed.
3. The user re-enabled Wi-Fi. The route initially still used cellular, then
   switched to Wi-Fi by 18:44:33. The probe continued reaching TLS.
4. The user disabled cellular. At 18:51:29–18:51:34 the Wi-Fi-only probe still
   reached TLS. Recovery did not depend on continued cellular connectivity.
5. A subsequent host-network probe from `.16` to rugged's port 18080 returned
   HTTP 401, confirming recovery in the original reverse direction too. This was
   a reachability check, not a completion through LiteLLM.

**The transitions also restarted Nebula.** The journal records
`nebula-underlay-route-refresh.service` detecting default-route changes and
restarting the Nebula worker stack at **18:29:44** and **18:44:32–18:44:34**.
Therefore this was not a controlled comparison changing only the uplink. Recovery
could involve NAT refresh, fresh Nebula state, or both; it cannot be attributed
solely to cellular bypassing Xfinity.

The existing dispatcher and earlier incident are documented in
[the roaming investigation](2026-05-18-rugged-nebula-roaming.md). Similar symptoms
are not proof that this recurrence had the same stale-endpoint mechanism.

## Why relay fallback was not a universal escape hatch

Rugged's live configuration enabled hole punching and relay use, advertising the
six lighthouse addresses as relays. The reachable `.19` peer logged successful
CreateRelayRequest/CreateRelayResponse exchanges between rugged and ordinary
clients such as wyrm2 and optiplex. Relay support was not simply absent.

The failing OVH nodes also serve as lighthouses and relays: `.16`'s live extension
configuration had `am_lighthouse: true` and `am_relay: true` (Nebula 1.11.0;
rugged ran 1.10.3). Nebula's
[relay configuration](https://nebula.defined.net/docs/config/relay/) disallows
advertising another relay for reaching a relay node. The
[1.11.0 discovery code](https://github.com/slackhq/nebula/blob/v1.11.0/lighthouse.go#L469-L474)
also skips peer queries when the local host is a lighthouse or the target is a
configured lighthouse. Thus reachable `.19` does not guarantee fallback for every
other pair. This explains a limitation, not the original packet loss.

These OVH nodes have public IPs; their lighthouse role is intentional and does
not itself justify a topology redesign. No topology or router change was made.

## If it recurs

Before restarting Nebula or changing uplinks, preserve routes, service PID/start
time, route-refresh journal, and handshake logs. Take synchronized captures on
rugged and the failing peer; if possible capture the router WAN and LAN too.
Compare a working peer with the failing one. Isolate a Nebula restart from an
uplink transition rather than changing both together. Keep the original failed
and recovered directions distinct, and do not equate Ready with full-mesh health.

The host capture used:

```bash
timeout --signal=INT 20s tcpdump -i any -nn -s 0 \
  -w /tmp/rugged-nebula-handshake.pcap \
  'host 147.135.104.5 and udp port 4242'
```

Only this summary is committed; raw packet captures and host logs remain local.
