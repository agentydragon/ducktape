# A Pod's handshake with its own node's Gateway listener never completes

Open. Observed 2026-09-25 from the staging egress proxy's Pods on `ovh-ns104952` and
`ovh-ns1001419`, Cilium 1.19.6.

## Symptom

A Pod's TCP connection to its own node on `:443`, whether to the node's public `eno1` address or
its Nebula address, gets as far as SYN and SYN-ACK. Then the Gateway listener keeps retransmitting
its SYN-ACK: the Pod's ACK and ClientHello never reach the listener's request socket. The Pod
treats the connection as established and waits for a ServerHello until its own timeout. Dials to
other nodes' addresses, and to the Gateway Service's ClusterIP, complete.

It surfaced through the egress proxy. For each host, the proxy pins the first IPv4 address of the
lookup for 30 s, and every public `*.allegedly.works` name resolves to every node
(<../../tf/gitops/dns-records/main.tf>). A pin that lands on the proxy's own node therefore stalls
every new connection to that host from that Pod until the pin expires. From a claude-ai box,
`//agentplane/acceptance:test_egress` failed twice and errored once in teardown, each time on a
30 s `httpx.ReadTimeout` against `agentplane-testing.allegedly.works`.

## Evidence

The staging egress proxy logs `server connect <host>:443 (<address>)` for each dial, and
`Server TLS handshake failed` when the client gives up first. The counts below are dials to
`agentplane-testing.allegedly.works` in those logs: four hours up to 01:15 UTC, which include the
failing `test_egress` run, and a 7-minute probe that made a fresh request every 10 s from a box.
Every stall the probe's client saw is one of the failures counted.

| Egress proxy Pod                    | Node, public address           | Own-node dials | Hung | Other dials | Hung |
| ----------------------------------- | ------------------------------ | -------------- | ---- | ----------- | ---- |
| `agentplane-egress-697cc6457-bzthr` | `ovh-ns1001419`, 51.81.245.180 | 7              | 7    | 32          | 0    |
| `agentplane-egress-697cc6457-n2rlk` | `ovh-ns104952`, 147.135.104.5  | 4              | 4    | 32          | 0    |

`cilium-dbg monitor -v --related-to 173` (the `n2rlk` endpoint) during one own-node attempt:

```text
Policy verdict log: ... egress, action allow, match L3-L4, 10.244.0.143:33210 -> 147.135.104.5:443 tcp SYN
-> stack    ... state new         10.244.0.143:33210 -> 147.135.104.5:443 tcp SYN
-> endpoint ... state reply       147.135.104.5:443 -> 10.244.0.143:33210 tcp SYN, ACK
-> stack    ... state established 10.244.0.143:33210 -> 147.135.104.5:443 tcp ACK
-> stack    ... state established 10.244.0.143:33210 -> 147.135.104.5:443 tcp ACK
-> endpoint ... state reply       147.135.104.5:443 -> 10.244.0.143:33210 tcp SYN, ACK
-> stack    ... state established 10.244.0.143:33210 -> 147.135.104.5:443 tcp ACK
-> endpoint ... state reply       147.135.104.5:443 -> 10.244.0.143:33210 tcp SYN, ACK
```

The Nebula address (`10.42.0.16:443`) traces the same way. The Gateway Service
(`10.106.122.5:443`) goes to the L7 load balancer (`-> proxy port 80`), completes, and closes
with the client's FIN.

Node state on `ovh-ns104952`, from its cilium agent:

- `cilium-dbg status`: `KubeProxyReplacement: True [eno1 147.135.104.5, nebula1 10.42.0.16
(Direct Routing)]`, `Routing: Network: Tunnel [vxlan] Host: Legacy`,
  `Masquerading: IPTables [IPv4: Enabled]`, `Attach Mode: TCX`, `Host firewall: Disabled`.
- `cilium-dbg config`: `BPFSocketLBHostnsOnly: false`, `EnableSocketLBPeer: true`,
  `EnableSocketLBPodConnectionTermination: true`, `EnableHostLegacyRouting: true`,
  `EnableBPFMasquerade: false`.
- `ss -lntpe 'sport = :443'`: five `0.0.0.0:443` Gateway listeners (cilium-envoy, host network),
  each `fwmark:0xb00`.
- `ip rule`: `9: from all fwmark 0x200/0xf00 lookup 2004`, where table 2004 is
  `local default dev lo`; there is no table 2005.
- `CILIUM_POST_nat`, in order: masquerade of `10.244.0.0/24` to non-Pod destinations not leaving
  via `cilium_+`, the proxy-return exclusion (`0xa00/0xe00`), host-to-cluster SNAT to
  `10.244.0.12`, and the "hairpin traffic that originated from a local pod" SNAT.
- The egress proxy's rule toward `world`, `remote-node` and `host` on 443 and 80 is L3/L4 only.
  The flow above is `-> stack`, never `-> proxy`.

## How this differs from the 2026-09-11 RCA

[The earlier local Gateway RCA](agentplane_oidc/local_gateway_tls_rca.md) was a reset at 2.0 s.
It needed an L7 (SNI) egress rule redirecting the connection through the policy proxy with source
preservation, and `envoy.useOriginalSourceAddress: false` (#6182) fixed it. This is a hang, with
no L7 redirect. That RCA's own control row passed 3/3 on 2026-09-11: an egress proxy Pod on
`ovh-ns103656` dialling its own node under the same L4-only rule. Both nodes reproducing now are
control-plane nodes; `ovh-ns1001419` joined the cluster six days before this was found. The RCA's
rows were all taken on workers.

## Open

- Where the Pod's ACK is lost between `-> stack` and the listener's request socket. Next: the
  host's socket and conntrack state and TCP counters, sampled during a probe run from the Pod's
  network namespace.
- Whether worker nodes still behave as the RCA's control did, and if so, what the control-plane
  nodes do differently.

## Mitigations

- #7911 routes claude-ai boxes' acceptance traffic to the testing app's Service.
- Nothing yet covers other in-cluster clients of public `*.allegedly.works` names through the
  egress proxy.
