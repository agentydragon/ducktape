# Firecracker sparse disks and a smaller guest

The operator requested a storage probe alongside the full acceptance rerun of PR #14029 at
`d110313300cc63e59f7b36cf514de06e57763adc`. This probe changes neither the deployed worker nor
the PR's guest configuration. The smaller variant was built only in the local Nix store; it
has not been booted or substituted into the acceptance test.

## Sparse copying is already present

`agentplane/firecracker/storage.go` uses `cp --reflink=auto --sparse=always` for regular files.
Its block-device path scans 1 MiB chunks, seeks over all-zero chunks, and truncates the result
to the original logical size. Both paths sync the resulting file and compute a full SHA-256.

The built current-head guest's root image has:

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

## Follow-on experiments

1. Validate the sparse-aware digest in the full hardware target and measure remaining
   sparse-copy I/O and sync latency. Preserve complete content verification,
   the paused RAM/disk boundary, durable manifest publication and boot/config/VMM identity fences.
2. Build and boot a smaller guest variant before selecting any tools to remove. Retain required
   agent capabilities or supply heavy tools through an explicit immutable tools disk.
3. Investigate retaining large immutable inputs by verified image digest, with a small writable
   root/state boundary. A read-only Nix store or immutable lower filesystem could avoid copying
   gigabytes of unchanged tools per checkpoint. Prove snapshot consistency, boot mounting,
   restore validation, upgrades and garbage-collection ownership before changing the format.
4. For writable disks, investigate Firecracker v1.17's optional [discard support](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/api_requests/block-discard.md).
   It is disabled by default, requires the Sync IO engine, and punches holes in regular backing
   files when the guest trims free blocks. The current launcher does not enable it. This was
   researched but not tested; it cannot remove occupied toolchain data or the logical cost of
   hashing zeroes.

Simply adding sparse copying would duplicate the existing implementation. Reducing logical
disk size or separating immutable contents can also reduce full-file verification work.
