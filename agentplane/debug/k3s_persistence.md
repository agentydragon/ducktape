# Hosted RBE k3s persistence (2026-11-01)

## Expansion to every k3s application suite

Persistence is the default for every `k3s_suite` declaration: Pod, Dispatcher, MCP Actions,
MCP Services, normal VM, manual VM, profile VM and real-model. Their standard targets use retained
infrastructure directly; the separate manual comparison targets have been removed. Existing manual
and live-model tags continue to control suite selection. Application fixtures remain fresh per action.

The retained baseline is k3s plus cert-manager/trust-manager, with KubeVirt and Kyverno added for VM
suites. VM snapshots have a separate recycling key, disk-backed image layers and the existing 8-unit
resource estimate. Their fingerprint includes the exact KubeVirt configuration, operator releases and
OCI image descriptors, layer mode, and installation/cleanup helpers. Warm readiness checks remain;
evidence requires all six virtualization resource UIDs and four PKI Deployment UIDs to match the seed.

Applications and PostgreSQL remain fresh, as do MCP fixtures, model endpoints, roots/intermediates,
collectors and workspace disks. Successful teardown stops the provisioner and validates guest logs,
removes fixture namespaces and cluster resources, confirms no VM/VMI or owned PV survives, deletes the
provisioner's StorageClass, and detaches every owned loop device before certifying the baseline clean.
Failures leave the marker dirty for a cold rebuild. The existing lifecycle and bare-cluster probes
retain their diagnostic semantics.

The current snapshot families are `k3s-persistence-v7-{pod,vm}`. Every application stack uses
persistence without an environment opt-in. Historical commands and commit-pinned links below retain
the comparison-target names used when those measurements were collected.

### Non-VM cold and warm results

