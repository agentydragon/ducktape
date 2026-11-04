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
