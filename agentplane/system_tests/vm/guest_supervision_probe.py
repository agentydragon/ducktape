"""Probe the real guest environment from an unprivileged harness command."""

from __future__ import annotations

import re

VM_SUPERVISION_PROBE = r"""set -eu
printf 'VM_UID %s\n' "$(id -u)"
printf 'VM_PID1 %s\n' "$(cat /proc/1/comm)"
printf 'VM_CGROUP %s\n' "$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)"
if systemctl_output=$(/usr/bin/systemctl --no-ask-password restart agentplane-cgroup-observer.service 2>&1); then
    echo VM_SYSTEMCTL_UNEXPECTED_SUCCESS
    exit 23
fi
printf 'VM_SYSTEMCTL_OUTPUT %s\n' "$systemctl_output"
"""


def assert_native_service_cgroup(cgroup: str, slice_name: str) -> None:
    prefix = f"/agentplane.slice/{slice_name}/agentplane-native-"
    assert cgroup.startswith(prefix), cgroup
    unit = cgroup.rsplit("/", 1)[-1]
    assert re.fullmatch(
        r"agentplane-native-.+-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}\.service",
        unit,
    ), cgroup


def assert_vm_supervision_probe(output: str, *, slice_name: str) -> None:
    assert "VM_UID 107" in output, output
    assert "VM_PID1 systemd" in output, output
    (cgroup_line,) = [line for line in output.splitlines() if line.startswith("VM_CGROUP ")]
    cgroup = cgroup_line.removeprefix("VM_CGROUP ")
    assert_native_service_cgroup(cgroup, slice_name)
    assert "VM_SYSTEMCTL_UNEXPECTED_SUCCESS" not in output, output
    systemctl_denial = re.search(r"authentication|authorization|not authorized|access denied", output, re.IGNORECASE)
    assert systemctl_denial is not None, output
