# Agentplane OIDC: local Gateway TLS investigation

Read-only observations on 2026-09-10 UTC. No cluster configuration changed.
PR #5990's global source-address mitigation was not deployed or validated.
It is being retired in favor of investigating Gateway Service routing; see
[Gateway Service probes](gateway_service_probe.md) for successful app/Actions controls.
The direct node-IP failure documented here remains unresolved.

## Controlled comparison

The current app Pod is `agentplane-app-5d6d88b587-g5sff`, IP `10.244.4.80`,
on `ovh-ns102453`. Its container is crash-looping. Its existing network namespace
and Cilium endpoint 290 remain available. `nsenter --net` from the Cilium agent
allowed credential-free TLS probes in that namespace without restarting or
changing the Pod. This is a connectivity diagnostic, not acceptance testing.

Every probe used `openssl s_client -brief -verify_return_error -verify_hostname
auth.allegedly.works -servername auth.allegedly.works`, with stdin closed and a
five-second bound. No HTTP request, credentials, or TLS secrets were collected.

| Source namespace | Destination                        | Result                   |
| ---------------- | ---------------------------------- | ------------------------ |
| App              | Local public `147.135.37.175:443`  | Reset, errno 104         |
| App              | Local Nebula `10.42.0.15:443`      | Reset, errno 104         |
| App              | Remote public `147.135.39.176:443` | TLS 1.3, verification OK |
| App              | Remote Nebula `10.42.0.14:443`     | TLS 1.3, verification OK |
| Host             | Local public `147.135.37.175:443`  | TLS 1.3, verification OK |
| Host             | Local Nebula `10.42.0.15:443`      | TLS 1.3, verification OK |

These comparisons hold the SNI constant and eliminate DNS selection from the
probe. They do not establish historical DNS behavior or validate OAuth.

## Socket evidence

During an additional failed local-public probe, `ss -nte --inet-sockopt` in the
host network namespace showed these sockets simultaneously:

```text
ESTAB    local 147.135.37.175:443 peer 10.244.4.80:48432
         inode 8856960 fwmark 0xb00
SYN-SENT local 10.244.4.80:48432 peer 147.135.37.175:443
         inode 8855171 fwmark 0xec680b00
```

Both sockets were in the same Envoy container cgroup. `0xec68` is app identity 60520. The socket options included `transparent`; the upstream socket remained
in `SYN-SENT` through retransmission before the client reset. An earlier capture
showed the same pattern for the local Nebula address, source port 60126.

The two failed matrix probes increased the previously observed policy-proxy
`egress-cluster-tls` connection failure and timeout totals from 47 to 49. Successful
host controls did not require the app's policy redirect.

This proves original-source reuse on the stalled upstream connection. It does
not capture the precise kernel lookup or packet that causes the stall.

## Locality safeguard and live mismatch

Deployed Cilium: `1.19.6`, commit
`9a8982433e18019e290b8199c0c4ad24f66befe8`. Deployed Envoy reports proxy commit
`edeb3f2af56c37c407efa1f63f0b32f595399bbc`, Envoy `1.36.9`.

