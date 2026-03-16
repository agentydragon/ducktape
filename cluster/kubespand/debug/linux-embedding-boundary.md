# KubeSpand Linux Embedding Boundary Analysis

What parts of Talos `machined` can kubespand embed wholesale when running as a systemd
service under NixOS (or any Linux), and where does the glue layer become expensive?

The key cost metric is **not binary size or LOC embedded** — it's **the size of the glue
layer** (shims, config injection, fake COSI resources) needed to make upstream controllers
run in a non-PID-1, non-Talos environment.

## Talos machined Controller Census (v1.13.0-alpha.2)

machined registers ~160 controllers across these subsystem groups:

| Group | Count | What it does |
|-------|-------|-------------|
| `block` | 16 | Disk/volume discovery, mount management, LVM, zswap, system disk |
| `cluster` | 10 | Discovery, affiliates, member list, node identity, k8s push/pull |
| `config` | 3 | Machine config acquisition, persistence, machine type |
| `cri` | 5 | Container runtime config, image cache, seccomp profiles |
| `etcd` | 5 | etcd PKI, config, spec, member management, advertised peers |
| `files` | 6 | CRI config generation, `/etc` file management, iSCSI/NVMe-oF IQN/NQN |
| `hardware` | 5 | PCI device inventory, driver rebinding, TPM PCR, system info |
| `k8s` | 34 | Control plane static pods, kubelet, manifests, endpoints, KubePrism |
| `kubeaccess` | 3 | Talos API access from K8s pods |
| `kubespan` | 5 | WireGuard mesh: identity, peer spec, manager, endpoints, config |
| `network` | 44 | Full network stack: links, addresses, routes, DNS, nftables, operators, probes |
| `perf` | 1 | Performance stats collection |
| `runtime` | 28 | Kernel modules, params, extensions, diagnostics, watchdog, OOM, logs |
| `secrets` | 16 | PKI for OS, K8s, etcd, kubelet, trustd, maintenance |
| `siderolink` | 4 | SideroLink tunnel management |
| `time` | 2 | NTP sync, adjtime status |
| `v1alpha1` | 1 | Legacy service controller |

## What kubespand Already Embeds

### Upstream controllers used directly (zero glue cost)

These controllers are imported from `@com_github_siderolabs_talos` and registered with
no modifications:

| Controller | Package | Why it works |
|-----------|---------|-------------|
| `LocalAffiliateController` | `controllers/cluster` | Reads COSI resources only (no syscalls) |
| `PeerSpecController` | `controllers/kubespan` | Pure COSI resource transformation |
| `EndpointController` | `controllers/kubespan` | Pure COSI resource transformation |
| `NfTablesChainController` | `controllers/network` | Applies nftables rules from COSI resources |
| `AddressSpecController` | `controllers/network` | Applies addresses via netlink |
| `RouteSpecController` | `controllers/network` | Applies routes via netlink |
| `KubePrismController` | `controllers/k8s` | TCP proxy, pure userspace |
| `APICertSANsController` | `controllers/secrets` | Pure COSI resource transformation |
| `APIController` | `controllers/secrets` | CSR generation + trustd gRPC call (pure userspace) |

These work because they interact with the world through either:
- COSI state (in-memory resource graph) — fully under kubespand's control
- Standard Linux syscalls (netlink, nftables, WireGuard) — work fine as root under any Linux
- Network I/O (gRPC to discovery/trustd) — standard userspace

### Reimplemented controllers (glue layer)

These controllers are written in kubespand because the upstream versions depend on Talos
internals that don't exist outside Talos:

