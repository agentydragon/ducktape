# Go runner spikes, 2026-10-30

Both ordinary Go `testing` and Ginkgo 2 can run independently reported cases against one
shared k3s stack. The repeat runs spent 126.7 s and 124.8 s in the same three VM scenarios;
the much larger whole-action differences came primarily from stack startup. The useful
change in fixture ownership is that each case can clean up its VM immediately, overlapping
that work with its peers.

Prefer ordinary Go `testing` for new Go scenarios: parallel subtests, parent `t.Cleanup`,
explicit phase boundaries and rules_go's existing JUnit integration cover this suite's
requirements with less wiring. Ginkgo is viable when its native spec deadlines, labels
and Kubernetes testing conventions justify the coordinator and process-level ownership.
Neither result justifies rewriting the existing assertions solely for speed. Per-case
resource ownership and overlapping cleanup can also be implemented around the current
Python scenario bodies.

## Scope and reproduction

The [runnable spikes](../x/go_runners/README.md) use Go 1.26.4, Ginkgo 2.33.1 and Gomega 1.44.0.
A test-only Bazel Python executable retains the existing `running_stack`, three VM scenario
bodies, scripted model implementations and assertions. Go owns scheduling, case lifetimes,
selection and JUnit. This evaluates the runner integration and migration boundary, not a
full Go rewrite of protocol clients or scenario assertions.

Each real case has a separate Client, scripted upstream and independently booted Sandbox.
Claude uses the supervision template, Codex the ordinary VM template, and the third case
runs Codex tool OOM containment and its follow-up turn. No assertions were removed. Model
requests reach the case's existing scripted fixture through the Pod identity verified by
LLM ingress. The host is a Bazel `py_binary`, launched directly through runfiles.

The VM targets use the production suite's `k3s_runtime("vm")` artifacts and RBE properties.
They are manual experiments. Measurements below used `--nocache_test_results` on source
`eea1a2cd` plus the spike working-tree patches uploaded by `bbr`; the PR was subsequently
rebased. The pytest control selected only `test_vm_sandbox_scenarios`, matching the three
spike cases rather than including the credential/sampler group.

## Lifecycle probes

Six cases with concurrency capped at three exercise more than one batch. Both runners
recorded exactly one host/stack identity, peak ownership of three cases, six passes,
six closes and one stack stop. The exclusive phase verified the stack remained alive
after every parallel case had cleaned up. Each VM run recorded three different Sandbox
UIDs and three different Pod UIDs, with three case passes and closes.

