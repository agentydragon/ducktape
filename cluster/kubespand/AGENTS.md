@README.md

## Critical Requirements

- **nftables is mandatory**: The KubeSpan agent MUST use nftables for packet marking and policy routing. Do NOT implement fallback modes that bypass nftables (e.g., direct routes without fwmark-based routing). If nftables operations fail, fix the root cause rather than working around it.

## Talos Correspondence

kubespand reimplements Talos's KubeSpan for non-Talos Linux. Maintain structural
correspondence with upstream Talos (`github.com/siderolabs/talos`):

```text
Upstream Talos code (imported via @com_github_siderolabs_talos//internal/...):
  @com_github_siderolabs_talos//internal/.../adapters/kubespan
  @com_github_siderolabs_talos//internal/.../adapters/network
  @com_github_siderolabs_talos//internal/.../controllers/cluster   (LocalAffiliateController)
  @com_github_siderolabs_talos//internal/.../controllers/kubespan  (PeerSpecController, EndpointController, ManagerController)
  @com_github_siderolabs_talos//internal/.../controllers/k8s       (KubePrismController)
  @com_github_siderolabs_talos//internal/.../controllers/network   (merge controllers, spec controllers, HardwareAddrController)
  @com_github_siderolabs_talos//internal/.../controllers/secrets   (APIController + APICertSANsController)
  @com_github_siderolabs_talos//internal/.../controllers/runtime   (KernelModuleSpecController, KernelParamSpecController)
  (standard Gazelle-managed go_library deps — no overlay files needed)

kubespand code:
  controllers/kubespan/identity.go     ↔  controllers/kubespan/identity.go          (reimplemented; reads network.HardwareAddr from upstream controller)
  controllers/cluster/discovery_service.go  ↔  controllers/cluster/discovery_service.go  (reimplemented, publishes affiliate)
  controllers/cluster/kubernetes_node.go    ↔  (kubespand-only: K8s informer → k8s.NodeStatus for PodCIDRs)
  controllers/kubespand/config.go      ↔  (kubespand-only: YAML → COSI config injection)
  controllers/kubespand/node_metadata.go  ↔  (kubespand-only: produces shim COSI resources for LocalAffiliateController)
  controllers/kubespand/os_root.go  ↔  (kubespand-only: produces secrets.OSRoot from YAML config for trustd CSR flow)
  controllers/k8s/kubeprism_config.go  ↔  controllers/k8s/kubeprism_endpoints.go + kubeprism_config.go  (adapted)
  controllers/network/wireguard_link.go  ↔  controllers/network/link_spec.go  (WG subset only)
  identity/                  ↔  (kubespand-only: disk identity persistence)
  discovery/                 ↔  (kubespand-only: discovery client wrapper)
  agentconfig/               ↔  (kubespand-only: YAML config + COSI resource)
```

## Cost Framing

kubespand's cost is the **size of the delta** from Talos — the glue code, shims, and
patches needed to bridge kubespand's YAML-config world to Talos's COSI controller world.
Code imported directly from Talos (via `@com_github_siderolabs_talos//internal/...`)
is free — it's an upstream dependency, not ducktape LOC.

**Prefer importing a 3k-LOC Talos controller over writing a 300-LOC reimplementation**,
if the controller works as a drop-in. Examples:

- `ManagerController`: imported directly from upstream Talos `controllers/kubespan`.
  Writes to `network.ConfigNamespaceName`; merge controllers bridge to `network.NamespaceName`.
- `RouteConfigController` (346 LOC), `RouteMergeController` (42 LOC): imported directly,
  our cost is ~25 LOC of glue (`NetworkConfig` struct, `DeviceConfigSpec` shim, registration).
- `PeerSpecController`, `EndpointController`: imported directly from upstream.
- `KubePrismController`, `APIController`, `APICertSANsController`: imported directly.

When evaluating whether to import vs reimplement, consider:

1. **Does it work as a drop-in?** Check what resources it reads/writes, and whether
   kubespand can produce the required inputs (possibly via a shim).
2. **Does it pull in unwanted dependencies?** Some Talos controllers depend on `machined`
   internals (udev, STATE partition) that don't exist on non-Talos hosts.
3. **Is the shim simpler than the reimplementation?** e.g., `DeviceConfigSpec` shim
   (~10 LOC) vs importing `DeviceConfigController` (245 LOC of device selector/bond
   expansion we don't need).

**Rules:**

1. **Upstream Talos code is imported via `@com_github_siderolabs_talos//internal/...`** —
   standard Gazelle-managed `go_library` deps. No overlay files or import rewrites needed.
2. **Check if Talos has it before reimplementing** — prefer adding a Bazel dep on the
   upstream package over writing new code.
3. **Reimplemented files** must reference the Talos equivalent at the top:
   `// Ref: internal/.../controllers/kubespan/identity.go`
4. **kubespand-only files** exist where Talos's approach doesn't apply (disk identity
   persistence, YAML config, WireguardLinkController instead of the monolithic
   LinkSpecController which depends on Talos udev integration).
