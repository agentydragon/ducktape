# agent-sandbox controller

`agent_sandbox.py` installs the [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
controller from its pinned `sandbox-with-extensions.yaml` release asset, upstream's
collision-free GitOps install: one controller Deployment, the core and extension CRDs,
RBAC, Services and the webhook configuration. The generated `kustomization.yaml` names the
release URL, so the `agent-sandbox-controller` Flux Kustomization owns the CRDs and
upgrades them with the pin. Its strategic-merge patches keep the namespace's labels, and the
controller on OVH (region `hil`) with a restricted security context and health probes.

Kustomize fetches the URL unverified; the asset's SHA-256 pin is `MODULE.bazel`'s
`agent_sandbox_release_bundle`, for the CRD bindings. One `_VERSION` sets both the release
URL and the patch's controller image. Renovate finds that image in the generated
`patches.k8s.yaml`, but a bump belongs in `_VERSION`: the release URL has no Renovate
manager, and an edit to the generated file alone fails `test_generate_manifests`.

Workspaces built on the controller: `agent_workspaces.py`, usage in
`cluster/k8s/agents/agent-sandbox/README.md`.
