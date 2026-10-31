# `bb remote` Internals

How `bb remote` works end-to-end, from CLI invocation to Bazel execution on the
runner. Bare source paths refer to the
[BuildBuddy source](https://github.com/buildbuddy-io/buildbuddy).

Configuration behavior checked on 2026-10-30 against the repo-pinned CLI
**5.0.466** (`nix/packages/bb.nix`) and upstream commit
[`d84e5dfd`](https://github.com/buildbuddy-io/buildbuddy/tree/d84e5dfd97439eeefbbf247681de47f3960adc11).
CLI behavior and hosted-service behavior have separate versions; upstream server
source does not establish which revision the hosted service runs.

## End-to-end flow

### 1. CLI arg processing (local, no Bazel rc expansion)

Source: `cli/cmd/bb/bb.go`, `cli/parser/parser.go`,
`cli/remotebazel/remotebazel.go`

`bb remote` is a **bb CLI command**. Its local dispatch expands **`.bbrc`**
settings through `resolveBBCliCommandArgs` / `ResolveBBArgs`, including
`--runner_exec_properties` and `--bb_config` (see Configuration below).
It does **not** expand local **Bazel rc files** (`.bazelrc`, `~/.bazelrc`,
`/etc/bazel.bazelrc`) or Bazel `--config=X` flags. Those arguments pass to
Bazel on the hosted runner. A config present only in the caller's home rc
therefore has no effect there.

Direct local `bb build` / `bb test` instead expand both `.bbrc` and Bazel rc
files locally. The current parser forwards startup directives and adds
`--ignore_all_rc_files` to prevent Bazel loading those files a second time.
The older startup-option loss is historical; see <bazelrc_startup.md>.

**bb remote flags**: `bb help remote` lists the current set. Two matter for
git-state sync: `--skip_auto_checkout` skips the runner's automatic GitHub
checkout step; `--use_system_git_credentials` makes the runner use its own
pre-configured GitHub auth instead of `--repo.url`-embedded HTTPS + token.

`bbr` and the GitHub `bb-remote` action pass `--disable_retry=true` before the Bazel
verb or `--script`. The CLI otherwise retries the **entire remote command** after
some transient observer errors, even when the first runner accepted and ran it.
If observing the run fails, treat its outcome as unknown and inspect the outer
runner and child Bazel invocation IDs before manually resubmitting. This option
does not change Bazel's RBE action retries or remote cache. See the
[observed replay](../debug/ci_remote_cancellation/vm_shutdown_retry.md).
The explicit `=true` is required by the pinned CLI: its remote-flag removal
does not strip a bare boolean `--disable_retry` before forwarding Bazel startup
options.

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

   The workspace `.bazelrc` is explicitly loaded **after** the generated rc,
   giving its directives higher file precedence. `--noworkspace_rc` prevents
   a double load. These flags do not disable the runner's system or home rc.
   Explicit command-line flags and config expansion also affect final values.

### 4. Bazel execution on the runner

Bazel on the runner reads `buildbuddy.bazelrc` then `.bazelrc` and expands all
`--config` flags — `--config=rbe` from the workspace `.bazelrc`,
`--config=buildbuddy_*` from `buildbuddy.bazelrc`.

Remote execution requires `--remote_executor`, supplied directly or through
rc/config expansion. In this repo, the workspace `.bazelrc` imports
`devinfra/bazel/rbe.bazelrc`, which enables `--config=rbe` for build-like
commands; callers need not repeat it. `bb remote` alone does not imply
that nested Bazel actions execute remotely.

## Credential and locality boundary

`bb remote` has two BuildBuddy hops, and they do not share the caller's
Agentplane identity:

1. The local `bb` CLI makes the outer `BuildBuddyService.Run` request. A local
   egress proxy can authenticate that request by substituting a configured
   placeholder in its whole metadata value.
2. BuildBuddy later starts a Bazel process on the hosted runner. That process
   reads `RunRequest.steps[].run` and makes its own nested BES, cache, and
   Remote Execution requests. The hosted process is outside the local Sandbox
   and does not inherit the caller Pod's workload token or egress path.

The current proxy contract substitutes whole HTTP headers and gRPC metadata;
it intentionally does not rewrite arbitrary request bodies. Consequently, a
placeholder copied into `steps[].run` remains a placeholder on the hosted
runner. The outer request can authenticate successfully while the nested Bazel
process fails authentication.

This is especially important for live tests that need the staging Kubernetes
API. A direct `bb run` keeps the completed binary on the caller, where the
caller can hold the intended Kubernetes access. `bbr`/`bb remote` runs that
binary on BuildBuddy infrastructure, where the current Sandbox-bound
credential-substitution design cannot authenticate it to the staging cluster.

Do not use hosted `bb remote` as a credentialless route to caller-local
services, and do not put a real staging credential into the hosted command as a
workaround. Hosted-runner authentication requires a separately designed
boundary, such as a runner-side credential reference or a run-scoped gateway.
A narrow BuildBuddy-specific `RunRequest` body rewrite is a possible weaker
boundary, but it gives the real BuildBuddy credential to hosted agent-controlled
code and is not equivalent to Sandbox workload authentication. The design
tradeoff and required evidence are tracked in
[`agentplane/docs/buildbuddy_remote_auth.md`](../../../agentplane/docs/buildbuddy_remote_auth.md).

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
  `devinfra/buildbuddy/bbr.py`'s `check_base_branch_freshness()` **refuses to run** when
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
  CI deliberately supplies both flags: the commit pins the source while the
  event's branch populates BuildBuddy's `BRANCH_NAME` metadata and branch filters.
- **Local clone sources cannot be fetched by hosted runners**: `bbr` rejects
  filesystem paths and `file://` remotes after applying Git's `insteadOf` rewrites.
  Configure a reachable Git remote and select it with
  `git config buildbuddy.remote-bazel-remote-name <name>`.
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

`bb remote` dispatches recognized Bazel subcommands, including `mod` and
`vendor`, to the runner. For shell commands or bb-specific commands, use
`--script` instead. For example:

```bash
bb remote --script 'git --no-pager status --short'
```

**Gotcha: `git` can page inside `--script`.** A `git diff` there stopped at `less`'s `:` prompt
and hung the run until the client's 28-minute timeout (2026-09-26); the same script with
`git --no-pager` finished in about a minute. Use `git --no-pager`, or `export GIT_PAGER=cat` at
the top of the script.

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

1. **Make stdin or stderr non-interactive** — piping stdout alone does not
   disable the interactive renderer, which checks stdin and stderr. Redirecting
   stderr in this example selects raw chunk output:

   ```bash
   RESULT=$(bb remote query 'deps(//foo)' 2>/dev/null)
   ```

2. **Pass `--invocation_id=<uuid>` after the verb**, then fetch logs post-hoc
   via the BuildBuddy API. `bbr` does this automatically, prints the ID before
   and after the run, and records it with `--invocation-id-file=PATH`
   (§ Invocation IDs).
3. **`--script` + file redirect** — redirect bazel output to a file on the
   runner, download via `--remote_download_regex`.

## Invocation IDs

A remote run produces two invocations:

- **Outer (runner)**: minted server-side by hostedrunner, passed to ci_runner
  via `--invocation_id`, returned to the CLI in `RunResponse` — the ID
  `--invocation_id_file` records. Carries the runner log; its recorded command
  is `remote <cmd> <first-target>`.
- **Inner (Bazel)**: the `bazel` invocation ci_runner executes, carrying
  targets, test results, and artifacts. Bazel mints its ID randomly unless
  `--invocation_id=<uuid>` appears after the verb — bb forwards bazel args
  verbatim, and ci_runner's bazel wrapper strips the flag only from the
  `EXPLICIT_COMMAND_LINE` UI metadata, not from the real command. It links
  back to the outer run via `PARENT_INVOCATION_ID` build metadata.

`bbr` mints the inner ID itself (uuid4, appended after the verb) and prints it
before the run and in the post-run summary. For scripting, either pass your
own `--invocation_id=<uuid>` after the verb (adopted, not overridden) or use
bbr's own `--invocation-id-file=PATH` flag, which writes the ID to that path
before the run:

```bash
bbr --invocation-id-file=/tmp/bbr-inv test //foo:bar
bbapi invocation "$(cat /tmp/bbr-inv)"
```

`bbapi` commands take either ID (outer auto-resolves to children), but the
inner one names the targets/logs/artifacts directly.

**Rejected: reading the ID back from `--invocation_id_file`.** bb writes that
file once, at the end of a successfully tracked run, and bbr pointed every run
at one shared per-user path — so a concurrent, failed, or interrupted run
leaves another run's ID in place for the post-run read to report (observed
2026-08-27: a `bbr test //cluster/...` run reported a concurrent run's
`remote test //haku/console/x/...` invocation). A per-run file would fix the
race but still name only the outer invocation; the outer ID stays discoverable
from bb's own "Streaming remote runner logs to:" line. A bbr-written shared
default file (`~/.cache/bbr/last_invocation_id`) has the same cross-run
trampling for whoever reads it — recording is opt-in and per-run via
`--invocation-id-file`.

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

### Checking a generated tree after download

A successful local writer only shows that it processed the input it received; it
does not establish that the downloaded tree matches the current build outputs.
Run the adapter's review-tree equality check and its normal smoke check. If the
diff shows removed files left behind or stale bytes at file ends, preserve the
writer's downloaded input and destination, then compare both with the remote
invocation artifact when available to identify which side is stale.

If the local downloaded tree is stale, stop commands using that checkout and
move aside only its idle, clone-owned `bb-out/` directory. Rerun the same
`bbr` command with the same remote-cache settings, then repeat the equality and
smoke checks. Keep the old tree for comparison; do not clear shared caches or
disable remote caching as a first response.

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

## Configuration sources and precedence

### CLI options: `.bbrc`

Source: `cli/cmd/bb/bb.go` (`resolveBBCliCommandArgs`),
`cli/parser/parser.go` (`consumeBBRCFileOptions`, `parseBBRCConfig`),
`cli/parser/bbrc/bbrc.go`

`bb remote` supports native repository and user configuration. `.bbrc` uses
rc-style sections for **bb CLI options**, including outer runner properties:

```text
remote --runner_exec_properties=workload-isolation-type=firecracker
remote --runner_exec_properties=recycle-runner=true
remote:large --runner_exec_properties=EstimatedComputeUnits=8
```

Select the named section with `bb remote --bb_config=large build //target`.
Bazel options belong in `.bazelrc`; Bazel named configs use `--config`, not
`--bb_config`.

The workspace root is the first ancestor of the current directory containing
`MODULE.bazel`, `WORKSPACE`, or `WORKSPACE.bazel`. Intermediate directories
do not contribute additional `.bbrc` files.

Files are read in increasing precedence order:

1. Workspace `.bbrc`.
2. Home `~/.bbrc` (higher precedence than workspace).
3. Explicit, repeatable startup flags `--bbrc=PATH`, in argument order.

Command-line options follow the default rc options. Repeatable options
accumulate; precedence does not replace an entire property list. Named configs
expand where selected. `--ignore_all_bb_rc_files` disables all these files;
`--bbrc=/dev/null` stops subsequent explicit files, but does not disable the
implicit workspace/home files.

The repo's root `.bbrc` supplies the runner image, resource defaults, snapshot
policy, and clock workaround. Both `bbr` and the GitHub `bb-remote` action
invoke native `bb remote`, so they share these settings without parsing rc
files themselves. CI overrides resources and candidate images explicitly.
`devinfra/update_image_pin.py` updates the runner image in `.bbrc` when repinning
`devinfra/image_pins.json`.

`bbr` adds session metadata (`BBR_BAZELRC`), a stale Git-base check, mounted
credential-file loading, and inner invocation-ID reporting/recording. Remote
options go before the Bazel verb, e.g. `bbr --timeout=10m test //target`.
Its dry-run prints the forwarded command; native `.bbrc` expansion happens
only when `bb` executes.

Validation with pinned 5.0.466: an explicit `.bbrc` containing
`remote --timeout=cli-doc-audit-invalid-duration` caused the remote parser to
reject that exact duration before submitting a remote run.

### YAML configuration and Git settings

Source: `cli/config/config.go`, `cli/storage/storage.go`, `cli/login/login.go`,
`enterprise/server/workflow/config/config.go`

- CLI YAML files are **`~/buildbuddy.yaml`** and
  **`<workspace>/buildbuddy.yaml`**, loaded in that order. Their CLI schema
  contains `plugins` and `local_cache`, not runner platform defaults.
- `$BUILDBUDDY_CONFIG_DIR` controls CLI storage (default OS user config dir +
  `/buildbuddy`), **not the location of that YAML file**.
- Hosted BuildBuddy workflows use `actions` in `buildbuddy.yaml`. That server
  schema includes per-action `platform_properties`, `container_image`, and
  `resource_requests`; it is a separate consumer from `bb remote`.
- `.git/config [buildbuddy]` stores API key (`api-key`), selected remote name,
  and default branch.

For `bb remote`, an API key supplied in the command's
`--remote_header=x-buildbuddy-api-key=…` takes precedence over
`BUILDBUDDY_API_KEY`, then `.git/config`, then interactive login. These are
resolution sources, not instructions to copy credentials between environments.

Other environment settings:

- `BUILDBUDDY_CACHE_DIR` — overrides CLI cache storage.
- `BB_USE_BAZEL_VERSION` — overrides Bazel version, ahead of `USE_BAZEL_VERSION`.
- `BB_DISABLE_SIDECAR=1` or `true` — disables the local sidecar. `CI` or
  `--build_metadata=ROLE=CI` instead enables synchronous sidecar writes when
  needed; CI alone is not the explicit disable switch.
- `GIT_REPO_DEFAULT_BRANCH` — overrides default branch detection for
  `bb remote`.
- `BUILDBUDDY_REMOTE_RUNNER` — overrides the `bb remote` runner endpoint.
- `BAZELISK_SKIP_WRAPPER=true` — makes bb behave as plain Bazelisk.

Bazel flags for `bb remote` are forwarded to the hosted Bazel process, which
reads its own rc files, including the checked-out workspace `.bazelrc` and
ci_runner's generated `buildbuddy.bazelrc`. Caller-local Bazel rc files are
not implicitly copied there.
