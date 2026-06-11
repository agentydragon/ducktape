# `bb build` silently drops startup directives from bazelrc

## TL;DR

`bb build` / `bb test` (the direct-local path, not `bb remote`) invokes Bazel with
`--nohome_rc --noworkspace_rc --nosystem_rc` — disabling **every** implicit
bazelrc source. `bb` re-extracts command-level directives (`common`, `build`,
`test`, …) from those files and inlines them as command-line flags, **but it does
not forward `startup` directives.** The only way to make a `startup` directive
survive `bb` is to pass the bazelrc explicitly via `--bazelrc=<path>`, which
Bazel reads in full (including startup lines).

This is a BuildBuddy CLI behavior — not something we configure. The matching
`bb remote` behavior is in <bb_remote_internals.md>; this doc covers the
local-Bazel path that trips people up.

## What the strace looks like

`bb version` resolves bazel and invokes it directly with:

```
execve(".../bazelisk/downloads/.../bin/bazel",
  [".../bin/bazel",
   "--nohome_rc", "--noworkspace_rc", "--nosystem_rc",
   "version",
   "--enable_bzlmod",
   "--lockfile_mode=off",
   "--@rules_python//python/config_settings:...",
   "--tool_tag=buildbuddy-cli-5.0.339", ...])
```

Notice:

- `--nohome_rc --noworkspace_rc --nosystem_rc` are _startup_ flags (before the
  subcommand). Bazel reads **no** implicit rc.
- `--enable_bzlmod`, `--lockfile_mode=off`, etc. come _after_ the subcommand.
  `bb` extracted these from workspace `.bazelrc` `common` lines and re-emitted
  them as command-level flags.
- **No `--host_jvm_args=...` anywhere.** Startup directives like
  `startup --host_jvm_args=-Xmx4g` do not make it through.

## What this breaks

### JVM trust store (the Claude-session case)

Bazel's embedded JDK ships with its own `cacerts` (231 certs) that does **not**
include Anthropic's TLS-inspection CA. The system JDK cacerts at
`/etc/ssl/certs/java/cacerts` (305 certs) does. The claude-hook session-start
writes a session bazelrc with:

```
startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts
```

The `bazel`/`bazelisk` shims at `<session_dir>/bin/{bazel,bazelisk}` ask the
daemon (`/shim-exec`) to inject `--bazelrc=<session-rc>` into argv, and Bazel
honors it. So direct `bazelisk build //…` works.

`bb`/`bbr` shims hit a different `/shim-exec` branch that does **not** inject
`--bazelrc`. bb then runs bazel with `--no*_rc`, bazel uses the embedded JDK's
cacerts, and any bzlmod fetch from bcr.bazel.build fails with
`PKIX path building failed` as soon as it routes through the TLS-inspection
proxy (i.e., in Anthropic Claude Code sessions).

See <../claude/claude_hook/main.rs> function `shim_exec_decision` line ~508 —
the bazel/bazelisk branch injects, the bb/bbr fall-through doesn't.

### Anything else that relies on startup options from a bazelrc

Same failure class: `--host_jvm_args=-Xmx…`, `--output_base`,
`--output_user_root`, `--server_javabase`, `--max_idle_secs`, etc. If it's
written as `startup --foo` in home/workspace/system rc, `bb build` silently
drops it.

## Workarounds

**Per-invocation**: `bb --bazelrc=<path> build //target …`. `bb` forwards the
explicit `--bazelrc` to bazel verbatim, bazel reads the file in full. For
Claude sessions:

```bash
bb --bazelrc="$HOME/.claude/session-env/$(
  ps aux | grep hook_daemon | grep -v grep |
  grep -oP '(?<=--sock /tmp/claude-hd/)[^/]+'
)/bazelrc" build //target
```

**Or fix the shim once** — extend the `bazelisk | bazel` match in
`devinfra/claude/claude_hook/main.rs` (`shim_exec_decision`) to also match
`bb` and `bbr`:

```rust
"bazelisk" | "bazel" | "bb" | "bbr" => {
    let mut argv = req.argv;
    if bazelrc_path.exists() {
        argv.insert(1, format!("--bazelrc={}", bazelrc_path.display()));
    }
    resolve_execve(&req.shim, argv, shim_dir, &req.env)
}
```

`bb` accepts `--bazelrc=<path>` as a startup flag (empirically verified).
`bbr` is `bb remote` under the hood and accepts the same flag; it just never
needs it today because `bb remote` bypasses all local rc handling anyway
(see <bb_remote_internals.md>).

## Why `bb` does this at all

BuildBuddy's CLI has its own rc parser so it can peek at the workspace
`.bazelrc` for plugin configuration, BES backend defaults, and auto-config
decoration (`--config=buildbuddy_bes_backend` etc.). It passes `--no*_rc` to
Bazel to prevent double-loading. The design assumes startup directives live
in the _user's_ home rc or the workspace rc; it doesn't anticipate a
third-party session bazelrc that the user has no opportunity to "merge"
into workspace rc.

## Verifying the behavior

Minimal repro, outside a Claude session:

```bash
# Put something observable in workspace .bazelrc:
echo 'startup --host_jvm_args=-Dclaude.canary=yes' >> /home/user/ducktape/.bazelrc

# Run through direct bazelisk (shim injection path):
bazelisk info server_pid
ps -o args= -p $(bazelisk info server_pid) | grep -c claude.canary   # 1

# Run through bb:
bb info server_pid
ps -o args= -p $(bb info server_pid) | grep -c claude.canary         # 0
```

(Remove the test line after.)
