"""The shared guest configuration used by the VM fixture and its assertions."""

from agentplane.runner.configuration import GuestResources, ResourceLimits

SUPERVISION_TEMPLATE = "runner-vm-supervision"
SUPERVISION_KERNEL_ARG = "agentplane.supervision-checks=1"
OOM_TEMPLATE = "runner-vm-codex-oom"

_MIB = 1024 * 1024
OOM_EXECUTOR_MEMORY_BYTES = 96 * _MIB
# The test guest gets a smaller work budget than staging while keeping enough room for the
# harnesses exercised by the VM system tests.
RESOURCES = GuestResources(
    guest_memory_bytes=1536 * _MIB,
    guest_vcpu_count=1,
    os_reserve_bytes=256 * _MIB,
    service_memory_high_bytes=1024 * _MIB,
    service_memory_max_bytes=1280 * _MIB,
    service_cpu_quota_micros=100_000,
    service_pids_max=1024,
    aggregate=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=768 * _MIB,
        pids_max=768,
        read_bps=50 * _MIB,
        write_bps=20 * _MIB,
        read_iops=1000,
        write_iops=500,
    ),
    process=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=512 * _MIB,
        pids_max=512,
        read_bps=50 * _MIB,
        write_bps=20 * _MIB,
        read_iops=1000,
        write_iops=500,
    ),
    executor=ResourceLimits(
        cpu_quota_micros=100_000,
        memory_max_bytes=256 * _MIB,
        pids_max=128,
        read_bps=50 * _MIB,
        write_bps=20 * _MIB,
        read_iops=1000,
        write_iops=500,
    ),
)
