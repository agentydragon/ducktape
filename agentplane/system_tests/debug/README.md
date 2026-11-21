# System-test investigations

Dated measurements, root-cause notes and raw evidence behind the system-test fixtures. Each note records what was observed on its date and invocation; the [system tests README](../README.md) and the BUILD files own current behavior. Folders carry their own `README.md` index.

## `debug/`

- [`kine_compaction_timeout_2026_11_18.md`](kine_compaction_timeout_2026_11_18.md): K3s embedded Kine (SQLite) compaction transaction failure in `test_vm`; source defect confirmed, this run's cancellation not.
- [`kvm_docker_recycling.md`](kvm_docker_recycling.md): Docker runner recycling on the existing KVM pool; retention rejected under the 2 GB limit, higher-cap follow-up.
- [`mcp_case_overlap_2026_11_12.md`](mcp_case_overlap_2026_11_12.md) ([evidence](mcp_case_overlap_2026_11_12.json)): case-owned OAuth linkage for MCP Actions; shorter case envelopes, no established CI or compute saving.
- [`setup_and_case_shortening_2026_11_12.md`](setup_and_case_shortening_2026_11_12.md): shortening setup and cases; the image split and the image-overlap trial did not establish a faster gate.
- [`test_vm_trust_manager_issuance_failure_2026_11_14.md`](test_vm_trust_manager_issuance_failure_2026_11_14.md): `test_vm` setup failure, trust-manager `Certificate` issuance (`CSR not signed by referenced private key`); no deterministic repository fix supported.
- [`usable_import_transactions_2026_11_12.md`](usable_import_transactions_2026_11_12.md): skip OCI import transactions when containerd already holds the exact usable image.
- [`vm_cpu_budgets.md`](vm_cpu_budgets.md): VM CPU budget at one, two and four emulated vCPUs per guest; two chosen for the fixture.
- [`vm_guest_progress_and_host_crashes_2026_11_12.md`](vm_guest_progress_and_host_crashes_2026_11_12.md): silent admitted-turn guest stalls kept separate from host crashes.
- [`codex_tool_oom/`](codex_tool_oom/README.md): Codex tool OOM in the VM fixture; executor continuity and the exec-server's shared cgroup.
- [`vm_boot_stalls/`](vm_boot_stalls/README.md): VM guest silence and timer stalls before userspace.
- [`vm_kvm_capacity/`](vm_kvm_capacity/README.md): VM test capacity on the existing KVM pool.
- [`vm_shutdown_stalls/`](vm_shutdown_stalls/README.md): what blocks a Ready VM guest between ACPI and orderly powerdown.

## `k3s/debug/`

- [`cold_image_import/`](../k3s/debug/cold_image_import/README.md): cold guest-root image import profile, Python stream buffering against CPU in the `ctr` client and containerd.
- [`process_trace_collection.md`](../k3s/debug/process_trace_collection.md): fixture-owned OTLP receiver and `process-spans.jsonl`; RBE proof and the bundled containerd tracing limit.
- [`pyc_precompilation.md`](../k3s/debug/pyc_precompilation.md): RBE bytecode precompilation (`include_pyc`) probes and a full `test_pod` comparison.
- [`retained_baseline/`](../k3s/debug/retained_baseline/README.md): retained K3s test baseline; hosted RBE persistence, reuse census, reset and KubeVirt handler findings.
- [`stack_timings.md`](../k3s/debug/stack_timings.md): measurements behind the k3s stack setup choices.
- [`vm_shutdown.md`](../k3s/debug/vm_shutdown.md): VM shutdown probe; the CI guest ignored KubeVirt's graceful shutdown until it loaded the `button` and `evdev` modules and ran `acpid`.
