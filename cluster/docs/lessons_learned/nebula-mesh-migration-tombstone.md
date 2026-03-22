# Nebula Mesh Migration (Completed 2026-03)

KubeSpan (Talos built-in WireGuard mesh) replaced by Nebula. KubeSpan lacked relay capability — symmetric NAT nodes couldn't communicate. Nebula provides relays via lighthouses.

- KubeSpan disabled on all Talos nodes
- Nebula deployed on all nodes (Talos extension + NixOS service)
- kubespand (custom KubeSpan reimplementation, ~15k LOC Go) decommissioned

Nebula PKI managed in `persistent-auth/nebula.tf`.

## kubespand (decommissioned)

**Built**: 2025-10 to 2025-12. Reimplemented Talos KubeSpan for non-Talos Linux workers using COSI framework. Included WireGuard mesh controller, discovery service client, KubePrism proxy (`localhost:7445`), apid mTLS proxy, and QEMU integration tests.

**Why it died**: KubeSpan has no relay or hole-punching mechanism. Double NAT (home + carrier-grade, two home networks) makes WireGuard tunnels impossible. Endpoint cycling averages ~240s per attempt and still fails. Confirmed by QEMU double-NAT test.

**Key findings**:

- `rp_filter=0` (or `2`) required — WireGuard decapsulated packets fail strict reverse path check. Same issue affects Nebula.
- Tailscale MagicDNS silently drops SRV queries matching certain patterns. gRPC-Go's `dns:///` resolver does SRV by default; use `passthrough:///`. See `debug/kubespand-grpc-dns-magicdns.md`.
