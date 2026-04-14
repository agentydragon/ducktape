# `bb remote` Internals

How `bb remote` works end-to-end, from CLI invocation to Bazel execution on the
runner. Based on reading the BuildBuddy source at
`/code/github.com/buildbuddy-io/buildbuddy`.

## End-to-end flow

### 1. CLI arg processing (local, no rc expansion)

Source: `cli/cmd/bb/bb.go`, `cli/remotebazel/remotebazel.go`

`bb remote` is a **bb CLI command**, dispatched at `bb.go:137`
(`interpretAsBBCliCommand`) _before_ the `ResolveArgs` path (line 172) that
reads rc files and expands `--config` flags. This means:

- **`bb remote` does NOT read `.bazelrc`, `~/.bazelrc`, or
  `/etc/bazel.bazelrc` locally.**
- **`bb remote` does NOT expand `--config=X` flags locally.**
- `--config=rbe` in `~/.config/bazel/buildbuddy.bazelrc` has **no effect** on
  `bb remote` invocations.

The only local processing is `CanonicalizeArgs` (flag format normalization,
e.g., `--flag value` → `--flag=value`). All `--config` flags are passed
through literally to the runner.

> **Contrast with `bb build`/`bb test`** (direct local Bazel): These go through
> `ResolveArgs`, which reads all rc files locally, expands configs, and appends
> `--nohome_rc --noworkspace_rc --nosystem_rc`. The "no longer being read"
> warning from Bazel is triggered by these `--no*_rc` flags — Bazel's legacy
> transition check (`option_processor.cc`) sees that `.bazelrc` exists but isn't
> in the read set and warns. Harmless — `bb` already consumed those files.

**bb remote flags** (partial list): `--runner_exec_properties`,
`--run_from_commit`, `--run_from_branch`, `--remote_run_header`,
`--container_image`, `--os`, `--arch`, `--timeout`, `--script`, `--env`.

**NOT a bb flag**: `--remote_header` is a Bazel flag. It must go after the
subcommand, otherwise bb puts it in Bazel startup options and Bazel rejects it.

### 2. `RunRequest` construction

Source: `cli/remotebazel/remotebazel.go`, `parseArgs()` ~line 1298

bb builds a `RunRequest` protobuf and sends it to the runner service via gRPC:

```
RunRequest {
  repo: { url, commit_sha, patches[] }
  exec_properties: [from --runner_exec_properties]
  remote_headers: [from --remote_run_header]
  steps: [{
    run: "bazel <subcommand> <user-flags-as-is> <targets> <auto-configs>"
  }]
}
```

`<user-flags-as-is>` includes literal `--config=X` flags — they are NOT
expanded. The runner's Bazel will expand them against the workspace `.bazelrc`.

**Auto-configs** (hardcoded in `parseArgs`): bb strips any user-supplied
`--bes_backend` and `--remote_cache`, then appends:

- `--config=buildbuddy_bes_backend`
- `--config=buildbuddy_bes_results_url`
- `--config=buildbuddy_remote_cache`
- `--remote_upload_local_results` (for `build` and non-remote `run`)

### 3. Runner bootstrap

Source: `enterprise/server/cmd/ci_runner/main.go`

The runner VM receives the `RunRequest` and:

1. **Git checkout**: fetches the commit, applies patches (local diffs).
2. **Writes `buildbuddy.bazelrc`** to the workspace root (`writeBazelrc`,
   ~line 2201). This file defines the auto-config values:
   ```
   common:buildbuddy_bes_backend --bes_backend=<runner's BES endpoint>
   common:buildbuddy_bes_results_url --bes_results_url=<runner's results URL>
   common:buildbuddy_remote_cache --remote_cache=<runner's cache endpoint>
   common:buildbuddy_remote_executor --remote_executor=<runner's RBE endpoint>
   ```
   Values are dynamic — they point to the same BB environment that triggered
   the run.
3. **Invokes Bazel** with startup flags (`customBazelrcOptions`, line ~1625):
   ```
   --bazelrc=buildbuddy.bazelrc --noworkspace_rc --bazelrc=.bazelrc
   ```
   This ensures `buildbuddy.bazelrc` has highest priority, then the workspace
   `.bazelrc` is loaded explicitly (via `--bazelrc`) while suppressing the
   default workspace rc loading (`--noworkspace_rc`) to avoid double-loading.