| kubespand Controller | Replaces | Why reimplemented | Glue cost |
|---------------------|----------|-------------------|-----------|
| `ConfigController` | `config.*` | Upstream reads `MachineConfig` from STATE partition + config acquisition protocol. kubespand reads a YAML file. | Low (~100 LOC) |
| `NodeMetadataController` | Multiple | Upstream has ~10 separate controllers producing these resources (hostname, nodename, machine type, node addresses, network status, etc). kubespand synthesizes all of them in one controller from `os.Hostname()` + config + sysfs. | Medium (~240 LOC) |
| `IdentityController` | `kubespan.IdentityController` | Upstream reads STATE partition + `FirstHardwareAddr` controller chain. kubespand uses a flat file + sysfs MAC detection. | Low (~120 LOC) |
| `ManagerController` | `kubespan.ManagerController` | Upstream has complex Enabled toggle, shared output mode. kubespand is always-on, exclusive outputs. WireGuard peer state machine logic is identical (uses upstream adapter). | Medium (~640 LOC, but most is 1:1 upstream logic) |
| `WireguardLinkController` | `network.LinkSpecController` | Upstream is ~700 LOC handling bonds, bridges, VLANs, physical links, WireGuard, plus Talos udev integration. kubespand only needs WireGuard. | Low (~230 LOC) |
| `DiscoveryController` | `cluster.DiscoveryServiceController` | Upstream has `MachineResetSignal`, `AffiliateMergeController` integration, dual namespace (raw + merged). kubespand writes directly to cluster namespace. | Medium (~390 LOC) |
| `KubernetesNodeController` | N/A | kubespand-only: K8s informer for PodCIDRs. No upstream equivalent. | Low (~200 LOC) |
| `KubePrismConfigController` | `k8s.KubePrismEndpointsController` + `KubePrismConfigController` | Upstream reads from `config.MachineConfig` + `cluster.Member`. kubespand reads from `cluster.Affiliate` + agent config. | Low (~130 LOC) |
| `OSRootController` | `secrets.RootOSController` | Upstream reads CA from MachineConfig + STATE partition. kubespand reads from YAML config. | Low (~100 LOC) |

**Total glue layer: ~2,150 LOC** (including `agentconfig`, `identity`, `discovery` packages).

## Boundary Map: What Can Be Folded In Next

### Tier 1: Free to Embed (no glue needed)

Controllers that are pure COSI-to-COSI transformations or use standard Linux syscalls.
They just need their input resources to exist in COSI state.

| Controller | What it does | Input resources needed |
|-----------|-------------|----------------------|
| `network.AddressStatusController` | Watches kernel addresses via netlink, writes status | None (reads kernel) |
| `network.RouteStatusController` | Watches kernel routes via netlink, writes status | None (reads kernel) |
| `network.LinkStatusController` | Watches kernel links via netlink, writes status | None (reads kernel) |
| `network.NodeAddressController` | Computes node addresses from AddressStatus | `network.AddressStatus` |
| `network.StatusController` | Aggregates network readiness | Various network status resources |
| `network.ProbeController` | Runs network connectivity probes | `network.ProbeSpec` |
| `perf.StatsController` | Reads `/proc` stats | None |

