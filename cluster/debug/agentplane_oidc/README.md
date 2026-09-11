# Agentplane OIDC connectivity investigation

- [Local Gateway TLS root cause](local_gateway_tls_rca.md): the SNI egress
  policy proxy's source-preserving upstream collides with its own downstream
  when the destination is the caller's node; options and recommendation.
- [Local Gateway TLS failure](local_gateway_tls.md): the 2026-09-10 socket and
  payload-free packet evidence the root cause builds on.
- [Gateway Service probes](gateway_service_probe.md): app/Actions policy changes
  and post-rollout positive and negative controls.
- [Cilium policy audit](policy_audit.md): remaining policy and route findings.
- [Internal DNS scope checks](service_dns_scope.md): why a cluster-wide DNS
  rewrite to the Gateway Service was unsafe at the time.

The backend permissions are verified. DNS is unchanged; the fix for the
node-IP TLS reset (`envoy.useOriginalSourceAddress: false`) is not yet
deployed, and complete OIDC acceptance is still outstanding.
