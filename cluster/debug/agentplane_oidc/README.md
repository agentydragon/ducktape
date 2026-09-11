# Agentplane OIDC connectivity investigation

Root cause found, fix not rolled out. <local_gateway_tls_rca.md>: the app's SNI
egress rule sends Authentik traffic through Cilium's policy proxy, whose
source-preserving upstream collides with its own downstream whenever public DNS
hands the pod its own node's IP (about one answer in five), so the TLS handshake
resets. The backend permissions for the Gateway Service path are verified
(#6007, #6012; lesson in <../../docs/cilium_network_policy.md> § Egress through
the Gateway Service). DNS is unchanged.

## Open items

- **Datapath fix rollout.** `envoy.useOriginalSourceAddress: false` is applied
  (Helm release 12, 2026-09-11 07:50 UTC) and every `hil-ovh` cilium-agent runs it,
  verified from each agent's `build-config` output. `wyrm2` and `optiplex` still
  run agents started before the change; they host no Agentplane Pods and pick it
  up on their next agent restart, which the stuck DaemonSet rollout (below) does
  not deliver.
- **Stuck cilium-agent DaemonSet rollout.** `maxUnavailable: 2` is held by two
  roaming nodes: `rugged` dropped off the mesh one second after its agent finished
  init (status frozen, tolerates the unreachable taint), and `iguana` has been
  NotReady since 2026-07-18 with an agent Pod that has carried a deletion
  timestamp since 2026-08-27. Every `cilium-config` change since then reached the
  `hil-ovh` nodes only by hand. Options: delete the `iguana` Node object (PodGC
  frees one slot; rejoin needs a manual CSR approval), and raise `maxUnavailable`
  to roaming-node count plus one in the Helm values.
- **Browser VM225 exception.** The `reportAllChanges/startTime` exception
  reported alongside the 2026-09-11 `/push/config` 403s has no matching symbol in
  the Agentplane frontend source or dependency manifests; its script source is
  needed before attributing it to the app or an extension.
- **Gateway on every node.** `gatewayAPI.hostNetwork.nodes.matchLabels: {}`
  (<../../terraform/main/cilium-values.yaml>) is deployed; a local Gateway
  Service backend is verified on wyrm2 only. optiplex and roaming laptops are
  unverified, including that ports 80/443 were free there.
