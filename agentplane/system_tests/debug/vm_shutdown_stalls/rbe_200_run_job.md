# 200-run serial RBE experiment

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

The Pods read the existing namespace-local `buildbuddy-api-key` Secret. The
continuation mounts its scripts from ConfigMap `vm-shutdown-stalls-190-20261108`.
Neither the key nor guest logs are copied into this repository. These are one-off
resources, not part of the cluster's GitOps configuration.

To inspect progress and locate the individual invocations:

```sh
kubectl -n agentplane-staging get job vm-shutdown-stalls-190-20261108
kubectl -n agentplane-staging logs job/vm-shutdown-stalls-190-20261108
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
