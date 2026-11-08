# 200-run serial RBE experiment

On 2026-11-08 UTC, a one-off Kubernetes Job was created in `agentplane-staging`
to drive 200 uncached repetitions of `//agentplane/system_tests/suites:test_vm`
using BuildBuddy remote execution. It pins source commit
`f2b019861e975fc2a63b656b339a7f201b4066d1` and requests the ordinary VM test
target, not the separate KVM pool target. The Job's small client Pod excludes
node `wyrm2`; the actual test actions run on BuildBuddy RBE.

The Job runs twenty consecutive batches of ten, each with `--jobs=1`,
`--runs_per_test=10`, `--flaky_test_attempts=1`, `--nocache_test_results` and
`--keep_going`. Each batch has a unique invocation ID and the BuildBuddy tag
`vm-shutdown-stalls-200-20261108`. Test failures do not abort the experiment;
after each batch the driver checks that all ten numbered results are present,
uncached, and report remote execution. If results cannot be verified, it stops
instead of silently counting or replaying unknown work. The Job has no automatic
Pod retry. BuildBuddy may independently retry infrastructure execution attempts;
inspect those separately from Bazel test attempts.

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

`BATCH_BEGIN` reports the invocation ID before submitting work;
`BATCH_VERIFIED` records the ten test statuses, and `EXPERIMENT_COMPLETE` is
printed only after all 200 results are verified. A Kubernetes Job completion
means the collection loop completed; a failed test remains a test failure
even when the Job continues. Inspect each failure's own run/shard/attempt and
guest artifacts before classifying it as the historical shutdown stall.
BuildBuddy artifact references have limited retention. The Job itself does not
automatically preserve raw artifacts outside BuildBuddy.
