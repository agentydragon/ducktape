# k8s manifests

- Pods failing to talk to each other (memberlist won't form, gRPC timeouts, intra-app
  probes failing): check CiliumNetworkPolicies first; they may need widening.
- Egress through a Cilium Gateway also needs permission for the **selected backend's**
  identity and `targetPort` (keep the client's SNI restriction); node:443 alone yields
  HTTP 403 after TLS succeeds. Validate a full HTTP request from the restricted pod, plus
  wrong-SNI and direct-plaintext rejection. Recipe: <../docs/cilium_network_policy.md>
  § Egress through a local Gateway.
