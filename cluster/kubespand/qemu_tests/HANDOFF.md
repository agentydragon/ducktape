# QEMU Tests Restructure — Agent Handoff

## Overarching Goal

Get kubespand to work in a double-NAT topology: A → NAT → B → NAT → C, where all
nodes discover each other and establish WireGuard tunnels. The kubespand doublenat test
currently times out on peer discovery.

Before debugging kubespand further, we need to answer: **does upstream Talos KubeSpan
work through double NAT at all?** The Talos diagnostic test (`talos/talos_test.go`)
boots real Talos VMs in the same double-NAT QEMU topology to find out. If Talos itself
can't do it, we know it's a protocol limitation and can stop trying to make kubespand
do it.

## Immediate Goal

Restructure the kubespand QEMU integration tests from a single monolithic test file
into separate test targets for parallel execution, and get the Talos diagnostic test
working end-to-end.

## Status

### Working

- **nft test**: PASSES on RBE. Standalone init binary + test.
- **Test helpers**: `qemu_tests` package with VM management, event channels, artifact saving.
- **VM init binaries**: discovery, router, kubespan, doublenat — all build and the Alpine
  VMs boot correctly.
- **Talos test infrastructure**: Pre-built nocloud qcow2 disk image boots, CIDATA config
  injection works, apid comes up, talosctl connects.

### Fixed

- **kubespan/doublenat tests**: Peer discovery timeout was caused by a race condition in
  `DiscoveryController` (introduced by `08bcc3b "use upstream Talos LocalAffiliateController"`).
  The controller skipped discovery manager creation when `LocalAffiliateController` hadn't
  produced its output yet. Fixed by restructuring the reconcile loop to start the discovery
  manager immediately and defer only the local affiliate publish step.
- **kubespan/doublenat tests**: Migrated from `time.Sleep` to event-driven `RequireEvent`
  pattern for infrastructure VM readiness. Added `t.Cleanup` with `KillAndWait` + `SaveLogs`
  so logs are preserved even on `Fatalf`.

### Needs Fixing

- **Talos test**: VPS can't reach discovery service — the QEMU user-mode mgmt NIC (eth1)
  gets DHCP default route that overrides eth0's subnet route. **Fix in progress**: testdata
  configs regenerated with `eth1: dhcp: false` for VPS. NAT1/NAT2 don't have mgmt NICs so
  they're fine.

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

## Observed Timings (RBE, Firecracker, TCG)

- Alpine VM boot (discovery/router): ~5s
- Talos qcow2 VM boot to apid healthy: ~33-64s
- Talos config acquisition from CIDATA: ~15-29s
- `xz` decompress 188MB qcow2: ~5s
- `cp` 1.2GB qcow2 per VM: ~3s

## Next Steps

1. Run Talos test with regenerated testdata (eth1 dhcp:false fix).
2. Once Talos API connects, check if KubeSpan peers are discovered through double NAT.
3. Run kubespan/doublenat tests on RBE to verify the DiscoveryController fix resolves
   the peer discovery timeout.
