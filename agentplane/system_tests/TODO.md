# TODO — Agentplane system tests

- [ ] Diagnose recurring MCP gateway-crash Lease-owner assertion failures without weakening the
      abrupt-crash/outage checks. Capture container restarts and Lease transitions between process
      kill and scale-down; see <debug/gateway_crash_lease_release.md>.
- [ ] The VM test and production/staging runner VMs both declare virtio-blk and ext4 for the
      workspace. Compare their backing storage and guest boot, format, and mount behavior, then
      decide whether to align any remaining differences. Performance impact is unknown; benchmark
      equivalent workloads before choosing a direction.
