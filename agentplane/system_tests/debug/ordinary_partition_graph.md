# Ordinary partitioned system-test graph

The architecture branch now implements the tested system-subtree composition as
ordinary Bazel targets. This is a draft implementation awaiting exact-head CI,
representative affected-target comparisons, cold/burst capacity and whole-PR
reliability qualification. It does not establish the 30% complete-feedback goal.

## Coverage and selection

| Ordinary target                    | Preserved behavior                                                        | Environment                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `suites:test_pod`                  | Fifteen App Session, History, storage, instruction and lifecycle cases    | Real App-backed K3s stack; six bounded case leases; storage exclusive first |
| `suites:test_pod_platform`         | Seven workload-trust, certificate-reissue and Egress-credential cases     | Real controllers, issuers, Gateway and proxy; native fixture identities     |
| `suites/mcp:test_services_mcp`     | Three discovery, consented-identity and Policy-binding cases              | Real App/MCP deployment, OAuth fixtures and Action Service                  |
| `suites/mcp:test_services_central` | Four setup, retained-answer, suspend/resume and Gateway-recovery cases    | Real central services and Pod-bound native readers                          |
| `suites:test_vm`                   | Unchanged complete VM scenario, both harnesses and process-trace contract | Fresh K3s/KubeVirt stack with hardware acceleration                         |
| `suites:test_firecracker`          | Unchanged wrong-issuer and checkpoint/thaw cases                          | Fresh K3s/Firecracker stack without incidental Notification deployment      |

Labels are relative to `//agentplane/system_tests`. Other ordinary system targets,
including the full 120-second Gateway budget and the App-dependent History-outage
witness, remain required through normal affected-target selection.

The Pod bindings have a disjoint exact union of 15+7 original cases; Services
has a disjoint exact union of 3+4. The retained App Pod case function ASTs and
resource owner are unchanged. The three promoted native/MCP modules preserve
their candidate function ASTs. VM and Firecracker source files and the extracted
MCP provenance helper are byte-identical to `bf57990ed4`; the helper retains all
30 assertions inherited from devel. These are source audits, not executed-case
or complete semantic-equivalence proof.

Each ordinary target collects its own complete roster. The former combined
Services entrypoint, duplicate Pod bindings and superseded manual partition/
capacity targets are removed. Historical experiments remain reproducible at
their immutable recorded source, including `22014e7b` on
`agent/ci-system-screen-22014e7b`.

The ordinary Bazel dependency graph owns selection. There is no directory-based
coverage exemption or new permission to ignore BUILD changes. CI still runs
normal Bazel-diff selection, retains affected builds, and uses its existing full
sweep for graph-wide inputs. The older journal substitution policy remains
shadow-only; its synthetic result is not credited to this implementation.
The existing Bazel-diff package-default fingerprint limitation remains a
qualification concern, not a newly solved problem.

## Evidence motivating implementation

At immutable `22014e7b`, the original 14-target and candidate 16-target system
graphs passed the same 50 canonical JUnit cases. In the B/A exposure, native
Bazel elapsed was 592.550s versus 382.619s, 35.4% lower once. Its reviewed physical
action/JUnit results pass the declared requested-resource screens, with actual
KVM CPU/memory unknown and unequal Pod setup warmth.

The reversed A/B exposure also completed successfully: control
[`5950e993`](https://app.buildbuddy.io/invocation/5950e993-0f67-4c8c-acbf-7b90cdd6f328)
took 555.785s, candidate
[`f504b2b7`](https://app.buildbuddy.io/invocation/f504b2b7-16c2-4fb7-b5b0-135ce135f748)
360.143s, 35.2% lower once. Detailed physical-action, case and resource review
is separate from terminal success. Neither pair measures complete required PR
checks, representative selection, p90 or flakiness. The current ordinary graph
also removes duplicate source bindings, so it needs its own execution proof.

## Reliability repair

Rebased ordinary CI at `bf57990ed4` exposed a branch-introduced test defect:
the new two-Gateway contract stopped reading on `exited`, before the companion
had closed and drained its output. Both test read loops now use the existing
executor completion condition: exited, closed and an empty output page. Exact
suffix, cursor advancement, one process identity and fresh successor-command
assertions remain. The corrected full module passes three fresh runs, 12 cases
each, with no failures, errors or skips in
[`9c3eb91e`](https://app.buildbuddy.io/invocation/9c3eb91e-e7f8-43fc-927a-576607ad458d),
using `bf57990ed4` plus the recorded repair overlay. This repairs this branch's
test; it is not a measured production flake-rate improvement.

The old combined Services target also retained an unused direct protobuf
dependency after its helper extraction. Removing it passes the target and
helper type checks in
[`eb67d122`](https://app.buildbuddy.io/invocation/eb67d122-b54a-46a0-990c-e27776a387cd).
The ordinary partition removes that combined entrypoint entirely.

## Resources and remaining qualification

The App Pod action requests eight compute units and permits six case leases;
the platform partition uses its own fresh stack. VM requests four CPUs, 7 GiB
memory and 50 GB disk; Firecracker requests four CPUs, 6 GB memory and 20 GB disk.
Both use `kvm-vm` without recycled K3s state. Requested resources are reservation
proxies, not physical CPU, concurrent memory or monetary cost.

The inspected autoscaler source caps the KVM pool at three workers. The earlier
concurrent trial observed 188 seconds from an additional worker's creation to
readiness. Moving the required VM action into that pool can therefore improve
isolated execution while worsening busy-period feedback. Do not qualify it
without real cold/burst measurements and actual queue ownership.

Remaining acceptance requires:

1. Ordinary exact-head CI and artifact review proving the same 50 canonical cases
   and all affected library/aspect checks pass after partitioning.
2. Actual source, test, BUILD and mixed-input selection evidence on the new graph,
   followed by representative PR replays counting conservative fallbacks.
3. Complete required-check comparisons with normal caching, queues and reruns;
   cold and concurrent PR exposure; no worse p90 or flake rate.
4. Reviewed managed/KVM resource use and recurring capacity/maintenance cost.

Maintenance consists of keeping scenario ownership and direct dependencies
accurate, retaining fresh per-case state, and qualifying KVM capacity. There is
no new persistent fixture service, lease broker or retained snapshot lifecycle.
