# Bazel commands hanging in agent sessions

Tracking ticket for an issue that has come up multiple times when running
`bazelisk` from inside a Claude Code agent session. **Not fully diagnosed.**
What we have so far, plus the mitigations we shipped, plus what to capture
next time.

## Symptoms

- `bazelisk <anything>` (including trivial commands like `bazelisk info workspace`)
  hangs indefinitely.
- `ps -ef | grep bazelisk` shows multiple `bazelisk build …` processes from earlier
  in the session, alive for hours, each with a `bazel` client child. The bazel
  server process (`A-server.jar`) is also alive.
- The clients have accumulated only a few seconds of CPU over many hours —
  almost entirely sleeping.
- The bazel server has accumulated significant CPU (33 % sustained in one
  observation), but no command appears to finish.
- New `bazelisk` invocations are accepted but never make progress.
- `kill -9` on the bazel server PID (the `A-server.jar` process) immediately
  unsticks everything: the next `bazelisk` invocation starts a fresh server in
  ~3 s and works normally.

## Reproduction conditions (observed)

- Long-running Claude Code agent session (hours, many bazelisk calls).
- Multiple bazelisk builds issued through the Bash tool, several of which were
  auto-backgrounded by the harness's wait-for-output timeout.
- The bash wrapper that launched a given bazelisk command exits when the
  harness backgrounds it, but the bazelisk client process continues running
  (orphan).
- After enough orphans accumulate (3–6 over a session is enough), the next
  bazelisk hangs.

## Hypotheses considered

These are **not confirmed.** A previous agent (me) jumped to story-fit
conclusions without diagnostic evidence; that's the failure mode this doc
exists to prevent.

1. **Bazel-server-side wedge from accumulated client churn.** Most ergonomic
   story but architecturally implausible: bazel handles SIGKILL'd clients
   constantly in CI without this.
2. **Stuck stdout drain on a client whose parent shell exited.** If the
   harness opens stdout/stderr as a pipe and the bash wrapper exits, the read
   end is gone. A bazel client writing progress chunks would EPIPE or block.
   If grpc-java's StreamObserver on the server side back-pressures on the
   stream, the command worker thread blocks. The command lock never releases.
   New clients queue on the lock forever. Plausible but unverified; would also
   be a bazel-server bug (mature systems shouldn't deadlock on dead client
   output streams).
3. **BES backend stall.** When the session bazelrc enables BES (BuildBuddy
   event upload), an unresponsive BES backend has been observed to delay
   `bazel build` completion by ~60 s in this environment (4 × 15 s retry).
   Not consistent with 7-hour hangs unless the retry doesn't terminate.
4. **JVM-level pathology in the bazel server.** Possible (`jstack` would
   reveal), no evidence.

## What unsticks it

`kill -9 <bazel-server-pid> <stuck-bazelisk-pids>`, then run bazelisk again.
The next invocation starts a fresh local server. That's confirmed.

## Mitigations shipped

In `devinfra/claude/claude_hook/main.rs` (`write_session_bazelrc`):

- `startup --output_base=<session_dir>/bazel-output-base` — each agent session
  gets its own bazel server, so a wedged session can't affect another session
  or the interactive shell.
- `startup --noblock_for_lock` — when a second `bazelisk` lands while the
  first is in flight, it exits immediately with "Another command (X) is
  running" instead of silently queueing. Converts the silent failure mode
  into a loud one. (It is a startup option and takes no value; the
  `common --block_for_lock=false` spelling this note originally recorded is
  rejected by Bazel — see the comment above the `lines.push` in
  `write_session_bazelrc`.)

Neither mitigation **fixes** the root cause. They reduce blast radius and
surface the failure earlier.

## What to capture next time it recurs

When you observe the symptom, **don't kill anything until you've captured
state.** Run, in this order, against the stuck bazel server PID (call it
`$BS`) and a stuck client PID (call it `$BC`):

```bash
# Which thread is the server stuck on (most informative single artifact)
jstack $BS > /tmp/bazel-server-jstack.txt

# What kernel call is each process sleeping in
for p in $BS $BC; do
  echo "=== $p $(ps -o comm= -p $p) ==="
  cat /proc/$p/wchan; echo
  cat /proc/$p/status | grep -E "^State|^Threads"
done

# Is the client's stdout pipe orphaned (no reader)?
ls -la /proc/$BC/fd/{1,2}
lsof -p $BC | grep -E "PIPE|sock"

# Did the bazel command_log file get any output recently?
ls -la ~/.cache/bazel/_bazel_*/<output_base>/command.log
tail -20 ~/.cache/bazel/_bazel_*/<output_base>/command.log

# Are there active gRPC connections from the server?
ss -tnp | grep $BS
```

Look for:

- `jstack`: which thread(s) in the bazel server are blocked, and on what
  (likely `LockSupport.park`, a `Semaphore`, or a gRPC `StreamObserver` send).
- `/proc/$BC/wchan = pipe_read` or `pipe_write` → confirms stdout deadlock.
- `lsof` showing a pipe with no reader (the harness's bash wrapper exited).
- `ss` showing a half-open TCP connection to BuildBuddy.

If the server's jstack shows it waiting on stream flow control while a client
has a dead-reader pipe, that proves hypothesis (2) above. Otherwise, the
hypothesis space stays open.

## Why this matters

The user has reported this happening across multiple agent sessions (not just
mine). It's a recurring failure mode that wastes session time and confidences
in tooling. The mitigations buy us readable errors and isolation; the real
fix requires identifying the mechanism, which requires evidence we haven't
collected yet.

## Related

- `devinfra/claude/claude_hook/main.rs:write_session_bazelrc` — where the
  mitigations live.
- `devinfra/claude/config/bazelrc.mako` — historical Python-era template,
  kept for specimens only; not consulted at runtime anymore.
- Earlier related note, now resolved and archived:
  <../archive/2026_05_18_stuck_shim.md> — a different but adjacent "agent
  process didn't terminate cleanly" case, from when the PATH shims still
  called the hook daemon over UDS.

## Adjacent failure mode: local Bazel fetches blocked (2026-08-16)

Distinct from the hang above, but it hits the same commands and is worth ruling
out first: locally-executing Bazel in an agent session (`bazelisk`, and
therefore `bb run` — including `bb run //devinfra:gazelle`) currently fails
fetching `rules_mypy` with a **403 from the egress proxy**. It fails fast and
loudly; it does not hang. `bbr` is unaffected because its module fetches happen
on the BuildBuddy runner, not in the session container. So a `bb run` that dies
on module resolution is this, not a wedged Bazel server — and Gazelle is not
runnable in-session until it is fixed.
