# Firecracker sparse disks and a smaller guest

The operator requested a storage probe alongside the full acceptance rerun of PR #14029 at
`d110313300cc63e59f7b36cf514de06e57763adc`. This probe changes neither the deployed worker nor
the PR's production guest configuration. The first phase built a smaller system closure;
subsequent isolated NixOS and Bazel-built CI guest boot/RAM-resume proofs are recorded below.

## Current policy: local checkpoints without bulk content hashes

The operator subsequently chose to remove the bulk SHA-256 scans. They detected later corruption
or file mutation and compared live disks with a checkpoint to avoid copying; they did not establish
the paused RAM/disk boundary. This same-Pod checkpoint is published locally rather than received
from an untrusted store or transferred between machines.

Checkpoint now records file sizes after sparse copying and syncing. Before touching live disks,
restore checks the complete file set, regular-file types and exact sizes, then copies every backing
disk from the committed generation. There is no content comparison or live-disk reuse branch.
The owner lock, active-continuation fence, paused capture and atomic durable manifest publication
remain. Small configuration and VMM compatibility digests remain too. Earlier experimental
manifests are rejected rather than migrated.

Missing, truncated or nonregular generation files are rejected. Silent same-sized corruption is
outside this contract. Unconditional restore adds disk writes compared with the previous reuse
path; measure that tradeoff rather than inferring an end-to-end speedup from removed hash CPU.

The hash profiles and benchmarks below describe earlier revisions. Their helpers and benchmark
cases have been removed; the reproduction commands apply to the cited historical commits.

## Sparse copying is already present

`agentplane/firecracker/storage.go` uses `cp --reflink=auto --sparse=always` for regular files.
Its block-device path scans 1 MiB chunks, seeks over all-zero chunks, and truncates the result
to the original logical size. Both paths sync the resulting file. The earlier checkpoint
`copyDurable` added a full SHA-256; the first copy-path change described below removed only hashes
discarded during initial preparation and restore repair. The current policy above removes the
remaining bulk hashes.

The production guest used for the baseline has:

| File                                         | Logical bytes | Allocated bytes |
| -------------------------------------------- | ------------: | --------------: |
| Nix boot output `rootfs.ext4`                | 9,073,119,232 |   6,732,808,192 |
| Copy using the launcher's regular-file flags | 9,073,119,232 |   6,665,351,168 |

The source is `/nix/store/krry17xf2zbsli03zpxwysy4zxlgf7ls-agentplane-environment-firecracker-boot-files/rootfs.ext4`.
Allocation is `stat.st_blocks * 512`; it is not a count of nonzero bytes or a measurement of
the filesystem inside the image. The host filesystem reports `ext2/ext3` through `statfs`.
The disposable copies were removed after measurement.

Host-side `perf stat` measured the existing sparse-copy primitive at 11.183 seconds elapsed
(6.114 seconds task-clock), and a subsequent SHA-256 of the sparse destination at 5.948 seconds
elapsed (5.936 seconds task-clock). Source and destination SHA-256 values were compared.
These are warm host-side observations, not a worker-VM checkpoint benchmark. The copy command
alone excludes the launcher's explicit `fsync`, manifest commit, and other checkpoint files.

Reproduction, using a disposable destination on the same filesystem class:

```sh
perf stat -e task-clock,context-switches,page-faults -- \
  cp --reflink=auto --sparse=always -- "$source" "$destination"
perf stat -e task-clock,context-switches,page-faults -- sha256sum "$destination"
stat -c '%s %b' "$source" "$destination"
```

At `d110313300`, sparsity avoided allocating holes, but `digest` still read and hashed their
logical zero bytes. Restore verified every checkpoint file, then each retained live
regular-file disk before reusing it. Checkpoint copied and verified every disk, including
immutable runner, kernel and configuration inputs. This incurred repeated complete logical-file
reads. Verified live disks were already reused rather than rewritten.

## Managed-worker observations and sparse-aware hashing

