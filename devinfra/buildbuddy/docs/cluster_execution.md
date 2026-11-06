# Cluster execution pilot

The `cluster-oci` pool runs ordinary container actions in two Kubernetes executor
pods, each reserving two CPUs and 4 GiB of memory, with a preference for separate
hosts. Placement allows Linux amd64 workers
without a hostname or region pin and excludes control-plane nodes. The executor
has no roaming-node toleration, KVM device, or Kubernetes service-account token.
The VM-isolated `kvm-vm` pool remains the environment for nested Kubernetes/KubeVirt.

`//devinfra/buildbuddy:test_bbr` opts into this pool with `--config=cluster-tests`:

```bash
bbr test //devinfra/buildbuddy:test_bbr --config=cluster-tests --nocache_test_results
```

Its `TestRunner` action requests OCI isolation without Docker initialization.
Its build actions and other tests continue to use the hosted Firecracker platform.
Without the config, this target also uses hosted execution. Ordinary CI does not
enable the pilot config.

BuildBuddy's `use-self-hosted-executors` property selects one backend per action;
`Pool` selects a pool within that backend. Using the same pool name does not combine
hosted and self-hosted capacity. The
[documented scheduling properties](https://www.buildbuddy.io/docs/rbe-platforms/#action-scheduling-properties)
provide no automatic overflow between the two. Hosted-service support for shared
routing needs confirmation from BuildBuddy; the pilot uses explicit routing.

After deployment, run the command above uncached and inspect its `TestRunner`
execution's worker ID against the pod executor's startup log. Confirm that build
executions still use hosted workers. Roll back by omitting `--config=cluster-tests`
and scaling the executor to zero in Git. Quote `replicas: "0"`: the pinned chart's
`default 1` replaces numeric zero with one replica.
