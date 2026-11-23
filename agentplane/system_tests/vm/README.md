# System test VMs

This package owns the optional VM part of a system-test deployment:

- `infrastructure.py` installs the retained KubeVirt and Kyverno controllers.
- `runtime.py` attaches VM lifecycle handling to the deployment fixture; the shared
  [`deployment/pytest_plugin.py`](../deployment/pytest_plugin.py) selects it through `runtime_factory`.
- `block_storage.py` provisions action-owned workspace volumes and detaches their loop devices
  after workload teardown.
- `guest_logs.py` and `trace_contract.py` collect and check guest evidence.
- `configuration.py` and `supervision.py` describe guest settings and drive supervision assertions.
- [`guest/`](guest/) builds the test guest, including systemd units and boot/shutdown evidence.

Only VM suites select the VM runtime inputs, and `runtime_factory` returns this runtime only for a KubeVirt
fixture; Pod and MCP suites run without one. The K3s suite macro remains in [`k3s/defs.bzl`](../k3s/defs.bzl).

Raw-volume release requires workload Pods/VMs to be removed, or the owned K3s server and container
tasks to be stopped on failure. Kubelet can hold a block volume through another loop device after
userspace FD owners exit. Teardown verifies the original open backing inode and releases only loop
mappings whose kernel backing-device identity matches the still-open owned volume.