The [current-head rerun](https://app.buildbuddy.io/invocation/d713bef1-5add-41a2-975a-31fdd3b07067)
at `d110313300` passed the scenario but failed teardown's unchanged 300-second deadline.
The second Codex turn verified the same child PID/start ticks, RAM marker, kernel, Runtime,
process and native-session identities. Cleanup diagnostics show the deleting Sandbox waiting
for restore, without a spent Quiesce attempt or a reported I/O/ENOSPC error. This is a failed
full target, not a full acceptance pass.

Observed file allocation on the worker during that run:

| File                                                | Logical bytes | Allocated bytes |
| --------------------------------------------------- | ------------: | --------------: |
| Containerd-imported boot root                       | 9,073,119,232 |   9,073,123,328 |
| Live root after sparse copy and initial boot writes | 9,073,119,232 |   6,665,912,320 |
| State disk                                          | 2,147,483,648 |      69,914,624 |
| Workspace disk                                      | 8,589,934,592 |      73,715,712 |

Thus the OCI import materialized the root's holes, while the launcher's sparse-copy path
recovered most of them. The nearly empty workspace still caused 8 GiB of ordinary file reads
for each digest.

`perf record -e cpu-clock -F 49 --call-graph fp` sampled the dedicated VM and, separately,
the launcher PID. There were no lost samples. The launcher-filtered report attributed 73.03%
of sampled CPU time to the kernel and 26.78% to the launcher executable; this does not measure
off-CPU wait time. Kernel samples included page clearing and copying. `iostat -dx -y 5 120`
reported mean work-disk reads of 61,736 KiB/s, writes of 26,993 KiB/s, and utilization of 77.26%.
The profile intervals include setup and lifecycle work, not exclusively checkpointing.

The profiler's default build-ID cache filled the small Ubuntu root disk during this run.
Its cache and raw profiles were moved to the disposable worker's work disk at
`/buildbuddy/tmp/agents/2026_11_03/firecracker-environment/profile-archive`, restoring 238 MiB
free on root. Profilers and the temporary loopback-only artifact listener were stopped.
This instrumentation disturbance makes the failed run unsuitable as a controlled latency
baseline. The raw archive is ephemeral; it does not survive replacement of the worker VM.

Candidate `9291cb128c` uses `SEEK_DATA`/`SEEK_HOLE` to hash allocated extents normally and feed
logical zero bytes directly to SHA-256 for holes. It preserves the digest, logical size and
manifest format. Filesystems that do not support extent queries retain sequential hashing;
filesystems that report everything as data remain correct. The snapshot and restore integrity
checks, syncs and identity fences are unchanged. This avoids populating the page cache with
logical holes; it does not eliminate the CPU cost of hashing their zero contents.

The [launcher/storage suite](https://app.buildbuddy.io/invocation/dc9babd6-71d6-41db-bd7f-453ea3566824)
passed, including comparisons against independent SHA-256 values for empty, data-only,
all-hole, leading-hole, trailing-hole and mixed-extent files.

A standard Go benchmark compares the old sequential loop and the new helper on the same
8 GiB sparse file with one small allocated extent. The [completed hosted runner](https://app.buildbuddy.io/invocation/3123949e-833a-4afa-84fb-56fffd824395)
reports these single-iteration results on an AMD EPYC RBE worker:

| Hash path                    | Seconds per operation | Logical throughput |
| ---------------------------- | --------------------: | -----------------: |
| Sequential file reads        |                75.722 |        113.44 MB/s |
| Sparse-aware logical hashing |                 4.447 |      1,931.46 MB/s |

This is about 17 times faster in this benchmark, not an end-to-end CI speedup estimate.
The [complete hardware rerun](https://app.buildbuddy.io/invocation/29aedad2-8644-40dd-95f9-e85001fb6b06)
passed at the candidate with the same production guest, without profiling, in 1585.0 seconds.
Cleanup took 291.652 seconds within the unchanged 300-second budget. The two full runs are
not a controlled comparison because of the first run's profiler disturbance.

```sh
bbr test //agentplane/firecracker:firecracker_test --test_output=all \
  '--test_arg=-test.run=^$' --test_arg=-test.bench=BenchmarkDigestSparseWorkspace \
  --test_arg=-test.benchtime=1x
```

## Copy/fsync costs and discarded digests

A second standard Go benchmark isolates copy, file sync and destination hashing on an
8 GiB sparse workspace with one small data extent. The
[three-arm RBE run](https://app.buildbuddy.io/invocation/6b0840d8-28c7-4a5e-9419-974dec1782ba)
passed on an AMD EPYC worker:

| Operation                     | One iteration |
| ----------------------------- | ------------: |
| Sparse copy                   |      12.27 ms |
| Sparse copy + fsync           |      20.07 ms |
| Sparse copy + fsync + SHA-256 |       4.690 s |

These are single iterations of an almost-empty file, not a populated root-disk or
end-to-end measurement. The hasher avoids reading holes but still processes their
logical zeroes. Reflink support and page-cache state can also affect these numbers.

```sh
bbr test //agentplane/firecracker:firecracker_test --test_output=all \
  '--test_arg=-test.run=^$' --test_arg=-test.bench=BenchmarkCheckpointCopySparseWorkspace \
  --test_arg=-test.benchtime=1x
```

Two callers discarded the digest returned by `copyDurable`: initial boot-image staging
and Restore's repair copy from an already-verified generation. Those hashes did not compare
source and destination. The candidate uses `copyAndSync` for those callers, retaining
sparse copying, file sync, rename and directory sync. Checkpoint records and all existing
restore integrity and live-disk equality checks remain. The
[package regression suite](https://app.buildbuddy.io/invocation/c6cd635b-f7e9-4cc5-8f84-f1185bbc35f1)
passed, including sparse-content and mode checks for the extracted helper.

Full-file SHA-256 is an application integrity policy, not a Firecracker prerequisite or
the mechanism establishing checkpoint ownership and atomicity. Here it detects damaged
generation files and decides whether an existing live disk equals the checkpoint, allowing
Restore to reuse it. It is not an authenticity signature. The separate owner lock, active
marker, complete file set, paused VMM, durable manifest publication and identity checks
establish the lifecycle boundary.

Firecracker 1.17's [snapshot contract](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/snapshot-support.md#creating-full-snapshots)
drains and fsyncs block-device backing files before snapshot creation returns. The
launcher's request uses the default sync behavior for RAM and VMM state too. Thus a
same-owner, same-generation reuse design can rely on that durable boundary, provided it
also proves no intervening writer or file replacement. Dropping content hashes would
remove corruption detection; retaining the live reuse branch would require an explicit
provenance rule. Neither broader change is made by this candidate. Small config and VMM
identity digests serve a separate compatibility purpose.

## A smaller guest closure is feasible

The Firecracker guest imports the VM guest's `environment.systemPackages`, which includes the
full `preCommitPackages` bundle. An isolated NixOS `extendModules` variant supplied
`specialArgs.preCommitPackages = []`; all other guest modules, normal tools, harnesses and
kernel settings were retained. The resulting system closure was built successfully.

| Variant                       | Store paths | Sum of NAR bytes |
| ----------------------------- | ----------: | ---------------: |
| Current-head guest            |         893 |    6,459,778,504 |
| Without the pre-commit bundle |         618 |    2,120,167,576 |

This is a 67.2% reduction in serialized closure bytes. NAR size is not the resulting ext4 size,
allocated size, resident RAM, or a boot/restore-time measurement. The variant removes lint and
development tools and is not a drop-in claim of feature parity. In particular, real agent
workloads may need these tools even though the scripted Codex acceptance scenario does not.

Largest paths absent from the smaller closure:

| Path                        |     NAR bytes |
| --------------------------- | ------------: |
| `rustc-1.95.0`              | 1,058,947,640 |
| `rustc-bootstrap-1.95.0`    |   618,603,736 |
| `python3.13-ansible-13.7.0` |   588,991,496 |
| `llvm-21.1.8-lib`           |   566,508,088 |

The baseline system is `/nix/store/rk9y3r0qq5vxhfaq6m3clmq2g5gxwhxn-nixos-system-agentplane-environment-firecracker-26.05.20261010.6fcc160`;
the smaller system is `/nix/store/ng0ddxrdjl7wh5prws62xi5bks9yh1sh-nixos-system-agentplane-environment-firecracker-26.05.20261010.6fcc160`.
The comparison used `nix path-info --recursive --json` and summed `narSize` once per store path.

```nix
let
  flake = builtins.getFlake "git+file:///path/to/checkout";
in
(flake.nixosConfigurations.agentplane-environment-firecracker.extendModules {
  specialArgs.preCommitPackages = [];
}).config.system.build.toplevel
```

## Boot and RAM resume of the smaller guest

The variant was assembled using the existing `firecracker-image.nix` root-filesystem
recipe and kernel, with only `preCommitPackages = []`. The host Nix store had about
2.5 GiB free, so the rootfs derivation's `buildCommand` ran through its `nix develop`
environment with `out` and `TMPDIR` in task scratch. The system closure itself was
already built in the Nix store. No production image pin or guest module was edited.

| File / observation               | Logical bytes | Allocated bytes |
| -------------------------------- | ------------: | --------------: |
| Assembled smaller rootfs         | 4,086,632,448 |   2,613,149,696 |
| Containerd-imported smaller root | 4,086,632,448 |   4,086,636,544 |
| Live smaller root after boot     | 4,086,632,448 |   2,548,985,856 |

The logical root is 55.0% smaller than the 9,073,119,232-byte baseline. Its live
allocated size is 61.8% below the earlier worker observation of 6,665,912,320 bytes.
These are disk measurements, not RAM or latency reductions. OCI import again
materialized holes, and launcher preparation recovered them.

The isolated OCI manifest is
`sha256:8da36c7d7e85f4c99c03719610cd1aef2ae2a4c6e48947bcf55eb1fda495a202`.
The existing full system-test target used this layout through a command-line
`--override_repository=+image_pins+firecracker_boot_linux_amd64=<layout-repository>`.
That local repository supplies the same `copy_to_directory` target as the generated
image repository, using the variant's OCI blobs, index and layout marker.

The [hardware probe](https://app.buildbuddy.io/invocation/88af37ca-0bda-4bfc-a9d7-0883c1840506)
uses runtime source `3ecae3a864` and the smaller guest. Both real Codex scripted turns
completed. PID `585`, start ticks `6071`, RAM marker, kernel boot ID, Runtime identity,
native session and process identity survived VMM exit and restore; the counter advanced
from `2` to `85`. The existing scenario also checked ext4 root/state/workspace, EROFS
runner and ISO9660 config/launch/trust mounts, and absence of a projected ServiceAccount
token in the guest. The full target passed, including teardown, in **959.2 seconds**.
Its Sandbox cleanup span took **129.575 seconds**, within the unchanged 300-second
budget. This is an observed result from a separate run, not a controlled comparison
with the production guest's 288.956-second cleanup.

This proves the reduced tool bundle can boot and resume this workload. It does not
establish tool parity for arbitrary agent tasks, reduced guest RAM requirements, or a
controlled latency improvement. The production guest remains unchanged.

## Bazel-built CI guest

The ordinary `//agentplane/system_tests/suites:test_firecracker` target now builds its guest
with Bazel and runs automatically on the dedicated KVM worker. The previous published NixOS
image remains available through the manual `test_firecracker_production` target. Both use
`scenarios/firecracker.py` with the same mount, projected-token absence, native harness and
RAM/process-continuity assertions.

The CI guest combines the pinned Debian cloud kernel, a BusyBox initramfs, and a small ext4
root containing systemd, Codex, Python and basic shell tools. The current-source runner disk
still supplies the Runtime and guest management agent. Systemd remains because the Runtime
uses its transient units and resource slices. This is a test workload closure, not production
tool parity, and the guest's 1536 MiB RAM reservation is unchanged.

The kernel's virtio MMIO/block/network/vsock, EROFS and ISO9660 drivers are modular. The
initramfs loads their dependencies before mounting `/dev/vda` and switching to systemd.
Firecracker 1.17 accepts the packaged bzImage directly; the artifact retains the existing
`vmlinux` filename. The stock kernel has ACPI enabled and command-line MMIO registration
disabled, so device discovery uses Firecracker's ACPI tables. The build checks that prerequisite.
The launcher stages optional `initramfs.cpio` alongside the boot files and includes it in
checkpoint restoration, including after launcher restart.

| CI artifact             |       Bytes |
| ----------------------- | ----------: |
| Root source files       | 610,732,316 |
| Ext4 root disk, logical | 732,880,896 |
| Kernel bzImage          |  11,737,024 |
| Initramfs               |   3,367,984 |

The logical root is 91.9% smaller than the earlier 9,073,119,232-byte NixOS baseline. The
kernel artifact replaces the probe's 413,271,304-byte debug ELF; this is a file-size comparison,
not a claim about loaded kernel RAM. The [complete OCI build](https://app.buildbuddy.io/invocation/8d97a015-bae0-48be-833c-19511989b1df)
passed with the rootfs-builder Python library explicitly checked by lint aspects. It took
406.712 seconds including repository resolution; this is not a controlled build-latency comparison.
The first attempt hit Debian snapshot download timeouts. Its retry exposed an archive conflict
between Debian's `/var/run` and `/var/lock` directories and the guest's required symlinks;
the builder now replaces those empty directories after extraction, refusing nonempty directories.
The outer client's artifact retrieval subsequently missed a 240-byte manifest blob despite the
successful Bazel build. The hardware CI action below built, imported and used the actual image.

The [ordinary CI invocation](https://app.buildbuddy.io/invocation/29bc1034-65a8-5a23-a707-3bb1ea13bbcd)
passed on synthetic merge `7a138ff28d37713488892023264a833e65cc0e44`, containing PR head
`31f4cf0567b2585d5f7aab0c8b761f0e7b319bad`. The full Firecracker scenario reports
**1 passed in 523.88 seconds**, including teardown. The existing OpenTelemetry span records
**51.124799 seconds** of Sandbox cleanup, within the unchanged 300-second limit. Importing
the guest image took 19.01 seconds. These are observations from this run, not a controlled
comparison with the earlier production-guest acceptance.

The captured continuation retained:

| Identity / evidence     | Before and after resume                |
| ----------------------- | -------------------------------------- |
| Pod UID                 | `09299566-aae1-430f-b7aa-e04f830c8781` |
| Kernel boot ID          | `dadf88b2-a680-4902-8a4e-0c1bdb0389df` |
| Runtime boot ID         | `fd2a1a61-998c-4f8c-afc2-df2e962bfca2` |
| Native session          | `01a1a76b-1848-73b2-9ab5-7baeee2ad9df` |
| Child PID / start ticks | `364` / `4219`                         |
| Child counter           | `2` to `52`                            |

The child kept its randomly generated memory marker. Both real Codex turns completed, all
required mount types matched, and the projected ServiceAccount token was absent before and
after resume. The retained VMM log identifies Debian kernel
`6.12.115+deb13.1-cloud-amd64` and reports `/dev/vda` as 1,431,408 sectors of 512 bytes,
matching the 732,880,896-byte root artifact. The production guest and staging configuration
remain unchanged.

An independent uncached action at source `31f4cf0567` also completed with exit code `0`:
its JUnit result has one test, no errors/failures/skips, and reports **519.10 seconds**.
Cleanup took **56.12 seconds**, and guest image import **14.77 seconds**. Remote execution
metadata records `skipCacheLookup: true`, worker execution from
`2026-11-04T16:38:38.013813095Z` to `2026-11-04T16:47:26.588677566Z`, and successful output
upload. The worker had only its executor container after the action finished.

The enclosing [invocation](https://app.buildbuddy.io/invocation/71bb3525-76d6-439e-9537-402fb8bacd2d)
is **disconnected**, rather than a successful Bazel invocation: its event-log stream reported
`ResourceExhausted: Log index limit exceeded`, and the outer client lost its run-result stream
while the action remained claimed. The live action was observed and allowed to finish without
resubmission. Its completed remote ActionResult, stdout and JUnit XML were recovered directly
with `bb execution get` and `bbapi blob download`. The execution ID is:

```text
/uploads/780ddd33-dabe-47cc-a9c4-de86bd88e5d9/blobs/8a1bf0666fed08a94a068e7ee45231142a802b4276133cca4e53fad476463bc6/558
```

This separate transport failure does not change the green ordinary CI invocation above. Neither
acceptance run is a controlled latency comparison with the production NixOS guest.

## Follow-on experiments

1. Measure remaining copy/sync I/O on populated disks, including unconditional restore writes.
   Preserve the paused RAM/disk boundary, durable manifest publication and boot/config/VMM identity
   fences. Bulk content checksums have been removed from local checkpoints.
2. Decide which agent capabilities require the omitted pre-commit tools before selecting a
   smaller production guest, or supply heavy tools through an explicit immutable tools disk.
3. Investigate retaining large immutable inputs by verified image digest, with a small writable
   root/state boundary. A read-only Nix store or immutable lower filesystem could avoid copying
   gigabytes of unchanged tools per checkpoint. Prove snapshot consistency, boot mounting,
   restore validation, upgrades and garbage-collection ownership before changing the format.
4. For writable disks, investigate Firecracker v1.17's optional [discard support](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/api_requests/block-discard.md).
   It is disabled by default, requires the Sync IO engine, and punches holes in regular backing
   files when the guest trims free blocks. The current launcher does not enable it. This was
   researched but not tested; it cannot remove occupied toolchain data or the logical cost of
   hashing zeroes.

Simply adding sparse copying would duplicate the existing implementation. Separating immutable
contents could reduce the remaining populated-disk copy work.
