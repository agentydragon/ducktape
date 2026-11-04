# Firecracker system test on the dedicated BuildBuddy VM

This follows the [launcher proof](2026_11_03_firecracker_environment.md) with an app-integrated,
disposable k3s acceptance target: `//agentplane/system_tests/suites:test_firecracker`.
The first complete pass below used `760367f0aa18437549b073631ca8269ac4502be0`, rebased onto
`devel` commit `2c86bf3bd1c09c1e3ccc4c1bc38cf323db41caf2`, on `ovh-ns104963`.
After permanent Flux adoption, the unrestricted scheduler selected `wyrm2`, as recorded in
the final section. The subsequent [storage probe](2026_11_04_firecracker_storage_probe.md)
records the managed-worker rerun, sparse-aware hashing candidate and its hardware invocation.

## Flux-managed wyrm2 hardware acceptance

[The complete rerun](https://app.buildbuddy.io/invocation/29aedad2-8644-40dd-95f9-e85001fb6b06)
passed at runtime code `9291cb128cd2d247f397d2a87f0b9fc763493798`, including teardown:
`PASSED in 1585.0s`. It used the permanent Flux-managed worker on wyrm2 and the unchanged
production guest, without profiling. Subsequent benchmark/report changes did not change
the production sources exercised by that run.

The [exact continuation data](2026_11_04_firecracker_system_test_continuation.json) records child
PID `586`, start ticks `8285`, and marker `d8ee0cd17298f58729cbea5dbd476ec82371e78052120178`
before and after resume, with counter `2` to `64`. Kernel boot ID
`b5e81095-e361-43b0-bf78-abe3f095f83a`, Runtime boot ID
`89a28ad6-3ed5-45ed-8d6e-adc12f20bfe2`, native session and process identities also matched.

The scenario ran from `13:45:59.307089Z` to `13:54:06.289113Z`. Cleanup took 291.652 seconds:
deletion was accepted at `13:54:06.397407Z` and confirmed at `13:58:57.943697Z`, within the
unchanged 300-second budget. That leaves only about eight seconds of margin; the storage
follow-up remains open. The earlier profiled run failed cleanup and is retained as a failure
in the storage report; its profiler filled the worker root disk, so it is not a controlled
latency baseline for attributing the difference to sparse-aware hashing.

## Full hardware acceptance passed

The [hardware run](https://app.buildbuddy.io/invocation/418f4407-9b83-47df-be8d-fb28df43b15e)
passed the complete target, including teardown: Bazel reports `PASSED in 804.5s`, and pytest
reports `1 passed in 794.65s`. It used the production guest and real Codex harness with a scripted
model, without live changes to the disposable cluster. All ordinary PR checks were green at the
same code head. The [retained continuation JSON](2026_11_03_firecracker_system_test_continuation.json)
contains the assertions' exact inputs:

| Evidence                        | Before and after resume                            |
| ------------------------------- | -------------------------------------------------- |
| Sandbox UID                     | `398abd01-7c1a-4852-8847-3a98496e5089`             |
| Same Pod, no container restarts | `32e4ba7c-a052-46c6-b1dc-0ac8c6335b90`             |
| Kernel boot ID                  | `7294f4b2-df22-4f9f-bbc8-7d4d57968873`             |
| Runtime boot ID                 | `cab612b0-f312-4af1-87c6-adc3c67b2d5a`             |
| Managed native process ID       | `eb2950c2-6195-42d3-85f8-c0ffc426a64f`             |
| Native session ID               | `01a1a656-6529-7360-88cb-85f0ad7031bb`             |
| Child PID / start ticks         | `553` / `1783`                                     |
| Child counter                   | `2` → `29`                                         |
| Random marker held in the child | `8df2516686096d2ed81f2d7a5f25fb6631be36c13f6b4d0b` |

The second probe never starts a replacement child. It observes fresh counter progress and checks
the live PID/start ticks before reporting the retained marker. The observations are 257.070083203
seconds apart in wall time. PID 1 remains systemd; root, state and workspace are separate ext4
mounts, the runner is EROFS, and config/launch/trust are ISO9660. The canonical projected
ServiceAccount token path is absent in both observations. The launcher's suspended phase was
observed after checkpoint commit and VMM reap, before app-driven resume.

The functional scenario ran from 11:32:39.104 to 11:38:21.982 UTC. Firecracker acknowledged its
first full RAM snapshot at 11:34:08.168 (2.91 seconds for the API call), and the resumed guest
acknowledged thaw at 11:38:17.091 with the same boot ID. Disk verification and checkpoint copying
dominate this implementation; these observations are not a comparative latency benchmark.

Cleanup ran from 11:38:21.990 to 11:42:46.824 UTC (264.834 seconds), within its existing 300-second
budget. Deletion was accepted at 11:38:22.112, the cleanup checkpoint acknowledged at 11:38:25.864,
and the final thaw acknowledged at 11:42:43.233. The restored Runtime reconnected, sessions were
sealed, and the Sandbox was deleted by 11:42:46.824. This exercises both deletion corrections
below: waiting for a verified connection before Quiesce, and completing finalization after the
sealed launcher is deliberately deleted. No readiness, cleanup or overall deadline was increased.

The worker was idle and healthy after output upload. The new fixture's image-store reset avoided
obsolete boot layers; the work disk still had about 35 GiB free during final restore. Historical
functional passes and failures remain below; this is the first complete target pass.

## Execution path

The existing `buildbuddy-executor/buildbuddy-kvm-worker` KubeVirt VM serves the
`wyrm2-kvm-vm` self-hosted BuildBuddy pool. The first complete pass ran on `ovh-ns104963`;
the permanent Flux-managed worker was subsequently scheduled on `wyrm2`. The pool name is
independent of its host. The test action uses Docker inside that VM, starts
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

The accepted production Nix output was built with inputs committed at
`57efc97b8c315ababd4046cd40f2bb7f58926dcf`, including the agent-owned `/state/codex` directory.
Its archive has 2,266,441,114 bytes and SHA-256
`c008dc2ec34e17ebf396f15d889e5e1b5646444363e0f916dbcbab512362cdd7`. It is pinned from the existing
workflow-owned Forgejo repository under nondeployable tag
`spike-firecracker-57efc97b8c315ababd4046cd40f2bb7f58926dcf`, manifest digest
`sha256:79cec82bc56731b75128797df9e9d2ef9fd954b60fbed089c5348493b58d1a97`.

The boot layout is a declared Bazel input; the test does not read this host's Nix store. Registry
credentials stay in a temporary Docker config on the Bazel client, outside test runfiles. The
experimental tag does not enable Firecracker in staging.

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
- [Complete Firecracker acceptance](https://app.buildbuddy.io/invocation/418f4407-9b83-47df-be8d-fb28df43b15e): passed at `760367f0aa`, including teardown.
- [Post-rebase adapter and fixture checks](https://app.buildbuddy.io/invocation/d6d5a00d-fab8-4041-a489-de42008b9aae): five tests and both explicitly named library builds passed.
- [OVH nested-KVM smoke](https://app.buildbuddy.io/invocation/0707f7db-1bce-4051-a165-5ddee411123a): passed uncached in 5.9 seconds.

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

The [eighth run](https://app.buildbuddy.io/invocation/7156a60e-a693-4ea4-b3d7-2eee9dc56e82)
completed the production boot import in 130.7 seconds and reached all app Deployments Available.
It then failed at `kubectl wait clusterpolicy --all`: unlike the VM fixture, Firecracker has no
launcher-injection ClusterPolicies. Infrastructure readiness is now a separate helper; the VM
fixture retains its policy check, while Firecracker waits for infrastructure and KVM allocation.
The refused API connection observed afterward was fixture teardown, not another k3s crash.
The [rebased launcher OCI startup test](https://app.buildbuddy.io/invocation/16e551ec-09e0-4208-911b-d76ce6249170)
passed in 14.4 seconds with the generated image descriptor.

The [ninth run](https://app.buildbuddy.io/invocation/a43b7a17-ff74-4c83-8832-552650a0476b)
reached device allocation after all app Deployments became Available. Kubernetes serialized KVM's
1000 available devices as `1k`, which the fixture incorrectly passed to `int`. The readiness check
now uses the Kubernetes client's quantity parser. The production boot import was cached (1.5 seconds).

A further rebase onto `b227dabb9a` incorporates devel's protection of imported fixture image IDs
before CRI publishes their tags. The merge conflict retained that protection for every image group
and the Firecracker virtualization condition. Devel also replaced `oci_image_py` with
`oci_layout_py` and digest-derived Docker references; the launcher image test follows that API.
[Focused checks on the rebased code](https://app.buildbuddy.io/invocation/42dba61c-12a1-4c9e-b235-fadffba5bf64)
passed: launcher OCI startup (17.1 seconds), fixture rendering (29.3 seconds), and retained guest logs
(cached pass). These are separate from the still-unproved full RAM continuation scenario.

The [tenth run](https://app.buildbuddy.io/invocation/89724f98-c5d8-4331-9800-1d3d5c36ee2b)
passed fixture setup and created the Sandbox, exposing an admission mismatch in the backend:
`SandboxTemplate` allowed raw state, but `Sandbox.status.volumes.state.layout` still required
DirectoryTree. The API server rejected the controller's durable state binding before Pod creation.
The Go status marker now admits DirectoryTree or ext4 RawDiskImage, with its existing filesystem
constraints retained. The real API-server regression tests cover both valid layouts and reject
missing filesystems, invalid filesystems and BlockFilesystem state.

For diagnosis only, that same status-validation rule was applied inside the disposable test cluster.
It unblocked both PVCs and created the launcher Pod. The original 300-second startup wait expired
before this late recovery completed disk preparation; cleanup also timed out. No guest tool turn or
RAM pass is claimed for this modified run. The clean rerun uses regenerated source manifests.
The CRD generator invocation `0395176a-2ae3-4574-9ef7-8c0a3ee2d4a1` completed successfully; most of
its elapsed time waited for the running Bazel test's local server lock, not for code generation.

The [eleventh run](https://app.buildbuddy.io/invocation/95b88e56-7e6d-4ce4-8b6e-2745278a521e)
used the corrected schema without live intervention. The launcher prepared the production disks,
started Firecracker under nested KVM, booted the kernel, discovered all eight virtio block devices,
and mounted the ext4 root. Kernel init began after about 10 seconds; NixOS reported its stage-2
configuration at about 18 seconds and entered activation. At 07:15:07 UTC, the launcher's shared
30-second management deadline expired before the guest management listener was ready. It failed
closed and reaped the VMM; the acceptance and cleanup waits subsequently expired.

Boot readiness now has a separate two-minute `GetStatus` transport wait. Credential synchronization
retains a fresh 30-second RPC deadline, no replay, and the existing boot-identity fence before key
transmission. The serial-console progress establishes that the old budget cut off an active boot;
the next clean run must establish whether activation completes. The
[Go supervisor tests](https://app.buildbuddy.io/invocation/fa18ed2d-c15a-4ccc-b106-706386eeb0fe)
pass with this change. [Generated manifests and real API-server admission](https://app.buildbuddy.io/invocation/a87ebb0a-3afe-43ef-9748-239996a4abc2)
also pass, including the new durable state-binding cases.

The branch was rebased again onto `c85180ea40`. New scenario and launcher-test imports follow
devel's moved undeclared-output and OCI helpers; the retained OpenTelemetry fixture helper remains
in its existing owning package.

The [twelfth run](https://app.buildbuddy.io/invocation/fdeba6d7-517a-41ff-a354-d115af68f1b1),
at source `9e63208d37`, completed guest startup and credential synchronization at 07:34:10 UTC.
The Runtime connected and the app opened a Codex Session. Its first command failed before
dispatch: systemd reported `200/CHDIR` for the harness unit. The shared app-server directory
`/state/codex` is created by the root Runtime with mode `0700`, but its systemd process runs as
`agent`. The guest tmpfiles policy now prepares that directory as agent-owned, keeping Runtime
journals root-owned. Failed native start errors also include systemd status/code diagnostics.

Cleanup then requested suspension at 07:34:33 UTC. Firecracker wrote the full 1,610,612,736-byte
RAM file and a 27,441-byte VM state file by 07:34:34, but had not acknowledged snapshot creation
when the generic 20-second HTTP client deadline expired at 07:34:54. The launcher fenced the
network and reaped the VMM; no checkpoint was committed and no resume was attempted. Snapshot
create/load now have their own two-minute I/O deadline, while control requests retain 20 seconds
and uncertain requests are never retried. The [pinned Firecracker snapshot configuration](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/vmm_config/snapshot.rs)
synchronizes snapshot files by default, and the [snapshot writer](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/persist.rs)
completes that synchronization before returning. The app readiness, suspend and test budgets are unchanged.
The [focused supervisor checks](https://app.buildbuddy.io/invocation/518686a2-f379-441d-92db-7011adacccdd)
passed with the deadline separation and native failure diagnostics. Ordinary PR CI was green at
`9e63208d37`; that does not cover this manual hardware acceptance target.

The production boot image was rebuilt with the Nix inputs committed in `57efc97b8c`, including
the agent-owned Codex directory. The 2,266,441,114-byte archive has SHA-256
`c008dc2ec34e17ebf396f15d889e5e1b5646444363e0f916dbcbab512362cdd7`. It was published to the
same workflow-owned repository under nondeployable tag
`spike-firecracker-57efc97b8c315ababd4046cd40f2bb7f58926dcf`, manifest digest
`sha256:79cec82bc56731b75128797df9e9d2ef9fd954b60fbed089c5348493b58d1a97`. The manual acceptance
input now pins that digest. This publication does not enable Firecracker in staging.

The thirteenth run's functional scenario passed, but its cleanup checkpoint crossed the node's
5% free-disk eviction threshold. Kubelet evicted PostgreSQL, the Gateway and the Firecracker Pod;
the app watch then returned HTTP 503. This was not a RAM continuation assertion failure. At the
end, containerd retained 26.9 GB of unpacked snapshots and 7.47 GB of content, including obsolete
pinned Nix boot inputs. The second checkpoint temporarily coexists with the prior checkpoint and
live backing disks. The worker reached roughly 4 GiB free on its 79 GiB work filesystem.

The Firecracker fixture now explicitly starts with a fresh containerd image store, removing
previous test state through the existing owned-stack reset before importing current declared
inputs. Metadata and layers are discarded together. This preserves checkpoint durability and
normal kubelet eviction thresholds, and does not retain an executor snapshot or live cluster.
The next full run must establish that this capacity change also permits teardown to finish.

The [fourteenth run](https://app.buildbuddy.io/invocation/dc167ace-6ba0-434e-b166-a95903c50f64),
at clean source `a3989b0e97`, again passed both tool turns and the RAM assertions (child PID `585`,
start ticks `6703`, counter `2` → `66`). The fresh image store left 20 GiB free during the final
checkpoint, with app services still Ready and no disk-pressure eviction. The enclosing target
still failed its cleanup budget: deletion resumes the guest to seal Session journals, so cleanup
covered another complete checkpoint and another restore before quiescence. The controller had
accepted deletion at 08:51:19 and was still restoring disks when the five-minute cleanup wait
expired. The restored guest must run to seal; bypassing this fence would weaken deletion safety.

Restore now verifies every checkpoint file first, then reuses each live regular disk only when
its full size and SHA-256 equal that checkpoint. The original VMM is already reaped and the owner
lock is held. Missing or different disks are still copied back atomically; block workspaces still
restore from the checkpoint. This removes redundant writes and temporary disk allocation while
preserving the paused RAM/disk boundary, active marker and identity checks. No readiness, cleanup
or overall test deadline was increased. [Storage and supervisor tests](https://app.buildbuddy.io/invocation/c023dee1-cf28-40a9-8ddf-04d4bda41e27)
passed, including unchanged-inode reuse, equal-sized corrupt live contents and a missing live disk.

The [fifteenth run](https://app.buildbuddy.io/invocation/ce7e31f8-b685-484c-bffc-7bdd0b06ff83),
at source `598b82f6f8`, failed before guest boot: while copying the boot disk, k3s logged slow
SQLite writes and a fatal `Transaction commit failed: sql: transaction has already been committed
or rolled back` at 09:13:00 UTC. The API disappeared and the app returned HTTP 503. The restore
optimization was not reached. Ordinary PR checks were all green at that source head.

The disposable control database shared the same filesystem/write queue as the large guest files.
Its database, WAL and shared-memory files occupied approximately 55 MB. The Firecracker fixture
now mounts its ordinary `server/db` directory on a bounded 256 MiB tmpfs, with cleanup registered
immediately and unmount after the k3s server is reaped. This keeps the default SQLite configuration
and does not change guest/PVC/checkpoint durability. Firecracker still rejects retained cluster
snapshots; the retained baseline path explicitly keeps its database on disk.

The [sixteenth submission](https://app.buildbuddy.io/invocation/c0eaf873-c377-4386-a42a-231c9aead126)
started successfully with its k3s database on the 256 MiB tmpfs (4.7 MiB used at the initial
inspection). It was cancelled during setup after devel advanced and the PR conflicted with the
landed infrastructure/runtime split; no guest was booted and no acceptance pass is claimed.

The branch was rebased onto `f439c32cd69efb57c07938a90b573f8afe68c616`. K3s storage policy now
belongs to `baseline.py`. The Firecracker pytest plugin supplies its lifecycle adapter through
the shared virtualization extension point, independently of the VM-only loop provisioner and
admission policies. Shared controller readiness stays in `vm_infrastructure.py`; the Firecracker
adapter also checks the node's KVM quantity. Cold guest imports remain serialized before app
deployment. The production Firecracker Go files, guest Nix configuration and boot artifact are
unchanged by this rebase. [Focused adapter and baseline checks](https://app.buildbuddy.io/invocation/cd75681e-7e9d-41ae-8abc-277d47c3105c)
passed four tests plus the explicitly named stack, baseline and pytest-plugin type/lint builds.

## Reconnect and deletion corrections

The [seventeenth submission](https://app.buildbuddy.io/invocation/7c9f74a6-e980-42be-a51e-e4ba7bac97cf)
failed Bazel analysis because the new pytest plugin lacked visibility to the suite. It did not
execute hardware work. Its visibility now matches the existing VM plugin.

The [eighteenth run](https://app.buildbuddy.io/invocation/37cde600-6f94-49ef-8f14-9128f7293874)
at `e892487d80fc30c1d5cda59010dda40fc6abd2b9` passed both Codex turns and the RAM continuation
assertions: child PID `591`, start ticks `6367`, counter `2` to `67`, and retained RAM marker
`5c91dd10c15061edaa36e5456d574447d94089e209476f82`. Its database tmpfs and fresh image store remained
healthy. The enclosing target still failed teardown. After deletion restored the guest, the
controller persisted a Quiesce attempt before the new Gateway stream had completed verification.
The guest's old stream then reset, and the controller waited its normal one-minute retry interval.

Deletion now waits for a live Gateway Lease for the same Pod, a verified Runtime boot ID, and
an epoch newer than the pre-suspend claim before consuming a Quiesce attempt. The
[controller regression](https://app.buildbuddy.io/invocation/8c2f4b42-eb64-411e-b157-c7662dda8ea4)
passed stale-epoch, unverified-boot, foreign-Pod and expired-claim cases, followed by a verified
reconnect making exactly one Quiesce request.

The [nineteenth run](https://app.buildbuddy.io/invocation/015d57c1-f35f-4f7f-8e31-77d80b76f0da)
at `685cdd37ba4a82e679a8a6292dcc07981f350111` completed cold boot and its first tool turn, but
missed the checkpoint deadline under severe physical-host contention. Wyrm2 had a load around
178 on 32 CPUs and approximately 40% full memory stalls; an unrelated analysis process held
about 25 GiB. That process was left untouched. No RAM continuation pass is claimed for this run.

Cleanup did restore the guest, wait for the verified reconnect, and seal native sessions. It then
exposed a second controller bug: after recording `Quiesced=True` and deliberately deleting the
launcher Pod, the next reconciliation mistook the missing Pod for an unsealed lost launcher and
retained the finalizer. The lost-launcher guard now applies only before sessions are recorded as
quiesced. The [regression tests](https://app.buildbuddy.io/invocation/c59e2f7b-1831-4b61-8a6a-e327f8571453)
pass both the sealed deletion through Sandbox removal and unsealed launcher loss retaining its
checkpoint and finalizer. The complete hardware rerun above subsequently passed with this correction.

## Runner placement and permanent Flux adoption

The initial move away from wyrm2 used a four-vCPU, 16-GiB worker on `ovh-ns104963`, with
one BuildBuddy action slot and 14 GiB advertised memory. Before that move the host had about
53.8 GB available memory, no memory pressure stalls, 134 GB node filesystem space and 183 GB
image filesystem space. The existing `wyrm2-kvm-vm` pool name, 80 GiB disposable work disk and
40 GB file cache were retained. The runner was idle and its executor was stopped before
replacing its disposable VMI; no PVC-backed disk was involved.

The new VMI UID is `18d03712-a6b8-4524-8282-37c31cec5168`, created at `2026-11-04T11:10:22Z`,
on `ovh-ns104963`. The VM UID remains `190a0589-2961-4d93-b528-f2857aee4ce2`. Its Ubuntu guest
reports four CPUs, 15,974 MiB total memory, `/dev/kvm`, and Intel nested KVM enabled. Docker
loaded the managed seccomp policy at bootstrap.

That preview was initially applied directly after server dry runs, with Flux reconciliation
disabled on three resources: the KubeVirt CR and the worker VM and ConfigMap. This was not a
Flux deployment. The operator requested making the smaller worker permanent and subsequently
simplified its policy to allow any node advertising Intel VMX or AMD SVM, whether control-plane
or worker. The permanent policy has no hostname, region or explicit OS selector on the runner,
and removes KubeVirt's global workload region/control-plane exclusions. Control-plane taint
tolerations are present on the VM and KubeVirt infrastructure/workload placement. API/controller
placement retains its existing region. Other taints, available resources and KubeVirt scheduling
requirements still apply.

[Infrastructure PR #14289](https://github.com/agentydragon/ducktape/pull/14289) landed separately
as `026a3696d26a5d613321d61fb92ce87fcd798ee7` at `2026-11-04T12:07:14Z`. All ordinary PR checks
passed at final head `e00918c46d`; [explicit manifest/lint and uncached KVM/keyring/pivot-root checks](https://app.buildbuddy.io/invocation/36a48bff-1586-4bcf-9f2e-51b1b5f67fd1)
also passed. This includes the runner's already-tested capability-scoped `pivot_root` policy.

Both Flux ExternalArtifacts were verified Ready with origin
`devel@sha1:026a3696d26a5d613321d61fb92ce87fcd798ee7` before releasing the preview:

| Artifact                | Revision digest                                                           |
| ----------------------- | ------------------------------------------------------------------------- |
| `kubevirt`              | `sha256:7433bf41fa329490f35bb61b00f643c8d54e753635af8c34a226396d19baa915` |
| `buildbuddy-kvm-worker` | `sha256:681f7768ad8ebf535c7db689ad44c0c0b04c3a43044cfcc0c6bbde0ba8093554` |

Server-side dry runs using Flux's field manager revealed that the old manually owned hostname
selector and KubeVirt workload affinity would survive omission from source. UID- and
value-guarded JSON removals cleared exactly those two preview fields. Removing the complete
selector field passed admission; the earlier Linux-selector workaround was unnecessary for
this removal. All three `kustomize.toolkit.fluxcd.io/reconcile` holds were then removed.
No Flux Kustomization was suspended. Flux reconciles the permanent source through the existing
`kubevirt` → `buildbuddy-kvm-worker` dependency.

KubeVirt became Available with all eight virt-handler Pods ready at DaemonSet generation 895.
Its Flux health check completed in 6 minutes 25 seconds. Both owning Kustomizations report
Ready and Healthy at the revisions above. The VM's live ConfigMap matches all 13 configuration
files in the landed artifact, and no reconciliation hold remains on any of the three resources.

The existing guest stayed healthy during node-agent rollout, but Flux correctly waited for the
new VM template: `metadata.generation=4` versus `status.observedGeneration=3`. The idle executor
was stopped, absence of action containers verified, and the disposable VM restarted through
KubeVirt's lifecycle API. Only rebuildable cache/work disks were discarded. No further desired
configuration override was introduced.

Kubernetes selected `wyrm2` under the unrestricted capability policy. The replacement VMI UID is
`b6e36912-8f01-48e1-8a8e-aca63ee5d9a9`, created at `2026-11-04T12:22:04Z`. The VM UID is unchanged;
its desired and observed generations are both 4. Guest bootstrap and its automatic reboot
completed, AMD nested KVM reports `1`, and the executor registered at 12:26:23 UTC with all
health probes passing. The VM has no nodeSelector and only the two alternative VMX/SVM affinity
terms. The controller-added KubeVirt schedulable-node requirements still apply.

An [early post-adoption submission](https://app.buildbuddy.io/invocation/845d6299-2120-430c-83ca-19b09b36f11f)
arrived before executor registration and was rejected with no test execution. The
[post-registration smoke](https://app.buildbuddy.io/invocation/b6950fb1-da29-421d-bdad-841e2b9703fc)
passed after all executor health probes were verified.

The feature branch was rebased onto that landed infrastructure, dropping the duplicate
fixed-host placement commits. Its Firecracker Go/controller files, guest Nix configuration,
boot artifact and system-test fixture/scenario sources are unchanged by this rebase. The
accepted hardware result remains tied to `760367f0aa`; rebased-head checks are separate.

The uncached [OVH KVM smoke](https://app.buildbuddy.io/invocation/0707f7db-1bce-4051-a165-5ddee411123a)
passed in 5.9 seconds. The [earlier post-rebase fixture checks](https://app.buildbuddy.io/invocation/d6d5a00d-fab8-4041-a489-de42008b9aae)
passed all five tests and the explicitly named stack and plugin builds. The
[analysis-only submission](https://app.buildbuddy.io/invocation/1dcf0f5a-7e83-4e51-8461-fa3266ef851b)
failed because launcher-image visibility still named the old test package; it ran no hardware
action. After correcting visibility, the [complete hardware rerun](https://app.buildbuddy.io/invocation/418f4407-9b83-47df-be8d-fb28df43b15e)
passed, including teardown, as detailed above.
