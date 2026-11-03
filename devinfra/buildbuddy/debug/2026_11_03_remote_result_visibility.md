# Remote completion lookup missed a successful runner

Observed on 2026-11-03 UTC while validating Debundle documentation PR
[#14076](https://github.com/agentydragon/ducktape/pull/14076), head
`23afa4bcfd0d2896a0b7e73183cb9426e0f145aa`.

The authoritative GitHub job
[125769933920](https://github.com/agentydragon/ducktape/actions/runs/41591782789/job/125769933920)
failed after its remote command exited 0. Its final CLI error was:

```text
Remote run completed at 2026-11-03 06:47:09.158 UTCwait for run result: ci_runner execution not found
```

The `bazel-ci-linkage-41591782789-1` artifact records `bb_remote_exit_code: 1`.
Both linked child invocations independently report complete/success:

| Role  | Invocation                                                                            |
| ----- | ------------------------------------------------------------------------------------- |
| Test  | [28892aa1](https://app.buildbuddy.io/invocation/28892aa1-5768-52a7-9fe6-1db728d879e2) |
| Build | [fc0283b2](https://app.buildbuddy.io/invocation/fc0283b2-a1c7-5409-989a-ce67cddc2870) |

The outer invocation
[ed8fac62](https://app.buildbuddy.io/invocation/ed8fac62-fc7b-4c18-ba47-0ce78f194d54)
also reports complete/success. A later `bbapi execution` lookup returned its
completed `RemoteBazelRun` record. `bb execution get` retrieved the corresponding
ExecuteResponse with a result, exit code 0 and no error status:

```text
bb-snapshot/uploads/ab6e484c-89f5-4423-9929-f3f08d574917/blobs/blake3/489d3777e648d1aed39ba1869cc9caff325cce95011f44ce81fe00e7e4e42e37/149
```

The worker's execution completed at `06:47:09.883154075Z`; output upload
completed at `06:47:09.939888255Z`. GitHub printed the lookup failure at
`06:47:23.8225248Z`. This establishes a successful retained result that was
unavailable to the CLI's lookup, without identifying why the hosted lookup
returned no rows or when that result became queryable.

## Completion path and recovery

The pinned `bb` 5.0.498
[completion path](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/cli/remotebazel/remotebazel.go#L1400-L1449)
reads the outer invocation and polls `GetExecution` after streaming logs. An
empty execution list is retried; exhausting those reads produces this error
before the client can call `WaitExecution`. The successful log command and
child invocations alone do not supply that final execution result.

After verifying the original runner had terminated successfully, the failed
GitHub job was retried at the unchanged PR head with `gh run rerun --failed`.
Attempt 2's native
[job 125774073416](https://github.com/agentydragon/ducktape/actions/runs/41591782789/job/125774073416)
passed at `06:58:26Z`; PR #14076 subsequently merged. The native GitHub job
remains authoritative; a retry must be checked separately.
No CLI retry policy or CI success condition was changed. In particular, do not
convert missing execution metadata into success based on a log line or a
passing test child.

If this recurs, capture the empty `GetExecution` responses and later record
visibility with timestamps, then investigate the hosted lookup/indexing path
or upstream completion tracking. Reading the accepted run's result does not
require launching another run. Whole-run replay and log-stream resets are
separate failure paths documented in
[the CLI retry investigation](ci_remote_cancellation/vm_shutdown_retry.md).
