# VM SSH Exposure — Scaling Past One Backend

**Status**: Plan, not yet implemented (2026-05-30).

## Current State

Gecko's SSH is reachable at `ssh agentydragon@gecko.allegedly.works:22` via a
hand-written `CiliumEnvoyConfig` in <../../k8s/gecko/app/ciliumenvoyconfig.yaml>.
The CEC binds `0.0.0.0:22` on every hil hostNetwork Envoy node and forwards
to `gecko-ssh:22`.

Limitations:

- One TCP listener per host port. Each additional VM that wants SSH on port 22
  would collide with gecko, so we'd need to pick a new port per VM
  (`2222`, `2223`, …) and remember which is which.
- Plain SSH wire format (`SSH-2.0-…` banner + KEX) carries no hostname or
  identity in clear, so an L4 proxy on `:22` has nothing to dispatch on. You
  can't multiplex many backends behind one external port the way HTTPS does.
- The CEC is also "outside" the Gateway API surface — every new VM means
  another bespoke YAML to maintain.

This is fine for one or two VMs. It gets clunky fast.

## Option A — TLSRoute + SSH `ProxyCommand` (most in-the-grain)

Tunnel SSH inside TLS so the SNI carries the destination hostname, then let
the existing Cilium gateway's `:443` listener route by SNI.

Wiring:

- Add a `TLSRoute` (`gateway.networking.k8s.io/v1` — Cilium 1.19 supports it,
  the experimental flag has been retired) in the VM's namespace with
  `hostnames: [gecko.allegedly.works]`, `parentRefs.sectionName: …`
  (a new `tls-passthrough-vms` listener on the existing `cluster-gateway`),
  `backendRefs: gecko-ssh:22`.
- The listener is `protocol: TLS, tls.mode: Passthrough` — the gateway just
  forwards the raw stream after SNI inspection; it doesn't terminate TLS.
- Client-side `~/.ssh/config`:

  ```text
  Host gecko.allegedly.works
    User agentydragon
    ProxyCommand openssl s_client -quiet -connect %h:443 -servername %h
  ```

  (Or `socat OPENSSL:%h:443,servername=%h,verify=0 -` for less verbose output.)

Pros:

- One listener (`:443`) routes N backends by SNI. Adding a new VM is one
  `TLSRoute` resource, zero gateway changes.
- Travels through everything: hotel Wi-Fi, corporate firewalls, captive
  portals — `:443` is the universal escape hatch.
- Same trust boundary (key auth on the VM's sshd); TLS just provides the
  routing envelope.

Cons:

- Two crypto handshakes (TLS outer, SSH inner). Negligible latency, but real
  CPU on the gateway.
- Need a `ProxyCommand` on every client. Not a problem on workstations with a
  managed home-manager config; annoying from a random machine.
- Connection-reuse and `ControlMaster` work normally inside the tunnel, but
  setup overhead per fresh control socket is higher.

Recommendation: when we get past ~2 SSH-exposed VMs, do this. It composes
SSH (which we keep) with the SNI router Cilium already implements, and the
`ProxyCommand` block can ship via Nix home-manager so it's transparent on
managed hosts.

## Option B — Tailscale Funnel / Tailscale SSH

Run a Tailscale operator (or a small sidecar per VM) that joins the tailnet
and exposes the host. Tailscale Funnel terminates a custom hostname at
Tailscale's edge and forwards into the tailnet.

Pros:

- Zero L4 plumbing on our side. SNI routing, NAT traversal, identity
  (Tailscale ACLs) all delegated.
- Works from anywhere; no client config beyond Tailscale itself.
- Could replace Nebula entirely if we ever wanted to consolidate the personal
  mesh.

Cons:

- Vendor dependency on Tailscale's control plane / coordination server.
- Different trust model from the rest of the cluster (mesh-membership instead
  of SSH key auth or our existing OIDC).
- Adds yet another sidecar pattern next to the existing Nebula one.

Recommendation: not worth introducing for SSH alone. Reconsider only if we
move the whole personal mesh from Nebula to Tailscale for unrelated reasons.

## Option C — SSH3

[`francoismichel/ssh3`](https://github.com/francoismichel/ssh3) is an
academic / experimental implementation of SSH-over-HTTP/3 (QUIC). The
protocol piggybacks on TLS so it inherits SNI routing, ALPN, multiplexed
channels, 0-RTT auth, and cert-based identity. There's an `Extended Connect`
extension that lets a standard HTTP/3 proxy carry the SSH session.

Pros:

- Solves exactly the problem we're working around. SNI routing comes built in.
- Modern handshake, faster startup than Option A.

Cons:

- Not in mainline OpenSSH. Both ends need the `ssh3` binary.
- IETF hasn't picked it up; no certainty it goes anywhere.
- Cilium has no HTTP/3 or QUIC listener support in 1.19, so we'd still need a
  hand-written `CiliumEnvoyConfig`, just on UDP/:443 instead of TCP/:22.

Recommendation: track upstream. Not worth implementing in production until
either OpenSSH integrates it or Cilium grows native QUIC routing — whichever
comes first.

## Decision Triggers

- **Stay on CEC-per-port (current)**: ≤ 2 SSH-exposed VMs.
- **Move to Option A (TLSRoute + ProxyCommand)**: ≥ 3 VMs, OR first time a
  new VM forces a non-canonical port.
- **Reconsider Options B/C**: never for SSH alone; only if we adopt the
  underlying stack (Tailscale, SSH3) for other reasons.

## Related

- <../../k8s/gecko/app/ciliumenvoyconfig.yaml> — current implementation.
- <../kubevirt_nixos_vm.md> "Exposing SSH Publicly" — runbook reference.
- <../../k8s/gateway/gateway.yaml> — where a new `tls-passthrough-vms`
  listener would live for Option A.
- <../../k8s/kube-api-proxy/tlsroute.yaml> — existing TLSRoute (v1) example
  in the cluster, suitable as a template.
