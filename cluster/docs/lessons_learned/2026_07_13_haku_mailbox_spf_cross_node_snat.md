# 2026-07-13 — Haku mailbox rejected valid Gmail because cross-node SMTP lost its source IP

## Symptom

Direct mail from the whitelisted `agentydragon@gmail.com` address to
`haku@allegedly.works` was rejected during SMTP DATA with:

```text
550 5.7.1 sender not SPF-verified (spf=softfail)
```

Gmail's SPF record was healthy, and the connecting Google MTA address was in
one of its published ranges.

## Root cause

`mx.allegedly.works` has A records for all five public OVH Kubernetes nodes.
The `haku-mailbox-smtp` Service exposed port 25 on all five addresses through
`externalIPs`, used the default `externalTrafficPolicy: Cluster`, and had one
Stalwart endpoint.

The cluster's Cilium kube-proxy replacement operates in VXLAN/SNAT mode. When
SMTP entered a public node other than the node hosting Stalwart, Cilium
source-NATed the connection before forwarding it across nodes. The incident's
live BPF state showed the complete translation:

```text
209.85.218.42:48496 -> 147.135.104.5:25
209.85.218.42:48496 -> 10.244.1.113:2525 XLATE_SRC 10.244.0.12:48496
```

`209.85.218.42` was the Google MTA and belonged to Gmail's published
`209.85.128.0/17` SPF range. `10.244.0.12` was the ingress node's
`cilium_host` address. Stalwart correctly evaluated SPF against its apparent
TCP peer, `10.244.0.12`; Gmail's `~all` fallback therefore produced
`softfail`, and the mailbox's operator-whitelist Sieve script rejected it.

The original Service comment was correct that traffic could reach Stalwart
through every public node, but missed that SPF requires source identity as
well as connectivity.

## Fix

Add a `haku-mailbox-smtp-ingress` DaemonSet selected onto the same five public
OVH nodes as the HTTP Gateway's per-node Envoy tier. Each pod binds its node's
public port 25 through `hostPort`, passes SMTP and STARTTLS through unchanged,
and adds a PROXY protocol header on its connection to Stalwart. All five MX
addresses therefore accept mail immediately, regardless of the Stalwart pod's
current placement, while Stalwart recovers the Google MTA address for SPF.

The public `externalIPs` Service was replaced by a ClusterIP-only SMTP backend.
Stalwart enables PROXY protocol for the cluster pod CIDR, and a
CiliumNetworkPolicy allows that backend port only from pods carrying the SMTP
ingress identity. The trust and policy must stay paired: trusting the pod CIDR
without the identity-aware network fence would let another cluster workload
forge the source address in a PROXY header.

Validation pins the DaemonSet's public-node selector and host port, the
internal-only Service shape, the Stalwart trusted proxy range, and the network
policy relationship. The namespace explicitly uses privileged Pod Security
admission because the baseline profile rejects all host ports; the regression
test couples that exception to the required `hostPort: 25`. Do not weaken the
SPF gate to accommodate source NAT.
