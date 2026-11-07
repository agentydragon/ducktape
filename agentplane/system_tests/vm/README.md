# System test VMs

This package owns the optional VM part of a system-test deployment:

- `infrastructure.py` installs the retained KubeVirt and Kyverno controllers.
- `runtime.py` and `pytest_plugin.py` attach VM lifecycle handling to the deployment fixture.
- `block_storage.py` provisions action-owned workspace volumes and detaches their loop devices
  after workload teardown.
- `guest_logs.py` and `trace_contract.py` collect and check guest evidence.
- `configuration.py` and `supervision.py` describe guest settings and drive supervision assertions.
- [`guest/`](guest/) builds the test guest, including systemd units and boot/shutdown evidence.

Only VM suites select the VM pytest plugin and runtime inputs. Pod and MCP suites use the common
deployment adapter without importing this runtime. The K3s suite macro remains in
[`k3s/defs.bzl`](../k3s/defs.bzl).

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
