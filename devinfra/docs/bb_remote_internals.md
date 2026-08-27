# `bb remote` Internals

How `bb remote` works end-to-end, from CLI invocation to Bazel execution on the
runner. Bare source paths refer to the
[BuildBuddy source](https://github.com/buildbuddy-io/buildbuddy).

## End-to-end flow

### 1. CLI arg processing (local, no rc expansion)

Source: `cli/cmd/bb/bb.go`, `cli/parser/parser.go`,
`cli/remotebazel/remotebazel.go`

`bb remote` is a **bb CLI command**, dispatched (`interpretAsBBCliCommand`)
_before_ the `ResolveArgs` path that reads rc files and expands `--config`
flags. So **`bb remote` reads NO rc files locally** (`.bazelrc`,
`~/.bazelrc`, `/etc/bazel.bazelrc`) **and expands NO `--config=X` flags
locally** — `--config=rbe` in `~/.config/bazel/buildbuddy.bazelrc` has no
effect on `bb remote` invocations. The only local processing is
`CanonicalizeArgs` (flag format normalization, e.g. `--flag value` →
`--flag=value`); all `--config` flags pass through literally to the runner.

> **Contrast with `bb build`/`bb test`** (direct local Bazel): those go through
> `ResolveArgs` — reads all rc files locally, expands configs, appends
> `--nohome_rc --noworkspace_rc --nosystem_rc`. Those `--no*_rc` flags make
> Bazel's legacy transition check (`option_processor.cc`) warn that `.bazelrc`
> is "no longer being read" — harmless for command-level directives, which `bb`
> already inlined, but **`startup` directives are silently dropped** — bites
> `startup --host_jvm_args=…` users (notably Claude sessions: the
> session-installed trust store never loads). Workaround + proper shim fix:
> <bb_bazelrc_startup.md>.

**bb remote flags**: `bb help remote` lists the current set. Two matter for
git-state sync: `--skip_auto_checkout` skips the runner's automatic GitHub
checkout step; `--use_system_git_credentials` makes the runner use its own
pre-configured GitHub auth instead of `--repo.url`-embedded HTTPS + token.

**NOT a bb flag**: `--remote_header` is a Bazel flag. It must go after the
subcommand, otherwise bb puts it in Bazel startup options and Bazel rejects it.

### 2. `RunRequest` construction

Source: `cli/remotebazel/remotebazel.go` (`parseArgs`), `proto/runner.proto`

bb builds a `RunRequest` protobuf and sends it to the runner service via gRPC:

```text
RunRequest {
  repo: { url, commit_sha, patches[] }
  exec_properties: [from --runner_exec_properties]
  remote_headers: [from --remote_run_header]
  steps: [{
    run: "bazel <subcommand> <user-flags-as-is> <targets> <auto-configs>"
  }]
}
```

`<user-flags-as-is>` still contains the literal `--config=X` flags; expansion
happens on the runner.

**Auto-configs** (hardcoded in `parseArgs`): bb strips any user-supplied
`--bes_backend` and `--remote_cache`, then appends
`--config=buildbuddy_{bes_backend,bes_results_url,remote_cache}` and, for
`build` and non-remote `run`, `--remote_upload_local_results`.

### 3. Runner bootstrap

Source: `enterprise/server/cmd/ci_runner/main.go`

The runner VM receives the `RunRequest` and:

1. **Git checkout**: fetches the commit, applies patches (local diffs).
2. **Writes `buildbuddy.bazelrc`** to the workspace root (`writeBazelrc`),
   defining the auto-config values:

   ```text
   common:buildbuddy_bes_backend --bes_backend=<runner's BES endpoint>
   … (bes_results_url, remote_cache, remote_executor likewise)
   ```

   Values are dynamic — they point to the BB environment that triggered the
   run.

3. **Invokes Bazel** with startup flags (`customBazelrcOptions`):

   ```text
   --bazelrc=buildbuddy.bazelrc --noworkspace_rc --bazelrc=.bazelrc
   ```

   `buildbuddy.bazelrc` gets highest priority; the workspace `.bazelrc` is
   loaded explicitly, with `--noworkspace_rc` preventing a double load.

### 4. Bazel execution on the runner

Bazel on the runner reads `buildbuddy.bazelrc` then `.bazelrc` and expands all
`--config` flags — `--config=rbe` from the workspace `.bazelrc`,
`--config=buildbuddy_*` from `buildbuddy.bazelrc`.

**If you don't pass `--config=rbe` explicitly, RBE is not enabled** — the
runner builds everything locally in linux-sandbox on the runner VM.

## Git state synchronization

Source: `cli/remotebazel/remotebazel.go` (`Config`)

`bb remote` mirrors your local working tree to the runner as a base commit +
patchset —
["automatic git-state mirroring"](https://www.buildbuddy.io/docs/remote-bazel/)
in BuildBuddy's docs. Three phases:

### Phase 1: Determine remote (`determineRemote`)

Runs `git remote -v`, picks a fetch remote. With multiple remotes, prompts the
user and caches the selection in `.git/config`.

### Phase 2: Find base branch + commit (`getBaseBranchAndCommit`)

When `--run_from_branch` and `--run_from_commit` are both empty (auto mode):

1. `getCurrentRef()` → `git symbolic-ref --short HEAD` → e.g. `feature-x` (or
   parses "detached at \<ref\>" from `git branch` output)
2. `branchTrackedRemotely(remote, "feature-x")` → checks if
   `refs/remotes/origin/feature-x` exists locally
3. If yes: `commitTrackedInRemoteBranch(remote, "feature-x", "HEAD")` →
   `git merge-base --is-ancestor HEAD refs/remotes/origin/feature-x`
   - If HEAD is an ancestor of (or equal to) the remote tracking ref:
     `branch=feature-x`, `commit=<HEAD SHA>`
   - If HEAD is ahead (unpushed commits): falls through to default branch
4. **Fallback** (branch doesn't exist remotely, or has unpushed commits):
   - `branch = defaultBranch` (e.g. `devel`)
   - `commit = git rev-parse devel@{upstream}` — the **remote-tracking** commit
     (e.g. `origin/devel`), so an unpushed local `devel` tip is never used as
     the base; unpushed commits are sent as patches instead. Falls back to
     `git rev-parse devel` (the **local** ref) only when `devel` has no
     upstream configured.
   - Resolving `@{upstream}` needs a **local** `devel` branch with tracking
     config — see Gotchas for the CI case.

Net effect: with the current ref on the remote and HEAD an ancestor of it, the
base is HEAD and patches carry only uncommitted changes; in every other case
(unpushed branch, HEAD ahead, detached ref not on remote) the base is
`devel@{upstream}` and patches carry everything since it.

### Phase 3: Generate patches (`generatePatches`)

A patchset of everything that differs between the base commit and the current
working tree:

1. `git diff --binary <baseCommit>` — all tracked changes in one patch
   (`--binary` is inert for text diffs and the only applyable form for binary
   ones, deletions included)
2. `git ls-files --others --exclude-standard` → for each untracked file,
   `git diff --no-index --binary /dev/null <file>` (synthetic "add file" patch)

Patches travel as `RepoState.Patch[]` in the `RunRequest`; the runner clones
at the base commit/branch and `git apply`s each, reproducing your local
working tree.

### Gotchas

- **Fallback base is `origin/devel` via `@{upstream}`**: needs a **local**
  `devel` branch with tracking config. A CI checkout (`actions/checkout`)
  creates only `origin/devel`, so both `@{upstream}` and the
  `git rev-parse devel` fallback fail — `bazel-ci.yml` creates a local `devel`
  ref for PR builds. A stale `origin/devel` puts the diff base hundreds of
  commits behind HEAD: a huge patchset — on `bb` < 5.0.445, possibly an
  unappliable one (next bullet) — instead of the small diff you expect.
  `devinfra/bbr.py`'s `check_base_branch_freshness()` **refuses to run** when
  the tracked base looks stale (`BBR_ALLOW_STALE_BASE=1` overrides); it
  deliberately never fetches (surprise network calls on every command), so on
  that error run `git fetch <buildbuddy-remote> <default-branch>` (and the
  current branch, if pushed, so bb can base on it directly) and retry. Session
  setup (`devinfra/claude/reconcile_bbr_remote.sh`) fetches once, at session
  start.
- **A deleted binary file made the patchset unappliable on `bb` < 5.0.445**:
  `generatePatches` ran `git diff --binary` only for files it detected as
  _modified_ — `isBinaryFile` ran `file --mime` on the working-tree path,
  which cannot classify a deleted file — so a binary file **deleted** since
  the diff base landed in the plain-text patch as a content-less stub, and the
  runner's `git apply` died with
  `cannot apply binary patch to '<file>' without full index line`: every run
  broke during git setup, even on a fresh base, until the deleting commit was
  pushed (moving the base past it). Fixed upstream in
  [buildbuddy#13067](https://github.com/buildbuddy-io/buildbuddy/pull/13067)
  (unconditional `--binary`), released in `bb` 5.0.445; the repo consumes the
  stock release.
- **`--run_from_commit` disables patches**: the runner checks out exactly that
  commit. Patches are only generated when BOTH `--run_from_branch` and
  `--run_from_commit` are empty. Do NOT use `--run_from_commit` in wrapper
  scripts — it silently drops all uncommitted local changes.
- **Large patchsets**: all untracked files are included — a stale `bazel-bin`
  symlink or large generated files can bloat the patchset (`.gitignore`'d
  files are excluded via `--exclude-standard`).
- **Repo-scoped Claude sessions' git `insteadOf` rewrite defeats the
  `github-no-proxy` remote**: web sessions rewrite `origin` to a local
  git-mirroring proxy (`http://127.0.0.1:<port>/git/...`) that the cloud
  runner can't reach, so `devinfra/claude/web_setup.sh` and
  `devinfra/codex_cloud/setup.sh` add a `github-no-proxy` remote pointing
  straight at GitHub, selected via `buildbuddy.remote-bazel-remote-name` (bb
  resolves the URL via `git remote get-url`, which applies the effective
  config). Some sessions ALSO install a **global**
  `url."http://local_proxy@127.0.0.1:<port>/git/".insteadOf = https://github.com/`
  rule, which rewrites **any** remote matching that literal prefix —
  `github-no-proxy` included — back to the same unreachable proxy. Fix: give
  `github-no-proxy` a URL outside the literal prefix, e.g.
  `https://github.com:443/<owner>/<repo>` (functionally identical but not
  literally prefixed, so `insteadOf` skips it).

## Flag taxonomy

- `--runner_exec_properties=K=V` (bb CLI → `RunRequest.ExecProperties`):
  runner VM platform (disk, recycling)
- `--remote_run_header=K=V` (bb CLI → `RunRequest.RemoteHeaders`): gRPC
  metadata for the runner execution request
- `--remote_header=K=V` (Bazel, after the subcommand): gRPC metadata for RBE
  actions (API keys, container image overrides)
- `--build_metadata=K=V` (Bazel, after the subcommand): BES metadata —
  `ROLE=X` → invocation role, `TAGS=a,b` → tags

For bbr's layered configuration (repo config, session bazelrc, env vars), see
`bbr --help`.

## Bazel linux-sandbox and Docker

Bazel's linux-sandbox (non-hermetic mode, the default) creates a new mount
namespace but **inherits the entire host filesystem read-only**, then
selectively makes output paths writable. It does NOT hide host paths.
([`linux-sandbox-pid1.cc`](https://github.com/bazelbuild/bazel/blob/master/src/main/tools/linux-sandbox-pid1.cc)
`MakeFilesystemMostlyReadOnly()` iterates `/proc/self/mounts` and remounts
everything `MS_RDONLY` except whitelisted writable paths.)

**Docker socket access**: `/var/run/docker.sock` is always accessible inside
the sandbox — Unix socket `connect()` works through read-only mounts
(read-only blocks file creation/modification, not socket operations).
`--sandbox_add_mount_pair` is only needed in hermetic mode (`-h`, with
`pivot_root`).

**Docker load gotcha**: `tarfile.TarFile.add()` on symlinks (like Bazel
runfiles) records them as symlink entries with absolute target paths; Docker
extracts and follows them, failing on sandbox-internal targets. Fix:
`tarfile.open(..., dereference=True)` to store file content instead.

## Firecracker VM boot sequence

Source:
`enterprise/server/remote_execution/containers/firecracker/firecracker.go`,
`enterprise/server/cmd/goinit/main.go`, `enterprise/server/vmexec/vmexec.go`

BuildBuddy isolates workloads in Firecracker microVMs. The container image is
NOT run as a Docker container — it's converted to an ext4 filesystem and
mounted as a block device. Both `bb remote` and `bb execute` boot this way
when `workload-isolation-type=firecracker` is set; without that exec property,
`bb execute` uses OCI containers instead (no VM, direct `runc`-style exec into
the container rootfs).

**Host side (executor)**: converts the Docker/OCI image to an ext4 image
(cached by content hash at
`/tmp/${USER}_remote_build/executor/<sha>/containerfs.ext4`), then launches
Firecracker with `goinit` as init and three block devices:

- `/dev/vda` — container rootfs ext4 (read-only)
- `/dev/vdb` — scratch disk ext4 (read-write, overlay upper layer)
- `/dev/vdc` — workspace ext4 (hot-swapped per action)

**Inside the VM**: `goinit` (PID 1, a custom init — it does NOT run the
container's `/init`) mounts `/dev` and `/sys`, assembles an overlayfs
(`lowerdir=/container` from vda, `upperdir=/scratch/bbvmroot` on vdb), pivots
root into it, mounts pseudo-filesystems (`/proc`, `/dev/pts`, `/dev/shm`,
cgroup2, …), creates `/etc/hostname`, `/etc/hosts`, `/etc/resolv.conf`, sets
the hardcoded PATH
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, then spawns
`vmexec` (gRPC server on vsock port 11), `dockerd` (if `--init_dockerd`), and
optionally a DNS server and VFS server.

**Command execution (vmexec)**: the host talks to the VM over vsock (virtio
socket, no TCP). An `Exec` gRPC makes vmexec run `os/exec.Command` with the
requested args, env vars, working dir, and optional UID/GID switch. The
workspace is hot-mounted per action: `MountWorkspace` RPC mounts `/dev/vdc` →
`/workspace`; between actions: unmount, swap disk, remount.

### Implications for container images

- **goinit does NOT run the container's `/init` or systemd.** NixOS activation
  scripts, envfs, and the `programs.nix-ld.enable` systemd unit never run.
- **PATH is hardcoded** to FHS paths. NixOS tools at
  `/run/current-system/sw/bin/` are not on PATH.
- **envfs never starts** — `/bin/bash` must be a real file/symlink, not a FUSE
  resolution.
- **`/etc/passwd` may be overwritten** — goinit creates its own
  `/etc/hostname`, `/etc/hosts`, `/etc/resolv.conf` during boot.
- **Container rootfs is ext4** — all symlinks into `/nix/store/` resolve
  correctly (the whole store is in the ext4 image).
- **NixOS glibc searches nix-store paths only** — not
  `/lib/x86_64-linux-gnu/`, `/usr/lib/`, or `/etc/ld.so.cache`. A
  dynamically-linked binary downloaded at runtime (like Bazel from bazelisk)
  fails to find `libstdc++.so.6` unless `LD_LIBRARY_PATH` is set or nix-ld is
  active.

## Limitations

### `bb remote` only supports bazel commands, not bb commands

`bb remote` dispatches recognized bazel subcommands (`build`, `test`, `query`,
`cquery`, `aquery`, …) to the runner. Non-bazel commands like `mod` are not
recognized — use `--script`:

```bash
bb remote --script 'bazel mod explain protobuf'
```

### Output stream separation

Source: `cli/remotebazel/remotebazel.go` (`streamLogs`, `printLogs`),
`cli/log/log.go`

Remote Bazel output (event log chunks) and ANSI cursor control both go to
**stdout**; CLI messages (`log.Printf`, `log.Warnf`) go to **stderr**.
Interactive mode (`terminal.IsTTY(os.Stdin) && terminal.IsTTY(os.Stderr)`):
`streamLogs()` polls `GetEventLogChunk()`, redrawing "live" chunks with ANSI
cursor escapes. Non-interactive (piped): `printLogs()` waits for each chunk to
finalize and writes raw bytes, no ANSI escapes.

**Extracting clean output programmatically**:

1. **Pipe stdout** — non-interactive mode activates when stdout is not a TTY,
   producing clean bazel output on stdout with CLI noise on stderr:

   ```bash
   RESULT=$(bb remote query 'deps(//foo)' 2>/dev/null)
   ```

2. **`--invocation_id_file`** — write the invocation ID to a file, then fetch
   logs post-hoc via the BuildBuddy API. `bbr` does this automatically
   (`~/.cache/bbr/last_invocation_id`) and prints a post-run summary with
   `bbapi` commands.
3. **`--script` + file redirect** — redirect bazel output to a file on the
   runner, download via `--remote_download_regex`.

## Downloaded artifacts land under `bb-out/bazel-out/`, NOT `bb-out/bazel-bin/`

Outputs fetched back by `--remote_download_outputs=toplevel` or
`--remote_download_regex=...` land at
`bb-out/bazel-out/<config>/bin/<pkg>/<name>`; `<config>` is `k8-fastbuild` for
our standard Linux x86_64 RBE builds. **There is NO
`bb-out/bazel-bin/<pkg>/<name>` convenience symlink** — it exists only in
local Bazel workspaces. Consumers of bb-remote-built artifacts (e.g.
`push-images.yml`) must use the full path:

```bash
bbr build //:requirements --remote_download_regex='.*requirements\.out'
cp bb-out/bazel-out/k8-fastbuild/bin/requirements.out requirements_bazel.txt
```

## Server-side: how `Run` becomes an RE action

Source: `enterprise/server/hostedrunner/hostedrunner.go`

`runnerService.Run()` translates the bespoke `RunRequest` into a standard
Remote Execution API action:

1. Uploads the input root (ci_runner + support files) to CAS, and each
   `RepoState.Patch[]` blob via bytestream — CAS URIs become `--patch_uri`
   args
2. Base64-encodes the steps YAML into `--serialized_action`
3. Builds a `Command` proto running ci_runner with backend endpoints
   (`--bes_backend`, `--cache_backend`, `--rbe_backend`), repo state
   (`--target_repo_url`, `--pushed_branch`, `--commit_sha`, `--patch_uri`),
   and `--invocation_id`
4. Calls standard RE `Execute()` (`SkipCacheLookup: true`,
   `DigestFunction: BLAKE3`), waits for the first `Operation` (execution
   created), returns the invocation ID to the CLI

### Client-side completion tracking

The CLI tracks the execution over two parallel paths:

- **BB bespoke API** (`BuildBuddyServiceClient`): `GetEventLogChunk` for live
  log streaming, `GetInvocation` for final invocation metadata, `GetExecution`
  to look up the execution ID, `CancelExecutions` on interrupt
- **Standard RE API** (`ExecutionClient`): `WaitExecution` on the execution ID
  for the final `ExecuteResponse` (exit code)

The bespoke APIs exist because RE `WaitExecution` only provides `Operation`
status updates, not live stdout or invocation-level metadata.

## bb CLI configuration (non-Bazel-flag)

Source: `cli/storage/storage.go`, `cli/config/config.go`, `cli/login/login.go`

Dotfiles:

- `$BUILDBUDDY_CONFIG_DIR/buildbuddy.yaml` (default `~/.config/buildbuddy/`) —
  plugins, local cache config
- `<workspace>/buildbuddy.yaml` — same schema, higher precedence
- `.git/config [buildbuddy]` section — API key (`api-key`), remote-bazel
  remote name, default branch

API key resolution order: `BUILDBUDDY_API_KEY` env → `.git/config` →
interactive login.

Environment variables:

- `BUILDBUDDY_API_KEY` — API key
- `BUILDBUDDY_CONFIG_DIR` / `BUILDBUDDY_CACHE_DIR` — override config/cache dir
- `BB_USE_BAZEL_VERSION` — override Bazel version (takes precedence over
  `USE_BAZEL_VERSION`)
- `BB_DISABLE_SIDECAR` — `1`/`true` disables the local sidecar; `CI` truthy
  also disables it
- `GIT_REPO_DEFAULT_BRANCH` — override default branch detection for
  `bb remote`
- `BAZELISK_SKIP_WRAPPER` — `true` makes bb behave as plain bazelisk

**None of these inject Bazel flags.** For `bb remote`, Bazel flags come only
from the CLI command line and the workspace `.bazelrc` (loaded by ci_runner).