Source `4a190b1991a0748884e0431d84371901c1074f52` passed all five non-VM suites in both the
[cold wave](https://app.buildbuddy.io/invocation/8752f1b1-ad4d-412f-a7f2-dccbe85a0f71) and repeats
([Pod/Dispatcher/real-model](https://app.buildbuddy.io/invocation/e7fcb343-9437-495a-97fa-1815a9631a4b),
[MCP](https://app.buildbuddy.io/invocation/544152d9-3e5f-4882-8c0a-6c0077aecc40)).
The enclosing cold invocation failed because of the VM outcomes described below; its five non-VM
suite results and the lifecycle probe passed.

| Suite        | Cases per action | Cold setup | Warm setup | Cold TestRunner wall | Warm TestRunner wall |
| ------------ | ---------------: | ---------: | ---------: | -------------------: | -------------------: |
| Pod          |               13 |    141.7 s |     37.6 s |              329.5 s |              224.6 s |
| Dispatcher   |                3 |    136.6 s |     39.6 s |              264.2 s |              167.3 s |
| Real-model   |                4 |    146.1 s |     40.3 s |              286.7 s |              189.6 s |
| MCP Actions  |                5 |    176.2 s |     39.1 s |              352.4 s |              212.3 s |
| MCP Services |                5 |    196.1 s |     51.8 s |              349.9 s |              213.7 s |

Each pair ran exactly the same JUnit case identities, with no skips or failures. All five repeats
reported warm, matching a cold seed's complete k3s and PKI identities. Pod and Dispatcher restored
an MCP Actions seed, demonstrating reuse across different application fixtures. The five fresh UID
fields (application namespace, PostgreSQL Pod, root Certificate, root Secret and collector namespace)
were pairwise distinct across all ten actions; every cleanup completed. Warm setup fell by 71–78%,
while action time fell by 32–40% in these selected pairs. They are not a fleet hit-rate estimate,
a controlled estimate of causal savings, or an end-to-end CI speedup.

### VM cleanup repair and trials

The initial profile and manual VM actions passed their scenario bodies but failed teardown's
cluster-resource absence check. That check treated `kubectl` stdout plus stderr as resource names;
the fixture uses the deprecated Kyverno ClusterPolicy API, which emits warnings. Source
`4e5ce89345f5a547d158f6420666aa5c62ee55fc` separates resource stdout from diagnostic stderr and includes
actual leftover names in any failure. No scenario assertion, timeout, shutdown or storage check changed.
The initial normal VM action first reported native status 14, `error pausing VM` with an unavailable
Firecracker socket and no JUnit. A later physical execution of the same action produced a passing
scenario body but a boot-evidence teardown error: the selected guest lacked a monitor-readiness
receipt. BuildBuddy exposed one logical test run/attempt despite the two physical executions. The
stdout repair occurs after guest-log validation and does not explain this separate receipt failure;
a later pass is not evidence that it was repaired.

[The repaired VM wave](https://app.buildbuddy.io/invocation/fd9d3fc8-81f8-44af-a21c-2cf7d798a178)
passed the normal and profile suites through cleanup. Both rebuilt cold after the source fingerprint
change, retained the existing digest-addressed image store, and released three workspace loop devices.
Normal VM setup was 95.0 s and TestRunner wall 374.2 s; profile setup was 95.5 s and wall 368.2 s.
A cold baseline rebuild is not necessarily an empty image cache.

### Final all-suite validation

[The final all-suite invocation](https://app.buildbuddy.io/invocation/a690a9d1-34b8-4b9a-ada6-2689937e3f83)
completed successfully at runtime source `4e5ce89345f5a547d158f6420666aa5c62ee55fc`: eight suites,
35 JUnit cases, no failures/errors/skips, with cleanup complete in every action. The normal and profile
VM targets each contain one batch case running three concurrent scenarios; manual VM has three cases.
This explicit command includes manual/live targets that ordinary PR CI excludes, so its 863 s wall
is not an ordinary CI latency measurement.

| Suite        |   Cases | Baseline outcome                       |   Setup | Successful TestRunner wall |
| ------------ | ------: | -------------------------------------- | ------: | -------------------------: |
| Pod          |      13 | Cold rebuild                           |  71.2 s |                    256.5 s |
| Dispatcher   |       3 | Cold rebuild                           |  80.6 s |                    203.6 s |
| Real-model   |       4 | Cold rebuild                           |  65.2 s |                    192.2 s |
| MCP Actions  |       5 | Cold rebuild                           |  71.4 s |                    244.6 s |
| MCP Services |       5 | Cold rebuild                           |  74.7 s |                    235.0 s |
| VM           | 1 batch | Warm                                   |  38.6 s |                    303.8 s |
| VM manual    |       3 | Warm                                   |  41.6 s |                    857.7 s |
| VM profile   | 1 batch | Cold fallback after a physical failure | 123.0 s |                    560.4 s |

The five non-VM targets restored prior snapshots but rebuilt because the helper change invalidated
their fingerprints. Their kernel boot IDs survived while k3s process/start, node, kube-system and PKI
identities changed. They are not warm hits. The normal and manual VM actions instead retained the
fixed-cold normal VM seed's complete k3s identity, four PKI Deployment UIDs and six virtualization
resource UIDs. All five fresh application/database/certificate/collector UID fields changed from the
seed and were pairwise distinct across all eight final actions. Each VM action also created a fresh
workspace StorageClass, completed cleanup and detached all three owned loop devices.

Normal VM setup went from 95.0 to 38.6 s; action wall went from 374.2 to 303.8 s in this selected pair.
The final profile action passed after a separate physical execution failed; its successful action was
a cold fallback, so profile-specific warm reuse is not established. Final warning-level logs show the
GET's Kyverno deprecation warning during successful teardown with no remaining resource names,
confirming the stdout/stderr cleanup mechanism. The profile-image variant used its default native
profiling setting; optional `AGENTPLANE_VM_NATIVE_PROFILE=1` was not added to these commands.

### Reliability findings retained from failed trials

The successful final run does not erase these earlier failures:

- The repaired-cold manual VM target passed its first storage case, then failed two 300 s Sandbox
  readiness waits with unbound workspace PVs. Its background provisioner had exited on a 10 s
  Kubernetes PVC-list read timeout, surfaced when teardown joined that worker. The fixture remained
  dirty. The final warm manual run passed all three cases, but does not establish that this failure
  was repaired. A concrete follow-up is bounded retry of that read-only list operation and prompt
  propagation of terminal provisioner failures, preserving claim-UID ownership and avoiding replay
  of uncertain PV creation.
- The original normal VM's later physical execution passed its scenarios but failed its boot-evidence
  assertion. Its selected console contains no monitor-readiness receipt. The guest monitor unit
  reported ready to systemd, then exited status 1 before emitting the receipt; the reporter independently
  recorded the failed unit. This is a guest-side failure rather than rejection of a present CRI marker.
  The exact command failure between notification and receipt is not observable from retained logs.
  The cleanup repair occurs later and does not address this issue; command-level guest diagnostics
  are the next evidence needed.
- Two physical VM actions (original normal VM and final profile VM) hit Linux 5.15 guest kernel
  general-protection faults in scheduler load-average code, followed by kernel panic and Firecracker
  exiting. The native `error pausing VM`/refused socket is downstream, during workspace output syncing
  after guest execution. The matching public
  [BuildBuddy callsite](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L3307-L3355)
  is distinct from post-action snapshot recycling; the hosted code version is unconfirmed. Native
  `serverLogs.vm_log_tail.txt` proves the panics and VMM exits, but not what corrupted the kernel
  pointers. Provider crash capture and host/VMM logs are needed for that boundary. A later physical
  execution produced the logical test result in each case; logical run/attempt counts alone hide
  this physical work. No snapshot-save failure or causal link to persistence is established.

Ordinary required CI checks passed at the measured runtime source. Persistence is now enabled in the
standard suite targets. The recorded reliability issues remain unresolved; setup savings do not
establish reliable fleet warm availability or an end-to-end 2x CI improvement.

### Reproduction and limits

Each command uses `bbr test --config=ci --test_tag_filters= --nocache_test_results` with the explicit
standard suite labels. The final all-suite command additionally passes `--test_arg=--log-cli-level=WARNING`
so successful cleanup's API warnings remain visible. The eight labels are:

- `//agentplane/system_tests/suites:test_pod`
- `//agentplane/system_tests/suites:test_dispatcher`
- `//agentplane/system_tests/suites:test_vm`
- `//agentplane/system_tests/suites:test_vm_manual`
- `//agentplane/system_tests/suites:test_vm_profile`
- `//agentplane/system_tests/suites:test_real_model`
- `//agentplane/system_tests/suites/mcp:test_actions`
- `//agentplane/system_tests/suites/mcp:test_services`

The real-model target still requests the existing `OPENAI_API_KEY` secret. Changed Python libraries
were also named directly for lint/type aspects; repository hooks passed. Raw invocation, native action,
JUnit, span and persistence identity artifacts remain under the task scratch directory's `all-k3s/`.
The samples do not quantify snapshot-save cost, eviction rates or fleet warm availability across
hardware partitions. Every standard suite now uses persistence.

## PostgreSQL reuse: no measured benefit, removed

Retaining PostgreSQL while recreating its seven service databases passed all 13 cases in each of
five completed actions, but showed no overall timing benefit. The final warm pair took
243.190/230.097 s, with setup 45.001/42.998 s, versus the k3s/PKI-only pair's 233.994/220.454 s
and setup 42.572/39.954 s. PostgreSQL verification took about 1 s and database reset 3–4 s;
LLM ingress readiness still took 23–25 s. These selected small samples do not establish a causal
regression. The PostgreSQL reuse implementation was removed; PostgreSQL remains fresh per action.

[Trial results](https://app.buildbuddy.io/invocation/7729cdcc-b174-488a-85ef-71c26683d01b) and
[full historical evidence](https://github.com/agentydragon/ducktape/blob/c57ec9ebe9d81271c5a295c37407d6daa8f00ff0/agentplane/debug/k3s_persistence_spike.md)
record the experiment at implementation source `59a66d10cad8e9f672a5471ced8de756e1af01cb`.

## Prior continuation: automatic baseline refresh

The manual full-Pod target now uses a v6 snapshot family. It retains the runtime input fingerprint
and clean/dirty ownership marker, and adds `allow-remote-snapshots=true`,
`remote-snapshot-save-policy=always`, and `snapshot-read-policy=newest` only to manual persistence actions.
This lets a changed baseline rebuild cold and replace the prepared snapshot without a manual key
bump. Concurrent revisions can replace each other's latest snapshot and cause cold rebuilds; the
fingerprint prevents reuse of incompatible state. At that revision, ordinary targets did not opt in; the current default is described above.

BuildBuddy's public implementation rejects updates to a restored local-only test snapshot before
applying its save policy, so `always` alone is insufficient. Remote snapshots are required for this
refresh strategy. `newest` requests remote-master reads without caching that fetched master locally;
an existing local manifest can still be used. It does not guarantee the globally latest completed
test's seed; snapshot publication happens after action completion. This is based on
[save gates](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L1439-L1527)
and [manifest selection](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/enterprise/server/remote_execution/snaploader/snaploader.go#L503-L566);
the hosted deployment version is unconfirmed. The probes below verify the observed hosted behavior.

The fingerprint now includes bootstrap, certificate-installation and iptables helper code as well
as k3s, stack code, CRDs and PKI manifests/images. Retained API probes have HTTP and subprocess
deadlines. A post-start failure stops the server until its ownership marker has been written; warm
full-suite setup also requires the retained PKI Deployment identities to match the saved ones.

The real-k3s lifecycle test passed in
[1d31b8ce](https://app.buildbuddy.io/invocation/1d31b8ce-0af9-424d-8708-de21087320de):
post-start identity failure stopped the process and left no marker; the next open was cold; clean
attachment retained the process/cluster identity; a dirty marker rebuilt and removed an actual
leaked namespace; changed fingerprints and dead servers rebuilt; a forged reused-PID marker left
the unrelated process alive. All cleanup completed. Its TestRunner took 67.698 s, with one pytest
case exercising the sequence, not the 13 application cases.

The bare cross-action probe used the same code paths. A comment-only change to `certificates.py`
changed its fingerprint without changing behavior, the recycling key or any execution properties:

| Input | Invocation                                                                            | Result                                   | TestRunner wall |
| ----- | ------------------------------------------------------------------------------------- | ---------------------------------------- | --------------: |
| A     | [1d31b8ce](https://app.buildbuddy.io/invocation/1d31b8ce-0af9-424d-8708-de21087320de) | Cold, snapshot save reported             |        38.345 s |
| B     | [18adc39c](https://app.buildbuddy.io/invocation/18adc39c-0100-4a1f-8ef7-f84217432df7) | Changed input rebuilt cold               |        31.122 s |
| B     | [d91821fb](https://app.buildbuddy.io/invocation/d91821fb-d1ff-46e8-9e67-fea22f4d4942) | Warm, same process/cluster identity as B |        16.901 s |

Every row was a fresh, uncached physical TestRunner with one passing pytest case. Native metadata
reported `savedRemoteSnapshot=true`, without a separate upload-completion clock. B's two actions
had the same action digest. Native metadata traced B's cold action
to the A snapshot and its warm action to the B snapshot; VM ID and snapshot-key components matched.
The temporary input comment was restored exactly after the experiment. Full raw evidence is under
`/tmp/agents/2026_11_01/ci-refresh/continuation/`; these bare probes do not establish full-suite or
production CI savings. Snapshot-write time and bytes are not isolated by the collected action
metadata, so the remote policy's extra cost remains a production decision.

The full Pod continuation passed all 13 existing case identities in each of three fresh actions:

| Full Pod action                 | Invocation                                                                            | Cases     | Setup span | TestRunner wall |
| ------------------------------- | ------------------------------------------------------------------------------------- | --------- | ---------: | --------------: |
| Cold seed                       | [430df10f](https://app.buildbuddy.io/invocation/430df10f-12dc-432f-ba75-acd071e21b11) | 13 passed |  119.027 s |       318.886 s |
| Concurrent run 1, cold fallback | [7326cb8f](https://app.buildbuddy.io/invocation/7326cb8f-c6c8-4982-92d1-3038f41a4c92) | 13 passed |   171.84 s |       363.312 s |
| Concurrent run 2, warm          | [7326cb8f](https://app.buildbuddy.io/invocation/7326cb8f-c6c8-4982-92d1-3038f41a4c92) | 13 passed |   24.994 s |       209.368 s |

The concurrent command added `--runs_per_test=2` to the full-suite command below; these are separate
runs, not retries. Native execution intervals overlapped for 209.368 s on distinct workers. All
three application namespaces, PostgreSQL Pods, test-root Certificate/Secret pairs and collector
namespaces had distinct UIDs, and all three cleanups completed. Run 2 retained the cold seed's
exact k3s and four PKI Deployment identities on a different reported worker; its native
`lastExecutedTask` names the cold seed's snapshot and execution.

Run 1 safely fell back to cold startup. The baseline fingerprint and platform hash matched the
other two actions, but its VM configuration hash was `b506fa68...` rather than `ee99195a...`; its
snapshot version and VM ID also differed, with no previous task reported. All three requested
4000 milliCPU, 10,000,000,000 bytes of memory and 53,687,091,200 bytes of free disk. The differing
provider VM configuration partitions the snapshot cache; resource requests and our fingerprint do
not alone identify that configuration.

Read-only REAPI `ActionCache.GetActionResult` reads of all three per-snapshot manifests resolved the
difference: every serialized `VMConfiguration` field matched except `host_cpuid.family`. The seed
and warm worker had family 26; the cold concurrent worker had family 25. The serialized configuration
SHA-256 values exactly matched the native `configurationHash` values above. BuildBuddy intentionally
includes host CPU vendor/family in snapshot compatibility
([configuration contract](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/proto/firecracker.proto#L31-L79),
[host identity](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L4469-L4475)).
The two families therefore need separate prepared seeds. Test execution properties cannot set this
host identity. Decoded manifests, hash checks and raw responses are retained in
`continuation/manifest-configs.json` and `continuation/manifest-*.response` under the scratch root.
One warm and one cold result in this selected pair is not a fleet hit-rate estimate. Eviction
behavior and stable production warm availability remain unproven.

One final two-run repetition after both observed CPU families had seeds passed 13/13 in each action
([716dea8f](https://app.buildbuddy.io/invocation/716dea8f-8867-4748-8d6d-4775ddc1dd6f)). TestRunner walls
were 234.0 s and 220.5 s; setup spans were 42.572 s and 39.954 s. Both reported warm with the same
baseline fingerprint as the earlier actions. Run 1 restored family 25's `f91b6928...` snapshot;
run 2 restored family 26's `136f284c...` snapshot, each retaining its seed's k3s/PKI identity.
Their native execution intervals overlapped for 220.396 s. These selected actions validate reuse
after both hardware families were seeded, not fleet-wide warm availability or a measured
end-to-end CI reduction.
The five fresh-resource UID fields were pairwise distinct across all five full-suite actions.

The initial continuation actions tested source `64f78edc9865201457ef8f1d151252283042c690` plus reviewed source patch A
SHA-256 `a9d9f47476535a045091080f02b31d7ba41e28f51c4a7d394f0a77a65014ee3f`; B's temporary input-change
patch was `fa88f7044c840a1988f0e2bb01a935eb8381b6107d935a4fa3a4b206bd50d905`.
Commit `27aa8b8733a975a079a3ad91bfe7515fcd841bfc` contains the tested Python/Starlark changes; its
additional changes are the investigation note and README. Two earlier preflight invocations,
`5bb3fa76-aa90-459d-a342-5abceab6a1c6` and `b983915a-b0ac-4c2a-8f92-4c61b50c04b0`, excluded manual
tests through CI's tag filter and executed no TestRunners. They are not passing test evidence.
The final pair ran directly from `27aa8b8733a975a079a3ad91bfe7515fcd841bfc` with only investigation
Markdown changes; its Python/Starlark inputs match the initial continuation actions.

Repeat the lifecycle contract with:

```sh
bbr test --config=rbe --config=ci --test_tag_filters=-live_openai_api --nocache_test_results --test_output=all //agentplane/system_tests/k3s:test_persistence_lifecycle //agentplane/system_tests/k3s:stack
```

## Initial v5 full-suite experiment

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

The initial manual `test_pod_persistence_spike` target used a dedicated v5 recycling key. Its local-only
prepared seed was immutable, so changed inputs rebuilt cold on every action until a new key was
chosen. The v6 refresh experiment above addresses that limitation. The persistence implementation copies the k3s executable, kubeconfig,
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
is pending. A production change still needs snapshot-cost and eviction evidence, more pairs,
and a decision on whether keeping a live
cluster in a recycled worker is worth its memory and storage cost.
