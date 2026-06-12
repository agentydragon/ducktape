# Bazel Cache Sharing Across Worktrees And Local Agents

Drafted 2026-05-20.

Scope: local developer machines running normal CLI Codex and Claude Code. This
does not cover Codex Web, Claude Code Web, or Claude web sessions.

This is cache-layout and local-sandbox context, not the operational rule for
agent sessions. Root <../../AGENTS.md> and <../../docs/claude_code_sandbox.md>
remain authoritative for agent execution: Bazel-family commands (`bazel`,
`bazelisk`, `bb`, `bbr`) run outside the sandbox there.

## The Boundary

Bazel's analysis state is server-local state under one output base. It is not a
content-addressed cache that can be shared safely between worktrees. Do not point
multiple worktrees at one `--output_base`: Bazel expects one workspace/server
owner, takes an output-base lock, and keeps path-sensitive Skyframe state there.

What can be shared is the expensive content cache underneath separate output
bases:

- `--output_user_root`: common parent for per-worktree output bases. This keeps
  all Bazel state under one mount, but each worktree still receives a separate
  hashed output base.
- `--repository_cache`: downloaded external repository archives.
- `--repo_contents_cache`: extracted/fetched repository contents. In the current
  Bazel available through `bazelisk help`, this defaults to empty, so set it
  explicitly if we want sharing.
- `--disk_cache`: local action cache/CAS. This is less important when `--config=rbe`
  uses remote execution and remote cache, but it helps local builds, no-RBE
  debugging, and actions that are not downloaded from the remote cache.
- `BAZELISK_HOME`: Bazelisk's downloaded Bazel binaries.

Remote execution/cache already shares action work across machines. It does not
share local analysis state between worktrees.

## Recommended Layout

Use one cache root per user:

```text
~/.cache/bazel/
  _bazel_$USER/
    <hashed output bases per worktree>
    cache/
      repos/
      repo-contents/
      disk/
~/.cache/bazelisk/
```

On `rugged`, the whole `~/.cache/bazel` tree is on the same btrfs filesystem.
On `wyrm2`, `~/.cache/bazel` is a 150G SSD mount for output bases, and
`~/.cache/bazel/_bazel_agentydragon/cache/repos` is a nested 100G HDD mount for
repository cache. Keep `--experimental_repository_cache_hardlinks` disabled on
`wyrm2`: hardlinks only work when repository cache and output bases are on the
same filesystem.

## Bazelrc Proposal

The first implementation is rugged-only. Bazel already defaults
`--output_user_root`, per-worktree `--output_base`, and `--repository_cache` into
the shared `~/.cache/bazel/_bazel_$USER` tree there, so only enable caches that
are not already on by default:

```nix
let
  bazelCacheRoot = "${config.xdg.cacheHome}/bazel";
  bazelOutputUserRoot = "${bazelCacheRoot}/_bazel_${config.home.username}";
  bazelRepoContentsCache = "${bazelOutputUserRoot}/cache/repo-contents";
  bazelDiskCache = "${bazelOutputUserRoot}/cache/disk";
in
{
  home.file.".bazelrc".text = lib.mkAfter ''
    common --repo_contents_cache=${bazelRepoContentsCache}

    build --disk_cache=${bazelDiskCache}
    build --experimental_disk_cache_gc_max_size=200G
    build --experimental_disk_cache_gc_max_age=14d
  '';
}
```

For long local debugging loops, consider adding this temporarily rather than
globally:

```text
build --noallow_analysis_cache_discard
```

That can keep more local analysis data around inside one server at the cost of
memory. It still does not share analysis across worktrees.

Create the cache directories declaratively:

```nix
home.activation.ruggedBazelCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
  mkdir -p '${bazelRepoContentsCache}' '${bazelDiskCache}'
'';
```

## Claude Code Local Sandbox

Claude Code has two filesystem concepts that matter here:

- `permissions.additionalDirectories` makes paths part of Claude's working set.
  Use this for real source/work directories.
- `sandbox.filesystem.allowWrite` adds writable paths inside the sandbox. Use
  this for caches and build artifacts.

For local CLI Claude Code, prefer absolute paths in Nix so path expansion does
not depend on where the settings file lives:

```nix
settings = {
  env.BAZELISK_HOME = "${config.xdg.cacheHome}/bazelisk";

  sandbox = {
    enabled = true;
    autoAllowBashIfSandboxed = true;
    allowUnsandboxedCommands = true; # Keep an explicit escape hatch.
    excludedCommands = [ "nvidia-smi" ];
    filesystem.allowWrite = [
      "${config.xdg.cacheHome}/bazel"
      "${config.xdg.cacheHome}/bazelisk"
      "${config.xdg.cacheHome}/pre-commit"
    ];
  };
};
```