**Why you'd want these**: `NodeAddressController` + `AddressStatusController` would let
kubespand dynamically track host network changes (currently NodeMetadataController reads
addresses once and assumes they're stable — the TODO in node_metadata.go). This replaces
the static address snapshot with live netlink monitoring.

**Glue cost: ~0 LOC.** These controllers read from the kernel or from COSI resources that
other controllers already produce.

### Tier 2: Cheap to Embed (small shim)

Controllers where upstream expects one or two COSI resources that kubespand would need
to inject (similar to how ConfigController already injects `kubespan.Config`).

| Controller | What it does | Shim needed | Glue cost |
|-----------|-------------|------------|-----------|
| `runtime.KernelModuleSpecController` | Loads kernel modules from KernelModuleConfig | Inject `KernelModuleConfig` with `["wireguard"]` | ~20 LOC |
| `runtime.KernelParamSpecController` | Applies sysctl from KernelParamConfig | Inject sysctl config for `rp_filter`, `src_valid_mark` | ~30 LOC |
| `network.HostnameSpecController` | Sets system hostname | Already have `HostnameStatus`; add `HostnameSpec` | ~20 LOC |
| `network.HardwareAddrController` | Detects primary MAC address | None needed if we keep our sysfs detection | 0 LOC |
| `time.SyncController` | NTP time sync | Inject time server config | ~30 LOC |
| More `secrets.*` controllers | Additional PKI (kubelet certs, etc) | Inject root secrets | ~50 LOC each |

**Why you'd want these**: Kernel module loading would replace manual `modprobe wireguard`.
Sysctl management would replace the `os.WriteFile("/proc/sys/...")` in
`WireguardLinkController`. These are small wins that reduce kubespand's imperative code.

### Tier 3: Moderate Glue (worth considering)

These controllers do useful things but need more substantial input injection or behavior
adaptation.

| Controller | What it does | Challenge | Glue cost |
|-----------|-------------|----------|-----------|
| `cluster.MemberController` | Produces `cluster.Member` from affiliates | Upstream reads from merged affiliates namespace | ~100 LOC |
| `cluster.AffiliateMergeController` | Merges raw → cluster namespace affiliates | kubespand currently skips this (single source). Adding it enables multi-source discovery. | ~50 LOC (mostly just registering it) |
| `k8s.NodeStatusController` | Watches K8s node, reports status | Needs kubeconfig injection | ~50 LOC |
| `k8s.NodeApplyController` | Applies labels/taints/annotations to K8s node | Needs kubeconfig + config injection | ~80 LOC |
| `k8s.NodeIPController` | Determines kubelet node IP | Needs `NodeIPConfig` injection | ~30 LOC |
| `network.DNSResolveCacheController` | Local DNS cache | Depends on CoreDNS fork (excluded from build) | **Blocked** |

### Tier 4: Expensive / Impractical to Embed

These controllers make assumptions that fundamentally conflict with running under another
Linux system.

| Controller | Why it's expensive | Core assumption |
|-----------|-------------------|----------------|
| **`block.*` (all 16)** | Manage disk partitions, mounts, volumes. Talos owns the entire disk layout (STATE, EPHEMERAL, boot partitions). NixOS owns its disks. | **PID 1 / full disk ownership** |
| **`config.AcquireController`** | Downloads machine config via platform metadata, maintenance mode, or siderolink. | **First-boot provisioning flow** |
| **`config.PersistenceController`** | Persists machine config to STATE partition. | **STATE partition** |
| **`cri.*` (all 5)** | Configure containerd/CRI-O runtime. NixOS manages its own container runtime. | **Container runtime ownership** |
| **`etcd.*` (all 5)** | Manage etcd lifecycle, PKI, membership. NixOS node is a worker, not running etcd. | **Control plane only** |
| **`files.EtcFileController`** | Generates `/etc/hosts`, `/etc/resolv.conf`, etc. NixOS manages these. | **`/etc` ownership** |
| **`files.CRI*` (3)** | Generate containerd config. NixOS manages containerd. | **CRI ownership** |
| **`k8s.KubeletServiceController`** | Manages kubelet as a process via cgroups/systemd. NixOS manages kubelet via systemd. | **Process management** |
| **`k8s.ControlPlane*` (8)** | Static pod management for API server, scheduler, controller-manager. Worker-only. | **Control plane only** |
| **`k8s.ManifestApplyController`** | Applies K8s manifests (needs etcd). | **etcd dependency** |
| **`k8s.StaticPod*` (3)** | Manages static pod lifecycle. | **Pod management** |
| **`runtime.ExtensionServiceController`** | Manages Talos system extensions as systemd-like services. | **Talos service manager** |
| **`runtime.WatchdogTimerController`** | Hardware watchdog management. | **PID 1** |
| **`runtime.KmsgLog*` (3)** | Kernel log management. | **PID 1** |
| **`runtime.OOMController`** | OOM score adjustment. | **PID 1 / cgroup root** |
| **`network.LinkSpecController`** | Full link management (bonds, bridges, VLANs, WG, physical). Depends on Talos udev, platform detection. | **Full network ownership** |
| **`network.Operator*` (4)** | DHCP, VIP, platform network operators. NixOS manages DHCP/IP assignment. | **Network ownership** |
| **`network.DNS*` (2)** | DNS cache, upstream forwarding. Depends on siderolabs/coredns fork. | **Build dependency blocked** |
| **`network.Platform*` (4)** | Cloud platform metadata (Hetzner, AWS, etc). | **Platform metadata** |
| **`network.EthernetSpec/Status`** | Ethernet configuration. Depends on siderolabs/ethtool fork. | **Build dependency blocked** |
| **`siderolink.*` (all 4)** | SideroLink management. Not relevant to kubespand. | **SideroLink-specific** |
| **`secrets.EtcdController`** | etcd PKI. Worker-only, no etcd. | **Control plane only** |
| **`secrets.Kubernetes*` (3)** | K8s control plane PKI. | **Control plane only** |

### Build Dependency Blockers

The `gazelle:exclude` directives in `third_party/go.MODULE.bazel` document what can't
currently compile:

| Excluded file | Depends on | Reason |
|--------------|-----------|--------|
| `dns_resolve_cache.go` | `io_etcd_go_etcd_api_v3` | Internal etcd API (go.mod replace) |
| `dns_upstream.go` | `io_etcd_go_etcd_api_v3` | Internal etcd API |
| `etcfile.go` | `containerd/containerd/v2` | CRI base runtime spec chain |
| `kubelet_service.go` | cgroup/systemd deps | Process management |
| `extension_service.go` | systemd deps | Talos service manager |
| `manifest_apply.go` | etcd | etcd client dependency |
| `secrets/etcd.go` | etcd | etcd PKI |
| `operator/vip.go` | etcd via ARP VIP | Virtual IP management |
| `ethernet_spec/status.go` | siderolabs/ethtool fork | go.mod replace directive |
| `operator_spec.go` | references excluded types | Transitive exclusion |

## The Actual Boundary

The boundary is **not** at the binary level. It cuts through subsystems:

```
┌────────────────────────────────────────────────────────────────┐
│                    FULLY EMBEDDABLE                             │
│                                                                │
│  KubeSpan (peer discovery, WG mesh, nftables, routing)         │
│  Cluster Discovery (discovery-client, affiliate management)    │
│  Secrets/PKI (API certs via trustd, cert SANs)                 │
│  KubePrism (TCP LB to apiserver)                               │
│  Network Spec Application (address, route, nftables, WG link)  │
│  Network Status Monitoring (netlink watchers)                  │
│  apid (Talos API server, subprocess)                           │
│                                                                │
├────────────────────────────────────────────────────────────────┤
│                    CHEAP SHIMS                                  │
│                                                                │
│  Kernel module loading (inject config resource)                │
│  Sysctl management (inject config resource)                   │
│  K8s node status/labels/taints (inject kubeconfig)             │
│  Additional PKI (kubelet certs, etc)                           │
│                                                                │
├────────────────────────────────────────────────────────────────┤
│              THE HARD CUT — HOST SYSTEM BOUNDARY               │
│                                                                │
│  Below here, Talos assumes it IS the operating system:         │
│                                                                │
│  ✗ Block devices / disk partitions (STATE, EPHEMERAL)          │
│  ✗ Container runtime (containerd) lifecycle                    │
│  ✗ Kubelet process management                                  │
│  ✗ etcd (control plane only)                                   │
│  ✗ /etc file generation                                        │
│  ✗ DHCP / platform network operators                           │
│  ✗ DNS cache (coredns fork dependency)                         │
│  ✗ System services (watchdog, OOM, kmsg)                       │
│  ✗ Machine config acquisition / persistence                    │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

## Glue Cost Summary

| What | Current glue | Hypothetical glue if expanded |
|------|-------------|------------------------------|
| **KubeSpan mesh** (current scope) | ~2,150 LOC | — |
| **+ Live network monitoring** | — | +0 LOC (free embed) |
| **+ Kernel module/sysctl** | — | +50 LOC |
| **+ K8s node management** | — | +160 LOC |
| **+ Member/affiliate merge** | — | +150 LOC |
| **+ Kubelet cert generation** | — | +100 LOC |
| **Total realistic ceiling** | — | ~2,600 LOC |
| **Everything below the hard cut** | — | Would require reimplementing Talos's OS layer (~10K+ LOC) |

## Key Insight

kubespand's current boundary is very close to the optimal embedding boundary. The "hard
cut" is where Talos starts assuming it IS the init system: disk ownership, container
runtime, kubelet management, `/etc` generation. Crossing that boundary doesn't mean
writing thin shims — it means reimplementing NixOS's job inside Go, which defeats the
purpose.

The remaining cheap wins are:
1. **Network status monitoring** (Tier 1) — replace static address snapshot with live
   netlink watchers. Cost: 0 LOC new glue, but enables dynamic address tracking.
2. **Kernel module + sysctl controllers** (Tier 2) — replace imperative
   `os.WriteFile("/proc/sys/...")` with declarative COSI resources. Cost: ~50 LOC.
3. **K8s node management** (Tier 3) — apply labels/taints to the K8s node representing
   this machine. Cost: ~160 LOC, but only useful if kubespand manages the node identity.

Everything else is either blocked by build dependencies (coredns fork, ethtool fork) or
would require kubespand to fight NixOS for system ownership.
