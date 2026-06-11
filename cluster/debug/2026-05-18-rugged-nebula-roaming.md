# Rugged ↔ talos-kimsufi-worker-0 Nebula handshake stuck after network reconfig

**Date:** 2026-05-18
**Status:** open; rugged-only impact, narrow blast radius

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

## Most likely cause

OVH per-host anti-DDoS/filter on kimsufi-worker-0 still has stale state
about rugged's _old_ public IPv4 endpoint. When rugged's network changed,
its new public IPv4 became `98.248.79.114`. Outbound UDP/4242 from rugged
reaches OVH (Nebula reports `Handshake message sent`); replies from
kimsufi-worker-0 may be going to the stale endpoint or dropped by OVH VAC
because the new pairing doesn't match a previously-established session.

Sister node `kimsufi-worker-1` works because each OVH host has its own
filter state and rugged ↔ kimsufi-worker-1 was apparently re-established
in time.

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

## Workarounds tried this session

- **`kubectl delete pod cilium-sj72q`** to restart Cilium on rugged →
  hook-denied (shared-infrastructure modification; would have re-established
  Cilium tunnels but not solved the Nebula peer wedge underneath).
- Manual investigation only; no remediation applied.

## Followups

- [ ] **Root cause Nebula's behavior here.** Capture pcaps on both sides
      during a rugged roam (rugged's Nebula sending handshakes; peer's
      Nebula log for received packets) to confirm whether OVH is silently
      dropping or whether peer-side Nebula doesn't update its
      remote-endpoint after the source IP changes. Nebula has a punchy
      mechanism for exactly this; verify it's enabled.
- [ ] **Force Nebula to re-resolve through the lighthouse on roam.** If
      Nebula's relay/lighthouse path is configured, the peer should learn
      the new endpoint via lighthouse query. Today it apparently doesn't.
- [ ] **Don't schedule augur (or any prod workload) to rugged**, until
      the above is fixed. Add a `kubernetes.io/hostname != rugged` affinity
      or a dedicated taint. This is a workaround, not the fix — the user
      explicitly flagged "adjusting node affinity would be just working
      around the issue" — but it stops the symptom from blocking deploys.
- [ ] **Lessons-learned writeup** once root cause is understood, in
      `cluster/docs/lessons_learned/` following the dated-filename pattern.