Do not put `bazel` or `bazelisk` in `sandbox.excludedCommands` if the goal is to
let them run in the Claude sandbox.

Avoid glob patterns in `sandbox.filesystem.allowWrite` on Linux. The restored
sandbox runtime strips or filters write globs before building bubblewrap mounts;
use concrete cache directories.

The rugged Nix config appends the Bazelisk cache directory to Claude's sandbox
writes and sets `BAZELISK_HOME`:

```nix
programs.claude-code.settings = {
  env.BAZELISK_HOME = "${config.xdg.cacheHome}/bazelisk";
  sandbox.filesystem.allowWrite = lib.mkAfter [ "${config.xdg.cacheHome}/bazelisk" ];
};
```

Claude network sandboxing is not controlled by a clean "filesystem sandbox only"
toggle. In the restored source, the Linux runtime enables `bwrap --unshare-net`
whenever `network.allowedDomains` is present. Claude Code's adapter builds that
domain list from explicit sandbox settings and from `WebFetch(domain:...)`
permission rules. Our config intentionally emits those WebFetch domain rules, so
removing network sandboxing globally would also change WebFetch permission
behavior unless we patch Claude.

Instead, Bazel can run through Claude's proxy. Claude's Linux sandbox exposes an
HTTP CONNECT proxy via `HTTP_PROXY=http://localhost:3128` and a SOCKS proxy via
`ALL_PROXY=socks5h://localhost:1080`. Bazel's repository downloader honors the
normal HTTP proxy environment, but Bazel's RBE/BES gRPC channels are grpc-java
Netty channels. They do not read `GRPC_PROXY`; grpc-java's default proxy detector
uses Java system properties such as `https.proxyHost` and `https.proxyPort`.

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

Current Ducktape config already points Codex at writable Bazel cache roots:

```nix
codexBazelCache = "${config.xdg.cacheHome}/bazel";
codexBazeliskCache = "${config.xdg.cacheHome}/bazelisk";
```

Keep that shape, and add the shared disk-cache directories if they become
separate roots:

```nix
shell_environment_policy.set.BAZELISK_HOME = "${config.xdg.cacheHome}/bazelisk";

sandbox_mode = "workspace-write";
sandbox_workspace_write = {
  writable_roots = [
    "${config.xdg.cacheHome}/bazel"
    "${config.xdg.cacheHome}/bazelisk"
    "${config.xdg.cacheHome}/pre-commit"
    "${config.xdg.cacheHome}/sccache"
    "${config.xdg.cacheHome}/nix"
    "/nix"
  ];
  network_access = true;
  exclude_tmpdir_env_var = false;
  exclude_slash_tmp = false;
};
```

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

If a normal `bazelisk build` unexpectedly picks up `--config=rbe`, check for a
stale user-level BuildBuddy import first, not the NixOS system Bazel rc:

```text
~/.bazelrc
  try-import ~/.config/bazel/buildbuddy.bazelrc

~/.config/bazel/buildbuddy.bazelrc
  build --config=rbe
```

Current generated `~/.config/bazel/buildbuddy.bazelrc` should contain the
BuildBuddy API header plus `build --shell_executable=/bin/bash`, but it should
not select Ducktape's repo-local `--config=rbe`. The shell override belongs with
the user BuildBuddy/RBE environment: on NixOS it prevents Bazel from generating
helper scripts with `/nix/store/.../bash` shebangs that do not exist on RBE
workers.

On `rugged`, `/etc/bazel.bazelrc` contributes NixOS-local PATH and nix-ld flags
such as `--host_action_env` and `--repo_env`; it does not enable RBE.
Ducktape's workspace `.bazelrc` defines what `build:rbe` means, but repo-aware
entrypoints such as `bbr`, CI, Codex Cloud, and Claude session startup should be
what select it.

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
  - network restrictions are enabled whenever `network.allowedDomains` is
    defined, and Linux/WSL write globs are stripped/filtered because bubblewrap
    needs concrete mount paths.
- `/home/agentydragon/code/claude-code-sourcemap/restored-src/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/linux-sandbox-utils.js`
  - Linux network sandboxing uses `bwrap --unshare-net`; when proxy sockets are
    available, sandbox commands get `HTTP_PROXY=http://localhost:3128`,
    `ALL_PROXY=socks5h://localhost:1080`, and `GRPC_PROXY`/`grpc_proxy`.

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
