# Bazel Cache Sharing Across Worktrees And Local Agents

Drafted 2026-05-20.

Scope: local developer machines running normal CLI Codex and Claude Code. This
does not cover Codex Web, Claude Code Web, or Claude web sessions.

This is cache-layout and local-sandbox context, not the operational rule for
agent sessions. Root <../../AGENTS.md> and <../../docs/claude_code_sandbox.md>
remain authoritative for agent execution: Bazel-family commands (`bazel`,
`bazelisk`, `bb`, `bbr`) run outside the sandbox there.

## The Boundary

Never point multiple worktrees at one `--output_base` — it's single-owner, locked,
path-sensitive Skyframe state, not a shareable cache. What can be shared is the
expensive content cache underneath separate output bases:

- `--output_user_root`: common parent for per-worktree output bases. This keeps
  all Bazel state under one mount, but each worktree still receives a separate
  hashed output base.
- `--repository_cache`: downloaded external repository archives.
- `--disk_cache`: local action cache/CAS. This is less important when `--config=rbe`
  uses remote execution and remote cache, but it helps local builds, no-RBE
  debugging, and actions that are not downloaded from the remote cache.
- `BAZELISK_HOME`: Bazelisk's downloaded Bazel binaries.

Remote execution/cache already shares action work across machines. It does not
share local analysis state between worktrees.

Do not enable `--repo_contents_cache`. Unlike the archive-only repository
cache, it snapshots complete fetched trees. Repository rules can put absolute
symlinks in those trees: rules_python's hermetic `python` link and Gazelle's
helper repositories have both pointed back into the output base that produced
the cache entry. Reusing that entry makes every consumer depend on the
producer's output base; deleting the producer then yields dangling links and
repository-fetch failures. Bazel `8.6.0` does not relocate those links.

## Output-base Lifecycle

Deleting a Git worktree does not delete its per-workspace output base. Bazel's
`--experimental_disk_cache_gc_*` options govern only the shared `cache/disk`
tree; they do not reclaim hashed output bases.

Run the local GC tool after worktree cleanup; it is a manual sweep, not part of
`wt rm` or a scheduled job. The default command is a dry run; a base becomes a
candidate immediately after its recorded workspace disappears:

```bash
bazel-output-base-gc
bazel-output-base-gc --all --sizes
bazel-output-base-gc --delete
```

The command is distributed in the released, artifact-pinned Nix `ducktape`
package. While developing an unmerged version from this repository, run the
source target instead:
`bb run //devinfra/gc:bazel_output_base_gc -- --all`.

The tool only auto-selects direct-child, default MD5-named output bases whose
recorded workspace no longer exists. It requires the persisted NUL-delimited
`server/cmdline`, `README`, and `DO_NOT_BUILD_HERE` records to agree; verifies
the workspace-path hash; and rejects live servers, symlinks, and nested mounts.
Missing or contradictory provenance is reported as `REVIEW` and never deleted
automatically. Deletion repeats the checks while holding Bazel's byte-range
lock, refusing a busy lock, then moves the base to a sibling quarantine before
removing it with the standard Python directory remover.

A failed removal leaves its `.bazel-output-base-gc-*` quarantine visible as
`REVIEW` on the next run. Resolve the reported mount or permission problem,
confirm no process uses it, then remove that quarantine manually.

Shared `cache/repos` and `cache/disk` directories, any legacy
`cache/repo-contents` directory, and the `install` base are outside the eligible
naming scheme and remain untouched. Only one output-user-root is scanned. Pass
`--output-user-root PATH` for an explicit non-default root; session and
temporary roots are not auto-discovered.

## Recommended Layout

Use one cache root per user:

```text
~/.cache/bazel/
  _bazel_$USER/
    <hashed output bases per worktree>
    cache/
      repos/
      disk/
~/.cache/bazelisk/
```

On `rugged`, the whole `~/.cache/bazel` tree is on the same btrfs filesystem.
On `wyrm2`, `~/.cache/bazel` is a 150G SSD mount for output bases, and
`~/.cache/bazel/_bazel_agentydragon/cache/repos` is a nested 100G HDD mount for
repository cache. Keep `--experimental_repository_cache_hardlinks` disabled on
`wyrm2`: hardlinks only work when repository cache and output bases are on the
same filesystem.

## Implementation

Lives in the shared `nix/home/modules/bazel-cache.nix` module (option
`ducktape.bazelCache`), enabled by both `rugged` and `wyrm2`. Bazel already
defaults `--output_user_root`, per-worktree `--output_base`, and the archive
`--repository_cache` into the shared `~/.cache/bazel/_bazel_$USER` tree. The
module enables only the action `--disk_cache`; it never sets
`--repo_contents_cache`, which Bazel leaves disabled by default. The one per-host knob is
`diskCacheGcMaxSize` — `wyrm2` lowers it from the `200G` default because its
`cache/disk` shares a 150G SSD with the per-worktree output bases. The module
owns the exact rc flags and directory-creation wiring; this doc does not restate
them.

