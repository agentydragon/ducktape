# Stuck `claude-hook shim` diagnostic

Symptoms and evidence observed during a Claude Code web session on 2026-05-18
that prevented bazel/bbr commands from completing for ~16 minutes at a time.
Multiple separate incidents in one session. Filed for forensic follow-up and
to inform whether to keep the shim system at all.

## TL;DR

- `claude-hook shim bbr test …` (the `bbr` PATH shim from
  `<session_dir>/bin/bbr`) spun at **99–110% CPU with zero output for
  11–16+ minutes**, while every parent and sibling shell command waited on
  its pipe. Killing the shim PID immediately unblocked everything.
- The same session's `claude-hook shim git …` invocations behaved
  correctly: each printed `[git-shim] daemon unreachable: connect:
Connection refused (os error 111) — passing through` to stderr and
  exec'd `/usr/bin/git` straight away.
- The 300 s outer timeout on `post_json_over_uds` (`main.rs:758-764`)
  should bound any single shim invocation, but the stuck duration was
  **>>300 s** — meaning the shim never reached that timeout, never
  reached the `decide()` call, or got stuck after `decide()` somewhere
  the timeout doesn't cover.
- The hook daemon for this session (`pid=6236`, recorded in
  `daemon.err.log`) **was dead by the time the shim hung**. The socket
  file `/tmp/claude-hd/<sid>/d.sock` still existed (kernel inode); no
  process was listening.

A 300 s ceiling-of-some-kind not being enforced is the architectural
smell — every `claude-hook shim` invocation should have a hard wall-clock
upper bound regardless of what state the daemon is in.

## Where the shim lives

- **PATH shim script** (`devinfra/claude/hook_daemon/shim_install.py` writes
  these into `<session_dir>/bin/{bazelisk,git,bazel,bb,bbr}`):
  ```sh
  #!/bin/sh
  export __DUCKTAPE_CLAUDE_HOOKS_SHIM_SESSION_ID=<session_id>
  exec claude-hook shim <name> "$@"
  ```
- **Rust shim runtime**: `devinfra/claude/claude_hook/shim_runtime.rs`,
  entry point `run_shim(name, forwarded)`.
- **UDS RPC helper**: `devinfra/claude/claude_hook/main.rs:753-764`:
  ```rust
  pub(crate) async fn post_json_over_uds(...) -> Result<Bytes, String> {
      tokio::time::timeout(
          Duration::from_secs(300),
          post_json_over_uds_inner(sock_path, path, body),
      ).await
      .map_err(|_| "daemon request timed out (300s)".to_string())?
  }
  ```
- The shim path **never calls `ensure_daemon`** (only the hook-dispatch
  path does). So a dead daemon should produce a fast `ECONNREFUSED` from
  `UnixStream::connect`, which `decide()` turns into
  `ShimDecision::Passthrough` and `run_shim` exec's the original argv.

## Observation 1 — git shim's correct passthrough

While the `bbr` shim was stuck, **other git invocations** in the same session
went through `claude-hook shim git`. Each one printed a single line of
stderr and continued normally:

```
[git-shim] daemon unreachable: connect: Connection refused (os error 111) — passing through
```

A single later commit attempt's output file contained **96,167** such
lines — but that's just because the `git` shim is invoked many times by
pre-commit / git internals, and each call leaves one passthrough line.
**Each individual git invocation finished fast** (sub-second). This is
the expected behaviour for a dead daemon.

So whatever is wrong with the `bbr` shim is **not** "the shim hangs
forever when the daemon is unreachable" — the git shim demonstrates the
passthrough path works.

## Observation 2 — bbr shim stuck at 100% CPU, no output

Process tree (excerpt) while stuck:

```
root  11869  /bin/bash -c '...bbr test //devinfra/js/debundle/...' (pipe into grep | tail)
root  12225  └─ claude-hook shim bbr test //devinfra/js/debundle/...   ← 99% CPU, 12m
```