### 4. Bazel execution on the runner

Bazel on the runner reads `buildbuddy.bazelrc` and `.bazelrc` (in that
priority order), and expands all `--config` flags. For example,
`--config=rbe` expands using the workspace `.bazelrc` definitions
(`--remote_executor`, `--remote_header`, `--extra_execution_platforms`, etc.).
`--config=buildbuddy_*` expands using `buildbuddy.bazelrc` definitions.

**If you don't pass `--config=rbe` explicitly, RBE is not enabled.** The
runner builds everything locally in linux-sandbox on the runner VM.

### Verified by experiment (2026-04-08)

```
# No --config=rbe → runner builds locally (57 linux-sandbox actions)
bb remote build //devinfra:gazelle --config=nolint

# Explicit --config=rbe → runner fans out to RBE (64 remote cache hits)
bb remote build //devinfra:gazelle --config=nolint --config=rbe
```

## Git state synchronization

Source: `cli/remotebazel/remotebazel.go`, `Config()` line 368

`bb remote` mirrors your local working tree to the runner as a base commit +
patchset. The logic has three phases:

### Phase 1: Determine remote (`determineRemote`, line 162)

Runs `git remote -v`, picks a fetch remote. With multiple remotes, prompts
the user and caches the selection in `.git/config`.

### Phase 2: Find base branch + commit (`getBaseBranchAndCommit`, line 404)

When `--run_from_branch` and `--run_from_commit` are both empty (auto mode):

1. `getCurrentRef()` → `git symbolic-ref --short HEAD` → e.g., `feature-x`
   (or parses "detached at \<ref\>" from `git branch` output)
2. `branchTrackedRemotely(remote, "feature-x")` → checks if
   `refs/remotes/origin/feature-x` exists locally
3. If yes: `commitTrackedInRemoteBranch(remote, "feature-x", "HEAD")` →
   `git merge-base --is-ancestor HEAD refs/remotes/origin/feature-x`
   - If HEAD is an ancestor of (or equal to) the remote tracking ref:
     `branch=feature-x`, `commit=<HEAD SHA>`
   - If HEAD is ahead (unpushed commits): falls through to default branch
