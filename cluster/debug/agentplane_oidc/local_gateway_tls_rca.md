# Local Gateway TLS reset: root cause

Read-only probes 2026-09-11 05:53–06:15 UTC on Cilium 1.19.6. App pod
`agentplane-app-575bc59dcc-74bb2` (`10.244.2.184`, endpoint 1939, identity 60520) on `ovh-ns103711` (`147.135.39.176`). No cluster configuration changed;
the only writes were trace files under `/tmp` in the cilium-agent container,
deleted afterwards.

## Root cause

The app's egress rule to `host`/`remote-node`:443 carries `serverNames`
(<../../k8s/agentplane-staging/app/networkpolicy.yaml>). SNI is an L7 rule, so
Cilium redirects the connection to the node's Envoy policy proxy (listener
`cilium-proxylib-egress:19983`), which opens its own upstream TCP connection to
the original destination. With `proxy-use-original-source-address: true` (chart
default `envoy.useOriginalSourceAddress`, rendered unconditionally into
`cilium-config`) that upstream socket is bound to the pod's IP **and port**.

When the destination is the node's own public IP, the upstream is a second
socket in the same host network namespace with the mirror 4-tuple of the proxy's
downstream socket, and the two legs of one proxied connection collide:

1. Cilium's iptables masquerade rule
   `-s 10.244.2.0/24 ! -d 10.244.2.0/24 ! -o cilium_+ -j MASQUERADE` precedes
   the `exclude proxy return traffic` rule in `CILIUM_POST_nat`, so the upstream
   SYN leaving via `lo` is SNATed to `147.135.39.176`. The Gateway listener sees
   `147.135.39.176:P → 147.135.39.176:443` and creates a request socket.
