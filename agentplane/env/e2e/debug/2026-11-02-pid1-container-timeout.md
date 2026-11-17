# PID1 container shard timeout

## November 17: setup-reaper case has no retained wait site

[PR #17433](https://github.com/agentydragon/ducktape/pull/17433), Actions run `44138201778`,
[test invocation](https://app.buildbuddy.io/invocation/36b4c9f5-f1fd-5012-9477-18e2e8de6c64),
commit `3a93035ac77eda86fffe0c11f27b74ce4d413c0c`: shard 3/4 timed out in 60.243 seconds.
Shards 1, 2 and 4 passed in 41.216, 36.613 and 52.448 seconds. These are action results, not
individual Python test durations.

The failed shard selected two cases by collection position: `fence_and_replacement[escalate]`
and `test_production_pid1_embedded_host_reaps_setup`. Its log ends with one success dot. The
retained escalation JSON records both generations exiting zero, and its container log reports
the Go contract passed. The setup-reaper case has no assertion or Python traceback. Its JUnit
XML is the wrapper's synthetic exit-142 error, not a completed pytest report.

The complete failed-shard artifact list contains `test.log`, synthetic `test.xml`, empty
`test.warnings`, the Kubernetes API server log, and the escalation JSON/container log. There is
no `production-pid1-embedded-host/container.log`, goroutine dump, Python stack or setup marker.
`LoggedContainer` starts its streaming log thread only after Docker's `__enter__` returns.
The API server log proves that the setup case's control-plane fixture began. These artifacts
do not prove that its remaining fixtures became ready, its container started, or its setup child
existed. In particular, they do not establish a missed PID1 reap. No unchanged failed target was
rerun to guess the missing boundary.

### Wait conditions and producers

The setup case first needs the Kubernetes fixture to produce its kubeconfig and Sandbox records,
then Docker to load the pinned images and return from container creation/start. After startup:

| Condition                                | Producer                                                                           |
| ---------------------------------------- | ---------------------------------------------------------------------------------- |
| First authenticated `WatchState` boot ID | Runtime control listener, after the Controller incarnation bind                    |
| `CreateSession` response                 | Runtime admission path                                                             |
| `native.pid` and `group.pid`             | The Go setup leader and its forked group descendant                                |
| Setup leader completion                  | The test sends `SIGUSR1`; the fixture's signal receiver returns exit 23            |
| Descendant pidfd readable                | PID1 observes setup leader exit, kills its process group, and the descendant exits |
| Runtime container exit                   | The test sends `SIGTERM`; Runtime closes and fences its namespace                  |
| Container/fixture teardown               | Docker remove responses and control-plane process exit                             |

The descendant observation already has a five-second bound. Runtime container exit already has
a fifteen-second Docker wait; on that wait's error the test sends `SIGQUIT` to preserve Go stacks.
Neither boundary was recorded as reached in this occurrence. Source inspection identifies the
possible conditions and their producers; it cannot identify which producer failed here.

### History population

On November 17, the paginated `bbapi target history` query for the canonical upstream repository,
from November 1 onward, returned 2,057 distinct indexed invocation summaries for this target:
2,050 `PASSED`, two `TIMED_OUT`, and five `FAILED_TO_BUILD`. The first represented invocation was
November 1 at 20:00 UTC; the latest was November 17 at 18:19 UTC. The CLI is absent in this Sandbox,
so this used the skill's HTTP fallback to the same `GetTargetHistory` RPC, following all 62 pages
until a whole page preceded the cutoff and filtering by invocation creation time, not cached test
start time.

The two indexed timeout summaries are [November 2 at 20:47 UTC](https://app.buildbuddy.io/invocation/70217bae-c267-5493-ac3e-6cfb703ebf72)
and [November 12 at 22:14 UTC](https://app.buildbuddy.io/invocation/d47aa113-7013-5097-826e-9a105c388440).
The latter's raw events show timeouts in shards 1 and 2, with shards 3 and 4 passing. This is a
target-wide timeout history, not an established recurrence count of the setup-reaper mechanism.
The supplied #17433 occurrence and the older PR invocation below are absent from this index.
Cached passes are included, so two of 2,052 indexed test outcomes is not an independent-execution
failure rate and does not describe all PR/manual runs. The retained evidence establishes timeouts
at least as far back as the older report below, not a first-bad code revision.

### Diagnostics for the next occurrence

Only `test_pid1_container` loads `testing.pid1_diagnostics`. Before each test's fixture setup, the
plugin opens `pid1-diagnostics/<case>/progress.jsonl` and arms stdlib `faulthandler` to write Python
thread stacks every ten seconds into `python-threads.txt`. It records fixture entry/readiness and
teardown boundaries. A function-scoped async fixture also writes `Task.print_stack()` output to
`asyncio-tasks.txt` while the event loop can run. This uses standard library diagnostics already
used elsewhere in the repository; no procps behavior is tested or reimplemented.

The setup case records each condition and producer above before waiting, plus the Docker container
ID and host PIDs when known. Each journal write is flushed by closing the file. Native thread
stacks still fire if synchronous Docker work blocks the event loop; task stacks distinguish an
awaiting call when the loop remains responsive. All files use `undeclared_outputs_dir()` so they
survive a shard kill. An unfinished fixture entry, last condition, and contemporaneous stacks can
distinguish control-plane/Docker startup from a live setup child or Runtime close wait.

The historical mechanism remains open. These diagnostics change no production code, wait outcome,
deadline, size or retry policy, and are not a regression test or a demonstrated root-cause fix.

Validation: [the instrumented target](https://app.buildbuddy.io/invocation/e3ae31e0-83ed-4227-9f20-8d8e3ffdcffe)
passed all seven cases across four shards (30.393, 51.453, 43.384 and 17.201 seconds), with the
diagnostics library named explicitly for Ruff/mypy checks. The retained setup-case journal starts
before `kubeconfig` setup and records every body checkpoint through container removal and fixture
teardown. Its native Python watchdog dump and a term-case asyncio task dump were downloaded and
inspected. The original timeout was not reproduced; this establishes artifact capture, not its cause.

## Earlier completed-pytest occurrence

The failing [CI invocation](https://app.buildbuddy.io/invocation/77577467-0a55-5a05-b28b-9353501bb50a)
on commit `2f2a56aa00` reports shard 1 of `//agentplane/env/e2e:test_pid1_container` as timed out.
Its log and JUnit XML report all three selected tests passed in 16.68 seconds. This does not prove
that Python shutdown or Bazel test-wrapper postprocessing completed.

## Recorded boundary

The remote action is `5ec595a85e120948ff127e1186be55605a9d28fe99e82888e44f432903082997/578`,
execution `/uploads/9bf89a8a-2adc-4dd9-8c4c-833df6cdb341/blobs/5ec595a85e120948ff127e1186be55605a9d28fe99e82888e44f432903082997/578`.
Its Action proto specifies a 60-second timeout; the result has deadline-exceeded status and exit -2.

| Evidence                                     | UTC time        |
| -------------------------------------------- | --------------- |
| RBE execution start                          | 19:28:08.550077 |
| JUnit suite timestamp                        | 19:28:50.780029 |
| JUnit suite timestamp plus reported duration | 19:29:07.473029 |
| RBE execution completion                     | 19:29:09.069806 |

The wall timestamps imply about 42.23 seconds before the recorded pytest session and about
1.60 seconds between its reported end and the action deadline. These combine guest and executor
wall clocks; they are not a trace of the original process's monotonic execution.
The final native container log continues through 19:29:07.286. Its test's container exit assertion,
owner-file lock release check, gateway teardown and asyncio fixture teardown precede pytest's summary.

The original action returned JUnit XML, the shard marker, warnings and container outputs. It did
not capture process/thread stacks or syscall tracing at the deadline. Therefore its final wait
site is unknown. In particular, neither a Runtime shutdown wedge nor an infrastructure-only cause
is established by these artifacts.

After pytest returns, testcontainers registers `Reaper.delete_instance` with `atexit`; it removes
Ryuk through Docker. Bazel's `test-setup.sh` also waits for the test process and then builds its
undeclared-output manifest. Both are boundaries after the pytest summary; source inspection alone
does not identify which was active in the failed attempt.

## Diagnostic rerun

On rebased code `5253755290`, all four shards [passed](https://app.buildbuddy.io/invocation/fd97767c-59a7-41ac-b1b7-3aa1eadf0623)
with their original deadlines:

```shell
bbr test //agentplane/env/e2e:test_pid1_container --nocache_test_results \
  --test_env=PYTHONPROFILEIMPORTTIME=1 --test_arg=-vv --test_arg=-s \
  --test_arg=--setup-show --test_arg=-o --test_arg=faulthandler_timeout=15
```

This uses Python's import profiler and pytest's fixture/stack diagnostics. The four action times
were 16.74, 47.57, 25.75 and 21.51 seconds. Shard 1's pytest session took 3.846 seconds; its fixture
trace records gateway, event-loop and session fixture teardown. It exited about 0.92 seconds after
the reported session end. The rerun did not reproduce the original timeout. It cannot establish the
original last wait site, and no timeout increase, skip, stub or speculative lifecycle change was made.

If it recurs, capture the test process and wrapper's stacks/syscalls at their original deadline;
separate interpreter/import startup, pytest execution, Python exit and wrapper postprocessing.

## Earlier timeouts

Two earlier action timeouts ([34316f8a](https://app.buildbuddy.io/invocation/34316f8a-2ee0-5d55-a251-262c2c3de123)
shard 3, [c4b91ed6](https://app.buildbuddy.io/invocation/c4b91ed6-4ca6-5bae-9675-3e850fb23e87) shard 1)
also retained no stacks. Diagnostic reruns with `-o faulthandler_timeout=10` all passed, but two
10-second captures caught the main thread waiting on Docker's container-start response and in
`docker.api.image.pull` from the Testcontainers Ryuk reaper. The test now preloads the pinned Ryuk
image (`//third_party/containers:ryuk`), removing that registry dependency; the start-response delay
is unexplained. Those reruns also measured 5.6–11.1 seconds importing `process_fixtures`, mostly
History test support and Kubernetes clients.
