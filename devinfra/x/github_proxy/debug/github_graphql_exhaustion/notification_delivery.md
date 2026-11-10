# Notification delivery: source-specific ntfy TCP failure

Read-only investigation on 2026-09-06, approximately 02:03–02:10 UTC. No secret
values were read; no notification POSTs, configuration/workload changes, or restarts.

## Why this matters for acceptance

The GitHub quota alerts use the shared Alertmanager ntfy receiver. At 01:59:05Z,
the preceding hour's webhook failure increase was approximately 51.86 for
`alertmanager-monitoring-1`, reason `other`, versus zero for replica 0. Both had
approximately 62–63 notification attempts. Replica 1 logged a TCP timeout to
`159.203.148.75:443` at 01:57:31Z; replica 0's last observed failure was an HTTP429
daily-quota response at 23:59Z.

These are **shared delivery-path** observations, not proof that a particular
GitHub quota alert was sent or received. Neither successful metrics evaluation
nor a public ntfy GET establishes user receipt of a GitHub notification. The
seven-day acceptance window must not treat an unverified notification path as
working solely because the quota sampler is healthy.

## Reproduction

| Check                          | Replica 0: ovh-ns103656         | Replica 1: ovh-ns103711        |
| ------------------------------ | ------------------------------- | ------------------------------ |
| Pod IP                         | 10.244.1.16                     | 10.244.2.38                    |
| Cilium identity                | 6554                            | 6554                           |
| Host route source              | 147.135.39.162                  | 147.135.39.176                 |
| Route to ntfy IP               | eno1 via 147.135.39.254         | eno1 via 147.135.39.254        |
| Pod to ntfy IP TCP443          | Public GET HTTP200 at 02:05:30Z | Timeout 02:06:08–02:06:14Z     |
| Host-network to ntfy IP TCP443 | Handshake succeeds              | Eight-second timeout           |
| Pod to 1.1.1.1 TCP443          | Not tested                      | Immediate success at 02:07:30Z |
| Host-network to 1.1.1.1 TCP443 | Not tested                      | Handshake succeeds             |

The failing pod check was bounded `nc -w 6 -z -v 159.203.148.75 443`.
Host-network checks used an eight-second-bounded Bash TCP connection in the
existing Cilium container. No credentials or HTTP notification request were used.
Both Alertmanagers use image v0.31.1 and the same policy-relevant labels; both
nodes report Ready. Early exec requests into ns103711 stalled, but subsequent
bounded requests succeeded without intervention.

The public GET used existing BusyBox wget, which unexpectedly reported TLS
certificate validation unimplemented. No bypass flag was used, and the body was
discarded. That result proves TCP/HTTP connectivity only, **not verified TLS**.
All subsequent network tests were explicitly TCP-only.

## Policy and observation boundary

- No monitoring NetworkPolicy or Cilium cluster-wide policy selects Alertmanager
  for egress restriction. Failing endpoint 821 has realized
  `policy-enabled: none`, identity 6554, and wildcard allowed egress. Working
  endpoint 385 also reports egress enforcement disabled.
- Cilium host firewall is disabled. The failing node's filter OUTPUT policy is
  ACCEPT; its referenced CILIUM_OUTPUT chain contains accept/mark rules, no drops.
  This was not an exhaustive inspection of every host firewall hook.
- Hubble retained outgoing SYNs from 10.244.2.38 to the ntfy IP at
  02:06:22.048920092Z and 02:06:25.124929877Z, verdict FORWARDED, trace subtype 3.
  This proves forwarding at that Cilium observation point, not physical delivery.
- A later reverse-direction Hubble query had no retained matching flows. A ring
  buffer is not a complete packet capture; this does not prove no reply arrived.

The reproducible timeout follows the **node/public-source path**, including
outside the pod network. It precedes TLS, webhook authentication, and HTTP quota
handling; it is distinct from the earlier HTTP429. Using the IP directly also
excludes DNS as a prerequisite for reproducing this timeout. An Alertmanager
egress-policy change would not address the evidence observed here.

The exact loss point remains unobserved: source-specific provider/remote
filtering and host return-path loss are candidates, not established causes.
The smallest next discriminator is a bounded SYN/SYN-ACK/header-only observation
on eno1 for this destination, paired with ntfy/provider evidence for the failing
source versus the working source. Existing Cilium containers lack tcpdump; no
tools or diagnostic workloads were added. No remediation was attempted, and no
actual GitHub notification receipt was verified in this investigation.
