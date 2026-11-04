# Firecracker system test on the wyrm2 BuildBuddy VM

This follows the [launcher proof](2026_11_03_firecracker_environment.md) with an app-integrated,
disposable k3s acceptance target: `//agentplane/system_tests/suites:test_firecracker`.
The implementation branch was initially rebased onto `devel` commit `e397149010`, then onto
`194ba5aa0d` for CRI image cleanup, `d2247659d0` for the managed worker keyring policy,
and `3473e3fd82` for the landed OCI descriptor API and current Session command clients.

## Execution path

The existing `buildbuddy-executor/buildbuddy-kvm-worker` KubeVirt VM on wyrm2 serves the
`wyrm2-kvm-vm` self-hosted BuildBuddy pool. The test action uses Docker inside that VM, starts
k3s there, and asks the deployed Agentplane app to create a Firecracker environment. The real
controller creates the launcher Pod, cert-manager issues its Runtime certificate, and the real
Gateway connects to its guest Runtime. A real Codex harness executes tools; the upstream model
is scripted. Build actions stay on ordinary RBE; only the TestRunner requires the KVM pool.

The fixture imports the production launcher OCI image, production EROFS runner disk, and a
digest-pinned production NixOS boot image. KubeVirt's device plugin advertises the KVM resource
inside k3s. State and workspace use ext4 raw files on filesystem PVCs. The test disables retained
executor snapshots and runner recycling. A local Bazel client fetches the private boot image;
the registry credential stays local and is not forwarded to a hosted Bazel runner or test action.

The acceptance scenario checks guest PID 1, separate mounted filesystems, absence of a projected
ServiceAccount token in the guest, and an authenticated harness/model/tool turn. It then suspends
through the app, verifies the launcher's checkpointed state, resumes through the app, and checks
the same Pod, kernel boot ID, Runtime boot ID, native process ID and native session ID. A child
created only before suspension must retain its PID, start ticks and random RAM marker, and must
produce fresh counter increments after restoration. No command recreates that child on resume.

This bare-k3s fixture omits the unsupported CiliumNetworkPolicy and does not prove Cilium
enforcement. It covers one suspend/resume, without rotating TLS while suspended. The primitive
launcher proof separately exercised guest packet filtering and credential refresh.

## Worker refresh and KVM prerequisite

The worker's authoritative ConfigMap already specified `docker_cap_add: ALL`; the existing guest
still had the older three-capability configuration. After observing no action containers, the
executor service was stopped and the disposable VMI restarted. VM, ConfigMap and Secret objects
were preserved; the VM has no PVC-backed disks. Cloud-init completed, the new guest loaded `ALL`,
and `/dev/kvm` plus `/dev/net/tun` were present.

| Identity               | Value                                  |
| ---------------------- | -------------------------------------- |
| VM UID                 | `190a0589-2961-4d93-b528-f2857aee4ce2` |
| Old VMI UID            | `81f77e2a-7e6f-4a7c-bd5e-8f1387bf340c` |
| New VMI UID            | `e7d7ec29-c344-496b-a65d-e54942cc78ab` |
| New launcher Pod UID   | `e1ffe168-e99b-4073-a728-47f6c4da3d74` |
| BuildBuddy worker host | `626e8370-6a87-4caf-85db-63a5af6dfa17` |
| Executor ID            | `1ddcd7b0-11fd-43a9-8189-4df4eefea364` |

