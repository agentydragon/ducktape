# Keeping tool memory from killing the harness

Runner implements isolated Codex execution through a supervised exec-server. Pods use a separate
container; VMs use a systemd service in a sibling slice. Configuration, lifecycle, and the limits of that
boundary are documented in [Codex executor isolation](../runner/docs/codex_executor.md).

The research below covers additional interception and enforcement options, principally for Claude.
It was based on Claude Code 2.1.252 and Codex `rust-v0.144.1`; it is not a statement of the current
Codex integration. The Sandbox remains the security boundary. Claude and Codex's local operating
mode still share a resource budget with their tool children.

The [observed VM OOM incident](vm_sandboxes.md#guest-resource-controls) demonstrates why a
per-harness budget alone cannot keep tool memory from killing its harness. Pod workloads run as
uid 1000 without capabilities or privilege escalation and with RuntimeDefault seccomp; their
cgroup filesystem is read-only. The VM systemd manager enforces sibling slice and per-service budgets.

## Interception points

### Claude Code

| Knob                                 | What it wraps                                                                                                        |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `CLAUDE_CODE_TOOL_MEMORY_LIMIT=<sz>` | Built-in memory cgroup for Bash-tool children. See below; inert in our Pods.                                         |
| `CLAUDE_CODE_SHELL=<path>`           | The shell every Bash-tool command runs in. Must contain `bash` or `zsh` and be executable.                           |
| `CLAUDE_CODE_SHELL_PREFIX=<path>`    | A wrapper around the assembled Bash-tool command, shell-form command hooks, and the executable of stdio MCP servers. |

**`CLAUDE_CODE_SHELL_PREFIX` contract.** The Bash tool assembles one command string (snapshot
`source`, extglob off, the user command under `eval`, `pwd -P` into a cwd file), then spawns
`<shell> -c [-l] "'<prefix>' '<command string>'"`. The wrapper therefore receives the whole script
as its **last argument** and must run it itself, typically `exec /bin/bash -c "$last"`. The
prefix is not shell-split: text before the last `" -"` becomes one quoted word and the remainder is
spliced in as flags, so `prlimit --as=4G` works but `/usr/bin/env A=1 wrapper` is one nonexistent
executable. Anything with more than an executable and trailing flags needs a wrapper script. The
wrapper runs inside Claude's outer shell, so that shell (a few MB) stays outside whatever the wrapper
sets up. The same contract (one shell-string argument) holds for hooks and for stdio MCP servers,
where the prefix becomes the spawned executable and the server's argv is joined into one quoted
string. Exec-form hooks (`args:` set) and PowerShell are not wrapped.

**Built-in tool cgroup (2.1.233+).** Linux only. `CLAUDE_CODE_TOOL_MEMORY_LIMIT` takes
`<n>[k|m|g|t]`, powers of 1024; a bare number is bytes, and `0`/`false`/`no`/`off`/`none` disables. Unset, the
feature waits for the `tengu_tool_memory_cgroup` flag (default off) and then caps at host RAM minus
max(2 GiB, 15%): `os.totalmem()` is the node, not the container. Claude creates
`claude-code-bash` as a **sibling** of its own cgroup, writes `memory.max` into it, and spawns
Bash-tool shells straight into it through Bun's `spawn({cgroup})` option; a shell spawned before
the flag resolves is moved in afterwards. It never writes `cgroup.subtree_control`. Only the
`shell` class is capped by default; `CLAUDE_CODE_TOOL_MEMORY_CGROUP_EXCLUDE` (a subset of `mcp`,
`lsp`, `hooks`, `plugin`, `tmux`, `helper`, `agent`) opts the others in. `memory.events`
`oom_kill` growth is logged, and an LSP server killed there is reported as such. Any failure
disables it for the process lifetime with only a debug log, so in our read-only-cgroupfs Pods it
silently does nothing. Making it work needs a writable, delegated cgroup v2 subtree (below).

**Uncovered.** Hooks spawn via `/bin/sh` (`shell: true`), not `CLAUDE_CODE_SHELL`. Git, `rg` and
other internal helpers spawn directly.

Reverse-engineering record: gaffer-private `claude/re/2.1.252/spec/modules/chunks/`:
`cli/shell/exec_command.yaml` (shell resolution, provider, prefix incl. its MCP use, command hooks),
`toolCgroup/shell/tool_memory_cgroup.yaml` (the cgroup), and
`hookServe/hooks/served_shell_prefix.yaml` (prefix for hooks served to a cloud session).

### Codex

Codex has no command-prefix option and no memory limiting. Its only `setrlimit` is `RLIMIT_CORE`.

| Point                               | Coverage                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The user's login shell              | The session shell is the `passwd` entry's shell (`getpwuid_r`), typed by file stem (`bash`, `zsh`, `sh`), falling back to `which bash`, then `/bin/bash`/`/usr/bin/bash`. `shell_command` and `exec_command` (unified exec) run `[<shell>, "-lc"/"-c", <script>]`. A shim at, say, `/opt/agent/shim/bash` set as uid 1000's shell catches both. |
| `PATH` precedence                   | The classic `shell` tool execs the model's argv directly. `["bash", "-lc", …]` resolves through `PATH`; `/bin/bash` or `["python3", …]` do not.                                                                                                                                                                                                 |
| Overwriting the shells in the image | Replacing `/bin/bash`, `/usr/bin/bash`, `/bin/sh`, `/bin/zsh` (for example with `dpkg-divert`) also catches an explicit `shell` argument to `exec_command` and absolute argv paths, but it wraps every shell in the container, including the harness's own.                                                                                     |
| `environments.toml` exec-server     | `$CODEX_HOME/environments.toml` (or `CODEX_EXEC_SERVER_URL`) points execution at a `codex exec-server`, over a websocket `url` or a stdio `program`. Remote `exec_command` goes through the exec-server backend. `shell_command` and `apply_patch` routing were not traced.                                                                     |

Agentplane runs Codex with `sandbox="danger-full-access"`, so no `codex-linux-sandbox` wrapper sits
in front of commands. With `shell_zsh_fork` enabled the session shell becomes the configured
`zsh_path` and an `EXEC_WRAPPER` escalation protocol runs; a shim would have to speak it. Leave that
feature off.

### Common to both

An argv-transparent `bash` shim (does its setup, then `exec /usr/bin/bash "$@"`) works for both:
`CLAUDE_CODE_SHELL` for Claude, the `passwd` shell plus a `PATH`-first shim directory for Codex.
Claude additionally gets `CLAUDE_CODE_SHELL_PREFIX` for hooks and MCP servers. A shim must be
idempotent under nesting (mark itself in the environment) because the harness also runs the shell
for snapshots.

## Enforcement

| Mechanism                                                      | Works in today's Pod | Isolation                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------------------------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Shim raises `oom_score_adj` to 1000                            | Yes                  | Picks the tool as the kernel's victim, but `memory.oom.group=1` then kills the whole container anyway. Useful only with kubelet `singleProcessOOMKill: true` on agent nodes, a node-wide switch back to single-process kills that affects every Pod there.                                                                                                                                           |
| Shim sets `RLIMIT_AS`/`RLIMIT_DATA` (`prlimit`)                | Yes                  | Per process, not per tree: `make -j32` still sums past it. Virtual-size limits break Go, the JVM, V8 and sanitizers, which reserve large address ranges.                                                                                                                                                                                                                                             |
| Userspace OOM guard (earlyoom-style) in the runner             | Yes                  | Polls the container's `memory.current` against `memory.max` and SIGKILLs the largest process tree that is neither the runner nor the harness (same uid, so permitted). The runner spawns the harness, so it knows both PIDs. A poll loop can be outrun by a fast allocator, so the threshold needs headroom; PSI triggers need a writable `memory.pressure` and are not available.                   |
| Tool cgroup with `memory.max` (Claude's built-in, or the shim) | No                   | Kernel-enforced, per tree; the right mechanism. Needs a writable delegated cgroup v2 subtree: the entrypoint moves itself into a leaf, enables `+memory` in `cgroup.subtree_control`, then starts the harness in its own leaf so `claude-code-bash` (or the shim's cgroup) is a sibling. Kubernetes has no field for a writable cgroupfs short of `privileged`; sysbox or a VM runtime provides one. |
| Tools in a separate container (Codex exec-server)              | Needs Pod change     | Kernel-enforced by that container's own limit, no privilege. A second container with the shared workspace volume runs `codex exec-server`; Codex points at it through `environments.toml`. Claude has no equivalent short of a `CLAUDE_CODE_SHELL_PREFIX` client that proxies each command, cwd and environment to that container.                                                                   |
| VM or microVM Sandbox                                          | No                   | The current VM uses systemd service cgroups per native harness. The harness and its tool descendants share a group; per-tool limits remain optional research.                                                                                                                                                                                                                                        |

## Further work

Claude tool cgroups and shell interception remain deferred. Any implementation needs an acceptance
scenario checking tool OOM, harness survival and Runner journal continuity. Codex code-mode workers,
MCP helpers and external daemons remain outside its executor boundary.