The [VM Sandboxes guide](../deployment/README.md#vm-sandboxes) describes the image layout, assertions,
boot evidence and teardown guarantees. Runnable tests stay under [`suites/`](../suites/), and VM
scenario assertions stay under [`scenarios/`](../scenarios/).

`vm-artifacts/` retains native persistent journals, CRI console logs, QEMU's raw serial files,
and launcher, relay, virtiofs and handler logs. Open descriptors preserve files after Pod
removal; native journal snapshots refresh during the test and after teardown. The guest
follows only its volatile journal to serial, covering early boot and the late switch away
from `/state`. Runtime already logs to `journal+console`. An observer starts at boot and waits
on a FIFO; ACPI wakes it to report PID 1 kernel state, descriptors, processes and pending jobs. See the active
[shutdown investigation](../debug/vm_shutdown_stalls/README.md) for reproduction evidence.

If a retained raw serial log records the exact guest shutdown request and no exact completion
proof follows within five seconds, `vm-artifacts/host-qemu/` retains at most two timestamped host
QEMU and read-only libvirt/QMP snapshots for that launcher. Each also retains the matching
launcher's Pod UID, deletion timestamp, and allowlisted compute/virtiofs container spec and
status, including restartable virtiofs initContainers, their initContainerStatuses,
restartPolicy, and running or terminated timestamps. Pod name and UID must match the serial
artifact before container details are recorded; environment variables and other containers are
omitted. Delayed samples also retain host virtiofsd process and thread `comm`, `status`,
`wchan`, `syscall`, and kernel `stack` for the exact launcher Pod UID cgroup. This `/proc`
scan omits command lines, limits matching processes and threads, and has a 0.75-second deadline
while reserving seven seconds for the existing QEMU/QMP probes. It marks missing daemons,
unreadable process data, and incomplete scans separately. Each sample has a ten-second budget;
the second starts at least two seconds after the first completes. QMP queries can wake QEMU's main
loop or vCPUs and may change what a later sample observes. The monitor stops with the VM artifact
capture.

The end-of-case VM report also samples launchers without proof, including cleanup blocked
at runner quiescence before ACPI. A `report` sample does not imply a shutdown request occurred.

When a VM Sandbox first reports Ready, the shared Sandbox driver reads the admitted launcher Pod
and its owning VMI, correlating their UIDs, namespace and KubeVirt labels before checking topology.
It requires each PVC-, Secret- or ConfigMap-backed VMI virtiofs filesystem to appear as a running,
Ready `initContainer` with `restartPolicy: Always`. It intersects volume sources with the VMI's
actual `virtiofs` devices, so block disks such as `/state/workspaces` and non-file-backed
ContainerPath daemons are excluded. The sanitized observation is retained at
`vm-artifacts/launcher-topology/<launcher-pod-uid>.json` on both pass and failure; API or
incarnation convergence failures are reported separately from an admitted-topology violation.

### Boot APIC delivery and EOI capture

Boot capture writes `vm-artifacts/boot-interrupts/<launcher-pod-uid>/` through
`undeclared_outputs_dir()`. It attaches as soon as the exact launcher's QEMU
process is found with the exact libvirt domain name in its arguments, excluding
helpers and capability probes, before the first QMP round trip. It stops when the serial
console reaches `systemd[1]`, capture ends, or its existing 300-second/150-sample
budget expires. The observer never pauses or resumes the guest. Discovery can
miss early boot: activation timestamps, the serial log and initial APIC register
writes determine which part of boot is covered.

The pinned launcher's APIC trace sites use DTrace/SystemTap and are unavailable
through QMP's trace API. The collector reads SDT notes from the running executable
and attaches kernel uprobes with their argument operands and semaphore offsets.
An exported `apic_get_interrupt` return probe records the vector accepted by a
vCPU (`-1` means none). `apic_register_write` records register index and value;
index `0x0b` is EOI. `apic_local_deliver` records the LVT **index** and delivery
mode, not the interrupt vector or destination CPU. Other APIC/IOAPIC sites retain
routing and level-triggered EOI transitions. `summary.json` maps each numbered
site to its name, arguments and file offset. Trace lines include monotonic host
time, thread name and TID; the host snapshot maps TIDs to vCPUs.

For the pinned `qemu-kvm-10.1.0-10.el9` executable, `apic-probes.json` adds five
verified instruction sites. `accepted` observes the APIC pointer, IDs, vector,
IRR timer word and all eight ISR words after `apic_get_interrupt` moves an
accepted vector into service. `eoi_before` and `eoi_after` sample the same APIC
state around the xAPIC EOI call. `guest_before` and `guest_after` sample the vCPU
index, APIC pointer/ID, guest EIP, flags and IDT base around QEMU's TCG hardware
interrupt dispatch call. These sites distinguish QEMU acceptance, entry into and
return from the emulator's interrupt helper, and APIC acknowledgement. A returned
helper with a changed EIP does not prove that Linux executed its handler. The
profile is checked against the OCI executable's SHA-256, GNU build ID, file offsets
and instruction bytes at build time and against the running executable before
attachment. Missing or mismatched sites are recorded as coverage gaps; the SDT
sites and monitor sampling remain available.

Each launcher owns a private tracefs instance and requests the kernel PID/fork
filter. Because uprobes attach to an executable inode shared by launchers, the
reader also verifies each TID against that QEMU's task directory, including new
vCPU threads. Other processes' events are counted and excluded from the output
budget. The noisy raw `ioapic_set_irq` site is omitted; delivery, register writes
and EOI sites remain enabled. The ring uses 128 KiB per host CPU and is drained
continuously. The last 16 MiB of complete trace lines are retained in
`interrupts.log`: 12 MiB for CPU 0, 2 MiB for CPU 1 and 2 MiB for other QEMU
threads. If tracefs prints `<...>` instead of a thread name, the collector uses
that owned TID's known name or `/proc/<qemu>/task/<tid>/comm` to select the reserve.
An unavailable comm adds an explicit coverage gap and leaves the record in the
other-thread reserve.
`summary.json` records lines and bytes seen, retained counts, overwritten
lines by group, actual enabled sites, setup/cleanup errors and kernel per-CPU
overrun/drop counters. An overwritten interval cannot support an absence claim.
Only this collector's events and
instance are removed at exit. Existing emulator logging is untouched. The
collector also samples `info lapic` for CPUs 0 and 1 and `info pic`, retaining
command times and individual errors in `timeline.jsonl`; the two-second interval
starts after each sample completes. Missing probe support leaves this monitor
timeline available.

Compare accepted vectors, EOI writes and subsequent in-service/priority state.
An absent acknowledgement is meaningful only for a covered interval with the
required probes and no reported loss; it must also account for any paravirtual
acknowledgement path. Late attachment, missing sites, kernel loss or overwritten history leave
that conclusion unproven. Both uprobes and monitor queries perturb timing and
are diagnostics, never readiness predicates. Early serial output is retained
independently. No diagnostic is printed to stdout, and collector errors never
replace a scenario failure.

Both collectors share serialized tracefs mount setup. Their event groups and
instances are separate (`agentplane_boot_<pod-uid>` and `ap_iotlb_<random-id>`),
and cleanup disables only the owning instance and events. The APIC output cap
is independent of the IOTLB caps below; neither observer extends the other
observer's 300-second window. Together they use at most four APIC rings of
128 KiB per host CPU plus four IOTLB ring pairs of at most 2 MiB each.

### QEMU IOTLB assertion capture

`host_qemu.capture` starts an IOTLB observer at exact-domain discovery, except in
explicitly passive virtiofsd mode. It matches both `-name guest=<namespace>_<vmi>`
and the launcher Pod UID in the host cgroup. It verifies the executable GNU
build-id, SHA-256, ELF file offsets and instruction bytes before attaching.
`qemu_iotlb_debug` fetches the core and supplementary DWZ debuginfo through Bazel,
checks their build-ids and `.gnu_debugaltlink`, and verifies the recipes against
the pinned amd64 launcher image. Runtime needs only the generated manifest;
there are no runtime downloads or symbol guesses.

The recipes in `iotlb-probes.json` are specific to `qemu-kvm-10.1.0-10.el9`,
build-id `3bdb548b9f6ed0a112c7ef0b9afc123c4a8bdf86`. Their register/stack locations
and structure offsets come from that exact build's DWARF and disassembly:

| Probe                                          | ELF address                                    | Records                                                                                                                                                   |
| ---------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fill_io`                                      | `0x70685f`                                     | I/O/ROMD fill preparation: CPU, MMU index, virtual/physical page, attributes, translated offset, section, owning FlatView and dispatch, CPU AddressSpaces |
| `iommu_target`                                 | `0x5f522d`                                     | IOMMU region, source dispatch and replacement target AddressSpace/FlatView/dispatch                                                                       |
| `access_0`–`access_3`                          | `0x7107e9`, `0x710f2f`, `0x71164d`, `0x711ed8` | MMIO callers before `iotlb_to_section`: packed TLB value, full-entry pointer, physical address, attributes, virtual address and MMU index                 |
| `check`, `failure`                             | `0x5f5565`, `0x5f5599`                         | Masked section index, selected dispatch, section count/base and CPU AddressSpaces, before register destruction                                            |
| `publish`                                      | `0x5ea15e`                                     | AddressSpace, old/new FlatViews and dispatches at `current_map` publication                                                                               |
| `commit_queued`, `commit_direct`, `commit_cpu` | `0x5f5972`, `0x5f5980`, `0x603bf4`             | TCG listener's queued/direct flush and CPU callback, including AddressSpace identity                                                                      |
| `flush_begin`, `flush_done`                    | `0x704e40`, `0x705029`                         | CPU and requested/completed MMU flush masks                                                                                                               |

Each launcher has private trace instances with explicit `common_pid` filters for
at most 64 QEMU threads. Thread discovery refreshes every collector turn (at
most a 100 ms idle wait); the interval before a new thread's filter is installed
is a recorded coverage gap. Linux 5.15's uprobe `traceoff` trigger affects the
top-level array and its snapshot trigger rejects private instances, so neither
is used. A separate failure-only ring protects the assertion record from normal
traffic. The collector stops both instances after reading that record; the
failure-to-stop interval can lose history and is recorded. Only this observer's
instances and probe definitions are removed on teardown.

Artifacts are under the test's undeclared-output root at
`vm-artifacts/host-qemu/<launcher>/iotlb/`: `rolling.log` (1 MiB), `failure.log`
(256 KiB, assertion record plus recent complete records), and `metadata.json`
(64 KiB). Per-launcher output is below the 1.5 MiB ceiling; at most four launchers
plus the bounded `iotlb-observer.json` fit the 6 MiB total cap. Kernel rings total
at most 2 MiB per launcher. Discovery lasts at most 600 seconds and each attached
capture lasts at most 300 seconds or until process exit, assertion or teardown.
No new QMP or Kubernetes API traffic is added. Uprobes perturb execution; a pass
cannot establish that the underlying race has been fixed.

Use trace `mono` timestamps and host TIDs to correlate events: sequence numbers
are collector read order across the two rings, not execution order. The section
stride is 64 bytes, `CPUTLBEntryFull.xlat_section` is at offset 0, `phys_addr` at 8,
and attributes at 16. Compare the failed access's packed index/attributes with a
retained candidate fill's section/dispatch and the intervening map/flush history.
An IOMMU target change to a different AddressSpace followed by a lookup in the
wrong dispatch supports the upstream AddressSpace candidate. A map replacement
without the required TCG flush supports stale-map invalidation instead. Neither
is established by the assertion alone. `fill_io` precedes the final TLB store;
victim TLB copies and multiple candidate fills can make exact provenance
ambiguous. RAM fills, individual-page invalidations and arbitrary TLB writes are
not traced, so this is not a complete TLB journal.

Metadata records attachment/end times, stop reason, overwritten history, kernel
overruns/drops, malformed/foreign records, invalid required pointer/count values,
thread changes, setup/cleanup errors and discovery caps. Scalar tracefs fetches
can return zero on a memory-read fault; zero or otherwise ambiguous values are
unusable evidence, not proof of a zero map. Read `as1`/`fv1` only when `num_ases`
permits them; null old maps during initial publication are expected. Missing
fills, overwritten history, pre-attachment activity, new-thread gaps or fetch
faults can leave the recurrence undecidable and must accompany any conclusion.
