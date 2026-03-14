# QEMU Tests Restructure — Agent Handoff

## Overarching Goal

Get kubespand to work in a double-NAT topology: A → NAT → B → NAT → C, where all
nodes discover each other and establish WireGuard tunnels.

## Immediate Goal

Restructure the kubespand QEMU integration tests from a single monolithic test file
into separate test targets for parallel execution, and get all topologies passing.

## Status

### All Passing (RBE)

- **nft test**: `//cluster/kubespand/qemu_tests/nft:nft_test` — PASSES (~8s)
- **kubespan test**: `//cluster/kubespand/qemu_tests/kubespan:kubespan_test` — PASSES (~44s)
  - TestFlat, TestCrossSubnet, TestDiscoveryOnly all pass
- **doublenat test**: `//cluster/kubespand/qemu_tests/doublenat:doublenat_test` — PASSES (~117s)
  - TestDoubleNAT passes (VPS↔NAT1 and VPS↔NAT2 probes succeed)

### Blocked (External)

- **talos test**: `//cluster/kubespand/qemu_tests/talos:talos_test` — blocked by 503 from
  `factory.talos.dev` (Talos image download). Not a code issue; retry when service recovers.

## Bugs Fixed

### 1. YAML `omitempty` causing 0 KubeSpan endpoints (ROOT CAUSE of doublenat failure)

**Symptom**: All kubespand nodes in the doublenat topology published 0 KubeSpan endpoints
to the discovery service. Peers discovered each other but had no endpoints to connect to.

**Root cause**: `agentconfig.go` YAML tags for slice fields lacked `omitempty`:

```go
// BEFORE (BUG):
EndpointFilters []string `yaml:"endpoint_filters"`

// AFTER (FIX):
EndpointFilters []string `yaml:"endpoint_filters,omitempty"`
```

**Data flow**: nil `EndpointFilters` → YAML marshal produces `endpoint_filters: []` →
YAML unmarshal produces `[]string{}` (non-nil empty) → passes `!= nil` check in upstream
Talos `LocalAffiliateController` → `FilterIPs(ips, []string{})` returns empty → 0 endpoints.

The fix was adding `omitempty` to `EndpointFilters`, `ExtraEndpoints`, and
`ExcludeAdvertisedNetworks` YAML tags. This preserves nil semantics through the
YAML round-trip that happens in `kubespanlib.StartKubespand()`.

### 2. Reverse path filtering on non-Talos hosts

**Symptom**: Decrypted WireGuard packets dropped on hosts with `rp_filter=1` or `2`
(NixOS, Ubuntu defaults via systemd sysctl.d).

**Fix** (`wireguard_link.go`): Set `rp_filter=0` on both `conf/<iface>/rp_filter` AND
`conf/all/rp_filter` (effective = max of both). Also enable `src_valid_mark=1` for
fwmark-based RPF as recommended by WireGuard docs.

### 3. Discovery controller race condition (previous session)

**Fix**: Restructured `DiscoveryController` reconcile loop to start the discovery manager
immediately and defer only the local affiliate publish step.

### 4. Port mismatch in doublenat VMs (previous session)

**Fix**: Set all WireGuard listen ports to 51820.

## Architecture

```text
kubespand/qemu_tests/
  events.go                    # Event types (shared by init binaries + tests)
  helpers.go                   # VM, BootVM, StartVM, McastNIC, KillAndWait, etc.
  BUILD.bazel                  # go_library + vmlinuz/modules genrules
  vms/
    initlib/                   # Shared PID-1 helpers (EmitEvent, MustRun, etc.)
    kubespanlib/               # Shared kubespand helpers (config, probes, peer wait)
    discovery/                 # Discovery VM: init + initramfs
    router/                    # NAT router VM: init + initramfs
    kubespan/                  # 2-node KubeSpan VM: init + initramfs
    doublenat/                 # 3-node double-NAT KubeSpan VM: init + initramfs
  nft/                         # TestNftSmoke (init + initramfs + test, all inline)
  kubespan/                    # TestFlat, TestCrossSubnet, TestDiscoveryOnly
  doublenat/                   # TestDoubleNAT
  talos/                       # TestTalosKubeSpanDoubleNAT
    testdata/                  # Pre-generated Talos machine configs + talosconfig
```

