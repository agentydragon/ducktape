# A Pod's handshake with its own node's Gateway listener never completes

Cause identified from source and host state; probes E and F, which test it directly, are pending.
Observed 2026-09-25 from the staging egress proxy's Pods on the control-plane nodes
`ovh-ns104952` and `ovh-ns1001419`. Every Talos node runs Talos v1.14.0 (kernel 6.18.48) and
Cilium 1.19.6.

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

## Cause

Three behaviours combine; the third arrived with Talos v1.14.0.

1. **Cilium keeps the listener's SYN-ACK out of conntrack.** The Gateway's host-network Envoy
   listeners carry `SO_MARK` `0xb00`. Cilium's rule for proxy return traffic,
   `-A CILIUM_OUTPUT_raw -o cilium_host -m mark --mark 0xa00/0xfffffeff -j CT --notrack`
   ([`installStaticProxyRules`](https://github.com/cilium/cilium/blob/v1.19.6/pkg/datapath/iptables/iptables.go)),
   matches that mark on the way to a local Pod. The entry the Pod's SYN created stays
   `SYN_SENT [UNREPLIED]`.
2. **Conntrack classifies every later Pod segment as invalid.** An original-direction ACK on a
   `SYN_SENT` entry is `sIV`, "ACK is invalid: we haven't seen a SYN/ACK yet"
   ([`nf_conntrack_proto_tcp.c`](https://github.com/torvalds/linux/blob/v6.18/net/netfilter/nf_conntrack_proto_tcp.c)).
   The state table decides this before any window check, so `nf_conntrack_tcp_be_liberal` does
   not change it.
3. **Talos v1.14.0 drops `ct state invalid` in default-accept mode too**
   ([`78efbb413`](https://github.com/siderolabs/talos/commit/78efbb413863a902319c2f0b3668e310280cc7e3),
   "install conntrack handler in accept ingress firewall mode"); v1.13 dropped it only with
   `ingress: block`. The `ingress` chain on the input hook exists only on a node whose machine
   config has a `NetworkRuleConfig`. Ours has one only on control-plane nodes:
   `control_plane_metrics_firewall_config` (<../terraform/main/infrastructure.tf>), which guards
   10257/10259.

The ACK and ClientHello therefore die in netfilter before TCP sees them, on control-plane nodes
only. On a worker, the invalid ACK passes and the handshake completes. The Gateway Service path
never enters conntrack in either direction: the to-proxy mark `0x200` is exempt in
`CILIUM_PRE_raw`, and the replies in `CILIUM_OUTPUT_raw`. Its packets are untracked rather than
invalid. A host-network client's SYN-ACK leaves through `lo`, which the NOTRACK rule does not
match.

The upgrade to v1.14.0 started 2026-09-19 (#7269, #7404, #7424). The
[2026-09-11 local Gateway RCA](agentplane_oidc/local_gateway_tls_rca.md) predates it; its
own-node control passed 3/3 on a worker. That RCA's failure was a different mechanism: a reset
at 2.0 s from the L7 policy proxy's 5-tuple collision, fixed by
`envoy.useOriginalSourceAddress: false` (#6182).

## Evidence

The staging egress proxy logs `server connect <host>:443 (<address>)` for each dial, and
`Server TLS handshake failed` when the client gives up first. The counts below are dials to
`agentplane-testing.allegedly.works` in those logs. They cover the four hours up to 01:15 UTC,
which include the failing `test_egress` run, and a 7-minute probe that made a fresh request
every 10 s from a box. Every stall the probe's client saw is one of the failures counted.

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

Host sockets and conntrack on `ovh-ns104952` during another attempt from `n2rlk`, sampled every
250 ms:

```text
SYN-RECV  0 0  147.135.104.5:443  10.244.0.143:54718  timer:(on,824ms,0) ... fwmark:0xb00
tcp 6 119 SYN_SENT src=10.244.0.143 dst=147.135.104.5 sport=54718 dport=443 [UNREPLIED] src=147.135.104.5 dst=10.244.0.143 sport=443 dport=54718
```

Over 3.6 s, the request socket's SYN-ACK retry count rose from 0 to 2, and the conntrack entry
stayed `SYN_SENT [UNREPLIED]`. Across the attempt, the host's `TcpExt` listen-drop, ACK-skip,
challenge-ACK and checksum counters did not move. `rp_filter` is 0 on every interface.

Node state on `ovh-ns104952`, from its cilium agent:

- `cilium-dbg status`: `KubeProxyReplacement: True [eno1 147.135.104.5, nebula1 10.42.0.16
(Direct Routing)]`, `Routing: Network: Tunnel [vxlan] Host: Legacy`,
  `Masquerading: IPTables [IPv4: Enabled]`, `Attach Mode: TCX`, `Host firewall: Disabled`.
- `ss -lntpe 'sport = :443'`: five `0.0.0.0:443` Gateway listeners (cilium-envoy, host network),
  each `fwmark:0xb00`.
- The egress proxy's rule toward `world`, `remote-node` and `host` on 443 and 80 is L3/L4 only.
  The flow above is `-> stack`, never `-> proxy`.

## Pending

- Probe E, from `n2rlk`: the node's Nebula address on `:443` (Gateway, `fwmark:0xb00`) against
  `:6443` (kube-apiserver, host network, no mark, in the Pod's policy). It tests whether only the
  first hangs, and whether the host's conntrack `invalid` count rises with it.
- Probe F: the same dial from a Pod on worker `ovh-ns103656`, the same Talos, kernel and Cilium
  with no `NetworkRuleConfig`. It tests whether the handshake completes while the ACK is still
  counted invalid.

## Scope

Any Pod that is not host-network, on a control-plane node, dialling its own node's public or
Nebula address on a Gateway port. Besides the three agentplane egress proxies, the control-plane
nodes currently run `gatus`, `headlamp`, the `agentplane-llm-ingress` Pods, LiteLLM,
cert-manager, Kyverno, SeaweedFS and CNPG instances among others. Which of them dial public
`*.allegedly.works` names is unchecked.

## Options

| Option                                                                                                                                         | Effect                                                                              | Cost                                                                                                                                            |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| a. Drop `control_plane_metrics_firewall_config`; return kube-controller-manager and kube-scheduler to Talos's loopback `bind-address`          | No firewall chain on any node; 10257/10259 unreachable off the host                 | Their ServiceMonitors cannot scrape. They have not since 2026-08-07 (<../k8s/TODO.md> § Alloy), and that entry's native-scrape option goes away |
| b. Keep the rule; keep clients off their own node's listener: the egress proxy skips own-node addresses, or in-cluster DNS answers the Gateway | Fixes the clients changed                                                           | Every other Pod on a control-plane node keeps the bug. The DNS variant is the RCA's option c, a cluster-wide change                             |
| c. Drop the rule, keep `bind-address: 0.0.0.0`                                                                                                 | Fixes                                                                               | 10257/10259 reachable from the internet (authenticated, `/healthz` anonymous)                                                                   |
| d. Upstream                                                                                                                                    | Talos: no accept-mode invalid drop on CNI interfaces; Cilium: the one-sided NOTRACK | Not in our hands                                                                                                                                |

The OVH edge and Game firewalls are not options: own-node traffic never leaves the host.

## Mitigations

- #7911 routes claude-ai boxes' acceptance traffic to the testing app's Service.
- Nothing yet covers other in-cluster clients of public `*.allegedly.works` names on
  control-plane nodes.