| Check                                                                 | Go `testing`                                                          | Ginkgo 2                                            | Evidence                                                                                                                                                             |
| --------------------------------------------------------------------- | --------------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Six cases, two bounded batches, exclusive phase, JUnit                | Pass                                                                  | Pass                                                | [probe](https://app.buildbuddy.io/invocation/7c93a555-7c83-488d-8e48-424276b76be6)                                                                                   |
| One injected assertion failure                                        | One failure; five peers pass; all six close; host exits               | Same                                                | [expected failures](https://app.buildbuddy.io/invocation/f4ceef62-044d-44bf-840c-383e67753332)                                                                       |
| One injected timeout                                                  | Per-case context deadline; five peers pass; all six close; host exits | Native `SpecTimeout`; same cleanup and peer results | [expected failures](https://app.buildbuddy.io/invocation/6794d4aa-1798-4e40-a341-c22746c51253)                                                                       |
| Select one case                                                       | Exactly one executes and closes                                       | Exactly one executes and closes                     | [Go](https://app.buildbuddy.io/invocation/80e0335e-b43e-4506-a460-7adb6c457d95), [Ginkgo](https://app.buildbuddy.io/invocation/775943af-a0cb-428f-8553-f6d0037eb722) |
| Existing three VM scenarios, including supervision and OOM assertions | Pass twice                                                            | Pass twice                                          | Runs below                                                                                                                                                           |

The failure and timeout commands deliberately return nonzero. Their artifacts show five
successful peers, all six case closes, no remaining owners, and a successful host exit.
They test cooperative cancellation through an HTTP request and subsequent fixture cleanup;
they do not establish recovery from an unkillable worker or a hard Bazel timeout.

Ginkgo filtering initially returned success without running anything: rules_go also
interpreted `TESTBRIDGE_TEST_ONLY` as a filter for the enclosing `TestSharedStack` function.
The wrapper now translates it to Ginkgo's `--focus` and unsets the Go-level filter. It also
uses `--fail-on-empty`. The corrected run's lifecycle artifact proves one case actually
executed; exit status alone was insufficient. Only the Ginkgo coordinator writes JUnit;
per-worker rules_go XML wrappers are disabled to prevent competing report writers.

## Runtime measurements

All values are seconds. Action durations come from Bazel `TestResult`, stack readiness from
the existing k3s timeline, and scenario/cleanup spans from the existing OpenTelemetry output.
The cleanup tail runs from the last scenario's completion to the final Sandbox cleanup.
It excludes subsequent stack shutdown. Build time is outside the test-action measurement.

| Run                      | Whole test action | Stack ready | Three-scenario wall time | Final cleanup tail | Evidence                                                                             |
| ------------------------ | ----------------: | ----------: | -----------------------: | -----------------: | ------------------------------------------------------------------------------------ |
| pytest TaskGroup control |             349.0 |        99.5 |                    162.2 |               53.2 | [control](https://app.buildbuddy.io/invocation/b8025b73-006b-4f14-b62c-219d7a2a1f12) |
| Go first                 |             330.5 |       169.6 |                    120.8 |               15.9 | [Go](https://app.buildbuddy.io/invocation/d0895db4-2f56-4cc1-a364-7bb589f3c68c)      |
| Ginkgo first             |             549.8 |       258.5 |                    229.6 |               21.9 | [Ginkgo](https://app.buildbuddy.io/invocation/7c5d3627-85ba-4662-b32d-9b83054da413)  |
| Go repeat                |             263.7 |        90.7 |                    126.7 |               19.0 | [repeat](https://app.buildbuddy.io/invocation/53eaff87-883b-4f1c-abbb-a969346579d2)  |
| Ginkgo repeat            |             354.8 |       190.3 |                    124.8 |               16.0 | [repeat](https://app.buildbuddy.io/invocation/53eaff87-883b-4f1c-abbb-a969346579d2)  |

These samples do not establish a framework speed ranking. Executors can have different
containerd stores, import state and VM boot latency. In the repeat pair, the scenario
windows differed by only 1.9 s while stack-ready times differed by 99.6 s. The first Ginkgo
run also had a slower scenario window; its passing result does not explain that variation.
More samples with controlled starting state would be needed for an attributable throughput
comparison.

The cleanup improvement is visible directly in the traces. The pytest control ended its
scenarios at 162.2 s, then cleaned up Sandboxes in intervals 162.2–179.4, 179.4–196.4 and
196.4–215.4. Go's repeat cleaned them up in intervals 109.2–125.3, 118.2–133.6 and
126.8–145.8. Ginkgo's repeat showed the same overlap. Independent case ownership removes
the current group-wide wait and serial teardown; a new language is not required for that.

## Integration tradeoffs

Go's named subtests share a process, so direct Go fixtures can be ordinary values. The
parent must use `t.Cleanup`, not `defer`, because parallel children resume after the parent's
function returns. A nested `t.Run` group waits for all children and cleanups before the next
exclusive phase. Per-case deadlines need contexts passed through operations; Go's global
`-test.timeout` is a process-level failure and is not an equivalent fixture-safe deadline.

Ginkgo's CLI coordinates three separate processes over a Bazel-precompiled `.test` binary.
`SynchronizedBeforeSuite` starts the host only on process 1 and distributes a serializable
handle. `DeferCleanup` gets an independent cleanup context after a spec timeout;
`SynchronizedAfterSuite` waits for all processes before the owner stops the stack. `Serial`
provides the exclusive phase. Live sockets and in-memory fixtures cannot simply be shared
across those processes, so an external owner or explicit Go fixture service remains necessary.

For adoption, first separate scenario assertions from framework fixture acquisition, then
port one scenario and its protocol helpers into Go. Keep stack presets, image artifacts and
provisioning runner-independent. Evaluate that authoring cost separately from the successful
scheduling probes; the Python host bridge here should not silently become the permanent
scenario API.
