# Canary execution observations

## 2026-10-19 UTC: host preparation

- Host preparation PR #11012 merged as `18186dd063ef971587a5b677178a51369a64121b`.
- Registered the new DRBD schematic in Terraform state. The saved registration
  plan created only `talos_image_factory_schematic.drbd_canary`.
- The normal targeted machine-config plan was rejected: it included replacement
  of all six `null_resource.install_talos_kimsufi` instances because their old
  `schematic_id` triggers differ from the current shared schematic. Those
  installation scripts were not invoked.
- The generated first-worker configuration also differed from live Nebula
  extension configuration (`documents[3].configFiles[3].content`). That drift
  was excluded from the canary configuration.
- Prepared a temporary resource-only Terraform root using the existing `main`
  state backend and resource address. Its desired input preserves the live
  multi-document configuration and copies only the committed canary image,
  modules, and label. The saved plan updates only the selected
  `talos_machine_configuration_apply.kimsufi` instance. This does not reconcile
  the unrelated full-root drift; future full-root applies still need review.
- Checked both ClickHouse replicas using `system.replicas` and
  `system.replication_queue`: each had 14 replicated tables, two active replicas,
  no queued items/errors, and zero maximum delay. The operator's incomplete
  `hostsWithReplicaCaughtUp` status was not evidence of replica lag.
- First-worker SeaweedFS gate: 348 hosted volumes, none below their required
  copy count and none with only one copy. No volume repair/deletion was run.
- Switched `mcp-oauth-state-db` from instance 2 on the first worker to healthy
  instance 1 on the second worker; confirmed two healthy instances afterward.
- Matrix Synapse's bound claim uses `seaweedfs-ovh` RWX. An initial inference
  from a similarly named local PV was incorrect; it has no local-PVC dependency
  on the second canary worker.

Private machine configurations, Terraform plans/state, and raw logs stay outside
the repository. No Piraeus provisioning or relocation acceptance result is
established by these preparation observations.
