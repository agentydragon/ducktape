# Agentplane OIDC connectivity investigation

- [Local Gateway TLS failure](local_gateway_tls.md): socket and payload-free
  packet evidence for the same-node node-IP path.
- [Gateway Service probes](gateway_service_probe.md): app/Actions policy changes
  and post-rollout positive and negative controls.
- [Cilium policy audit](policy_audit.md): remaining policy and route findings.
- [Internal DNS scope checks](service_dns_scope.md): the current Gateway Service
  has no backends on non-Gateway nodes; a cluster-wide DNS rewrite is unsafe.

The backend permissions are verified. DNS is unchanged, the direct node-IP TLS
failure remains unresolved, and complete OIDC acceptance is still outstanding.
