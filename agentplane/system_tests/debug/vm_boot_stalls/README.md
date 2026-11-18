# VM boot stalls before userspace

- November 9 guest silence during early PCI/ACPI initialization. [Readiness failure](vm_boot_before_userspace_2026_11_09.md), [evidence index](vm_boot_before_userspace_2026_11_09.json)
- November 17 managed TCG timer recurrence, cleanup fence, and snapshot outcome. [Pre-Runtime stall](vm_runtime_connection_2026_11_17.md), [evidence index](vm_runtime_connection_2026_11_17.json)

Other failure boundaries include QEMU aborts, post-ACPI shutdown stalls, and guests losing progress after Runtime admission. [VM shutdown investigation](../vm_shutdown_stalls/README.md#identify-the-failing-boundary), [admitted-turn investigation](../vm_guest_progress_and_host_crashes_2026_11_12.md)
