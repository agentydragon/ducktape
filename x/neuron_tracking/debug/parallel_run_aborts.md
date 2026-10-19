# Unexpected SIGTERM during parallel Codex runs

## Observed failure

Four CLI attempts across trials 1, 2, and 4 ended with exit status 143
(`SIGTERM`), in two simultaneous pairs. The invoking Codex process remained
alive. No annotation trace contained a command that killed the other jobs.
These exits are distinct from WebSocket reconnects, which the CLI recovered
from by switching transport.

The CLI launches already ran outside the invoking tool's sandbox. Keeping the
annotation agent's own tool sandbox enabled does not explain the signal.

## Candidate mechanism

The installed version is `codex-cli 0.162.1`. Its upstream source at commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d` unconditionally enables Linux
parent-death termination for pipe-spawned commands:

- [`pipe.rs:160`](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/utils/pty/src/pipe.rs#L160)
  calls `terminate_on_parent_death()` independently of sandbox mode.
- [`process_group.rs:29`](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/utils/pty/src/process_group.rs#L29)
  sets `prctl(PR_SET_PDEATHSIG, SIGTERM)` and checks the parent process ID.

Linux sends that signal when the thread that created the child terminates,
even if other threads in its parent process remain alive. The
[`PR_SET_PDEATHSIG` documentation](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)
describes this distinction. Checking `getppid()` does not detect the creating
thread's later exit.

A standalone C reproduction created a child from a worker thread, installed
the same signal, and let the worker return while the main thread waited. It
reported:

```text
parent_pid=2 spawning_tid=209
child_pid=210 signal=15 sender_pid=2 parent_pid=2 parent_alive=1
main_parent_still_alive=2 child_exit=0
```

A separate probe launched through this session's tool outside its sandbox
confirmed that `PR_GET_PDEATHSIG` returned 15. It recorded its creating parent
thread and waited five minutes; that thread remained alive and no signal arrived.

This establishes a reachable failure mechanism, not the sender of the four
historical signals. Their creating thread IDs were not captured before death.
Attaching a signal tracer was denied by host ptrace policy, and passwordless
sudo was unavailable. User journal and CLI logs did not identify a sender.

## Mitigation under observation

Resume interrupted jobs in the same conversation using a transient
`systemd-run --user` service, with an explicit working directory and native
stdout/stderr files. The user service manager then owns the job instead of the
invoking tool's worker thread. Trials 1 and 4 both reached hand-off using this
launch method, with no further SIGTERM exits observed. This does not establish
that every possible abort cause has been eliminated.

Retain native Codex event logs and conversation IDs. If another termination
occurs, capture the signal sender and creating thread lifetime before claiming
the candidate mechanism as the historical cause.
