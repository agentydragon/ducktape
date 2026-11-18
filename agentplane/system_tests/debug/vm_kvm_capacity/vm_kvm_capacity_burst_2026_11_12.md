# KVM burst admission and selected graph

Do not promote required VM routing from the isolated KVM execution advantage.
A four-action burst on the existing three-slot pool made its fourth action
finish 66.7 seconds after the managed VM control. Pod was last in both observed
selected graphs, so a VM-only change has zero fixed-clock gate headroom here.
No routing, worker size, pool range or required coverage was changed.

## Reproduction and input identity

The burst used literal source `4415ac2544aef190cce5596d5ee2e73ba627b56f`
plus the adjacent temporary sizing patch. That patch matches the managed VM
request to KVM: four CPUs, 7 GiB and 50 GB. Apply it only in an isolated checkout:

```bash
bbr test //agentplane/system_tests/suites:test_pod \
  //agentplane/system_tests/suites/mcp:test_actions \
  //agentplane/system_tests/suites:test_vm \
  //agentplane/system_tests/x/vm_kvm:test_vm_kvm \
  --runs_per_test='//agentplane/system_tests/x/vm_kvm:test_vm_kvm@4' \
  --nocache_test_results --keep_going
```

[Invocation b2fe7d70](https://app.buildbuddy.io/invocation/b2fe7d70-e305-4bf1-a933-bca60f59405b)
passed all seven physical actions and all 31 JUnit cases in 493.894 seconds.
The adjacent JSON retains physical IDs, source identities, phase evidence and
limits. Decoded Actions use the same pinned OCI image and explicit resources.
Five actual CAS runfiles match between managed and KVM: `test_vm.py`, `stack.py`,
`baseline.py`, `vm/configuration.py` and the rendered VM fixture. This is a
five-file check, not a complete runfiles-tree comparison. All ten returned KVM
QEMU shutdown artifacts contain `-accel kvm`; both real harness turns and guest
supervision still run. KVM cleanup completed through ordinary K3s stop and
pytest teardown in every run.

## Capacity and completion

| Physical action | Native queue | Preparation | Input fetch |  Process |
| --------------- | -----------: | ----------: | ----------: | -------: |
| First KVM       |      Unknown |      2.192s |      2.062s | 213.219s |
| Second KVM      |      Unknown |      8.175s |      8.065s | 249.947s |
| Third KVM       |      Unknown |     34.220s |     13.397s | 240.266s |
| Fourth KVM      |     215.314s |      0.317s |      0.296s | 219.338s |
| Managed VM      |       0.076s |      2.556s |      1.954s | 365.655s |

Input fetch is nested inside preparation. The first three KVM queued timestamps
are after worker start; their queue intervals are unknown. The fourth reused
the first completed worker. Its queue plus execution ended at 03:09:06 UTC,
after managed VM at 03:07:59 UTC. These are a bounded burst, not queue percentiles
or four independent complete PR invocations.

A 15-second Mimir series from 03:00–03:10 UTC on November 13 showed three
registered healthy workers throughout, up to three busy, and each busy worker
assigned 4,000 milli-CPU and 7,516,192,768 bytes. Local reservation sum reached
nine; reservations are neither unique tasks nor central backlog. All workers
were already registered. This did not test the one-to-three-worker boot path.
Other CI and the independent MCP candidate overlapped; managed provider load
and actual delivered resources remain unknown. KVM native CPU usage is absent,
so requested CPU-minutes cannot be presented as measured compute or billing.

## Repeated graph and cold/warm boundary

The second [selected invocation](https://app.buildbuddy.io/invocation/3b6a56ab-9e98-4428-964c-4e9ef30b2ed3)
passed four actions and 28 cases in 439.182 seconds. Its branch was created from
then-current `origin/devel`, which had advanced to
`42e142053c685c792366bd47620d45fbe43513e8`. A full MCP runfiles-tree comparison
identified a changed central sampler binary/image from the History exporter
extraction. Consequently this is a newer-source observation, not a matched
full-graph treatment or a causal speedup.

Pod and managed VM nevertheless have identical native Action digests across
both invocations and fresh physical executions. Pod took 451.423 then 407.994
seconds and finished last both times. Managed VM was cold then warm: setup
184.534 then 66.493 seconds, cases 128.270 then 169.533 seconds, whole process
365.655 then 289.582 seconds. That demonstrates actual baseline-state exposure,
not a causal baseline saving under equivalent worker load. The second KVM
process took 211.644 seconds, with 111.054 seconds setup and 79.715 seconds case
time; its local image cache state was not certified cold. VM-only routing still
has zero conditional headroom when the unchanged Pod clock is held fixed.

Before promotion, collect at least three bounded four-PR-equivalent bursts on
literal source/image pins, including one beginning after natural scale-down to
one registered worker. Record boot/registration, distinct useful backlog,
healthy/busy slots, exact physical executions, mutable-state isolation and the
last required GitHub gate for each exact head. Keep the existing three-slot cap
unless separate capacity evidence supports an approved infrastructure change.
Queue visibility is tracked separately in #15568; this experiment belongs to
#16026. Its result does not close the remaining full-CI and autoscale questions.