The uncached [`kvm_vm_test` invocation](https://app.buildbuddy.io/invocation/9419f30b-9b1e-433b-aba0-40aed693b2b9)
passed on that worker, creating a VM and vCPU through KVM API 12. It queued at 03:44:19 UTC,
started on the worker at 03:53:12 UTC, and executed for 9.645 seconds. Queue time is not guest
startup time. An independent KubeVirt suite occupied the single-slot pool; it was not cancelled
or changed by this work, and its later cancellation is not a successful full-suite result.

## Boot artifact

`nix build .#agentplane-environment-firecracker-image` at source commit
`ff14e8bd057d51591be67f5d94a085bb5f3f6f91` reused the accepted production derivation in 19 seconds.
This is cache reuse, not a cold-build measurement. The archive is 2,266,443,551 bytes with SHA-256
`a0334c39979b4766cdd8276ca50c7d6ca4cafac9bf5071099b0493a5e6e872e0`.

It was published to the existing workflow-owned Forgejo repository under the nondeployable tag
`spike-firecracker-ff14e8bd057d51591be67f5d94a085bb5f3f6f91`, manifest digest
`sha256:f1465053413c073aa5aa9c75d4c3e9fa6225021b94f48cc8cceb2a30271a05e1`.
The boot layout is a declared Bazel input; the test does not read this host's Nix store. Registry
credentials stay in a temporary Docker config on the Bazel client, outside test runfiles.

## Checks

- [Changed fixture and scenario libraries](https://app.buildbuddy.io/invocation/87ff4007-3d4b-4139-bb6d-eb3eb22ffadc):
  explicit builds of all six changed Python libraries passed, including their type/lint aspects.
- [Scenario type/lint build](https://app.buildbuddy.io/invocation/ffcaeb7f-9cdf-47df-b5e6-3e383da9d8d5): passed.
- [Fixture determinism](https://app.buildbuddy.io/invocation/03325758-21c6-4119-b1ca-c24f77bdcf36): passed.
- [Fixture render and retained guest logs](https://app.buildbuddy.io/invocation/7ac174c0-398d-45e7-b9f5-e0cb752977ec): both passed.
- [CI hash-generation probe](https://app.buildbuddy.io/invocation/f501ac71-adc9-4635-961e-2afe36623bbd):
  ordinary `bazel-diff generate-hashes` completed without Forgejo credentials on the hosted runner,
  producing an 8,280,714-byte hash file. Both private-image input targets are tagged `manual`.
- [Post-rebase fixture checks](https://app.buildbuddy.io/invocation/86ae21db-00f3-499c-bc70-c2e4d9330670):
  render, determinism, retained logs and all six changed library aspects passed.
- Full Firecracker acceptance: setup attempts below; no deployed-system pass claimed yet.

## Acceptance attempts

The [first app-integrated run](https://app.buildbuddy.io/invocation/d7d2bbbe-5321-4f8e-8f1d-ca5b729baef1)
reached the existing KVM worker and started k3s, but failed during cert-manager readiness, before
creating a Sandbox. Nested containerd reported `unable to join session keyring: unable to create
session key: operation not permitted` for every infrastructure Pod. This was a container startup
failure rather than a certificate timeout or a Firecracker boot failure.

The outer [Docker default seccomp profile](https://docs.docker.com/engine/security/seccomp/)
blocks keyring syscalls. That attempt set containerd's documented
[`NoNewKeyring` runc option](https://github.com/containerd/containerd/blob/release/2.1/docs/cri/config.md)
so nested test containers inherited the action's session keyring. That temporary KVM-only drop-in
was subsequently removed in favor of the managed worker policy described below.

The [next scheduling attempt](https://app.buildbuddy.io/invocation/1028f860-0bc5-44bb-8720-10d2fee5773d)
executed no tests: the scheduler reported no registered executors in `wyrm2-kvm-vm`. Independently
of this test, the VMI was deleted at 04:20:10 UTC and replaced at 04:20:24 UTC by VMI
`753a973e-5f9d-4e59-a258-244567105d98`, launcher Pod UID
`4334a239-0ae9-42f3-a158-2eee7d121d7e`. The new guest completed cloud-init, explicitly rebooted,
and started pulling its executor image. Neither the scheduling failure nor the first setup
failure validates the keyring fix or RAM continuation scenario.

The [third attempt](https://app.buildbuddy.io/invocation/aaa719da-59fe-4bcb-a151-985a7316b854)
ran on the replacement worker (host `57dfd687-22ab-49e8-9bad-4b0fb1bf2fb5`, executor
`c6af19b6-70be-4925-b52d-6e59a066e911`). It stopped at the old image-prune postcondition before
deployment; devel PR #14194 already fixed that exact eligibility race. The rebase retains that fix.
Its containerd log confirms the effective `NoNewKeyring=true` option, followed by a distinct
`pivot_root .: operation not permitted` error from the initial system Pods. Docker's default
seccomp profile also blocks `pivot_root`, so the next attempt enabled
`NoPivotRoot=true`. The [containerd 2.1 option schema](https://github.com/containerd/containerd/blob/release/2.1/api/types/runc/options/oci.proto)
supports both settings; [runc's implementation](https://github.com/opencontainers/runc/blob/main/libcontainer/configs/config.go)
uses MS_MOVE and chroot when pivot-root is disabled. The outer worker security profile remains
unchanged. These setup failures still precede the Firecracker guest and app continuation proof.

The [fourth attempt](https://app.buildbuddy.io/invocation/b3f4ea78-4233-4b8e-8721-45a0bca37b25)
loaded both options, started CoreDNS, local-path, cert-manager/trust-manager, KubeVirt and Kyverno,
and imported the real boot image (184.65 seconds) and launcher (44.55 seconds). It failed before
Sandbox creation: every app Pod's sandbox failed in runc's no-pivot path with
`mount dst=/proc, flags=MS_REC|MS_SLAVE: invalid argument`. Increasing deployment timeouts would
not help. A teardown `du` timeout also obscured the primary error; the retained logs identify it.

The operator approved a scoped worker policy change. An independent session concurrently landed
[PR #14207](https://github.com/agentydragon/ducktape/pull/14207), supplying Docker 29.1.3's default
allowlist plus the specific keyring operations required by runc. This branch rebases onto that
owner and adds only a `pivot_root` allow rule requiring `CAP_SYS_ADMIN` to its existing
`docker-seccomp.json`. Both fixture workarounds are removed: normal keyring creation and pivot-root
now belong to the worker profile. No outer-cluster privileges or VM placement are changed.

In the first live policy experiment, the worker was observed idle (all execution-stage gauges
zero and only the executor container present), then its executor stopped. A reviewed default
profile plus the pivot rule was installed in that same guest, Docker validated/restarted, and
the executor restarted and registered healthy. A Docker action reached pathname lookup for
`pivot_root` (`ENOENT` for intentionally absent paths), while `keyctl` remained denied (`EPERM`).
The subsequently landed managed policy also admits runc's restricted keyring operations.

An independent restart to apply PR #14207 then deleted VMI `753a973e-5f9d-4e59-a258-244567105d98`
at 05:17:11 UTC, replacing it with `64876921-7368-43df-9064-e7256266af4b`. Its disposable work disk
and the guest-local experimental profile were lost. The [fifth scheduling attempt](https://app.buildbuddy.io/invocation/fa701f63-849b-45bb-b5d3-34b4e988eb44)
ran no tests because the replacement executor was not registered yet. The operator confirmed the
other session's restart. The pivot extension was then applied to the replacement while it was idle, preserving the landed
keyring rules. Docker and the executor restarted and registered healthy. This live guest change
precedes merge: recreating the worker before the PR lands loses this extension.

The [replacement-worker smoke check](https://app.buildbuddy.io/invocation/d02a7c27-e28d-481e-a1fe-1d604d975910)
passed all three Go tests: KVM VM/vCPU creation, runc's restricted keyring operations and a
`pivot_root` permission probe using intentionally absent paths. Execution took 7.703 seconds after
698.302 seconds in the single-slot queue. The outer `bbr` completion-log RPC disconnected after
inner Bazel exited zero; the durable test summary independently records PASSED.

The [sixth acceptance attempt](https://app.buildbuddy.io/invocation/5ac7046b-9301-470a-9364-63c5f33d3bef)
used normal runc keyrings and pivot-root and successfully started infrastructure and app containers.
It still failed before Sandbox creation, during the production boot-image import. K3s logged slow
SQLite writes and finally `Transaction commit failed: sql: transaction has already been committed
or rolled back` at 05:50:22 UTC; the import then reported EOF from containerd. The boot-image import
span lasted 384.34 seconds before failure. No OOM or full filesystem was observed: afterward the
worker had 21 GiB available memory and 54 GiB available disk, while IO pressure remained elevated.
This is a fixture/control-plane failure, not a Firecracker guest result.

Live read-only Pod inspection also found a separate fixture recovery defect: PostgreSQL completed
its bootstrap SQL, but its shutdown checkpoint took 112.820 seconds, exceeding pg_ctl's wait. The
container restarted with PG_VERSION present and skipped the client-access rule that the script had
placed after shutdown. The app then received `no pg_hba.conf entry` permanently. The rule now goes
before shutdown so that restart remains usable. The Firecracker fixture also joins the cold boot
image import before starting app workloads, avoiding overlap with PostgreSQL initialization and
its additional control-plane writes. No readiness or test timeout was increased.

The [seventh submission](https://app.buildbuddy.io/invocation/3bd73418-b464-4273-9d2b-af9dd95bbe2d)
remained queued behind other KVM work and was cancelled before execution after devel advanced.
PR CI's merge commit exposed the changed `OciImage` API in the launcher image test. The branch
was rebased again and that test now consumes `oci_image_py`'s generated descriptor, including its
image runfiles, matching the landed API rather than hand-writing its positional arguments.
