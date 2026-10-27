# Central sampler v1 validation

Evidence for [PR #12225](https://github.com/agentydragon/ducktape/pull/12225), 2026-10-27.
The [service contract](../x/central_sampler/SPEC.md) and
[runtime instructions](../x/central_sampler/README.md) describe the implementation.

Review found four correctness and delivery defects: known provider failures permanently blocked
Sessions; a late interrupt remained pending for the next queued turn; exited, unpolled processes
retained runtime capacity; and History export imposed a 16-record/500-ms delivery limit.
[The original PostgreSQL probes](https://app.buildbuddy.io/invocation/4b0cd371-4b3d-4391-9f7d-a2b75a6b783f)
reproduced the first two defects. The fixes distinguish resolved rejection from uncertain effects,
settle turn-targeted interrupts atomically, reconcile process exits while retaining unread output,
and drain History through commit-driven bounded workers. No deployment or production data changes
are part of this follow-up.

[The combined review regression run](https://app.buildbuddy.io/invocation/e4068f42-a8ab-4fe8-b0c2-b5f18c246f13)
passed seven test targets: session correctness, full service/setup/Gateway integration, compaction,
controls and real process interruption, including all 14 new correctness cases and changed-library
lint. The standalone schema test exhausted its 60-second action limit during fixture startup,
before entering its migration assertions. Its retained `container_setup.log` shows the reaper
loading successfully and PostgreSQL image loading still in progress at termination. That test now
has medium sizing to include cold image/import startup; no runtime or assertion deadline increased.

[The final acceptance run passed](https://app.buildbuddy.io/invocation/5d55fcc6-93d3-4570-b7dd-8b30f4529d73):
the data-preserving standalone schema migration test completed in 30.511 seconds, and the real k3s
Dispatcher → sampler → executor → retained/folded History scenario completed in 149.568 seconds.

| Review regression                                                                                                                                | Result and retained evidence                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| Unpolled process exit releases the only runtime slot; another Session completes; retained output remains readable                                | [Passed](https://app.buildbuddy.io/invocation/cb73855b-9935-4e22-ba37-97e2172de457), 51.266 seconds |
| Multi-page History export, commits during export, final snapshot, lost local acknowledgement, cross-instance notification and listener reconnect | [Passed](https://app.buildbuddy.io/invocation/c9b9e684-6516-4b5f-9ae6-b4a3e3c6f75d), 79.371 seconds |
| Shared standalone sampler regression                                                                                                             | [Passed](https://app.buildbuddy.io/invocation/0736fc25-9f1f-49b0-99e8-96e3533c8898), 55.191 seconds |

| Check                                                                                                               | Result and retained evidence                                                                                                                                              |
| ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sampler service, settings, shared runtime and Gateway configuration                                                 | [Passed](https://app.buildbuddy.io/invocation/4ff5d8fb-34cc-4c2a-873d-aa8f03edade2)                                                                                       |
| Authenticated History export, exact retained events and lost-ack reconciliation                                     | [Passed](https://app.buildbuddy.io/invocation/1a2ea88d-9bb6-41e3-9a63-f364b34917e9)                                                                                       |
| Full Gateway regression, including executor relay framing and close ordering                                        | [Passed](https://app.buildbuddy.io/invocation/35c83a4a-b813-4cb5-ad30-52fb40394c2c)                                                                                       |
| Combined sampler → Gateway → Runner → pinned exec-server shell/patch, fencing, shutdown and retained/folded History | [Passed](https://app.buildbuddy.io/invocation/6ac5c921-7583-4281-b1ee-5db95f194669), 88.884 seconds                                                                       |
| Real k3s Dispatcher → sampler → bound executor → retained/folded History                                            | [Passed](https://app.buildbuddy.io/invocation/650b3fa1-c2ee-4b9f-a8a8-ed615a9ed55e), 225.541 seconds                                                                      |
| Native executor companion regression                                                                                | [Passed](https://app.buildbuddy.io/invocation/06b2fa44-6bff-47ab-9f4f-497c95df28a2)                                                                                       |
| Rebased launch form behavior                                                                                        | [Passed after shared-fixture integration](https://app.buildbuddy.io/invocation/6c046d65-39a8-42fb-a436-34235901683c)                                                      |
| Desktop/mobile central launch and unavailable-executor views                                                        | [Passed](https://app.buildbuddy.io/invocation/002995a1-a5c4-4e56-8177-ba0babed1cec); all ten updated captures inspected, including editable setup and the renamed harness |
| Verified Session attribution through the pinned LiteLLM image                                                       | [Passed](https://app.buildbuddy.io/invocation/478ebe24-8d5a-4537-928e-bc7118d9ae9c), 54.8 seconds                                                                         |

The combined Gateway test initially passed shell execution, patching and stale-incarnation
rejection, then stalled during Runner shutdown. A
[diagnostic run](https://app.buildbuddy.io/invocation/c109a73f-da4c-42f6-89d8-3856572590a4)
captured Go goroutines: relay workers had exited; `codexExecutor.stop` was waiting for
`fenceExecutorContainer` to observe a fresh idle companion generation. The Docker fixture omitted
the kubelet-style restart already modeled by the native companion test. The correction waits for
container exit and restarts it, retaining the existing 20-second Runner shutdown assertion.
Temporary SIGQUIT diagnostics were removed. The exact History comparison requests a 1,000-entry
page, matching the Store read; the default 128-entry page was smaller than this test's 161 events.

The LiteLLM test's former 60-second limit was exhausted after 38.3 seconds loading the image and
continued Python imports. A diagnostic run passed in 56.2 seconds, and an earlier actual pass took
50.3 seconds. The final test uses medium sizing and streams image/container logs to an undeclared
output artifact, preserving diagnostics if execution is interrupted.

These checks use real PostgreSQL and pinned Codex host/executor binaries with scripted provider
responses. They do not establish live provider/subscription compatibility, deployed remote-v2
compaction support, or the production cgroup delegation boundary. Deployment remains separate.

Setup review follow-up: [all setup acceptance, service and Gateway tests passed](https://app.buildbuddy.io/invocation/56f2b464-3e7e-4668-9b19-a7a018dc15a9), including durable no-replay recovery and setup output retained in History. The Gateway scenario took 62.837 seconds.

The broad PR CI run passed all 220 tests but failed mypy on executor-frame reads. Explicit protobuf type narrowing fixed the errors; [library and mypy passed](https://app.buildbuddy.io/invocation/888df6c7-2ac2-410e-a6d9-581ef8feb42c), and the [existing executor-client test passed](https://app.buildbuddy.io/invocation/64d7cd68-1404-442a-b562-0ec67e05922c).

The standalone [Langfuse fixture refactor #12228](https://github.com/agentydragon/ducktape/pull/12228) landed on `devel` as `7e3c3f80` before central integration. URL, Sandbox and Thread tests now share the fixture; the central Session metadata key is added once there.

The integrated Langfuse URL, Sandbox launch and projected-Session tests all passed in [the shared-fixture run](https://app.buildbuddy.io/invocation/6c046d65-39a8-42fb-a436-34235901683c). The launch test submits a non-empty setup script unchanged for `HARNESS_CENTRAL_SAMPLER`.

The first k3s scenario run ([`acaf0225`](https://app.buildbuddy.io/invocation/acaf0225-00e4-4dac-aa56-241898514487)) failed before central admission: kube-proxy could not restore rules for the multiple Gateway endpoints during a rolling update because the single-node fixture lacks `xt_statistic`. The disposable scenario now uses `Recreate`, waits for the Dispatcher channel to become ready, and retains service/EndpointSlice/Pod and Gateway/History diagnostics. Its sampler identity Pod also uses the fixture’s fully qualified imported image tag and must become Ready before token admission. Production rollout configuration is unchanged.

The next [k3s run](https://app.buildbuddy.io/invocation/ffb97a01-891a-42fc-9f63-c0e6f2173169) reached Dispatcher admission but failed before the first provider request: the node-hosted fixture treated the per-Sandbox headless control Service as a ClusterIP and tried to resolve `None:8080`. Retained Service/EndpointSlice evidence identified the ready holding Gateway address. Stack startup took 323.6 seconds; the test action took 587.8 seconds. This was an addressing failure, not exhaustion of the test body's timeout.

The corrected [k3s scenario passed](https://app.buildbuddy.io/invocation/650b3fa1-c2ee-4b9f-a8a8-ed615a9ed55e), including fixture-library mypy and Ruff checks. The fixture resolves the Sandbox-owned, Gateway-managed EndpointSlice and waits for it to name the restarted holding Gateway before admitting a Session. It then verifies setup output, the model's exact executor output and opaque reasoning continuation, the final answer, raw source links, and matching completed reasoning/assistant/tool/input entities in History. The retained `test.outputs/central-sampler-k3s.json` reports setup success and `terminal_cursor == folded_through == 157`. Stack startup took 131.9 seconds; pytest took 206.64 seconds and the Bazel test action took 225.541 seconds.

This k3s scenario uses deployed Dispatcher, Gateway, Runner, History, PostgreSQL and Kubernetes TokenReview with the actual pinned JS host and exec-server. Its sampler runs on the test node, assembling `Sampler`, `Runtime` and the exporter directly. Production `main`, an in-cluster sampler Deployment, delegated host cgroups and live provider compatibility require separate coverage.
