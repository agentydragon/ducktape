# Rugged Nebula handshakes stuck after network reconfiguration

**Date:** 2026-05-18
**Status:** resolved — failure mechanism established and both halves of the
mitigation are live: the shared Nebula module binds `listen.host = "::"`
(<../../nix/nixos/modules/nebula.nix>) and Rugged imports the underlay-refresh
dispatcher (<../../nix/nixos/hosts/rugged/nebula-underlay-refresh.nix>, covered
by <../../nix/nixos/tests/rugged-nebula-underlay-refresh.nix>).

## 2026-07-14 recurrence

The same intermittent hang recurred for `ssh wyrm2.nebula.allegedly.works` while
rugged was on Wi-Fi. The evidence ruled out the name and inner route: DNS returned
Wyrm2's expected `10.42.0.20` address and the kernel selected `nebula1`. Nebula was
still trying a stale learned UDP endpoint ending in `:13146`, while its logs also
reported errors using IPv6 candidates with the IPv4-only `0.0.0.0` listener. After
Nebula learned a new endpoint ending in `:34029`, the next SSH connection completed
in 32 ms without a Nebula restart.

This narrows the intermittent failure to endpoint refresh after an underlay change,
not split DNS or kernel routing. It also supersedes the May incident's
OVH-specific filtering hypothesis: the same failure class occurs between Rugged
and a NixOS peer behind unrelated residential NAT.

The repair has two parts:

1. The NixOS Nebula module now follows the Nebula 1.10.3 example and binds
   `listen.host` to `::`, retaining UDP port 4242 while accepting both IPv4 and
   IPv6 underlay endpoints.
2. Rugged has a NetworkManager dispatcher that compares canonical IPv4 and IPv6
   default-route state after a five-second debounce. A real route change restarts
   `nebula.service`, `haproxy.service`, and `kubelet.service`; it ignores the
   route-less gap during a handoff and records new state only after the restart
   succeeds.

As of 2026-07-14, Nebula 1.10.3 remains the latest stable release. Upstream has
similar open reports where NAT or direct/relay state stays unusable until restart:
[#889], [#1616], and [#1748]. If this recurs after the repair is activated, capture
both peers' control state and compare it with #1748 before adding a broader
watchdog. Upstream also recommends port `0` for roaming nodes, but Rugged should
keep fixed port 4242 until the NixOS firewall behavior for an ephemeral
hole-punching port is validated.

[#889]: https://github.com/slackhq/nebula/issues/889
[#1616]: https://github.com/slackhq/nebula/issues/1616
[#1748]: https://github.com/slackhq/nebula/issues/1748

## Symptom

After rugged's underlying network reconfigured at `00:01:26-07:00` (Comcast
residential, IPv4 default route on `wlp0s20f3` changed), augur pods
scheduled to rugged could not reach CoreDNS on the talos worker nodes.
The new augur pod's `oauth2-proxy` sidecar entered `CrashLoopBackOff`
because OIDC discovery (`auth.allegedly.works`) timed out — DNS lookups
against `10.96.0.10` and the CoreDNS pod IPs (`10.244.7.121`,
`10.244.1.96`) both hung at the L4 level.

The augur `app` and `frontend` containers in the same pod were fine
because they don't initiate cross-node traffic at startup.

## Diagnosis

Pod-to-pod connectivity from rugged → other nodes goes through Cilium
tunnels that ride over Nebula (the 10.42.0.0/16 overlay).
`kubectl describe node rugged` shows `InternalIP: 10.42.0.30` — that's a
Nebula IP. So Cilium's tunnel endpoint on rugged is the Nebula interface.

`journalctl -u nebula` confirmed the path-level state:

- **rugged → 10.42.0.12 (talos-vps-worker-1, Hetzner)** — handshake
  recovered after a few retries; ping works.
- **rugged → 10.42.0.13 (talos-kimsufi-worker-0, OVH)** — handshakes
  sent every ~6 s, every single one times out. Ping (ICMP) over Nebula
  also drops; ping to the _public_ IP `147.135.39.162` succeeds (78 ms RTT),
  and ICMP to the sister kimsufi node `147.135.39.176` also succeeds.
  So the public-internet path is fine; only Nebula UDP/4242 in the
  return direction is broken to this one peer.
- **rugged → 10.42.0.14 (talos-kimsufi-worker-1)** — works. Same DC,
  same physical link.

This isolates the fault to **rugged ↔ kimsufi-worker-0 UDP/4242
return path**.

## Initial May hypothesis

The initial hypothesis was that OVH's per-host anti-DDoS/filter on
kimsufi-worker-0 still had stale state
about rugged's _old_ public IPv4 endpoint. When rugged's network changed,
its new public IPv4 became `98.248.79.114`. Outbound UDP/4242 from rugged
reaches OVH (Nebula reports `Handshake message sent`); replies from
kimsufi-worker-0 may be going to the stale endpoint or dropped by OVH VAC
because the new pairing doesn't match a previously-established session.

Sister node `kimsufi-worker-1` worked, which was consistent with per-host filter
state. The July recurrence against Wyrm2 disproved OVH filtering as the general
cause; stale Nebula/NAT endpoint state after roaming is the shared mechanism.

## Why this matters

> _"this is a roaming node so it goes down pretty often"_
> _"i don't see any \*good reason\* why a restart should make pods stop
> working on rugged"_

Today's pattern is: rugged roams → Nebula loses some peers → Cilium
tunnels riding on top go dark → cross-node pod traffic for pods scheduled
to rugged breaks → workloads scheduled to rugged hang.

This isn't supposed to happen — Nebula has a lighthouse mechanism for
NAT punching and Cilium handles per-node tunnel resets cleanly. The
specific symptom (one Nebula peer permanently wedged after a roam) points
at peer-side state, not at Cilium or kubelet.

## Workarounds tried in the May incident

- **`kubectl delete pod cilium-sj72q`** to restart Cilium on rugged →
  hook-denied (shared-infrastructure modification; would have re-established
  Cilium tunnels but not solved the Nebula peer wedge underneath).
- Manual investigation only; no remediation applied.

## Followups

- [x] Capture a recurrence against a second peer and distinguish DNS/routing
      from stale endpoint state.
- [x] Add a Rugged-scoped, debounced underlay-change refresh instead of changing
      workload placement.
- [ ] Activate the repair on Rugged and Wyrm2, then exercise Wi-Fi → WWAN →
      Wi-Fi while continuously probing Wyrm2 and a lighthouse.
- [ ] If a fixed-port restart ever fails to recover, validate Nebula port `0`
      with the NixOS firewall and packet capture before adopting it on Rugged.
- [ ] Upgrade to Nebula 1.11 after a stable release and re-evaluate whether the
      host-side refresh is still needed.
