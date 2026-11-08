# 200-run serial RBE experiment

On 2026-11-08 UTC, a one-off Kubernetes Job was created in `agentplane-staging`
to drive 200 uncached repetitions of `//agentplane/system_tests/suites:test_vm`
using BuildBuddy remote execution. It pins source commit
`f2b019861e975fc2a63b656b339a7f201b4066d1` and requests the ordinary VM test
target, not the separate KVM pool target. The Job's small client Pod excludes
node `wyrm2`; the actual test actions run on BuildBuddy RBE.

The Job uses one `bb remote --script` call to start a single Bazel test
invocation with `--jobs=1`, `--runs_per_test=200`, `--flaky_test_attempts=1`,
`--nocache_test_results` and `--keep_going`. Its test invocation carries the
BuildBuddy tag `vm-shutdown-stalls-200-20261108`. The remote runner has a
48-hour timeout, and the Kubernetes Job has no automatic Pod retry. The bb
client's whole-run retries are disabled to avoid replaying work after an
uncertain disconnect. BuildBuddy may independently retry infrastructure
execution attempts; inspect those separately from Bazel test attempts.

The Pod reads the existing namespace-local `buildbuddy-api-key` Secret and
mounts its scripts from ConfigMap `vm-shutdown-stalls-200-20261108`. Neither
the key nor guest logs are copied into this repository. The Job and ConfigMap
are one-off resources, not part of the cluster's GitOps configuration.

To inspect progress and locate the individual invocations:

```sh
kubectl -n agentplane-staging get job vm-shutdown-stalls-200-20261108
kubectl -n agentplane-staging logs job/vm-shutdown-stalls-200-20261108
bbapi invocation list --repo https://github.com/agentydragon/ducktape \
  --tag vm-shutdown-stalls-200-20261108 --count 100 --all
```

`EXPERIMENT_BEGIN` reports the test invocation ID before submitting work.
`EXPERIMENT_COMMAND_EXIT` records the bb client's outcome. A completed or
failed Job does not by itself establish that all 200 results were produced;
check the invocation's individual test results and distinguish test failures
from interrupted or retried infrastructure actions. Inspect each failure's
own run/shard/attempt and guest artifacts before classifying it as the
historical shutdown stall.
BuildBuddy artifact references have limited retention. The Job itself does not
automatically preserve raw artifacts outside BuildBuddy.