2. Its SYN-ACK is un-NATed to `10.244.2.184:P`, routed to `cilium_host`
   (`ip route get 10.244.2.184 mark 0xb00` → `dev cilium_host`) and enters
   `bpf_host` carrying the listener's `SO_MARK` `0xb00` =
   `MARK_MAGIC_PROXY_EGRESS`. [`inherit_identity_from_host`](https://github.com/cilium/cilium/blob/9a8982433e18019e290b8199c0c4ad24f66befe8/bpf/lib/identity.h)
   sets `TC_INDEX_F_FROM_EGRESS_PROXY`; the pod's ingress program then skips
   the reply-to-proxy redirect (`ct_state->proxy_redirect &&
!tc_index_from_egress_proxy(ctx)` in
   [`bpf_lxc.c` ipv4_policy](https://github.com/cilium/cilium/blob/9a8982433e18019e290b8199c0c4ad24f66befe8/bpf/bpf_lxc.c))
   and delivers the SYN-ACK to the pod, whose socket is already established
   with the policy proxy.
3. Envoy's upstream never sees a SYN-ACK, stays `SYN-SENT`, and after
   `proxy-connect-timeout` (2 s, `envoy.connectTimeoutSeconds` default) resets
   the downstream: errno 104 during the TLS handshake.

Cilium's own safeguard against this — the proxy drops source preservation when
the destination has a policy entry ([`bpf_metadata.cc`](https://github.com/cilium/proxy/blob/edeb3f2af56c37c407efa1f63f0b32f595399bbc/cilium/bpf_metadata.cc),
`npmap_->exists(other_ip)`) — covers local pod endpoints only. The node's own IP
is `reserved:host` (`cilium-dbg ip get 147.135.39.176/32`) and has no policy
entry.

Every condition is required: an L7 egress rule toward the node entities, a
destination IP equal to the caller's own node, source-address preservation, and
a listener in the host namespace (hostNetwork Gateway). Public DNS naming the
node IPs is what makes the second condition true for roughly one resolution in
five.

## Observed

Verified-TLS handshakes (`ssl.create_default_context` + certifi, SNI
`auth.allegedly.works`, no HTTP request), source ports recorded:

| Source pod (egress rule)                                                       | Destination                   | Result                 |
| ------------------------------------------------------------------------------ | ----------------------------- | ---------------------- |
| app `10.244.2.184` on ovh-ns103711 (host/remote-node:443 + `serverNames`)      | own node `147.135.39.176:443` | reset 7/7, each 2.00 s |
| same                                                                           | remote `147.135.37.175:443`   | TLS 1.3 5/5, 4–6 ms    |
| same                                                                           | Service `10.106.122.5:443`    | TLS 1.3 3/3            |
| egress proxy `10.244.1.185` on ovh-ns103656 (host/remote-node:443, no L7 rule) | own node `147.135.39.162:443` | TLS 1.3 3/3            |
| same                                                                           | remote `147.135.37.175:443`   | TLS 1.3 3/3            |

The fourth row is the discriminating control: same node class, same
destination class, no L7 rule, no failure.

Node state on `ovh-ns103711`, from the cilium-agent pod:

- `cilium-dbg status`: `Routing: Network: Tunnel [vxlan] Host: Legacy`,
  `Masquerading: IPTables [IPv4: Enabled]`, `Host firewall: Disabled`,
  `Proxy Status: OK, ip 10.244.2.240, 17 redirects active`.
- `ip rule`: `9: fwmark 0x200/0xf00 lookup 2004`, `100: lookup local`, main.
  Table 2004 is `local default dev lo`; table 2005 is empty (the from-proxy
  rules are skipped in tunnel mode,
  [`requireFromProxyRoutes`](https://github.com/cilium/cilium/blob/9a8982433e18019e290b8199c0c4ad24f66befe8/pkg/proxy/routes.go)).
- `ss -lntpe`: all Gateway `0.0.0.0:443`/`:80` listeners and the policy proxy
  `127.0.0.1:19983` carry `fwmark:0xb00`.
- `iptables-save -t nat`, `CILIUM_POST_nat` in order: `MASQUERADE` for
  `-s 10.244.2.0/24 ! -d 10.244.2.0/24 ! -o cilium_+`, then `ACCEPT` for
  `--mark 0xa00/0xe00` (`exclude proxy return traffic from masquerade`), then
  the host→cluster SNATs. Raw-table NOTRACK rules cover only `-o cilium_host`
  and `--mark 0x200/0xf00`, not this upstream.
- `cilium-dbg monitor -v --related-to 1939` during a local probe (port 41166)
  and a remote probe (50378):

  ```text
  Policy verdict log: ... egress, action redirect, match L3-L4, 10.244.2.184:41166 -> 147.135.39.176:443 tcp SYN
  -> proxy port 19983 ... identity 60520->unknown state new ... 10.244.2.184:41166 -> 147.135.39.176:443 tcp SYN
  -> endpoint 1939 ... identity unknown->60520 state reply ... orig-ip 147.135.39.176: 147.135.39.176:443 -> 10.244.2.184:41166 tcp SYN, ACK
  -> endpoint 1939 ... identity unknown->60520 state reply ... 147.135.39.176:443 -> 10.244.2.184:41166 tcp ACK, RST
  Policy verdict log: ... action redirect ... 10.244.2.184:50378 -> 147.135.37.175:443 tcp SYN
  -> proxy port 19983 ... 10.244.2.184:50378 -> 147.135.37.175:443 tcp SYN
  -> endpoint 1939 ... identity remote-node->60520 state reply ... 147.135.37.175:443 -> 10.244.2.184:50378 tcp SYN, ACK
  -> proxy port 19983 ... 10.244.2.184:50378 -> 147.135.37.175:443 tcp ACK, FIN
  ```

  Both paths are redirected to the same proxy; `identity unknown` on the local
  path is `resolve_srcid_ipv4` discarding an ipcache answer of `HOST_ID` for a
  proxy-marked packet. Monitor aggregation hides repeated flags, so the absence
  of further SYN-ACK lines is not evidence.

- Host-namespace sockets sampled every 200 ms during the failing probe on port
  60952 (`ss -tanoe`, `/proc/net/nf_conntrack`):

  ```text
  ESTAB     1527 0  147.135.39.176:443   10.244.2.184:60952   fwmark:0xb00         (policy-proxy downstream, ClientHello queued)
  SYN-SENT  0    1  10.244.2.184:60952   147.135.39.176:443   fwmark:0xec680b00    (policy-proxy upstream; retransmits at 1 s)
  SYN-RECV  0    0  147.135.39.176:443   147.135.39.176:60952 fwmark:0xb00         (Gateway listener; SYN-ACK retries 0→3, outlives the RST)
  tcp 6 59 SYN_RECV src=10.244.2.184 dst=147.135.39.176 sport=60952 dport=443 src=147.135.39.176 dst=147.135.39.176 sport=443 dport=60952
  ```

  The conntrack reply tuple shows the SNAT. Across an earlier two-probe window
  `TcpExt` `TCPChallengeACK`, `TCPSYNChallenge`, `ListenOverflows` and
  `SyncookiesSent` did not move, so the upstream SYN reached neither the
  established child socket nor a full backlog.

- Envoy logs contain no entry for these failures; the policy proxy does not log
  connect timeouts.
- The 2026-09-10 probes from the previous app pod (`10.244.4.80`, endpoint 290
  on `ovh-ns102453`, local node `147.135.37.175`) recorded the same two sockets
  (`ESTAB 147.135.37.175:443 ← 10.244.4.80:48432 fwmark:0xb00`,
  `SYN-SENT 10.244.4.80:48432 → 147.135.37.175:443 fwmark:0xec680b00`, both in
  the Envoy cgroup) and a payload-free Talos `pcap` on the pod's host-side veth
  for source port 49125:

  ```text
  03:18:47.284687 Pod -> node SYN     seq=734713715
  03:18:47.284723 node -> Pod SYN-ACK seq=4096322733 ack=734713716
  03:18:47.284733 Pod -> node ACK                    ack=4096322734
  03:18:47.285337 node -> Pod SYN-ACK seq=2559352832 ack=734723413
  03:18:47.285344 Pod -> node ACK                    ack=4096322734
  03:18:49.288832 node -> Pod RST-ACK seq=4096322734 ack=734714038
  ```

  The second SYN-ACK acknowledges the upstream ISN+1 and reaches the pod; its
  sequence number is unrelated to the first SYN-ACK's, as expected from a
  listener that saw a rewritten tuple. The pod re-ACKs the first connection and
  the proxy resets it two seconds later. The same matrix reset on the node's
  Nebula address `10.42.0.15:443` and succeeded from the host namespace to both
  local addresses; the capture postdates the Gateway listener repair (#5999).

## Inferred

- The SYN-ACK's exact path from un-NAT through `bpf_host` to the endpoint
  program is read from code plus the observed marks, routes and the earlier veth
  capture; the BPF branch was not instrumented.
- The remote-node path is masqueraded by the same rule (out `eno1`); its
  conntrack entry had expired before it was read.
- Without masquerade the upstream SYN would match the proxy's established child
  socket and draw a challenge ACK instead: the collision, not the NAT, is the
  defect.

## Upstream Cilium

No issue matches this exact shape (pod egress SNI policy → hostNetwork Gateway
on the same node). Same family:

- [#47769](https://github.com/cilium/cilium/issues/47769): ingress proxy upstream
  leg "5-tuple-shaped like the downstream connection", reply re-classified as
  proxy traffic (1.20, VXLAN, open).
- [#48045](https://github.com/cilium/cilium/issues/48045): Gateway same-node
  backends fail in tunnel mode; `proxy-use-original-source-address: "false"`
  resolves it (open).
- [L7 traffic management docs](https://docs.cilium.io/en/stable/network/servicemesh/l7-traffic-management/):
  E/W CECs must set `cec.cilium.io/use-original-source-address: "false"`, as
  binding upstream sockets to the original source address/port "may cause
  5-tuple collisions".
- [`--proxy-use-original-source-address`](https://docs.cilium.io/en/v1.19/cmdref/cilium-agent/)
  "doesn't affect Ingress/Gateway API": the Gateway's own upstreams already run
  without source preservation; only policy proxies keep it.

## DNS and availability

- `*.allegedly.works` is five node A records, TTL 300
  (<../../../tf/gitops/dns-records/main.tf>); CoreDNS runs `loadbalance` and
  `cache 30` (<../../k8s/coredns-custom/config/Corefile>).
- JWKS: `httpx.get(..., timeout=10)` in <../../../mcp_infra/authentik_auth/oidc_principal.py>,
  no retries; `socket.create_connection` stops at the first address whose TCP
  connect succeeds, which the policy proxy always is. The TLS reset is
  terminal, so P(fail) ≈ 1/5 per fetch from a Gateway-node pod; observed 4/12
  from the app pod with normal DNS on 2026-09-11. Token exchange uses Authlib
  `AsyncOAuth2Client`; anyio's `connect_tcp` staggers addresses by 250 ms but
  likewise accepts the first TCP success.
- Node down: the same clients move past a dead address only after a connect
  timeout (sync httpx: up to 10 s per dead address ahead of a live one; anyio:
  250 ms; browsers: their own per-address timeout), and a pulled A record needs
  up to 300 s plus resolver caches to disappear. The operator's read is right:
  node-IP DNS gives retry-shaped, not failover-shaped, availability. That is a
  separate decision from this defect, which an LB avoids only incidentally
  (the upstream would target a non-local `world` address).

## Options

| Option                                                    | Fixes this | Blast radius                                                                                                                                                                                                                                     | Verdict                                     |
| --------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------- |
| a. `envoy.useOriginalSourceAddress: false` (#5990's knob) | yes        | Only L7-proxied egress: the four `serverNames` policies under `agentplane-{staging,testing}`; nothing else in `cluster/k8s` uses HTTP/SNI rules. Gateway/DNS proxy unaffected. Backends see the node address, which they already see cross-node. | Real fix: removes the collision class       |
| b. drop `serverNames` (plain L4 rule)                     | yes        | Loses SNI scoping: the pod could reach any Gateway vhost on node:443.                                                                                                                                                                            | Policy downgrade                            |
| c. workload-scoped route to the Gateway Service           | yes        | Couples clients to the operator-generated ClusterIP; the Service now has a local L7LB backend on every node (verified on wyrm2).                                                                                                                 | Sound for other reasons, not a datapath fix |
| d. OVH load balancer                                      | incidental | Cost; answers node-down failover, not this defect.                                                                                                                                                                                               | Separate decision                           |
| e. `bpf.masquerade: true`                                 | no         | Collision remains (challenge-ACK form).                                                                                                                                                                                                          | —                                           |
| f. Cilium upgrade                                         | no         | No upstream fix found.                                                                                                                                                                                                                           | —                                           |

Recommendation: (a). It is the documented Cilium setting for exactly this
collision class, it aligns policy proxies with what the Gateway's own upstreams
already do, and its only effect here is a source address the backends already
see today for the cross-node majority of requests. Roll it out through the
`//cluster:bootstrap` Cilium values path (<../../docs/network.md> § Changing
MTUs safely describes the Helm recreate mechanics), then rerun the table
above from app and Actions pods plus a twelve-sample JWKS fetch with normal
DNS before calling it fixed. Whether in-cluster clients should stop using the
public IPs at all (c) and whether the public edge needs an LB (d) stay
separate decisions.

## Rollout

Canary first, on the two `hil-ovh` nodes hosting the app and Actions Pods
(`ovh-ns103711`, `ovh-ns102453`), through a Flux-managed `CiliumNodeConfig`
(<../../k8s/kube-system/ciliumnodeconfig-proxy-original-source-canary.yaml>). The
agent reads it in its `build-config` init container, so after Flux applies it,
delete the `cilium-agent` Pod on each canary node and wait for the replacement to
be Ready. No Helm or `//cluster:bootstrap` run is involved until the global flip.

Acceptance, each against a non-canary `hil-ovh` node as control:

1. Setting present: `cilium-dbg config` in the canary agent shows
   `ProxyUseOriginalSourceAddress: false`; the Envoy config dump shows
   `use_original_source_address: false` on the policy listener.
2. Defect gone: rerun the table in § Observed from a pod on the canary node; the
   own-node row succeeds every time, remote rows still succeed; twelve JWKS
   fetches with normal DNS produce no `ConnectError`; a real operator login,
   federation exchange, and one Action approval from the app succeed.
3. Policy unchanged: wrong SNI to the Gateway is still refused; a host outside
   the policy is still denied.
4. Attribution: before deleting the agent, list every endpoint on the canary
   with a non-DNS proxy redirect (`cilium-dbg endpoint list`, policy status);
   only the four `serverNames` policies should appear. After the flip the
   Gateway access log shows the node address as source for those flows, as it
   already does for cross-node requests.
5. Counters: the policy proxy's upstream connect-timeout and connect-failure
   totals for the TLS egress cluster stop increasing on the canary and keep
   increasing on the control.

Rollback is deleting the object and recreating the same agent Pods. After a
day without regressions, set `envoy.useOriginalSourceAddress: false` in
<../../terraform/main/cilium-values.yaml>, run `//cluster:bootstrap`, and delete
the canary object in the same change.
