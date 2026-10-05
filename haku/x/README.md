# Parked Haku components

This tree holds Haku components removed from active use but kept for historical reference. Each
component README records its status and any reactivation boundary. Nothing in this tree is an active
Flux source by virtue of its location here.

- [`dispatch/`](dispatch/) — retired Haku z.ai dispatch plane and worker-zone perimeter. Its live
  namespaces and resources were pruned after the Flux registrations were suspended and removed.
- [`zones/`](zones/) — retired Haku z.ai worker zone: the `haku-sandbox-zai` namespace perimeter, the
  shared `haku-zones-mitmproxy` egress proxy, and the Kyverno proxy-injection policy. No longer
  registered in the active Flux root.

The unrelated shared LiteLLM z.ai provider remains active.
