# wyrm2 `/tmp` inventory and cleanup limits

Snapshot: 2026-11-03 around 21:30 PST. Counts below are direct children of
`/tmp`, excluding `/tmp/agents` itself and without traversing it. They are a
point-in-time census, not a disk-usage ranking; the directory changes while it
is scanned. Most entries are owned by UID 1001 (`agentydragon`).

`/tmp` is a dedicated ext4 filesystem with `relatime`. It had 889 GiB used and
68 GiB available (93% full). That total includes `/tmp/agents` and says nothing
about how much of the usage belongs to any individual category below.

| Direct-child name | Count | What it is and cleanup confidence                                                                                                              |
| ----------------- | ----: | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `nix-shell.*`     | 4,380 | 4,379 directories and one file; see Nix lifecycle below. Some trees contain Node caches or pip scratch. No reliable PID in the name.           |
| `nix-develop-*`   | 2,864 | Nix develop support directories, all empty when inspected. Names contain a namespace-local creator PID, not a reliable host liveness token.    |
| `tmp*`            |   249 | Generic temporary names: 192 files and 57 directories. Name alone gives no producer or lifetime.                                               |
| Other names       | 2,267 | Mixed scratch, browser state, BuildBuddy files, application IPC, logs, checkouts and other items. Do not treat the group as a single producer. |

The two Nix families account for roughly 74% of the 9,760 entries in this
snapshot. Among the Nix directories, 2,211 `nix-shell.*` were empty and 2,168
were non-empty; all 2,864 `nix-develop-*` directories were empty. These counts
do not establish how many are unused. Of the Nix entries, 2,080 had a top-level
mtime less than a day old, 5,083 were 1–14 days old, and 81 were older. Directory
mtime does not reflect every access to its contents or future use by an idle
shell.

The mixed group includes approximately 26 `com.google.Chrome.scoped_dir.*`
directories, 19 `org.chromium.Chromium.*` entries, 26 `buildbuddy-cli-*`
directories, 26 `uv-*.lock` files, and nine `bb-run-*` launchers. These are
examples, not a complete partition of the group. Chrome-prefixed scoped
directories do **not** match the reaper's `scoped_dir*` pattern. Lock files,
IPC objects and sockets need producer-specific lifetime rules: deleting a lock
path while a process holds it can allow another process to take a different
lock. A `buildbuddy-cli-*` sample contained `bazel.log`; its lifecycle has not
been established. The nine `bb-run-*` files were less than a day old.

Earlier in this investigation, 67 explicitly selected `rules-*` source trees,
patch scratch directories and `rules_go_work-*` compiler directories were
removed, freeing approximately 363 MiB. Some were research clones with Git
metadata already partially missing. They illustrate why name and age alone do
not prove that a checkout or scratch tree contains no unique work.

## Nix lifecycle and possible reclamation

The installed Nix was 2.34.8. Its [`nix develop` implementation](https://github.com/NixOS/nix/blob/2.34.8/src/nix/develop.cc)
creates a `nix-develop-*` support directory and emits shell code that creates a
separate `nix-shell.XXXXXX` directory and exports it as `TMPDIR` and
`NIX_BUILD_TOP`. Nix's [temp-path implementation](https://github.com/NixOS/nix/blob/2.34.8/src/libutil/file-system.cc)
includes `getpid()` in the `nix-develop-*` name. Other versions and invocations
should be checked before assuming exactly the same lifecycle.

- **`nix-shell.*`:** the suffix is random, so there is no creator PID to test.
  An empty directory can still be a live shell's future temp directory. A host
  process-environment scan found three referenced `nix-shell.*` roots; one had
  a directory mtime of 3.8 days and another was empty. An active reference is
  evidence to keep a directory, not proof that all unreferenced directories are
  unused.
- **`nix-develop-*`:** a dead creator is suggestive but not sufficient. The PID
  in the name is local to the creator's PID namespace; 690 observed names had a
  PID below 100, including 489 with PID 2. It need not identify the same process
  on the host. PIDs can be reused, and a descendant or another process may
  outlive the creator while still needing the directory.
- `/proc/PID/environ`, open files and working directories can provide positive
  evidence of use, but absence is not proof of disuse. In particular,
  [`/proc/PID/environ`](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html)
  reflects the environment at exec, not subsequent shell updates; processes can
  be inaccessible, and a process may hold only a path it will use later.
- A safer future design needs explicit producer/consumer lifetime tracking,
  including child processes and PID namespaces, and fail-closed behavior for
  ambiguous or inaccessible users. A cooperative lock or supervised scope is a
  possible mechanism, but only for processes that actually participate; a PID
  marker alone is not enough. Legacy directories remain candidates for manual
  review or conservative age-based cleanup, not provably safe automatic removal.

Setting a user-wide `TMPDIR` could group future temporary files from programs
that honor it under one top-level directory. It does not reclaim existing data,
establish liveness, or affect programs that use `/tmp` explicitly or have a
separate environment. Nix daemon build scratch uses the daemon's configuration.
The merged agent-specific configuration is separate; its two configured root
directories were not present on wyrm2 during this snapshot, so rollout there
was not established.

## Cleanup already in place

Wyrm2's system tmpfiles configuration applies a 14-day age rule to `/tmp`; it
also has explicit exclusions for service-private and container runtime paths.
Age-based cleanup considers more than mtime, including access time. Reads and
directory scans can refresh atime, while individual old files can be removed
from a tree whose parent remains; neither outcome proves the tree is unused.
At the snapshot, the last `systemd-tmpfiles-clean` run succeeded in about 26
minutes and reported a 30.8 GiB service `MemoryPeak`. This is cgroup memory,
not evidence that the cleaner's userspace heap alone consumed that amount.

The deployed daily `tmp-leak-reaper` had last succeeded on November 3. Its
Nix rule deletes only matching empty directories older than a day. **This is
still unsafe for a live but idle shell**, as described above. The checked-in
[reaper](../../nixos/modules/tmp-leak-reaper.sh) also has a one-day `bb-run-*`
rule, but the deployed service inspected at this snapshot did not yet contain
that rule. Neither the broader Nix reclamation nor the empty-directory safety
problem has been fixed by the inventory.