## Key Design Decisions

- **Separate go_test targets per test group** for Bazel parallelism.
- **Separate init binaries per VM role** — each VM type has its own `package main`.
  Shared code in `initlib` (PID-1 basics) and `kubespanlib` (kubespand startup + probes).
- **Event channel on VM struct** — `VM.Events` is a buffered channel. Tests use
  `select` over multiple VM channels for fail-fast signaling instead of `time.Sleep`.
- **Pre-generated Talos configs** committed as testdata — avoids runtime `talosctl gen`
  dependency and saves ~3s per test run.
- **nocloud qcow2 disk image** from Talos Image Factory — boots directly without
  install+reboot cycle. Each VM gets a `cp` of the base image (no COW overlays since
  `qemu-img` isn't in the RBE image).

## Gotchas Found

### talosctl flag ordering

Flags like `--nodes`, `--endpoints`, `--insecure` are **per-subcommand**, not global.
They must come AFTER the subcommand name:

```bash
talosctl version --nodes 192.168.50.2 --endpoints 127.0.0.1:12345 --talosconfig ./talosconfig
```

NOT:

```bash
talosctl --nodes 192.168.50.2 version  # "unknown flag: --nodes"
```

### talosctl --nodes vs --endpoints

- `--endpoints`: transport address (where to actually connect, can include port)
- `--nodes`: target node identity (just IP, no port — apid uses this for routing)
  Using `--nodes 127.0.0.1:12345` gives "invalid target" error.

### talosctl --insecure

Only for maintenance mode (pre-config). Once a node has a config, apid requires mTLS
client certs. `--insecure` skips sending client certs → "tls: certificate required".

### Talos apid TLS SANs

apid's server cert SANs come from the node's detected IPs. When connecting via localhost
port forward (127.0.0.1), the cert won't match unless `machine.certSANs: ["127.0.0.1"]`
is in the machine config.

### QEMU user-mode NIC steals default route

VPS has two NICs: eth0 (mcast bridge, static 192.168.50.2/24) and eth1 (user-mode,
DHCP 10.0.2.15/24 with default route). The DHCP default route on eth1 prevents eth0
from reaching other hosts on 192.168.50.0/24 directly. Fix: set `eth1: dhcp: false`
in the Talos config.

### Talos KSPP kernel parameters

Talos requires `slab_nomerge` and `pti=on` on the kernel cmdline. Without them, the
`systemRequirements` phase fails and boot stalls. (Only relevant for kernel+initramfs
boot, not for disk image boot.)

### RBE PATH doesn't include /usr/sbin

`mkfs.vfat` is at `/usr/sbin/mkfs.vfat`, `mcopy` at `/usr/bin/mcopy`. Use full paths
in test code since Bazel's sandbox PATH is minimal.

### Talos workers need controlplane trustd

Worker nodes' apid waits for "api certificates" which are issued by trustd on the
controlplane. Making all nodes workers causes apid to never start. At least one node
must be controlplane.

### YAML nil vs empty slice semantics

Go's `yaml.v3` marshals nil slices as `[]` (empty sequence) without `omitempty`.
Unmarshaling `[]` produces `[]string{}` (non-nil empty), not nil. This matters when
upstream code checks `!= nil` to decide whether to apply filtering. Always use
`omitempty` on optional slice YAML tags to preserve nil semantics through round-trips.

## Observed Timings (RBE, Firecracker, TCG)

- Alpine VM boot (discovery/router): ~5s
- Talos qcow2 VM boot to apid healthy: ~33-64s
- Talos config acquisition from CIDATA: ~15-29s
- `xz` decompress 188MB qcow2: ~5s
- `cp` 1.2GB qcow2 per VM: ~3s

## Next Steps

1. Retry Talos test once `factory.talos.dev` recovers from 503.
2. Consider adding the YAML omitempty gotcha to kubespand's AGENTS.md.
