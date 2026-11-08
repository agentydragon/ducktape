# 200-run serial RBE experiment

The observed guest, infrastructure and setup failures, with invocation and
execution IDs, are catalogued in [rbe_200_flakiness.md](rbe_200_flakiness.md).

On 2026-11-08 UTC, a one-off Kubernetes Job was created in `agentplane-staging`
to drive 200 uncached repetitions of `//agentplane/system_tests/suites:test_vm`
using BuildBuddy remote execution. It pins source commit
`f2b019861e975fc2a63b656b339a7f201b4066d1` and requests the ordinary VM test
target, not the separate KVM pool target. The Job's small client Pod excludes
node `wyrm2`; the actual test actions run on BuildBuddy RBE.

The initial Job used one `bb remote --script` call with `--jobs=1`,
`--runs_per_test=200`, `--flaky_test_attempts=1`, `--nocache_test_results` and
`--keep_going`. Although the client requested 48 hours, BuildBuddy terminated
the hosted runner at one hour due to its free-tier limit. The Job failed.

The test invocation is [`b834d3e5-8dc4-4bc5-8dd4-4da10000f07e`](https://app.buildbuddy.io/invocation/b834d3e5-8dc4-4bc5-8dd4-4da10000f07e).
Its test summary retains nine passing runs. Run 10 continued on RBE after the
runner was killed and finished with exit code 0 at 03:49:40 UTC. Its execution
ID is `/uploads/e3260da6-6730-4967-8471-8b5dad8ea5e1/blobs/952098f78359119047bda8851b06b8f8eda5a01f5108ead5d218247f65b3caa8/1852`;
its retained `test.xml` digest is
`b279acefc7dec7407bd26aa505ffa0ddb7d45281484b035e0fa16bdeb2b72be4/354`
and reports one test, zero errors and zero failures. Thus ten completed tests
passed; the tenth is not present in the interrupted invocation's test summary.
No completed shutdown-stall reproduction was observed in these ten tests.

A continuation Job, `vm-shutdown-stalls-190-20261108`, requests the remaining
190 tests using consecutive `bb remote` calls, three fresh serial tests per
invocation and one final single-test invocation. It keeps the same pinned
source, options and tag, with a 58-minute client timeout per batch. Before
starting the next batch, it uses the BuildBuddy API to verify the preceding
invocation completed and contains all requested fresh remote test results.
Test failures are retained and do not by themselves stop the continuation;
unknown or incomplete outcomes stop it without replay. The Kubernetes Job has
no automatic Pod retry and the bb client's whole-run retries are disabled.
BuildBuddy may independently retry infrastructure executions.

The continuation completed 30 tests: 29 passed and one failed. Including the
initial ten, the cumulative count is 39 passes and one failure. In failed
[batch 6, run 2](https://app.buildbuddy.io/invocation/1c1dc380-f62b-48f4-8925-e9eea2c11347),
`systest-vm-codex-oom` did not reach Ready. Its VMI UID was
`822bc372-1f45-451e-96aa-990e7e6d893f` and launcher Pod UID was
`5d089109-6e9d-42f8-bc8b-318293900281`. The guest console reported repeated
RCU stalls from 07:26:19 UTC, with CPU 1 in `pv_native_safe_halt` and a possible
timer-handling issue. The host sent graceful shutdown at 07:30:27 and destroyed
the domain after grace expiry at 07:30:57. The retained guest console contained
no ACPI receipt, shutdown proof, powerdown or SysRq marker. The other two guests
recorded proof and powerdown. This resembles the separately observed pre-Ready
RCU wedge, but does not establish its cause or reproduce the original post-ACPI
quiet interval.

The next [invocation](https://app.buildbuddy.io/invocation/823c6c31-0892-44d1-889c-e308b257743d)
failed during analysis when GitHub returned 504 for the uv distribution
manifest; BuildBuddy reported no test targets and no TestRunner executions.
The continuation stopped without replay. A replacement Job,
`vm-shutdown-stalls-160-20261108`, starts from the verified count of 40. It
retains the same batch verification and permits at most two automatic retries
only for this exact 504 when the invocation completed as a build failure,
its log reports zero test targets, and the execution API shows no TestRunner.
Other incomplete or ambiguous outcomes still stop without replay.

The Pods read the existing namespace-local `buildbuddy-api-key` Secret. The
latest continuation mounts scripts from ConfigMap `vm-shutdown-stalls-160-20261108`.
Neither the key nor guest logs are copied into this repository. These are one-off
resources, not part of the cluster's GitOps configuration.

To inspect progress and locate the individual invocations:

```sh
kubectl -n agentplane-staging get job vm-shutdown-stalls-160-20261108
kubectl -n agentplane-staging logs job/vm-shutdown-stalls-160-20261108
bbapi invocation list --repo https://github.com/agentydragon/ducktape \
  --tag vm-shutdown-stalls-200-20261108 --count 100 --all
```

`BATCH_BEGIN` reports each test invocation ID before submitting work and
`BATCH_VERIFIED` records the cumulative completed count. Tag-filtered search
did not return the original disconnected invocation during the review, even
though its detail displayed the tag; use the IDs from Job logs as well.
Inspect each failure's own run/shard/attempt and guest artifacts before
classifying it as the historical shutdown stall.
BuildBuddy artifact references have limited retention. The Job itself does not
automatically preserve raw artifacts outside BuildBuddy.
