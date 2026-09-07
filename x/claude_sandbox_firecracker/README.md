# claude_sandbox_firecracker

> **Archived 2026-09-07** — the cluster manifests were unwired from Flux and
> parked here under `./cluster/`. firecracker-manager and its dynamically
> spawned VM pods (`devinfra/firecracker/manager`, `devinfra/firecracker/vm_pod`)
> aren't part of the live stack anymore, and nothing here is being reconciled.
> The Kustomization was already `suspend: true` (parked to free `wyrm2`
> resources, per `cluster/docs/decisions.md`); this makes that permanent.
> Nothing was running on the live cluster at archival time. To bring it back,
> re-add `x/claude_sandbox_firecracker/cluster/flux-kustomization.yaml` to
> `cluster/k8s/kustomization.yaml` and re-locate `./cluster/` under
> `cluster/k8s/x/claude-sandbox-firecracker/`.
