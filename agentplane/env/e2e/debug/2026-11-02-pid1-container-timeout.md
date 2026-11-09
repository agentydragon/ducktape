# PID1 container shard timeout

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
