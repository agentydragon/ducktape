# A Pod's handshake with its own node's Gateway listener never completes

Cause confirmed by probes E and F. The one link read from source rather than from a node is which
rule drops the invalid packets. Observed 2026-09-25 from the staging egress proxy's Pods on the
control-plane nodes
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

The upgrade to v1.14.0 started 2026-09-19 (#7269, #7404, #7424).

## Earlier own-node failures

Own-node hairpins through the public Gateway have failed at four other layers, each fixed where
it broke. None of those fixes covers this one, which involves neither a policy proxy nor a
backend setting.

- Authentik's ingress policy saw the caller Pod's identity rather than `ingress` and dropped the
  connection. Callers' namespaces were admitted (<../docs/mcp_oauth_authentik_notes.md>
  § Hairpin + CiliumNetworkPolicy; #5932 for `agentplane-staging`).
- A caller's `toEntities: [world]` egress rule excluded the node's own address, and the dial
  hung. Rules now name `host` and `remote-node` (#3647, <../docs/cilium_network_policy.md>).
- An SNI egress rule's policy proxy collided with its own downstream and reset TLS to Authentik
  at 2.0 s. `envoy.useOriginalSourceAddress: false` fixed it (#6182,
  [RCA](agentplane_oidc/local_gateway_tls_rca.md)). That RCA's own-node control, which had no L7
  rule, passed on a worker before the v1.14 upgrade.
- Authentik distrusted forwarded headers from a pod-network source and advertised `http://`
  issuer URLs. The pod CIDR is now trusted (#6981).

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

Probe E, 07:00 UTC, from `n2rlk` to the node's Nebula address: first `:443` (the Gateway,
`fwmark:0xb00`), then `:6443` (kube-apiserver, host network, no mark, also in the Pod's policy):

```text
gateway-443 10.42.0.16:443: 4007 ms: no handshake
  host  SYN-RECV  10.42.0.16:443  10.244.0.143:39356  timer:(on,1.004ms,1)
  host  SYN_SENT src=10.244.0.143 dst=10.42.0.16 sport=39356 dport=443 [UNREPLIED]
  Pod   ESTAB 0 336  10.244.0.143:39356  10.42.0.16:443  timer:(on,1.292ms,3)
  host conntrack invalid +9 (+0 in the 3 s before)
  "NOTRACK for proxy return traffic" [0:0] -> [3:180]
apiserver-6443 10.42.0.16:6443: 78 ms: CONNECTION ESTABLISHED
  host conntrack invalid +0; entry TIME_WAIT ... [ASSURED]
```

The Pod held its unacknowledged 336-byte ClientHello, and the NOTRACK rule counted three 60-byte
packets: the SYN-ACK and its two retries.

Probe F, 07:00 UTC, from the Haku openclaw spike proxy (`10.244.1.119`) on worker `ovh-ns103656`.
Its egress rule has the same shape as the staging egress proxy's:

```text
own-443 147.135.39.162:443: 207 ms: CONNECTION ESTABLISHED Protocol version: TLSv1.3
  host  SYN_SENT src=10.244.1.119 dst=147.135.39.162 sport=40764 dport=443 [UNREPLIED]
  host conntrack invalid +7
own-443 10.42.0.13:443: 68 ms: CONNECTION ESTABLISHED Protocol version: TLSv1.3
  host conntrack invalid +7
```

On the worker the connection completes even though conntrack never leaves `SYN_SENT` and counts
the Pod's segments invalid: nothing there drops them. The rule that drops them on `ovh-ns104952`
was not read from the node. That takes `talosctl get nftableschains` or `nft`, and the
cilium-agent image has no `nft`.

Node state on `ovh-ns104952`, from its cilium agent:

- `cilium-dbg status`: `KubeProxyReplacement: True [eno1 147.135.104.5, nebula1 10.42.0.16
(Direct Routing)]`, `Routing: Network: Tunnel [vxlan] Host: Legacy`,
  `Masquerading: IPTables [IPv4: Enabled]`, `Attach Mode: TCX`, `Host firewall: Disabled`.
- `ss -lntpe 'sport = :443'`: the `0.0.0.0:443` Gateway listeners (cilium-envoy, host network)
  each carry `fwmark:0xb00`.
- The egress proxy's rule toward `world`, `remote-node` and `host` on 443 and 80 is L3/L4 only.
  The flow above is `-> stack`, never `-> proxy`.

## Scope

Any Pod that is not host-network, on a control-plane node, dialling its own node's public or
Nebula address on a Gateway port. Besides the three agentplane egress proxies, the control-plane
nodes currently run `gatus`, `headlamp`, the `agentplane-llm-ingress` Pods, LiteLLM,
cert-manager, Kyverno, SeaweedFS and CNPG instances among others. Which of them dial public
`*.allegedly.works` names is unchecked.

## Options

| Option                                                                                                                                | Effect                                                                              | Cost                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| a. Drop `control_plane_metrics_firewall_config`; return kube-controller-manager and kube-scheduler to Talos's loopback `bind-address` | No firewall chain on any node; 10257/10259 unreachable off the host                 | Their ServiceMonitors cannot scrape. They have not since 2026-08-07 (<../k8s/TODO.md> § Alloy), and that entry's native-scrape option goes away |
| b. Keep the rule; the egress proxy skips its own node's addresses                                                                     | Fixes the egress proxy                                                              | Every other Pod on a control-plane node keeps the bug                                                                                           |
| c. In-cluster DNS answers `*.allegedly.works` with the Gateway Service                                                                | No Pod dials a node address for our names, the precondition of all five failures    | Cluster-wide: rules written for node addresses (entity egress rules, Authentik's admissions and header trust) need review. The RCA's option c   |
| d. Drop the rule, keep `bind-address: 0.0.0.0`                                                                                        | Fixes                                                                               | 10257/10259 reachable from the internet (authenticated, `/healthz` anonymous)                                                                   |
| e. Upstream                                                                                                                           | Talos: no accept-mode invalid drop on CNI interfaces; Cilium: the one-sided NOTRACK | Not in our hands                                                                                                                                |

The OVH edge and Game firewalls are not options: own-node traffic never leaves the host.
Recommended: (a) now, and a design note for (c), since this is the fifth failure of the same
hairpin.

## Mitigations

- #7911 routes claude-ai boxes' acceptance traffic to the testing app's Service.
- Nothing yet covers other in-cluster clients of public `*.allegedly.works` names on
  control-plane nodes.
