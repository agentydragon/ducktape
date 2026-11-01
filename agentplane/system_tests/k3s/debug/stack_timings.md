# k3s stack timings

Measurements behind the stack's setup choices, for the work on k3s system test latency. Older
measurements (Python services, image splits, import profiles before 2026-10-21) are in
[the README as of `7fd9f16f7d`](https://github.com/agentydragon/ducktape/blob/7fd9f16f7d/agentplane/x/k3s_env/README.md).

This is historical measurement data. Target names and invocation identities below are preserved as
recorded before scenarios were grouped into suites; current targets are listed in the
[system tests README](../../README.md).

## VM stack timings, 2026-10-21

Booting, suspending and resuming one VM Sandbox with those images imported
([invocation](https://app.buildbuddy.io/invocation/0929e52f-9074-4bf7-8565-7ee4c7720432)) took 350 s
of action time; pulling them at run time, 405 s and 429 s. Temporary markers in `stack.py`, not
committed, timed the stack from k3s's start:

| Seconds after k3s starts                                       |   s |
| -------------------------------------------------------------- | --: |
| k3s up                                                         |  19 |
| KubeVirt's and Kyverno's images imported, 1.15 GB of blobs     |  81 |
| Their releases applied                                         |  89 |
| The stack's own images imported, 1.52 GB                       | 167 |
| Every Deployment Available, KubeVirt and Kyverno already ready | 202 |

Importing the two sets at once took 169 s and 202 s, against 58 s and 78 s one after the other
([invocation](https://app.buildbuddy.io/invocation/a7f1a8e7-b891-4495-9252-a4327412d10b); that worker
was slower throughout, k3s up at 47 s).

The `_vm` targets in one invocation ([invocation](https://app.buildbuddy.io/invocation/b1635c1d-db13-4262-842f-cf07b912d07b)),
each on its own worker: `test_vm_sandbox_vm` (two VM Sandboxes, a turn on each) 483 s,
`test_storage_vm` 492 s, `test_suspend_resume_vm` 597 s; the Pod case,
`test_suspend_resume`, 216 s.

In a run with pulls ([invocation](https://app.buildbuddy.io/invocation/989d9e78-957b-4e0c-8f54-717e5f679e59)),
create, Ready, suspend, resume and Ready again took under 2 minutes; the resumed VM was running 14 s
after its VMI was created. In the run before it, a Sandbox's VM was scheduled 18 s after creation and
running 3 s later; the emulated guest reached `/init` at 6 s, and the runner started about 14 s after
the VM was running.

## Recycled runners, 2026-10-23

Separate invocations, one after another. Import is the time from node Ready to the last image imported:

| Target               | Runner                            | Import, s | Stack ready, s | Action, s | Invocation                                                                            |
| -------------------- | --------------------------------- | --------: | -------------: | --------: | ------------------------------------------------------------------------------------- |
| `test_thread_stream` | Fresh                             |     100.9 |          153.6 |     219.7 | [2cee4afd](https://app.buildbuddy.io/invocation/2cee4afd-2664-4bd2-8d96-8973b048a72b) |
| `test_thread_stream` | Recycled from a pod stack         |       9.9 |           49.5 |     114.4 | [989dde6f](https://app.buildbuddy.io/invocation/989dde6f-18a6-4a49-871a-48630a399397) |
| `test_thread_stream` | Recycled from a pod stack         |       9.5 |           50.4 |     125.1 | [34b657ed](https://app.buildbuddy.io/invocation/34b657ed-e240-4fdd-a57e-30212feee1d7) |
| `test_vm_sandbox_vm` | Not identified                    |         — |          196.9 |     380.4 | [5e4df251](https://app.buildbuddy.io/invocation/5e4df251-1f5b-46b2-8c7e-3217fd2fedac) |
| `test_thread_stream` | Recycled from a pod stack         |      11.5 |           53.0 |     136.4 | [d7529758](https://app.buildbuddy.io/invocation/d7529758-327e-4dcc-81ca-39566fe922d8) |
| `test_thread_stream` | Likely recycled from the VM stack |     196.6 |          336.4 |     461.5 | [8283cca5](https://app.buildbuddy.io/invocation/8283cca5-9dc0-402e-bed2-df32e4832b2a) |

The first three rows ran an earlier teardown, which removed the Pods while k3s was paused; setup was the
same. In the last row the node took 33 s to become Ready, against 9–17 s elsewhere, and the import was cold:
a VM stack keeps its layers on disk, so a pod stack after it deletes them and rebuilds the store in memory.

## VM guest root import ordering, 2026-10-28

On source `9ab44ab64b`, one candidate run with the import reorder (code diff SHA-256
`0b06e1f5188b98ac7031b58244a1f4122d41599326248482630a577d806179c0`, uploaded by `bbr`
as a working-tree patch) and one unchanged control ran the same
`test_vm_sandbox_vm` target with `--nocache_test_results`. Both passed all three cases.
They used the same observed BuildBuddy worker identifier, but that does not establish the same
Firecracker guest or starting containerd snapshot. The candidate ran first.

| Seconds after stack start              | Candidate [b262742f](https://app.buildbuddy.io/invocation/b262742f-68dd-40ba-9bf4-17d6333c984f) | Control [6afbeff6](https://app.buildbuddy.io/invocation/6afbeff6-c996-4682-9e8b-291c4ec9cdcf) |
| -------------------------------------- | ----------------------------------------------------------------------------------------------: | --------------------------------------------------------------------------------------------: |
| Node Ready                             |                                                                                            11.4 |                                                                                          12.8 |
| KubeVirt and Kyverno applied           |                                                                                            26.3 |                                                                                          30.9 |
| Service and guest boot images imported |                                                                                            93.9 |                                                                                         122.9 |
| Fixture manifests applied              |                                                                                           100.1 |                                                                                         195.1 |
| Guest root import duration             |                                                                                            65.1 |                                                                                          64.9 |
| Guest root import ended                |                                                                                           165.2 |                                                                                         187.8 |
| Deployments Available                  |                                                                                           169.1 |                                                                                         220.9 |
| Stack ready                            |                                                                                           170.3 |                                                                                         221.8 |
| Whole test action duration             |                                                                                           600.4 |                                                                                         636.3 |
| Whole test action CPU seconds          |                                                                                          1217.9 |                                                                                        1261.5 |

The candidate applied manifests before importing the guest root. Deployment readiness then
proceeded during the 65-second import; only 3.9 seconds elapsed from its end to Deployments Available.
In the control, 25.8 seconds elapsed from manifest application to that milestone.
The 51.5-second stack-ready difference also includes 29 seconds of variation before the guest-root
import, so it is not an isolated estimate of the reorder's effect. One pair cannot establish a
population-wide action or CPU saving. Neither run recorded the exact pre-import image target,
content, and unpacked snapshot state needed to prove a safe digest-based import skip.

## Image layers in memory, 2026-10-21

The [containerd import tail](https://github.com/agentydragon/ducktape/blob/7fd9f16f7d/agentplane/x/k3s_env/README.md#containerd-import-tail) profile found unpacking bound by filesystem writes.
One run each of `test_thread_stream`, importing the same images one `ctr images import` per image:

| containerd storage                                 | Import, s | Action, s | Invocation                                                                            |
| -------------------------------------------------- | --------: | --------: | ------------------------------------------------------------------------------------- |
| Disk, every image streams all its blobs            |     107.7 |     235.0 | [10eae102](https://app.buildbuddy.io/invocation/10eae102-419a-45f1-8670-3868978cd6af) |
| Whole root on tmpfs, every image streams all blobs |      75.6 |     182.5 | [c78cffa4](https://app.buildbuddy.io/invocation/c78cffa4-7619-4420-945d-bf1c716d4d38) |
| Snapshotter root on tmpfs, shared blobs once       |      64.8 |     190.7 | [d1c5e40b](https://app.buildbuddy.io/invocation/d1c5e40b-d99b-40e4-9d9b-a6f802e69195) |

The first two ran alone; the last ran beside `test_mcp` and `test_suspend_resume_vm`, also with the
snapshotter root on tmpfs. `free -m` when each stack was ready (before any scenario ran), in MiB of the
executor's 9724; shared memory is mostly the layer tmpfs:

| Target                   | Shared | Available |
| ------------------------ | -----: | --------: |
| `test_thread_stream`     |   3042 |      4991 |
| `test_mcp`               |   3520 |      4328 |
| `test_suspend_resume_vm` |   4521 |      2426 |

On the VM stack that is close to the tmpfs default cap (half of memory), and 2.4 GiB is left for guests
that request 1.5 GiB each, so a stack with VM Sandboxes keeps its layers on disk.

## Phase timings and image bytes, 2026-10-21

Pytest time of every passing run of `test_lifecycle` (a model-free create, bootstrap, suspend, resume and delete spike, since deleted) BuildBuddy holds, by UTC day (each day mixes
branches, and some runs carried temporary profilers; only the paired sections below isolate a change):

| Day   | Landed that day                                            | Runs | Median s | Range s |
| ----- | ---------------------------------------------------------- | ---: | -------: | ------: |
| 10-14 | OCI-layout streaming                                       |    5 |      723 | 534–957 |
| 10-15 | shared Python interpreter layer                            |   13 |      721 | 336–874 |
| 10-16 | Go Gateway/Dispatcher fixture                              |    3 |      293 | 254–338 |
| 10-17 | harness-free runner, Go egress sidecar, Go LLM ingress     |   17 |      204 | 157–309 |
| 10-21 | Go History and Notifications, k3s 1.34, runner disk volume |    5 |      158 |  98–318 |

Three fresh runs on `c0d2b833` ([invocation](https://app.buildbuddy.io/invocation/703e55fa-95f8-41bc-9cc1-c8b6d9aeb2f1),
`--runs_per_test=3 --nocache_test_results`) passed all four cases. Temporary markers in `stack.py`,
not committed, wrote wall-clock phase times; the RBE execution metadata gives the action start and end:

| Seconds on each worker                              | Fast | Middle | Slow |
| --------------------------------------------------- | ---: | -----: | ---: |
| Before pytest starts                                |   27 |     78 |   41 |
| k3s up, CoreDNS moved to the node                   |   13 |     15 |   25 |
| Image import: streaming the blobs to containerd     |   21 |     30 |   54 |
| Image import: containerd finishing after the stream |   13 |     30 |   35 |
| CRDs and manifests applied                          |    3 |      5 |    7 |
| All Deployments Available                           |   31 |     35 |   53 |
| The four lifecycle cases                            |   40 |     40 |   42 |
| Teardown and upload                                 |    7 |      6 |   12 |
| Action execution                                    |  156 |    241 |  269 |

PostgreSQL started in about 2 s in every run; each service with a migration init container failed
its first connection and waited one 10 s `CrashLoopBackOff` before starting. In the slower
[ffa9564c](https://app.buildbuddy.io/invocation/ffa9564c-3c9d-4937-b091-9fc8b191104d) run (318 s
pytest, 421 s action, 586 CPU-seconds) the back-offs reached 40 s; in
[a973af66](https://app.buildbuddy.io/invocation/a973af66-e28b-48b9-8ae2-c1111feeb441) the same
fixture took 98 s pytest, 131 s action and 166 CPU-seconds.

The import is 84 distinct blobs, 938,776,320 bytes. Stored layer bytes by image group, from the
build outputs of the same revision (PostgreSQL from its pinned descriptor sizes):

| Images                                                                                                          |   MiB |
| --------------------------------------------------------------------------------------------------------------- | ----: |
| Python: app, app migrate, Action Service, Action migrate                                                        | 410.7 |
| Go: Gateway, egress, Notifications, Sandbox Controller, History, Policy Controller, LLM ingress, egress sidecar | 313.0 |
| Goose migrators (4)                                                                                             |  24.9 |
| Runner disk image and the tools image's own layers                                                              |  68.0 |
| PostgreSQL                                                                                                      |  78.8 |

382.1 MiB of the Python group is used by no other image; the rest is the Debian base the tools
image shares. The eight Go images share under 1 MiB. The Python group still counts the Action
Service and its migrator, which the Go port has since replaced, so remeasure before using these
shares to prioritize further ports.

## Fixture chart rendered at build time, 2026-10-21

The test module imported the cdk8s chart code, and with it jsii and Node, only to render the
manifest; rendering itself took about 1 s. `render_fixture` now renders one manifest per model
backend at build time, and `rendered_fixture.load` fills in the CAs and model address.
Three fresh `test_lifecycle` runs each, with temporary wall-clock markers
([before](https://app.buildbuddy.io/invocation/0f6663f7-3cca-458b-b7f2-0cbfdb095df7),
[after](https://app.buildbuddy.io/invocation/94bb75ae-3b07-4f5e-8955-ba3788f2ebaf)):

| Seconds per run                               | Before           | After            |
| --------------------------------------------- | ---------------- | ---------------- |
| Test module imports                           | 27.0, 42.8, 28.2 | 12.1, 12.6, 10.1 |
| Action start to `pytest_bazel.main()`         | 36.2, 52.6, 37.7 | 22.8, 23.5, 15.8 |
| Manifest ready in the fixture (render / load) | 1.5, 1.3, 0.9    | 0.1, 0.1, 0.1    |

What is left before pytest is 5–10 s of VM and workspace setup and the remaining imports, mostly
bytecode compilation ([executor constraints](../README.md#rbe-executor-constraints)).

The test process no longer builds the app's `Settings`, which parses argv, so pytest flags such as
`--test_arg=-s` work on `test_lifecycle` and the system test k3s targets
([run](https://app.buildbuddy.io/invocation/93bfc317-4354-46fd-b19b-fac04650fb20)). `test_render`
still renders the chart in process and rejects them.

## Migrators retrying their first connection, 2026-10-21

The Goose and Alembic migrators retry their first PostgreSQL connection in process
([`migrate`](../../../postgres/migrate/migrate.go), [`db_migrations`](../../../../util/db_migrations.py)).
Three fresh lifecycle runs each, on `devel` and with the change, with temporary uncommitted markers
in `stack.py`:

| Three fresh runs                              | Before ([0f6663f7](https://app.buildbuddy.io/invocation/0f6663f7-3cca-458b-b7f2-0cbfdb095df7)) | After ([6c7624c6](https://app.buildbuddy.io/invocation/6c7624c6-272c-4006-b012-250c6ab0cbbd)) |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------: | --------------------------------------------------------------------------------------------: |
| Migration init container restarts, per run    |                                                                                      10, 10, 9 |                                                                                       0, 0, 0 |
| Manifests applied to Deployments Available, s |                                                                               61.8, 51.6, 36.2 |                                                                              39.7, 36.1, 30.8 |
| Last migrator done after PostgreSQL Ready, s  |                                                                                     14, 17, 15 |                                                                                       2, 3, 3 |

What is left of that phase is mostly the services' readiness probes: a 3 s initial delay and a
10 s period, so a service that is not ready at its first probe waits 10 s more.

## Shared suites, 2026-10-30

An uncached run of the original 16 targets at `40834883`
([`303b29db`](https://app.buildbuddy.io/invocation/303b29db-ab30-4858-88fc-f42bb22ddaa9))
and an uncached run of the seven shared suites at `340372b6`
([`520cadd0`](https://app.buildbuddy.io/invocation/520cadd0-f4cb-4000-a8bb-951027d2af8e))
passed every target. Durations come from Bazel `TestResult` events, including fixture setup and teardown.
These are single samples, with no test-result cache hits; they do not measure full CI wall time.

| Metric                         | Original targets | Shared suites | Change |
| ------------------------------ | ---------------: | ------------: | -----: |
| Sum of test execution, seconds |         3503.193 |      2977.788 | −15.0% |
| Longest test, seconds          |          520.977 |       670.706 | +28.7% |
| Bazel elapsed, seconds         |          621.070 |       678.909 |  +9.3% |

Sharing reduced aggregate execution but increased the longest target. The candidate's longest target
was `test_pod`; its messaging cases took 239 s for Claude and 90 s for Codex, including the target's
first stack setup. `test_mcp` took 584 s, with its first case including 280 s of stack setup. Further
latency work should measure these cases and suite balance separately from aggregate setup savings.

## Four shared stacks and ordinary VM supervision, 2026-10-30

The Pod sampler shares MCP's stack; the VM sampler shares the credential group's stack and runs
alongside its two guests. Private supervisor checks run during the existing Claude guest's boot,
while both harnesses check workspace ownership, credentials, resource limits and descendant cleanup
through ordinary Session setup. This leaves four k3s stacks and six independent VM boots, with at
most three VMs running at once.

Passing full CI invocations before ([`0e2a2d5f`](https://app.buildbuddy.io/invocation/0e2a2d5f-5370-5abb-b42f-2629caa2bb47),
`c70cebc9`) and after ([`0a0a3a5b`](https://app.buildbuddy.io/invocation/0a0a3a5b-11c3-58e9-b983-01dbf08d10dc),
PR head `7ba1d438`) recorded these Bazel `TestResult` durations. All listed suites executed without
test-result cache hits; other tests in these CI invocations could use the cache.

| Metric                          | Seven stacks | Four stacks | Change |
| ------------------------------- | -----------: | ----------: | -----: |
| Sum of suite execution, seconds |     2968.011 |    2093.900 | −29.5% |
| VM suite, seconds               |      618.727 |     510.875 | −17.4% |
| Longest suite (Pod), seconds    |      726.915 |     751.619 |  +3.4% |
| Bazel test invocation, seconds  |      780.287 |     798.831 |  +2.4% |

The new VM suite also includes the sampler and supervisor checks that previously had separate
targets. Its action took 8.5 minutes; MCP took 10.8 minutes, Pod 12.5 minutes and Dispatcher 3.0
minutes. The Pod suite remains the longest target. Consolidation reduced aggregate execution in
these samples; it did not reduce full CI latency.

A separate uncached focused comparison also passed every suite:
[`83882cff`](https://app.buildbuddy.io/invocation/83882cff-d0ff-4f78-ab39-b316bc5b194e)
versus [`a2a03f15`](https://app.buildbuddy.io/invocation/a2a03f15-d398-4a0f-947c-68a4b730d8a3).
Aggregate execution fell from 3084.474 to 2087.000 seconds (−32.3%); the longest target fell from
803.810 seconds (VM) to 736.298 seconds (Pod). The final VM suite took 509.579 seconds. Pytest's
first VM case took 281.848 seconds including shared stack startup, and its sandbox/OOM case took
197.677 seconds including both harnesses' Session setup checks and the private supervisor checks.

These are single samples with uncontrolled worker/cache state, and the candidate was rebased onto
`e2d26f48`, which also changed model routing. They establish passing coverage and observed execution
costs, not an isolated causal estimate or a reliable CI latency improvement.

## Concurrent Pod and MCP harness pairs, 2026-10-30

Two uncached runs per suite compared sequential harness cases at `ba7d657f` with seven Pod pairs
and four MCP pairs using TaskGroups. The baseline is
[`ff06ed1e`](https://app.buildbuddy.io/invocation/ff06ed1e-5845-44a5-9e65-b143d0deccc2).
The passing Pod candidate at `8c026141` is
[`14bf838e`](https://app.buildbuddy.io/invocation/14bf838e-7721-4b37-8e36-edb3e37a3cef);
the passing MCP candidate at `96cbd44e` is
[`6b324d31`](https://app.buildbuddy.io/invocation/6b324d31-8ac7-4d51-b7d2-5b05708c2612).
The latter invocation's Pod runs failed on deployment-wide command-ID collisions; the final
candidate gives independent thread-stream commands unique IDs while retaining intentional retries.

| Suite | Baseline action seconds | Candidate action seconds | Mean change |
| ----- | ----------------------: | -----------------------: | ----------: |
| Pod   |            526.6, 599.9 |             485.0, 592.7 |       −4.3% |
| MCP   |            498.8, 525.5 |             372.6, 377.0 |      −26.8% |

These are Bazel test-action durations, including startup and teardown. The MCP stack-ready mean
fell from 199.9 to 69.9 seconds, accounting for most of its apparent improvement. Pod stack-ready
time increased from 93.3 to 118.4 seconds on average. Unchanged storage cases also varied widely.

Summing the affected pytest cases, and deducting shared stack-ready time from the first Pod case,
gives 290.4 → 265.2 seconds for Pod (−8.7%) and 220.6 → 209.1 seconds for MCP (−5.2%). These figures
still include per-case setup and cleanup. For example, the launcher pair took about 114 seconds
concurrently versus 110 seconds summed across sequential harnesses. The measurements support modest
observed savings in the changed scenarios, not a twofold improvement from parallel execution.

Each candidate suite passed twice without test-result caching. The helper's overlap and peer
cancellation tests also passed twice in `6b324d31`. Worker/snapshot state was uncontrolled, and the
candidate base includes intervening devel changes, including kvm-clock restoration work. These
samples do not isolate a causal CI latency improvement. Alternative runner compatibility results
are recorded in [the framework evaluation](../../debug/concurrent_runners.md).