- [Proxy metadata source](https://github.com/cilium/proxy/blob/edeb3f2af56c37c407efa1f63f0b32f595399bbc/cilium/bpf_metadata.cc#L505)
  suppresses source preservation when `npmap_->exists(other_ip)` is true.
- [Upstream socket option](https://github.com/cilium/proxy/blob/edeb3f2af56c37c407efa1f63f0b32f595399bbc/cilium/socket_option_source_address.cc#L93)
  also suppresses it when the destination has a policy entry.
- [Agent policy publication](https://github.com/cilium/cilium/blob/9a8982433e18019e290b8199c0c4ad24f66befe8/pkg/envoy/xds_server.go#L1818)
  skips endpoints without policy names, explicitly discussing host endpoints
  without IPs. Endpoint policy names are endpoint IP addresses.
- Live Envoy policy dump: 68 endpoint IPs; neither `147.135.37.175` nor
  `10.42.0.15` exists. The app IP does exist.
- Live listener 13556 has `use_original_source_address: true` and a configured
  BPF root. Agent config has `proxy-use-original-source-address: true`.
- Cilium's IP identity lookup classifies `147.135.37.175/32` as `reserved:host`;
  Linux routes it through the local table to `lo`.

Thus the safeguard's endpoint-policy definition of local excludes these host
addresses. This is the concrete mismatch in the local host-network Gateway path.
No documentation examined establishes that the user must disable source
preservation for this combination. Pod egress policy targeting a host is distinct
from applying L7 host-firewall policy.

## Separate configuration defect

The live Gateway reports `Conflicted=True`, `Accepted=False`, and
`Programmed=False` for `https-wildcard` and `kube-api-passthrough`. Its explicit
reason is overlapping HTTPS termination and TLS passthrough hostnames on 443.
The wildcard includes `api.allegedly.works`.

The active Envoy config nevertheless contains both filter chains. The successful
host controls reach `auth.allegedly.works` on the same listener. Therefore this
invalid configuration warrants separate repair, but is not evidence that the
policy proxy's upstream TCP timeout was caused by TLS filter-chain selection.

## Payload-free packet capture after listener repair

PR #5999 merged as `d12c20541dd899587767e9886eda395e37eccbf6`. At
03:17:03 UTC the Gateway reached generation 5, with all three remaining
listeners Accepted and Programmed. The API TLSRoute was pruned. The
`kube-api-proxy` Flux Kustomization was still waiting for health checks at the
last observation; do not equate the Gateway success with its health result.

Talos's read-only `pcap` API overcame the absence of container-local tcpdump.
The capture filter admitted only IPv4 TCP packets for app IP `10.244.4.80`
and the selected probe source port, with IP total length equal to IP plus TCP
header lengths (zero payload). No diagnostic Pod or binary was installed.

On the app's host-side veth, the probe using source port 49125 showed:

```text
03:18:47.284687 Pod -> node SYN     seq=734713715
03:18:47.284723 node -> Pod SYN-ACK seq=4096322733 ack=734713716
03:18:47.284733 Pod -> node ACK                    ack=4096322734
03:18:47.285337 node -> Pod SYN-ACK seq=2559352832 ack=734723413
03:18:47.285344 Pod -> node ACK                    ack=4096322734
03:18:49.288832 node -> Pod RST-ACK seq=4096322734 ack=734714038
```

The second SYN-ACK has different sequence numbers and acknowledges a different
client SYN. It arrives at the Pod, whose response still acknowledges the first
connection's server sequence. The policy proxy resets the first connection
approximately two seconds later. A previous capture using port 49123 shows the
same sequence. TLS probes still reset after the Gateway listener repair.

The actual failure is therefore upstream reply misdelivery. The earlier theory
that the upstream SYN was swallowed by the existing downstream socket was not
demonstrated. Loopback captures contained no matching packets and do not prove
where that SYN traveled.

Local artifacts: `/tmp/agentplane-hairpin-veth-49123.pcap` and
`/tmp/agentplane-hairpin-veth-49125.pcap`. Decode with `TZ=UTC tcpdump -nn -S
-tttt -r <file>`; the filter excludes TCP payload at collection time.

## Return-to-proxy exemption

Live `ss -lntpe` shows both the Gateway's `:443` listeners and the policy
proxy's `:13556` listeners carry `fwmark:0xb00`. The deployed proxy's
[listener factory](https://github.com/cilium/proxy/blob/edeb3f2af56c37c407efa1f63f0b32f595399bbc/cilium/bpf_metadata.cc#L75)
sets this mark whenever `is_ingress` is false. The live Gateway metadata has
`is_l7lb: true` and no `is_ingress: true`.

Cilium's
[identity inheritance](https://github.com/cilium/cilium/blob/9a8982433e18019e290b8199c0c4ad24f66befe8/bpf/lib/identity.h#L231)
classifies `0xb00` packets as coming from the egress proxy. Its
[IPv4 reply handling](https://github.com/cilium/cilium/blob/9a8982433e18019e290b8199c0c4ad24f66befe8/bpf/bpf_lxc.c#L2202)
redirects `ProxyRedirect` replies only when they are not already from the egress
proxy (with a separate exception for L7LB-originated connections).
The captured connection's CT entry has `ProxyRedirect`, `RevNAT=0`, and app
identity 60520.

This source path explains how the Gateway's reply can bypass the policy proxy
and reach the Pod. Executing that exact BPF branch has not been instrumented;
the directly observed facts are listener marks, socket reuse, connection state,
and the mismatched SYN-ACK arriving at the Pod. Merely adding an OUTPUT routing
rule or changing the Gateway mark would need separate safety analysis.

## Validation status

The global source-preservation change has not been deployed. Its existing build
validation passed, but does not validate connectivity or source identity after
rollout:
[BuildBuddy](https://app.buildbuddy.io/invocation/b6f1390b-871c-4a0d-b7e5-390aaa968b98).
No new build or acceptance invocation was run for these read-only diagnostics.
The app is not continuously Ready, so acceptance remains gated.
