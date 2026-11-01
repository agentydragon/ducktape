# Hosted RBE k3s persistence spike (2026-11-01)

At source commit `de5f671002f516e952c3dfafcbf06659a4bc731b`, a manual BuildBuddy-hosted
Firecracker test target retained a live k3s server and cert-manager/trust-manager controllers between
two complete, independently executed `test_pod.py` actions. Both actions passed all 13 existing Pod
cases. The second action reused the same k3s boot ID, PID and process start time, Kubernetes node and
`kube-system` UIDs, and four PKI controller Deployment UIDs. Its `systest` namespace, PostgreSQL Pod,
per-action root CA Certificate and Secret, monitoring namespace, and scripted-model listener were
fresh. Both actions completed explicit cleanup before the VM was snapshotted.

| Full Pod action | BuildBuddy invocation                  | Cases     | Stack setup span | TestRunner wall | TestRunner CPU | Peak memory |
| --------------- | -------------------------------------- | --------- | ---------------: | --------------: | -------------: | ----------: |
| Cold            | `7ef08fcc-d50a-402a-bef7-f54fb1e3f8c8` | 13 passed |         109.43 s |       285.051 s |      693.425 s |    4.934 GB |
| Warm            | `182acac7-a360-4f2e-9818-2eb8860e209a` | 13 passed |          35.54 s |       214.168 s |      465.318 s |    4.991 GB |

Both actions ran:

```sh
bbr test --config=rbe --config=ci --test_tag_filters=-live_openai_api --nocache_test_results --test_output=all //agentplane/system_tests/suites:test_pod_persistence_spike
```

The setup span fell by 73.89 s and TestRunner wall by 70.883 s in this one pair. Native execution
metadata reports worker queue 0.060/0.058 s and worker preparation 1.411/0.236 s. The warm action's
VM metadata names the cold action as `lastExecutedTask`; both used worker
`891471c9-f4e0-4c51-8a39-87e493a92c02` and VM `4a52b958-2a4e-4a92-80c7-8de2fad63766`.
The cold action saved a local snapshot; neither action proves cross-worker remote-snapshot availability.
The native metadata does not isolate snapshot-write time from other post-test TestRunner overhead.
This is one pair under concurrent CI load, not an estimate of production CI savings.

The manual `test_pod_persistence_spike` target uses a dedicated v5 recycling key. The ordinary Pod
target and workflow have no persistence policy. The key must change when baseline inputs change:
the default prepared seed is immutable, so a fingerprint mismatch safely rebuilds cold on every
action until a new key seeds the updated baseline. The v5 path copies the k3s executable, kubeconfig,
and logs outside action runfiles, launches k3s in its own process session, and verifies a fingerprint
of the k3s binary, stack code, CRDs, PKI release YAML, and the PKI OCI index manifests. It checks
the live API, process start time, node address, and cluster UIDs before attaching. A dirty marker
prevents reuse after incomplete teardown; an owned stale server is stopped before rebuilding.
Warm setup validates retained PKI Deployment readiness and issues fresh fixture certificates without
reimporting or reapplying PKI controllers. Teardown waits for the two app namespaces, monitoring
namespace, fixture-owned PVs, root CA resources, exact rendered cluster-scoped resources, and
policy-controller-created ClusterRoleBindings to disappear before marking the state clean. Normal
`running_stack` still stops k3s and removes all Pods.

Earlier discriminating probes found two separate blockers. A v1 pair with the default snapshot
policy restored the same older seed rather than each other's state, and both started k3s cold.
A v2 pair with `allow-remote-snapshots=true`, save policy `always`, and read policy `newest` advanced
the snapshot lineage, but k3s still restarted: it was a child of Bazel's test process group, which
[Bazel 9.2 kills after the test exits](https://github.com/bazelbuild/bazel/blob/9.2.0/tools/test/test-setup.sh#L280-L283).
The v3 detached-server probe with a fresh key and default policy proved live reuse across two bare
API actions: the same boot ID, PID/start ticks, and Kubernetes UIDs, with cold/warm TestRunner walls
32.731/12.827 s. [BuildBuddy's documented VM reuse](https://www.buildbuddy.io/docs/rbe-microvms/#preserving-microvm-state-across-actions)
is best effort. Public BuildBuddy source at `acaf608b95780c2b0681b0205182c38bf5b6237d` describes
default snapshot behavior, but may differ from the hosted deployment.

The first full-suite v4 attempt passed all 13 cases but failed in teardown when cert-manager's
garbage collector removed an already enumerated CertificateRequest before explicit deletion.
That failed action saved a local VM snapshot while the marker was dirty. The v5 trial used a fresh
key and idempotent deletion; both actions passed and completed cleanup. All raw BuildBuddy API
responses, test logs, action identities, and a machine-readable reviewed summary are under
`/tmp/agents/2026_11_01/ci-refresh/persist-spike/`. The upcoming
[latency evidence publication](https://agentydragon.github.io/ducktape/latency/evidence.json)
is pending. A production change would still need a versioned key
strategy, cross-worker/eviction evidence, more pairs, and a decision on whether keeping a live
cluster in a recycled worker is worth its memory and storage cost.