For long local debugging loops, consider adding this temporarily rather than
globally:

```text
build --noallow_analysis_cache_discard
```

That can keep more local analysis data around inside one server at the cost of
memory. It still does not share analysis across worktrees.

## Claude Code Local Sandbox

Claude Code has two filesystem concepts that matter here:

- `permissions.additionalDirectories` makes paths part of Claude's working set.
  Use this for real source/work directories.
- `sandbox.filesystem.allowWrite` adds writable paths inside the sandbox. Use
  this for caches and build artifacts.

The Bazel and Bazelisk cache write grants (`~/.cache/bazel`, `~/.cache/bazelisk`)
live with the rest of the Claude-sandbox settings in
`nix/home/claude_code/default.nix`; this doc does not restate them. The bazelisk
cache needs no dedicated env var — bazelisk already defaults `BAZELISK_HOME` to
`~/.cache/bazelisk`; the only wiring it needs is that sandbox write grant. Two
caveats that are easy to get wrong:

- Do not put `bazel` or `bazelisk` in `sandbox.excludedCommands` if the goal is
  to let them run in the Claude sandbox.
- Avoid glob patterns in `sandbox.filesystem.allowWrite` on Linux. The restored
  sandbox runtime strips or filters write globs before building bubblewrap
  mounts; use concrete cache directories.

Network-sandbox behavior and the Bazel incompatibility are owned by
<../../docs/claude_code_sandbox.md>. This local-CLI note only records the cache
layout and the proxy shim used when direct `bazel`/`bazelisk` runs are kept in
the Claude CLI sandbox.

Bazel's repository downloader honors the normal HTTP proxy environment, but
Bazel's RBE/BES gRPC channels are grpc-java Netty channels. They do not read
`GRPC_PROXY`; grpc-java's default proxy detector uses Java system properties
such as `https.proxyHost` and `https.proxyPort`.

The Ducktape Claude hook daemon therefore makes the Bazel shim translate
`HTTP_PROXY`/`http_proxy`/`HTTPS_PROXY`/`https_proxy` into Bazel startup args:

```text
--host_jvm_args=-Dhttps.proxyHost=<host>
--host_jvm_args=-Dhttps.proxyPort=<port>
--host_jvm_args=-Dhttp.proxyHost=<host>
--host_jvm_args=-Dhttp.proxyPort=<port>
```

For local CLI Claude Code, that proxy-shim path is intended to keep
`bazel`/`bazelisk` usable in the Claude sandbox without needing Bazel's
`--remote_proxy` or `--bes_proxy`. It does not change the root agent-session
rule, and it does not make `bb`/`bbr` sandboxed runs the supported path. Those
Bazel flags only accept `unix:/path/to/socket` and are not HTTP/SOCKS proxy
settings.

## Codex Local Sandbox

The cloned Codex source at `~/code/codex` shows:

- `sandbox_workspace_write.writable_roots` adds extra absolute writable roots.
- The current working directory is writable automatically in `workspace-write`
  mode.
- `/tmp` and `$TMPDIR` are writable unless excluded.
- Missing writable roots are filtered out by the Linux bubblewrap backend.
- With `network_access = true`, the Linux bubblewrap mode uses full network
  access unless a managed network proxy policy is active.

Ducktape's Codex config already points Codex at the writable Bazel and Bazelisk
cache roots under `workspace-write` mode; the exact `writable_roots` and
`sandbox_workspace_write` wiring lives in `nix/home/codex/default.nix` and is not
restated here.

Important Codex-specific detail: generated exec-policy `decision="allow"` rules
can bypass Codex's shell sandbox for matching command prefixes. That is
acceptable for the current Ducktape setup: Bazel is already treated as a trusted
build tool in `nix/home/allowed-commands.nix`, including `build`, `test`,
`query`, `cquery`, `aquery`, and `info`, plus the `nix develop --command ...`
variants.

The writable-root configuration is still worth keeping. It lets sandboxed Bazel
work when a command is unmatched, manually run with sandboxing, or launched
through a wrapper that does not hit a trusted exec-policy prefix. But do not use
Codex's current Bazel `decision="allow"` rules as proof that those invocations
were sandboxed; they are trusted commands.

## Verification

After applying the Bazelrc piece:

```bash
bazelisk info output_base repository_cache --config=nolint
```

Expected shape:

```text
output_base: /home/agentydragon/.cache/bazel/_bazel_agentydragon/<workspace-hash>
repository_cache: /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache/repos/v1
```

From inside Claude Code and Codex sandboxed shell commands, verify cache writes:

```bash
touch ~/.cache/bazel/sandbox-write-probe
touch ~/.cache/bazelisk/sandbox-write-probe
rm ~/.cache/bazel/sandbox-write-probe ~/.cache/bazelisk/sandbox-write-probe
```

