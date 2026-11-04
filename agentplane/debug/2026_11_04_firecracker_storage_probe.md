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

Sparsity avoids allocating holes, but `digest` still reads and hashes their logical zero bytes.
Restore verifies every checkpoint file, then verifies each retained live regular-file disk
before reusing it. Checkpoint copies and verifies every disk, including immutable runner,
kernel and configuration inputs. A sparse image therefore still incurs repeated complete
logical-file scans. The existing optimization correctly avoids rewriting verified live disks;
it does not eliminate verification work.

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

1. Measure the actual launcher on the managed VM during checkpoint and restore, separating
   CPU spent hashing, sparse-copy I/O, and sync latency. Preserve complete content verification,
   the paused RAM/disk boundary, durable manifest publication and boot/config/VMM identity fences.
2. Build and boot a smaller guest variant before selecting any tools to remove. Retain required
   agent capabilities or supply heavy tools through an explicit immutable tools disk.
3. Investigate retaining large immutable inputs by verified image digest, with a small writable
   root/state boundary. A read-only Nix store or immutable lower filesystem could avoid copying
   gigabytes of unchanged tools per checkpoint. Prove snapshot consistency, boot mounting,
   restore validation, upgrades and garbage-collection ownership before changing the format.

Simply adding sparse copying would duplicate the existing implementation. Reducing logical
disk size or separating immutable contents can also reduce full-file verification work.