- `claude-hook shim bbr …` had been running for **12 m 49 s** at the
  point of capture, consuming 99–110% CPU continuously.
- The output file fed by the bash pipe (the eventual stdout of `bbr`)
  was **0 bytes** the whole time.
- No child `bazel` / `bazelisk` / `bb` / `bbr-wrapped` process appeared
  under PID 12225 (which would happen after a successful passthrough
  exec). The shim binary had **not** exec'd yet.
- A different stuck incident earlier in the same session reported
  16+ minutes ("11 minutes" / "16 minutes" / "11m 23s" appear in the
  transcript) before manual `pkill -9 -f 'claude-hook shim'`.

## Observation 3 — strace of the stuck process

`strace -p 12225 -ewrite,read` for ~3 s while stuck:

```
read(3, "7:pids:/\n6:blkio:/\n5:freezer:/\n4"..., 128) = 127
read(3, "", 1)                          = 0
read(3, "-1\n", 32)                     = 3
read(3, "", 17)                         = 0
```

That's a `read(2)` on FD 3, reading what looks like `/proc/self/cgroup`
content. Repeated reads of `""` (EOF) suggest the process is in a tight
loop opening or re-reading the same procfs/cgroup file rather than
blocked on a socket. **No `connect`, `sendto`, `recvfrom`, `epoll_wait`
or socket syscalls visible in the sample** — so the shim is not stuck in
the UDS RPC at all. It's stuck somewhere earlier, before reaching
`call_daemon` / `post_json_over_uds`.

The 300 s timeout in `post_json_over_uds` is therefore irrelevant — the
process never got there.

## Observation 4 — daemon was dead, socket dangling

```
$ cat /tmp/claude-hd/<sid>/daemon.err.log
daemon: sourcing startup_env_script /home/user/ducktape/devinfra/secrets/web_env.sh
startup_env_script output: …
daemon: env overlay captured (5 vars: …)
claude-hook daemon pid=6236 sock=/tmp/claude-hd/<sid>/d.sock

$ wc -l /tmp/claude-hd/<sid>/daemon.log
0 …/daemon.log

$ ps -p 6236
  PID TTY          TIME CMD     # (empty — pid 6236 is gone)

$ ls -la /tmp/claude-hd/<sid>/d.sock
srwxr-xr-x 1 root root 0 May 18 04:28 …/d.sock
```

The daemon started at 04:28, sourced env, then died with **no requests
served** (empty `daemon.log`). The socket file lingers because the
kernel doesn't unlink it on process death. `UnixStream::connect` on a
dangling socket inode returns `ECONNREFUSED` instantly (as the git shim
confirms). So a dead daemon is not the proximate cause of the bbr-shim
hang; it's a coincident fact.

## Possible root causes (ordered by my prior)

### H1 — Process-level deadlock outside the RPC, in `claude-hook`'s startup

The strace shows procfs reads, not socket IO. Candidates:

- **Tokio runtime construction** during `#[tokio::main]` initializes
  thread pools and queries `nproc`/cgroup limits. If cgroup files are
  malformed or recursive in some way (gVisor / Firecracker procfs
  quirk?), tokio's worker-thread bootstrap could spin reading cpu/memory
  cgroups.
- **`std::env::vars().collect()`** at `shim_runtime.rs:58` — usually
  instant, but if libc's environ lookup gets confused by an enormous
  environ block (Claude's process passes ~hundreds of env vars including
  several-KB JWTs), pathological behaviour is possible.
- A hyper / rustls / dns init touching `/etc/resolv.conf` or cgroup
  fingerprint files at startup.

These are speculative — the strace fragment is suggestive, not
conclusive. A longer strace (no `-e` filter) or attaching gdb to a
stuck PID and dumping the backtrace would settle it. Worth adding
`RUST_BACKTRACE=1` and `RUST_LOG=trace` to a reproduction.

### H2 — UDS connect blocks indefinitely on a half-listening socket

