# Retained K3s state disappeared before teardown (2026-11-02)

The hosted `test_dispatcher` action in BuildBuddy invocation
`e3ffe50b-9197-5cd9-9a5b-9b3606276596` failed while invalidating a retained
baseline. Its standalone containerd exited because
`/var/lib/k3s-env/agent/etc/containerd/config.toml` was absent. The failure
occurred in `_baseline_stop_owned`, before any dispatcher scenario ran.

The retained logs establish that the prior K3s instance started at 02:49:39 UTC,
launched containerd with that exact config at 02:49:42, and served Pods. At
02:57:31, live containerd observed removal of
`/var/lib/k3s-env/agent/etc/cni/net.d/10-flannel.conflist` and exited because
its watched CNI directory had been removed. K3s simultaneously reported its
server TLS and agent files missing. The next action, around 05:54 UTC, still had
the baseline ownership marker but lacked the generated containerd config.

`_reset_previous_stack` deletes the agent configuration and server trees and is
the in-repository operation matching those removals. Its normal callers stop an
owned K3s process and unlink the marker before resetting, however. The retained
logs do not identify which action or process removed the files, so they do not
establish whether reset overlapped a live server, whether a snapshot restored
inconsistent files, or whether something outside this fixture removed them.
Restoring only `config.toml` would leave the missing CNI and TLS state unexplained.

The fixture now writes a retained transition log outside the reset tree. It
records the marker, exact process start ticks, test target, and existence of the
containerd config, CNI directory, and server TLS directory before and after a
reset and when stopping an owned baseline. The next affected action publishes
the bounded log as `baseline-transitions.jsonl` in Bazel undeclared outputs.
Compare its last `reset_begin` and `stop_owned_begin` events to identify the
caller and first point where the state changed.
