# Agentplane OIDC connectivity investigation

Root cause found, fix not rolled out. <local_gateway_tls_rca.md>: the app's SNI
egress rule sends Authentik traffic through Cilium's policy proxy, whose
source-preserving upstream collides with its own downstream whenever public DNS
hands the pod its own node's IP (about one answer in five), so the TLS handshake
resets. The backend permissions for the Gateway Service path are verified
(#6007, #6012; lesson in <../../docs/cilium_network_policy.md> § Egress through
the Gateway Service). DNS is unchanged.

## Open items

- **Datapath fix and acceptance.** The RCA's recommendation (a),
  `proxy-use-original-source-address: false`, is canaried on two Gateway nodes by
  a Flux-managed `CiliumNodeConfig` (RCA § Rollout); recreate the cilium-agent
  Pods there, run the acceptance steps, then flip `envoy.useOriginalSourceAddress`
  in the Helm values through `//cluster:bootstrap` and remove the canary object. Whether in-cluster clients
  should route to the Gateway Service instead of the public IPs (option c) is a
  separate decision; a shared CoreDNS rewrite would first need a client policy
  audit — `public-coder-agent-proxy` and `agentplane-egress` allow node HTTPS
  without the Authentik backend:9000 permission, so the Service path would deny
  them, and non-OVH consumers (aiquota, Airlock, Manifold/Plaid/Postscanmail/Tana
  MCPs on wyrm2; Home Assistant and the JWT rotation Jobs on optiplex) were
  inventoried but never exercised through the Service.
- **App shutdown budget.** <../../../x/agentplane/app/main.py> builds Uvicorn
  without `timeout_graceful_shutdown`, and `bridge.close()`/`store.close()` run
  only after `serve()` returns, so an open browser stream can hold shutdown past
  the 30 s pod grace. The app is one replica with `Recreate`
  (<../../k8s/agentplane-staging/app/deployment-agentplane-app.yaml>), so a
  rollout's outage is termination plus startup (174 s observed on 2026-09-11).
  Bound the HTTP/SSE drain and leave time for bridge and database close.
- **mitmproxy Docker-CI rule.** The `toFQDNs docker-ci.allegedly.works:2376`
  rule in <../../k8s/agents/mitmproxy/cnp-cloud-api-egress.yaml> (annotated as
  suspected dead) is confirmed dead: on 2026-09-10 04:08 UTC a TCP connect from
  the proxy pod timed out and Cilium monitor logged
  `Policy denied; bpf_lxc.c:1651 identity 58778 -> remote-node
10.244.2.242:40074 -> 147.135.37.175:2376 tcp SYN`.
  Operator decision pending: retire the rule, or add 2376 to the
  `toEntities: cluster` port list if the public Docker path is wanted (#6161
  already dropped the dangling TLSRoute).
- **Browser VM225 exception.** The `reportAllChanges/startTime` exception
  reported alongside the 2026-09-11 `/push/config` 403s has no matching symbol in
  the Agentplane frontend source or dependency manifests; its script source is
  needed before attributing it to the app or an extension.
- **Gateway on every node.** `gatewayAPI.hostNetwork.nodes.matchLabels: {}`
  (<../../terraform/main/cilium-values.yaml>) is deployed; a local Gateway
  Service backend is verified on wyrm2 only. optiplex and roaming laptops are
  unverified, including that ports 80/443 were free there.
