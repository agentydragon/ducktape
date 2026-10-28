# Retry-disabled cancellation comparison: 2026-10-28

**`--remote_retries=0` suppressed the plain-action requeue in one comparison,
but did not prevent continued remote work.** With retries disabled, a different
action released after cancellation still executed successfully for 30 seconds.
This rules out disabling retries as a complete solution to the reproduced
cancellation behavior. No CI retry policy was changed.

These probes extend the [initial reproduction](normal_path_reproduction.md).
[Selected evidence](retry_comparison.json) contains the three new runs, including
the effective explicit retry option from child BES, original cancellation
records, final execution snapshots, observer events, and successful output.

## Results

Runs were sequential, each with a fresh invocation UUID and unique action
identity. Every entry exited 130, the child helper acknowledged cancellation,
and the outer execution record was terminal with cancellation status code 1.

| Case                    | Retry setting | New execution after cancellation checks                             | Verified outcome                                                    |
| ----------------------- | ------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Plain action            | `0`           | None in 65.271 s of observation                                     | Only the original canceled execution remained in the index          |
| Plain action control    | Default       | Same action digest queued at approximately +8.159 s                 | Command ran 30.033 s, exit 0; output `active-action-completed`      |
| Analysis-barrier action | `0`           | Different action queued after publication of the post-cancel marker | Command ran 30.057 s, exit 0; output includes marker and completion |

Including the initial probe, both plain default-retry attempts produced a
successful replacement execution; the one plain zero-retry attempt did not.
The default and zero-retry barrier attempts both produced successful new action
work. This is a small experimental set, not a measured failure probability.

### Plain action, retries disabled

[Child](https://app.buildbuddy.io/invocation/49a7ae0d-3995-4bfa-bba9-67b46d664f13),
[outer](https://app.buildbuddy.io/invocation/dca0ce25-0e5f-41af-a168-c4c58ff47c30).

Cancellation checks completed at `03:47:20.322941Z`. Observation continued for
65.271 seconds, beyond both the default control's approximately eight-second
requeue delay and the 30-second action workload. There were 104 valid execution
snapshots from the first observed child cancellation record onward, no non-JSON
error responses in that interval, and no snapshot missing the original execution.
The final snapshot contained only that original execution, canceled with code 1.
Child BES explicitly recorded `remote_retries=0`.

This demonstrates absence of an indexed replacement in the observation window.
The original's cancellation record is synthetic; it is not proof of the original
worker process's physical stop.

### Plain default-retry control

[Child](https://app.buildbuddy.io/invocation/a6f9a9a6-29f7-47ca-883e-88e1e085d8c5),
[outer](https://app.buildbuddy.io/invocation/ccb7b465-4e14-4348-b52c-c7f9f429b7ad).

Cancellation checks completed at `03:48:56.175877Z`. A new execution of the
original digest was queued at `03:49:04.335138153Z`, started its command at
`03:49:04.431956362Z`, and completed successfully at `03:49:34.465288169Z`.
Its downloaded output matched the recorded CAS digest. There was no explicit
retry override in child BES. The controller neither resubmitted the build nor
released a gate after cancellation.

### Analysis barrier, retries disabled

[Child](https://app.buildbuddy.io/invocation/6bdb45f8-d832-4fd6-b254-da44a7bc3966),
[outer](https://app.buildbuddy.io/invocation/8db50d25-6e6b-405b-8f31-4d7f43263012).

| Event                                       | UTC                |
| ------------------------------------------- | ------------------ |
| Observer sends TERM to entry                | 03:50:09.215928    |
| Observer reaps entry, exit 130              | 03:50:09.429759    |
| Observer completes cancellation checks      | 03:50:09.544453    |
| Observer finishes publishing release marker | 03:50:09.758848    |
| Late execution queued, worker metadata      | 03:50:10.136569923 |
| Late action command starts                  | 03:50:10.914742210 |
| Late action command completes, exit 0       | 03:50:40.971650801 |

Child BES explicitly recorded `remote_retries=0`. The final index contained the
original canceled active action and the successful late action; there was no new
execution ID for the original action. The late action had a different digest.
Its downloaded output contains the unique release digest
`9f2e06497679fff16402b13f1c07ab164e9e3d1b47cc1b70c72c87a20f81a6f0/45`
and `late-action-completed`. Its generated input depended on publication of that
marker after cancellation checks. This establishes causal ordering independently
of cross-host clock calibration.

## Conditions and evidence limits

Use the [original procedure](normal_path_reproduction.md#reproduction-procedure),
adding `--remote_retries=0` only to the child Bazel build in the zero-retry arms.
The default control omitted the option. Workload, runner properties/images,
Bazel 9.2.0, CLI 5.0.466, and cancellation entry point remained the same.
The source commit was `9fac91535c6456e889191a51a360ee2a4fc541ec`; its changes
since the original source commit were documentation/evidence only. New child
UUIDs intentionally changed action digests between runs; compare original and
replacement digests within a run.

The comparison controller extended observation to 65 seconds, ending earlier
only after a successful new execution appeared. It recorded observation duration
and pending execution-lookup errors. Before invocation creation, lookups returned
HTTP 500/non-JSON responses; the controller treated those as pending. All three
post-cancel lookup audits retained the original record and had no such errors.
The signal trigger was an observed `EXECUTING` index stage, which can precede
command start. Successful continued commands were verified afterward from worker
results and downloaded outputs.

The disposable source snapshot is retained locally in
`/tmp/ci-normal-cancel-repro-2026-10-28/retry-source-snapshot/`, with hashes in the
selected evidence. Per-child raw evidence is adjacent. The snapshot captures the
final comparison controller; the first zero-retry run preceded the change from
a hardcoded plain case to a selectable case, with identical plain-case behavior.
These temporary paths are not a durable supported harness. Credentials and raw
API responses remain outside the repository.

All six final cleanup cancellation requests, for the exact owned child and outer
invocations, returned HTTP 200. Neither that acknowledgment nor the terminal
index records measure physical worker teardown. The hosted lease duration and
exact Execute/dispatch initiator remain unobserved.

## Consequence for the investigation

The plain comparison supports the
[Bazel retry mechanism](normal_path_reproduction.md#interpretation-and-next-discriminating-test)
as an explanation of same-digest requeues. It does not attribute the historical
VM action conclusively; there is no client Execute trace or server dispatch
reason for these runs. The barrier counterexample establishes that new work can
escape independently of enabling Bazel remote retries.

Keep the two mechanisms distinct when choosing a change. A targeted treatment
of `CANCELLED` retries could address the first, while cancellation sequencing or
an invocation-level admission fence would need to address later new submissions.
Disabling all remote retries also removes recovery from unrelated transient
remote failures. Next obtain the outer command-stop/lease evidence and initiating
Execute/dispatch trace; then validate a change against both reproduction cases.
