# Gateway placement on every cluster node

The Gateway Service had no local proxy backend on `wyrm2` or `optiplex` during
our OIDC investigation. An empty `gatewayAPI.hostNetwork.nodes.matchLabels`
selects every Cilium node, including roaming laptops whenever connected. This
provides a local Gateway for clients on each node without new node labels.

Public DNS remains the explicit OVH address list in
`tf/gitops/dns-records/main.tf`. SMTP ingress stays on the public OVH nodes;
its placement is independent of Gateway placement.

## Rollout and verification

Deploy the committed Cilium values through the supported `//cluster:bootstrap`
operator workflow. Cilium is infrastructure-managed Helm configuration, not a
Flux HelmRelease: merging alone does not deploy this change.

Host-network Gateway listeners bind port 80/443 on all interfaces, subject to
the host firewall. Check port availability on each newly selected node before
rollout. The shared NixOS worker firewall trusts Nebula and Cilium interfaces;
this change adds no firewall openings. Reserve these ports for Gateway on
cluster-connected laptops, too. Offline laptops require verification when they
return; an unavailable Cilium agent cannot provide the local Gateway.

After deployment, collect read-only evidence:

- Confirm the generated Gateway CiliumEnvoyConfig selects all nodes, and each
  healthy node has listeners and local Gateway Service proxy backends.
- From existing Pods on wyrm2, OptiPlex, and connected roaming nodes, verify TLS
  to the Gateway Service with SNI and certificate verification for
  `auth.allegedly.works`, then public OIDC discovery and JWKS responses. Record
  only status and transport metadata.
- Recheck Agentplane's restricted-policy path: canonical SNI succeeds, wrong
  SNI is rejected, and direct plaintext Authentik backend access is denied.
- Recheck public ingress on OVH. Keep shared issuer DNS migration separate
  until these checks pass, including on returning laptops.

If placement causes problems, revert the selector to
`topology.kubernetes.io/region: hil` through the same infrastructure workflow.

Reference: [Cilium host-network Gateway placement](https://docs.cilium.io/en/stable/network/servicemesh/gateway-api/gateway-api/#deploy-gateway-api-listeners-on-subset-of-nodes).
