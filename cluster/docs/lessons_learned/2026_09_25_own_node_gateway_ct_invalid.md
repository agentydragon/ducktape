# A Pod's handshake with its own node's Gateway hung on control-plane nodes

**Date**: 2026-09-19 to 2026-09-26. **Status**: Fixed (#7960, applied by bootstrap);
guarded by the `gateway-probe` DaemonSet (#7965). Issue #7918.

## Symptom

After the Talos v1.14.0 upgrade, a Pod on a control-plane node that dialled its own node's
public or Nebula address on `:443` got SYN and SYN-ACK, then hung: its ACK and ClientHello
never reached the Gateway listener. Dials to other nodes, and to the Gateway Service's
ClusterIP, completed. Workers were unaffected. It surfaced as 30 s `httpx.ReadTimeout`s through
the agentplane egress proxies, which pin one address per host for 30 s. Every public
`*.allegedly.works` name resolves to every Gateway node, so a pin on the proxy's own node
stalled that host until the pin expired. External checks, Gatus included, stayed green.

## Cause

Three behaviours combined:

1. Cilium's "NOTRACK for proxy return traffic" rule in `CILIUM_OUTPUT_raw` matches the host
   Envoy listener's `SO_MARK` `0xb00`. The listener's SYN-ACK to a local Pod therefore skips
   conntrack, and the Pod's entry stays `SYN_SENT [UNREPLIED]`.
2. Conntrack classifies the Pod's following ACK and data as invalid. `nf_conntrack_tcp_be_liberal`
   does not change this, because the state table rejects the segment before any window check.
3. Talos v1.14.0 ([`78efbb413`](https://github.com/siderolabs/talos/commit/78efbb413863a902319c2f0b3668e310280cc7e3))
   drops `ct state invalid` in its ingress chain even in default-accept mode. The chain exists
   only on nodes with a `NetworkRuleConfig`. Here that was the control planes, which had one
   guarding kube-controller-manager and kube-scheduler on 10257/10259.

On a worker the invalid segments pass and the handshake completes. A host-network client is
never affected: its SYN-ACK leaves through `lo`, which the NOTRACK rule does not match.

## Fix

The `NetworkRuleConfig` was deleted, and both components went back to Talos's loopback bind.
With no ingress chain on any node, nothing drops the invalid segments. The comment on
`common_cluster_config` in <../../terraform/main/infrastructure.tf> holds the invariant. It
also records why a Nebula-address bind doesn't work: Talos's probes target localhost. Because
of the loopback bind, the two components can only be scraped from the host
(<../../k8s/TODO.md> § Alloy).

The upstream behaviours are unchanged. Adding any `NetworkRuleConfig` brings the hang back.
`OwnNodeGatewayHandshakeFailing` (<../../cdk8s/monitoring/gateway_probe.py>) completes this TLS
handshake from an ordinary Pod on every public node and alerts when it fails.

## Own-node hairpin is a recurring failure

This is the fifth time a Pod dialling its own node through the public Gateway has broken, each
time at a different layer:

- Authentik's CiliumNetworkPolicy saw the caller Pod rather than `ingress`
  (<../mcp_oauth_authentik_notes.md> § Hairpin + CiliumNetworkPolicy).
- `toEntities: [world]` excludes the cluster's own nodes (<../cilium_network_policy.md>).
- The L7 policy proxy collided with its own downstream (#6182).
- Authentik distrusted forwarded headers from pod-network sources (#6981).

Each fix covered only its own layer. A new component on this path, such as a policy proxy, a
host firewall or a backend's trust setting, should be checked against a Pod dialling its own
node before anything else. Having in-cluster DNS answer `*.allegedly.works` with the Gateway
Service would take away the precondition behind all five failures. That hasn't been
attempted: rules written for node addresses (entity egress rules, Authentik's admissions and
header trust) would need review first.