Less likely given Observation 3 (no socket syscalls in the strace
window), but worth ruling out: if the daemon process is killed mid-RPC
and the kernel leaves the listening socket in a state where `connect(2)`
neither completes nor returns `ECONNREFUSED`, callers can block until
their own ceiling. With the daemon **gone** (pid 6236 dead), this
shouldn't happen on Linux — the kernel reaps the listener and subsequent
connects fail fast. But on gVisor / 9p quirks have shown up before
(see `README.md` "9p filesystem doesn't support Unix socket hard
links"). If the session's `/tmp` is on an exotic FS, this is worth
testing.

### H3 — Per-shim wrapper, separate from the Rust binary

The PATH shim script is `exec claude-hook shim …`. `exec` replaces the
shell, so PID 12225 is the Rust binary, not the shell. Confirmed by
`ps`'s argv. So the wrapper is not the issue.

### H4 — Argv parsing / clap stuck on something

`Cli::parse()` (clap derive) at `main.rs:870` could in principle pause —
e.g. if it shells out for shell completion. With argv
`shim bbr test //…`, no completion is requested. Unlikely.

## H5 — Missing fallback: Python shim took over as daemon on slow connect

**User-supplied context (2026-05-18):** the previous Python shim
implementation handled the "daemon connect didn't succeed quickly enough"
case by **double-forking and starting a fresh daemon itself**, then
connecting to the new daemon. The Rust port (`claude_hook/shim_runtime.rs`)
does not appear to have this fallback — it only handles "connect
returned an error" (passthrough) and "connect returned a response"
(approved exec).

If the daemon's listening socket is in a state where `connect(2)`
**neither completes nor errors fast** (the H2 half-open variant), the
Python shim would have side-stepped the hang by spawning a replacement
daemon. The Rust shim has no such recovery — it just sits in the
`UnixStream::connect` future until the 300 s outer timeout in
`post_json_over_uds`. That at least bounds the hang to 5 minutes per
invocation, but the observed 11–16 minute hangs imply even that timeout
isn't reached (see Observation 3).

The takeaway: regardless of whether we restore the double-fork-takeover
behaviour, the Rust shim is missing **at least one** of the safeguards
the Python version had. The minimum credible fix list is:

1. Wall-clock ceiling on the whole `run_shim` body (not just the RPC).
2. Aggressive `connect()` deadline — 1-2 s, not 300 s. If connect
   doesn't succeed in 1 s, the daemon is effectively unreachable;
   passthrough or take-over (per Python behaviour).
3. Optional: port the Python double-fork-and-replace-daemon path so
   transient daemon death recovers without operator intervention. Look
   for the precedent in the git history of `devinfra/claude/hook_daemon/`
   before the Rust port landed.

If we're considering removing the shim system entirely (see below),
this work goes away. Otherwise, items 1+2 are cheap and would have
prevented this incident.

## Why the SPEC's 300s ceiling isn't enough

Even if H1/H2 are eventually fixed, the architectural ceiling lives
**inside** `post_json_over_uds` — i.e. it only fires once the shim has
already decided to call the daemon. Anything that prevents the shim
from getting to that call (tokio init, env parsing, clap parsing,
socket-path computation) is uncovered.

Suggested defence-in-depth, in priority order:

1. **Process-level wall-clock kill** for `claude-hook shim` invocations.
   E.g. wrap the body of `run_shim` in a top-level
   `tokio::time::timeout(Duration::from_secs(30), ...)` that, on expiry,
   prints the original-argv passthrough message and `exec`'s the
   original argv anyway. The shim should never burn 30 s of wall clock
   regardless of upstream state.
2. **`alarm(2)` / `setitimer`** as a belt-and-braces backup that
   delivers `SIGALRM` to self if the tokio-level timeout itself wedges
   (e.g. if the deadlock is in the runtime, the tokio timer never
   fires). Default handler is `terminate`, which would at least produce
   a fast failure visible to the shell.
3. **Print one stderr line per invocation** even on the happy path
   (gated by an env var or `--verbose`) so when the shim **is** running
   normally, you can confirm it's not silently spinning.

## Why the shim system might be worth removing entirely

You asked. From this session's evidence and a re-read of the SPEC, the
behaviours the shim currently delivers split roughly:

| Shim                         | Behaviour                                                                                                                                                                         | Could it go away?                                                                                                                                                                                  |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `git`                        | Block dangerous git operations (`git add -A`, `git stash`, `git commit --amend`) when `git_shim` is enabled in profile; otherwise no-op passthrough. Web profile **disables** it. | On web: yes — it's pure passthrough that produces "[git-shim] daemon unreachable" noise. On CLI: the safety checks are the only feature, and they could be a pre-commit hook or git alias instead. |
| `bazelisk`                   | Inject `--bazelrc=<session_dir>/bazelrc` (RBE headers etc.)                                                                                                                       | Could be done by writing `BAZELISK_OPTS` / `BAZEL_OPTS` env vars in the session env file instead. No daemon needed.                                                                                |
| `bb`, `bbr`, `bazel`         | Currently no-op passthrough.                                                                                                                                                      | Yes — these wrappers do nothing. Remove them.                                                                                                                                                      |
| `/shim-exec` daemon endpoint | Resolves the real binary off PATH (skipping the shim dir), applies policy.                                                                                                        | The PATH-skip can be done in the wrapper shell script directly; the policy bit is git-only and can move to a git hook.                                                                             |

If you remove the shims, every "daemon unreachable" stderr line and
every "claude-hook shim stuck" incident disappears. The session-bazelrc
injection becomes an env var or a `~/.bazelrc.local` include. The git
safety becomes a pre-commit (web profile already doesn't run the safety
checks, so removal is a no-op there). The daemon stays — it still does
the env script, OTEL, hook dispatch — but stops being on the critical
path of `bazelisk build`.

The biggest risk of removal: any future "TLS-inspection proxy is back"
scenario would re-need credential injection per command. The historical
auth_proxy code was removed (per `README.md`); restoring it would be
a bigger lift than rebuilding the shims would be at that point. So:
removing the shims **is** safe under the current network-policy
assumption stated in `README.md` ("egress just works").

## What to do next (suggested)

1. Add the wall-clock ceiling in (1) above and ship a fix-forward.
2. Capture a reproduction: spin a session, kill the hook daemon, run
   `bbr build //...`, and `gdb -p <pid>; thread apply all bt` to dump
   the stack of a stuck shim. That nails H1 vs H2.
3. Independently of (1) and (2), decide whether the shims are worth
   keeping. The behaviours they deliver are small, and the cost of
   keeping them is incidents like this plus per-invocation
   "[git-shim] daemon unreachable" noise polluting transcripts.

## Files referenced

- `devinfra/claude/claude_hook/shim_runtime.rs` — `run_shim`, `decide`,
  `call_daemon`.
- `devinfra/claude/claude_hook/main.rs:753-802` — `post_json_over_uds`
  with the 300 s timeout.
- `devinfra/claude/claude_hook/main.rs:868-878` — `#[tokio::main] async
fn main` dispatcher.
- `devinfra/claude/claude_hook/daemon_lifecycle.rs:223` — the other
  place a 2 s timeout exists (daemon HTTP send during `ensure_daemon`).
- `devinfra/claude/hook_daemon/shim_install.py` — Python (CLI / older
  install path) that writes the PATH shim wrappers.
- `devinfra/claude/claude_hook/shim_install.rs` — Rust equivalent.
- Session evidence (this session, not persisted):
  - `/tmp/claude-hd/5dfb3626-adf1-48de-805c-23deba728651/daemon.err.log`
  - `/tmp/claude-hd/5dfb3626-adf1-48de-805c-23deba728651/d.sock`
    (dangling)
- Workaround used in this session:
  `PATH=$(echo $PATH | tr ':' '\n' | grep -v 'session-env.*bin' | tr
'\n' ':') <cmd>` — strips the shim dir from PATH so `bazelisk` /
  `bbr` / `git` resolve to their real Nix-profile binaries directly.