4. **Fallback** (branch doesn't exist remotely, or has unpushed commits):
   - `branch = defaultBranch` (e.g., `devel`)
   - `commit = git rev-parse devel` — uses the **local** ref, not
     `origin/devel`

### Phase 3: Generate patches (`generatePatches`, line 514)

Generates a patchset of everything that differs between the base commit and
the current working tree:

1. `git diff <baseCommit>` — tracked modified files (text), as unified diff
2. `git diff <baseCommit> --binary -- <files>` — binary modified files
3. `git ls-files --others --exclude-standard` → for each untracked file,
   `git diff --no-index /dev/null <file>` (synthetic "add file" patch)

All patches are sent as `RepoState.Patch[]` in the `RunRequest`.

### Runner side

The runner clones the repo at the base commit/branch, then applies each patch
with `git apply`. Result: workspace matches your local working tree.

### Scenario matrix

| Scenario                                   | Base branch       | Base commit                 | Patches contain                  |
| ------------------------------------------ | ----------------- | --------------------------- | -------------------------------- |
| Local branch, pushed, HEAD on remote       | `feature-x`       | HEAD SHA                    | uncommitted changes only         |
| Local branch, pushed, HEAD ahead of remote | `devel` (default) | local `git rev-parse devel` | all branch commits + uncommitted |
| Local branch, not pushed                   | `devel` (default) | local `git rev-parse devel` | all branch commits + uncommitted |
| Detached HEAD, ref exists remotely         | detached ref      | ref SHA                     | uncommitted changes only         |
| Detached HEAD, ref not on remote           | `devel` (default) | local `git rev-parse devel` | everything since devel           |

### Gotchas

- **Local devel ref used, not origin/devel**: If your local `devel` is behind
  `origin/devel`, the base commit may not exist on the remote → runner clone
  fails. Run `git fetch` first.
- **`--run_from_commit` disables patches**: When set, the runner checks out
  exactly that commit. Patches are only generated when BOTH `--run_from_branch`
  and `--run_from_commit` are empty. Do NOT use `--run_from_commit` in wrapper
  scripts — it silently drops all uncommitted local changes.
- **Large patchsets**: All untracked files are included. A stale `bazel-bin`
  symlink or large generated files can bloat the patchset (though
  `.gitignore`'d files are excluded via `--exclude-standard`).

## Flag taxonomy

| Flag                           | Owned by | Where it goes                 | Purpose                                                             |
| ------------------------------ | -------- | ----------------------------- | ------------------------------------------------------------------- |
| `--runner_exec_properties=K=V` | bb CLI   | `RunRequest.ExecProperties`   | Runner VM platform (disk, recycling)                                |
| `--remote_run_header=K=V`      | bb CLI   | `RunRequest.RemoteHeaders`    | gRPC metadata for the runner execution request                      |
| `--remote_header=K=V`          | Bazel    | Bazel args (after subcommand) | gRPC metadata for RBE actions (API keys, container image overrides) |
| `--build_metadata=K=V`         | Bazel    | Bazel args (after subcommand) | BES metadata: `ROLE=X` → invocation role, `TAGS=a,b` → tags         |

For bbr's layered configuration (repo config, session bazelrc, env vars), see `bbr --help`.

## Bazel linux-sandbox and Docker

Bazel's linux-sandbox (non-hermetic mode, the default) creates a new mount
namespace but **inherits the entire host filesystem read-only**. It then
selectively makes output paths writable. It does NOT hide host paths.

Source: [`src/main/tools/linux-sandbox-pid1.cc`](https://github.com/bazelbuild/bazel/blob/master/src/main/tools/linux-sandbox-pid1.cc) — `MakeFilesystemMostlyReadOnly()`
iterates `/proc/self/mounts` and remounts everything `MS_RDONLY` except
whitelisted writable paths.

**Docker socket access**: `/var/run/docker.sock` is always accessible inside the
sandbox because Unix socket `connect()` works through read-only mounts (read-only
blocks file creation/modification, not socket operations).
`--sandbox_add_mount_pair` is only needed in hermetic mode (`-h` flag with
`pivot_root`), not the default non-hermetic mode.

**Docker load gotcha**: `tarfile.TarFile.add()` on symlinks (like Bazel runfiles)
records them as symlink entries with absolute target paths. Docker extracts the
tarball and tries to follow the symlinks, which fail when the targets are
sandbox-internal paths. Fix: `tarfile.open(..., dereference=True)` to store file
content instead of symlinks.

## Firecracker VM boot sequence

Source: `enterprise/server/remote_execution/containers/firecracker/firecracker.go`,
`enterprise/server/cmd/goinit/main.go`, `enterprise/server/vmexec/vmexec.go`

BuildBuddy uses Firecracker microVMs for workload isolation. The container image
is NOT run as a Docker container — it's converted to an ext4 filesystem and
mounted as a block device in a Firecracker VM.

### Host side (executor)

1. **Image conversion**: Docker/OCI image → ext4 filesystem (`containerfs.ext4`),
   cached by content hash at `/tmp/${USER}_remote_build/executor/<sha>/containerfs.ext4`
2. **VM disk layout** — 3 block devices:
   - `/dev/vda` — container rootfs ext4 (read-only)
   - `/dev/vdb` — scratch disk ext4 (read-write, overlay upper layer)
   - `/dev/vdc` — workspace ext4 (hot-swapped per action)
3. **Launch Firecracker** with `goinit` as init, kernel args like
   `ro console=ttyS0 reboot=k panic=1 pci=off`

### Inside the VM (goinit, PID 1)

`goinit` is a custom init process — it does NOT run the container's `/init`.

4. **Mount basics**: `/dev` (devtmpfs), `/sys` (sysfs)
5. **Overlay assembly**:
   - Mount `/dev/vda` → `/container` (read-only)
   - Mount `/dev/vdb` → `/scratch` (read-write)
   - Create overlayfs: `lowerdir=/container, upperdir=/scratch/bbvmroot` → `/mnt`
6. **Pivot root** to `/mnt` — container rootfs is now `/`
7. **Pseudo-filesystems**: `/proc`, `/dev/pts`, `/dev/shm`, cgroup2, etc.
8. **Create `/etc/hostname`, `/etc/hosts`, `/etc/resolv.conf`**
9. **Set PATH** to `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
   (hardcoded in goinit)
10. **Spawn children**:
    - `vmexec` gRPC server on vsock port 11 (receives exec requests from host)
    - `dockerd` (if `--init_dockerd` flag)
    - Optional: DNS server, VFS server
11. **Wait forever** (child reaper handles SIGCHLD)

### Command execution (vmexec)

The host communicates with the VM over vsock (virtio socket, no TCP needed):

12. Host sends `Exec` gRPC → vmexec runs `os/exec.Command` with requested
    args, env vars, working dir, and optional UID/GID switch
13. **Workspace** is hot-mounted: `MountWorkspace` RPC → mounts `/dev/vdc` →
    `/workspace`. Between actions: unmount, swap disk, remount.

### Implications for container images

- **goinit does NOT run the container's `/init` or systemd.** NixOS activation
  scripts, envfs, nix-ld systemd services — none of these run.
- **PATH is hardcoded** to FHS paths. NixOS tools at
  `/run/current-system/sw/bin/` are not on PATH.
- **envfs never starts** — `/bin/bash` must be a real file/symlink, not a FUSE
  resolution.
- **nix-ld activation doesn't happen** — `programs.nix-ld.enable` creates a
  systemd unit that never runs.
- **`/etc/passwd` may be overwritten** — goinit creates its own
  `/etc/hostname`, `/etc/hosts`, `/etc/resolv.conf` during boot.
- **Container rootfs is ext4** — all symlinks into `/nix/store/` resolve
  correctly (the whole store is in the ext4 image).
- **NixOS glibc searches nix-store paths only** — it does NOT search
  `/lib/x86_64-linux-gnu/`, `/usr/lib/`, or read `/etc/ld.so.cache` from the
  FHS path. Any dynamically-linked binary downloaded at runtime (like Bazel
  from bazelisk) will fail to find `libstdc++.so.6` unless `LD_LIBRARY_PATH`
  is set or nix-ld is active.

### `bb execute` vs `bb remote` Firecracker behavior

Both use the same Firecracker boot sequence when
`workload-isolation-type=firecracker` is set. `bb execute` without
`-exec_properties=workload-isolation-type=firecracker` uses OCI containers
instead (no VM, direct `runc`-style exec into the container rootfs).

## Limitations

### `bb remote` only supports bazel commands, not bb commands

`bb remote` dispatches recognized bazel subcommands (`build`, `test`, `query`,
`cquery`, `aquery`, etc.) to the runner. Non-bazel commands like `mod` are not
recognized. Use `--script` mode instead:

```bash
# Does not work:
bb remote mod explain protobuf

# Use --script:
bb remote --script 'bazel mod explain protobuf'
```

### Output stream separation

Source: `cli/remotebazel/remotebazel.go` (`streamLogs`, `printLogs`), `cli/log/log.go`

| Source                                   | Destination                    |
| ---------------------------------------- | ------------------------------ |
| Remote Bazel output (event log chunks)   | **stdout** (`os.Stdout.Write`) |
| CLI messages (`log.Printf`, `log.Warnf`) | **stderr** (Go default logger) |
| ANSI cursor control (progress rewriting) | **stdout** (`fmt.Print`)       |

Interactive mode (detected via `terminal.IsTTY(os.Stdin) && terminal.IsTTY(os.Stderr)`):

- **Interactive**: `streamLogs()` — polls `GetEventLogChunk()`, redraws "live"
  chunks with ANSI cursor-up/delete-line escape sequences on stdout
- **Non-interactive** (piped): `printLogs()` — waits for each chunk to finalize,
  writes raw bytes to stdout, no ANSI escapes

**Extracting clean output programmatically**:

1. **Pipe stdout** — non-interactive mode activates automatically when stdout is
   not a TTY, producing clean bazel output on stdout with CLI noise on stderr:
   ```bash
   RESULT=$(bb remote query 'deps(//foo)' 2>/dev/null)
   # or force non-interactive:
   bb remote query 'deps(//foo)' | cat
   ```
2. **`--invocation_id_file`** — write the invocation ID to a file, then fetch
   logs post-hoc via the BuildBuddy API. `bbr` does this automatically
   (`~/.cache/bbr/last_invocation_id`) and prints a post-run summary with
   `bbapi` commands for fetching targets, logs, and artifacts.
3. **`--script` + file redirect** — redirect bazel output to a file on the
   runner, download via `--remote_download_regex`

## Downloaded artifacts land under `bb-out/bazel-out/`, NOT `bb-out/bazel-bin/`

When `bb remote build //pkg:name --remote_download_outputs=toplevel` (or
`--remote_download_regex=...`) fetches build outputs back to the local
workspace, they land at:

```
bb-out/bazel-out/<config>/bin/<pkg>/<name>
```

The `<config>` for our standard Linux x86_64 RBE builds (via
`--config=rbe --config=ci` from `.github/actions/bb-remote/`) is
`k8-fastbuild`. So `bb remote build //x/grocy_mcp:server_image.digest`
lands at `bb-out/bazel-out/k8-fastbuild/bin/x/grocy_mcp/server_image.json.sha256`.

**There is NO `bb-out/bazel-bin/<pkg>/<name>` convenience symlink.** That
symlink only exists in local Bazel workspaces — it's created by Bazel's
local runner, not by `bb remote`. Workflows that consume bb-remote-built
artifacts on the runner side (e.g. `push-images.yml` after PR #1290)
must use the full `bb-out/bazel-out/k8-fastbuild/bin/...` path.

`bbr` follows the same layout, as shown in `CLAUDE.md`:

```bash
bbr build //:requirements --remote_download_regex='.*requirements\.out'
cp bb-out/bazel-out/k8-fastbuild/bin/requirements.out requirements_bazel.txt
```

## Key source files

All paths relative to <https://github.com/buildbuddy-io/buildbuddy>.

| File                                                                                                                                                                                                           | Purpose                                                                           |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| [`cli/cmd/bb/bb.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/cmd/bb/bb.go)                                                                                                                 | Entry point; dispatches bb CLI commands before `ResolveArgs`                      |
| [`cli/parser/parser.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/parser/parser.go)                                                                                                         | `ResolveArgs` (rc reading + config expansion) vs `CanonicalizeArgs` (format only) |
| [`cli/remotebazel/remotebazel.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/remotebazel/remotebazel.go)                                                                                     | `bb remote` flag parsing, `RunRequest` construction, auto-config injection        |
| [`enterprise/server/cmd/ci_runner/main.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/cmd/ci_runner/main.go)                                                                   | Runner bootstrap, `buildbuddy.bazelrc` generation, Bazel invocation               |
| [`enterprise/server/hostedrunner/hostedrunner.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/hostedrunner/hostedrunner.go)                                                     | Runner service, processes `RunRequest`, handles remote headers                    |
| [`proto/runner.proto`](https://github.com/buildbuddy-io/buildbuddy/blob/master/proto/runner.proto)                                                                                                             | `RunRequest` protobuf definition                                                  |
| [`enterprise/server/cmd/goinit/main.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/cmd/goinit/main.go)                                                                         | Firecracker VM init process (PID 1), mounts, pivot root, spawns vmexec            |
| [`enterprise/server/vmexec/vmexec.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/vmexec/vmexec.go)                                                                             | VM exec service: runs commands via gRPC over vsock, workspace mount/unmount       |
| [`enterprise/server/remote_execution/containers/firecracker/firecracker.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/remote_execution/containers/firecracker/firecracker.go) | Firecracker container orchestration, image conversion, VM lifecycle               |
| [`cli/storage/storage.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/storage/storage.go)                                                                                                     | `ConfigDir`, `CacheDir`, `.git/config [buildbuddy]` read/write                    |
| [`cli/config/config.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/config/config.go)                                                                                                         | `buildbuddy.yaml` loading (user + workspace), plugin/local-cache config           |
| [`cli/login/login.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/login/login.go)                                                                                                             | API key resolution (`BUILDBUDDY_API_KEY` env → `.git/config` → interactive login) |

## Server-side: how `Run` becomes an RE action

Source: `enterprise/server/hostedrunner/hostedrunner.go`

The `runnerService.Run()` RPC translates the bespoke `RunRequest` into a
standard Remote Execution API action:

1. **Upload input root** — the ci_runner binary and supporting files go to CAS
2. **Upload patches** — each `RepoState.Patch[]` blob is uploaded via bytestream,
   producing CAS URIs passed as `--patch_uri` args to ci_runner
3. **Serialize action** — the steps YAML is base64-encoded into
   `--serialized_action` arg
4. **Build `Command` proto** — ci_runner binary with args:
   `--bes_backend`, `--cache_backend`, `--rbe_backend`, `--invocation_id`,
   `--target_repo_url`, `--pushed_branch`, `--commit_sha`, `--patch_uri`, etc.
5. **Call standard RE `Execute()`** — `ExecuteRequest` with the action digest,
   `SkipCacheLookup: true`, `DigestFunction: BLAKE3`
6. **Wait for first `Operation`** from the stream (ensures execution is created),
   then return invocation ID to the CLI

### Client-side completion tracking

The CLI uses **two parallel paths** to track the execution:

- **BB bespoke API** (`BuildBuddyServiceClient`): `GetEventLogChunk` for live
  log streaming, `GetInvocation` for final invocation metadata, `GetExecution`
  to look up the execution ID, `CancelExecutions` on interrupt
- **Standard RE API** (`ExecutionClient`): `WaitExecution` on the execution ID
  to get the final `ExecuteResponse` (exit code)

Both are standard — the RE action is a normal execution on BB's infrastructure.
The bespoke APIs exist because RE `WaitExecution` only provides `Operation`
status updates, not live stdout or invocation-level metadata.

## bb CLI configuration (non-Bazel-flag)

Source: `cli/storage/storage.go`, `cli/config/config.go`, `cli/login/login.go`

### Dotfiles

| File                                                                       | Purpose                                                       |
| -------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `$BUILDBUDDY_CONFIG_DIR/buildbuddy.yaml` (default `~/.config/buildbuddy/`) | Plugins, local cache config                                   |
| `<workspace>/buildbuddy.yaml`                                              | Same schema, higher precedence                                |
| `.git/config [buildbuddy]` section                                         | API key (`api-key`), remote-bazel remote name, default branch |

Both YAML files support `plugins` (list of repos/paths) and `local_cache`
(enabled, max_size, root_directory). Env vars in YAML are expanded via
`os.ExpandEnv`.

### Environment variables

| Variable                   | Purpose                                                            |
| -------------------------- | ------------------------------------------------------------------ |
| `BUILDBUDDY_API_KEY`       | API key (checked before `.git/config`)                             |
| `BUILDBUDDY_CONFIG_DIR`    | Override config dir                                                |
| `BUILDBUDDY_CACHE_DIR`     | Override cache dir                                                 |
| `BB_USE_BAZEL_VERSION`     | Override Bazel version (takes precedence over `USE_BAZEL_VERSION`) |
| `BB_DISABLE_SIDECAR`       | Set to `1`/`true` to disable the local sidecar                     |
| `BB_SIDECAR_ARGS`          | Extra args passed to the sidecar process                           |
| `BB_WATCHER_LOCKFILE_PATH` | Override watcher lockfile path                                     |
| `GIT_REPO_DEFAULT_BRANCH`  | Override default branch detection for `bb remote`                  |
| `BAZELISK_SKIP_WRAPPER`    | Set to `true` to make bb behave as plain bazelisk                  |
| `CI`                       | Disables sidecar when truthy                                       |

**None of these inject Bazel flags.** For `bb remote`, Bazel flags come only
from the CLI command line and the workspace `.bazelrc` (loaded by ci_runner).

## Future: custom runner orchestration

`bb remote` output is verbose and not designed for programmatic consumption.
Long-term, we could implement our own runner orchestration that calls
BuildBuddy's hosted runner API directly (via the `RunWorkflow` / `Run` RPCs
in `runner.proto`), giving full control over output format, invocation ID
extraction, and post-run reporting. This would replace `bb remote` in `bbr`.
