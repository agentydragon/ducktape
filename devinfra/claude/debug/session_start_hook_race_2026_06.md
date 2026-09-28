# Rust Session Start Pidfile Race (2026-06)

## Incident

On 2026-06-11, devel CI failed in GitHub Actions run `27317974658`, job
`release / Release claude-hooks`, while running:

```bash
bazel test --config=rbe --config=ci //devinfra/claude/...
```

The failing BuildBuddy invocation was
`bd34a5a1-7bfc-4774-8a2a-d239bfe25de3`.

`//devinfra/claude/claude_hook/container_e2e:test_container_e2e` failed after
`SessionStart`: the test expected
`/root/.claude/session-env/container-e2e-test/sessionstart-hook-0.sh`, but the
file did not exist.

The daemon stderr artifact contained:

```text
claude-hook daemon pid=49 sock=/tmp/claude-hd/container-e2e-test/d.sock
daemon: ready signal write failed: Broken pipe (os error 32)
```

## Root Cause

The Rust daemon launcher used two startup signals:

- a readiness pipe: daemon writes `READY\n` after binding the Unix socket
- a pidfile flock probe: parent treats `daemon.pid` existing but unlocked as a
  late-crash signal

`run_daemon()` wrote `daemon.pid` before acquiring its exclusive flock. The
parent could observe this transient state:

1. `daemon.pid` exists
2. flock is not yet held
3. parent concludes the daemon died during startup
4. parent returns before sending the hook request that writes the session env
   file
5. parent drops the readiness pipe
6. daemon later writes `READY\n` and gets `Broken pipe`

The daemon was actually alive and had already decrypted the test secrets; the
client-side startup check made a false liveness decision.

## Fix

PR #2040, squash commit `a4e3368fc`, changed daemon startup so the pidfile is
published only after the flock is already held:

- write the PID into a temp file under the daemon directory
- acquire the exclusive flock on the temp file
- atomically persist it as `daemon.pid`
- keep the file descriptor alive for the daemon lifetime

The PR also added a unit test that verifies a visible pidfile is already locked
and that dropping the pidfile fd releases the lock.

## Verification

Targeted RBE test:

```bash
bbr test //devinfra/claude/claude_hook:claude_hook_test \
  //devinfra/claude/claude_hook/container_e2e:test_container_e2e
```

Clean worktree stress run:

```bash
bbr test //devinfra/claude/claude_hook:claude_hook_test \
  //devinfra/claude/claude_hook/container_e2e:test_container_e2e \
  --nocache_test_results --runs_per_test=3
```

BuildBuddy invocation `697336e8-446e-4a98-958b-81552861f80d`: both targets
passed; `test_container_e2e` passed all three uncached runs.

## Relation to 2026-03 Incident

A 2026-03 incident hit a Python daemon TOCTOU race where concurrent hook
processes could start multiple daemons because the installed wheel lacked the
intended FileLock.

This June incident is not the same implementation bug. It is the same failure
class: daemon startup state was visible to another process before the associated
lock/liveness invariant was true.

## Operational Note

The post-merge devel push run for `a4e3368fc` had `bazel-ci / Test & Build`
green, but the `release / Release claude-hooks` job was cancelled while running
`Run tests for claude-hooks`. If this specific release path matters for final
confidence, rerun that cancelled job from GitHub Actions run `27322351519`, job
`80716249013`.