For Codex specifically, remember that the current Nix-generated Bazel
`decision="allow"` rules mean matching Bazel commands are trusted and may bypass
the shell sandbox.

### Empty Repo Sanity Check

Observed on `rugged` after the cache change: a Codex-run `bazelisk build` in an
otherwise empty repo, using latest Bazel 8 and with rc-file/RBE config disabled,
can build a simple `genrule`. That is a useful baseline: local genrule execution
works under Codex and Bazel 8 when Ducktape's user/workspace rc layers are out of
the picture.

Ducktape intentionally enables its workspace `rbe` configuration for every
build-like command. The generated user credential is scoped to that config and
does not affect other repositories. See <bazel_configuration.md> for rc
ownership and execution policy.

### Disk Cache Effect Probe

This probe verifies the configured `--disk_cache` is saving action work between
separate output bases. It creates two temporary git worktrees, adds the same
untracked `genrule` package to both, builds it in the first worktree to populate
the disk cache, then builds it in the second worktree. The second build starts
from a different output base, so a `disk cache hit` proves the shared disk cache
is doing work.

Run the script with the active user Bazel config:

```bash
devinfra/debug/bazel_disk_cache_probe.sh
```

Expected result: the second build reports one `disk cache hit` and completes
without sleeping for the action. If the second build executes the genrule again,
check that `~/.bazelrc` contains the rugged `--disk_cache=.../cache/disk` line
and that the sandbox/user has write access to that directory.

The script disables remote execution/cache and BES for the probe target, uses
different temporary `--output_user_root` values for the two worktrees, and writes
a unique synthetic input each time so the first build should populate the cache
and the second build should be the first possible hit. It also passes
`--shell_executable="$(command -v bash)"` so the synthetic genrule works on
NixOS without `/bin/bash`, and embeds absolute `sleep`/`sha256sum` paths so the
action does not depend on Bazel's stripped action `PATH`. Pass `--keep` to leave
the temporary worktrees, output roots, and logs under `/tmp` for inspection.

## Source Basis

Claude Code local source checked:

- `/home/agentydragon/code/claude-code-sourcemap/restored-src/src/entrypoints/sandboxTypes.ts`
  - `sandbox.filesystem.allowWrite` is the setting for extra writable sandbox
    paths.
- `/home/agentydragon/code/claude-code-sourcemap/restored-src/src/utils/sandbox/sandbox-adapter.ts`
  - `convertToSandboxRuntimeConfig` resolves `~`, absolute paths, and
    settings-file-relative paths.
- `/home/agentydragon/code/claude-code-sourcemap/restored-src/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-manager.js`
  - Linux/WSL write globs are stripped/filtered because bubblewrap needs
    concrete mount paths.
- `/home/agentydragon/code/claude-code-sourcemap/restored-src/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/linux-sandbox-utils.js`
  - when proxy sockets are available, sandbox commands get
    `HTTP_PROXY=http://localhost:3128`, `ALL_PROXY=socks5h://localhost:1080`,
    and `GRPC_PROXY`/`grpc_proxy`.

OpenAI Codex source checked at
`/home/agentydragon/code/codex`, commit
`f6970214d2802ffae0c55e3f30bbc051c1482c1d`:

- `codex-rs/config/src/types.rs` and `codex-rs/config/src/config_toml.rs`
  - `sandbox_workspace_write.writable_roots` and `network_access` map from TOML
    config into the runtime permission profile.
- `codex-rs/protocol/src/protocol.rs`
  - `workspace-write` adds the explicit writable roots, cwd, `/tmp`, and
    `$TMPDIR`, with protected metadata subpaths.
- `codex-rs/linux-sandbox/src/bwrap.rs`
  - missing writable roots are filtered out before constructing bubblewrap args.
- `codex-rs/linux-sandbox/src/linux_run_main.rs`
  - full network access is used when the policy enables network and no managed
    proxy-only mode is active.
- `codex-rs/core/src/exec_policy.rs`
  - explicit `decision="allow"` exec-policy matches can set
    `bypass_sandbox = true` for the command.

Bazel and grpc-java source checked:

- `/home/agentydragon/code/bazel`, commit
  `10efaccb1d885a37ece9b9e32e3f99bc7c513368`
  - `RemoteOptions --remote_proxy` and `BuildEventServiceOptions --bes_proxy`
    only support Unix domain sockets.
  - `GoogleAuthUtils.newNettyChannelBuilder` rejects non-`unix:` proxy values.
- `/home/agentydragon/code/grpc-java`, commit
  `cc0d1a810b58095bc835acaad703a758f3e0040b`
  - the default `ProxyDetectorImpl` uses Java's `ProxySelector`; its own
    validation comment configures proxies with
    `-Dhttps.proxyHost=... -Dhttps.proxyPort=...`.
  - Netty transport handles `HttpConnectProxiedSocketAddress` by installing an
    HTTP CONNECT proxy negotiator.
