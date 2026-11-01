# TODO — Agentplane system tests

- [ ] The VM test and production/staging runner VMs both declare virtio-blk and ext4 for the
      workspace. Compare their backing storage and guest boot, format, and mount behavior, then
      decide whether to align any remaining differences. Performance impact is unknown; benchmark
      equivalent workloads before choosing a direction.
