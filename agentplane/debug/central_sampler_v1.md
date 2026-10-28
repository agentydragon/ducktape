# Central sampler v1 validation

Evidence for [PR #12225](https://github.com/agentydragon/ducktape/pull/12225), 2026-10-27.
The [service contract](../x/central_sampler/SPEC.md) and
[runtime instructions](../x/central_sampler/README.md) describe the implementation.

## Go runtime validation

The central service, sampling loop, persistence and exporter are now Go. Acceptance fixtures
start the actual Go binary, including schema initialization and TokenReview authentication.
The pinned upstream JavaScript host remains a separately supervised process.

[The native Go suite passed](https://app.buildbuddy.io/invocation/4257efda-6967-4d64-a782-93fc4bd193b3)
in 27.160 seconds, including real PostgreSQL lease fencing, shutdown ownership races,
interrupt cleanup, exact JSON numbers above 2^53, provider wire handling
and History export/reconnect coverage.

[The actual Go binary passed the scripted-model k3s scenario](https://app.buildbuddy.io/invocation/61474059-c6d8-40fd-b2e5-875b7df97952)
in 153.036 seconds: Dispatcher → sampler → holding Gateway → pinned exec-server → retained/folded
History. The same invocation passed the sampler's race-enabled native suite in 34.046 seconds.
[Runner's race suite and the merged Langfuse/status frontend checks passed](https://app.buildbuddy.io/invocation/0e164925-9d27-4dd9-a2a9-e004b6c39a4b).

[Setup, safe-continuation failures and Gateway-to-History acceptance passed against the Go binary](https://app.buildbuddy.io/invocation/b21d8c03-ebe9-4b2e-bb21-8c0960928970).
That run found two remaining service failures: compaction cleared the active command only in memory,
and a new lease owner could not consume an earlier owner's retained terminal process output.
The fixes persist compaction command settlement atomically and allow the local consumption of
terminal output while retaining fencing for remote process mutations. Focused PostgreSQL
regressions and the full standalone API acceptance [passed](https://app.buildbuddy.io/invocation/3b12753c-62e1-40b7-9983-b34d61ebe74f):
37.710 seconds for the race-enabled native suite and 42.872 seconds for the API suite, including
wait/stdin, queueing, compaction/restart, interrupts, uncertainty recovery and retained-output reads.

After incorporating the current Runner lifecycle refactor from `devel`, executor relay admission
publishes demand under the Runner admission barrier. Its worker observes controller-owned readiness
without blocking the control stream. [Final integration checks passed](https://app.buildbuddy.io/invocation/5057e76f-9cc9-4a62-b471-21709fceeffd):
Runner's race suite in 37.133 seconds, authenticated Gateway-to-History acceptance in 36.143 seconds,
and the actual Go binary's scripted-model k3s scenario in 167.7 seconds.

## Python prototype evidence

The results below describe the earlier Python implementation. They establish the acceptance
scenarios and fixture diagnoses; they do not validate the Go runtime.

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

That Python k3s scenario used deployed Dispatcher, Gateway, Runner, History, PostgreSQL and Kubernetes TokenReview with the actual pinned JS host and exec-server. It assembled the Python sampler objects on the test node. The Go fixture now starts the service binary on that node. An in-cluster sampler Deployment, delegated host cgroups and live provider compatibility still require separate coverage.

### Split prerequisite landing and full CI

The executor companion fixture extraction merged independently in
[PR #12288](https://github.com/agentydragon/ducktape/pull/12288), commit
`08a47c40c4c18b23e4ca822d2826f57c6e868149`, and the central branch incorporates it
from `devel`. The standalone sampler correctness changes also merged independently in
[PR #12292](https://github.com/agentydragon/ducktape/pull/12292), commit
`436e4de36751a96f6f50e7b3641fa38a96032c61`, and are incorporated from `devel`.

[Full central CI](https://github.com/agentydragon/ducktape/actions/runs/40581583291)
passed on its second attempt at `78415dcc`. The first attempt passed 1,556 tests,
including the scripted-model k3s acceptance (158.4 seconds), and timed out only in
the unchanged `//agentplane/system_tests/k3s:test_iptables_rule`. An
[uncached diagnostic run](https://app.buildbuddy.io/invocation/c4cc082f-bee4-40e9-98a8-8bf1a36f645e)
passed that target in 46.4 seconds without changes or a longer timeout; the failed
CI job then passed on rerun. The original timeout log stopped after the three
fake-iptables cases; it does not establish which operation stalled.

### Graceful shutdown and lease ownership

[CI at `c3acaa58`](https://app.buildbuddy.io/invocation/6195e2a1-c114-51d5-b40d-dea24592409a)
timed out waiting for the first provider request after a clean sampler restart.
Shutdown cancelled whole driver tasks, including ownership acquisition and release.
Cancellation after acquisition committed, or while release was awaiting the database,
could strand a live lease and delay the new runtime until expiry.

[Two deterministic PostgreSQL cases](https://app.buildbuddy.io/invocation/95659374-fec7-4338-a236-6ec43bca4f95)
pause those boundaries and both fail against the old runtime: immediate takeover raises
`LostOwnershipError`. Shutdown now signals drivers to cancel execution and finish ownership
transitions. [Both cases, the original restart test, setup and Gateway integration passed](https://app.buildbuddy.io/invocation/8389b40d-3eb8-4bb5-92e8-be5cc207a1ae).
Abrupt process loss still relies on lease expiry and unresolved-effect recovery.

### Output-limit comparison with Codex 0.162.1

The current sampler raises on more than 1 MiB of unread process output and blocks continuation.
A regression forces that threshold; no observed live-session incident establishes its frequency.
Verbose build/test output can exceed it, so this is a reachable behavior limit.

In the inspected upstream 0.162.1 source, `core/src/unified_exec/mod.rs` sets
`UNIFIED_EXEC_OUTPUT_MAX_BYTES` to 1 MiB. `process.rs` applies `HeadTailBuffer` to
both pending output and the completion transcript. `head_tail_buffer.rs` keeps
half the budget for the prefix and half for the suffix, counts omitted middle
bytes, and provides an explicit omission marker. The process continues.
Separately, `exec-server/src/local_process.rs` retains at most 1 MiB or 50,000
chunks per process and evicts the oldest chunks; retained replay is already lossy.

Recommendation: bound/truncate model-visible output with explicit omissions and
track process exit independently. Exceeding an output budget alone should not
make execution outcome unknown. This comparison records a recommendation; the
sampler's blocking behavior has not been changed here.

### Protocol and persistence cleanup

The Go runtime now owns its baseline schema and test-state inspection; it no longer imports the
Python experiment's Store or migrations. Lifecycle transitions are explicit, retained conversation
items are append-only, and protocol evidence writes read only the ownership header rather than
loading the transcript. Host, executor and Responses boundaries use concrete Go types while
retaining opaque provider JSON for continuation and evidence.

The existing `openai-go` dependency was considered for the provider stream. Its public Responses
shapes do not cover Codex's `additional_tools` and remote-v2 compaction items. Its low-level SSE
decoder imposes a roughly 1 MiB scanner line limit. The sampler retains its buffered framing and
small wire adapter; this avoids adding a second model of those private shapes around SDK types.
PostgreSQL JSONB preserves numeric precision but normalizes JSON formatting and number spelling.

The earlier native VM CI failure did not reproduce in the
[focused rerun](https://app.buildbuddy.io/invocation/c3d08a88-a4c0-4f0a-888e-bae4dfa69164):
all three cases passed, including executor OOM exit 137 followed by successful execution with the
same running Runner and Session identities. This validates the tested OOM path; it does not
establish the cause of the original pre-OOM supervision timeout.

[The integrated cleanup run](https://app.buildbuddy.io/invocation/fde16f5b-af0d-46c5-a317-a29a1148bacb)
passed all eight sampler-focused targets: Go/PostgreSQL, standalone service, correctness, setup,
Gateway-to-History, mocked-model Pod k3s acceptance and both Python experiment regressions.
The broader Gateway suite timed out after 300 seconds with an empty test log. A
[verbose rerun](https://app.buildbuddy.io/invocation/ef6304ad-f627-44a1-be4a-45cc4e588d34)
reproduced the hang in the terminal-error sender test: it constructed the sender directly, leaving
the new queues and worker uninitialized. The test now uses the constructor, and cancellation
records the same terminal error for subsequent callers. The
[full Gateway follow-up passed](https://app.buildbuddy.io/invocation/d551c170-9288-4629-b053-3fbe97a6505e),
including the terminal-error cases and executor relay. Commit hooks passed. A repository-wide hook run also found an unrelated missing
Ansible role, `petermosmans.customize-gnome`; no Ansible files changed here.

The [extended Pod/VM acceptance run](https://app.buildbuddy.io/invocation/70aa18d8-1b6c-4824-a6a1-a1a7af42e695)
passed the Pod scenario. The VM scenario failed waiting for its first provider request after
Dispatcher admission. The guest booted and passed the fixture's systemd check, but no model-driven
execution or History assertion was reached. The retained guest console also shows the control
connection dropping during the fixture's Gateway restart.

The [VM rerun with sampler diagnostics](https://app.buildbuddy.io/invocation/a736ad69-e27c-46c9-b150-dbfd3dffb5ba)
reproduced the first-provider timeout. Its retained `central-sampler-stalled-session.json` shows
the Sandbox Ready and connected, but sampler setup interrupted exactly ten seconds after it
started, with no provider or executor boundary records. The sampler stderr reports
`context deadline exceeded`. The guest console places executor reconciliation at 21:11:38.096Z
and successful exec-server readiness at 21:11:48.892Z, after the sampler's 21:11:47.787Z deadline.
The sampler incorrectly applies its ten-second RPC timeout to lazy executor startup, while the
Runner permits twenty seconds. The mock endpoint also starts its ten-second request wait before
setup finishes. Startup and test-readiness changes remain unvalidated; this result does not
validate the VM acceptance path.

The [startup-context follow-up](https://app.buildbuddy.io/invocation/736ab387-4f84-41b7-bdc1-6569d508533a)
passed the Go suite (57.117 seconds), setup acceptance (72.318 seconds), and the Pod k3s scenario
(129.609 seconds). The VM scenario still failed before any provider request. Setup began at
21:28:16.168Z and stopped at 21:28:37.607Z; sampler stderr identifies Gateway executor readiness
as the failing boundary. The guest console records executor process reconciliation at
21:28:16.017Z and the Runner relay's `context deadline exceeded` at 21:28:37.255Z, without a
preceding exec-server readiness message. Teardown follows around 21:28:39Z, so this evidence
does not establish that startup would have completed with a longer timeout. The relay has a
separate twenty-second deadline overlapping the executor controller's readiness budget; that
overlap and retained controller state are under investigation. VM acceptance remains unverified.

The [Gateway-lifetime follow-up](https://app.buildbuddy.io/invocation/76472dd5-4a0a-49d7-9981-0dc71dd9db05)
passed the Runner race suite (39.051 seconds), Pod k3s acceptance (131.026 seconds), and VM k3s
acceptance (320.568 seconds). The relay now waits within the Gateway's readiness lifetime and
cancels a pending readiness wait or connection attempt when the Gateway closes the channel;
executor-controller readiness budgets are unchanged. The VM's retained `central-sampler-k3s.json`
reports PID 1 `systemd`, executor UID 107, a native service under `agentplane-executors.slice`,
successful setup, and `terminal_cursor == folded_through == 163`. The test exercised mocked-model
sampling, real remote execution, and retained/folded History. This validates that VM scenario;
one passing run does not establish the frequency of the earlier startup failure.

## VM setup stdout missing from retained events

In [CI invocation 1f9fca25](https://app.buildbuddy.io/invocation/1f9fca25-3dec-528a-96d0-9fdcff5b2c67),
`test_central_sampler_vm` reached successful setup, read the file created by setup through the
VM executor, and passed PID 1, UID and cgroup checks. Retained `setup_finished` had exit code 0,
but retained setup stdout was empty instead of `setup-on-executor`. The Pod variant passed.
The VM timeline includes the runner disk and both guest images. This run reached execution;
it did not fail importing or starting an image.

The old failure hook captured early setup/provider stalls, so this later assertion left no
nested `process/read` reply evidence. The fixture now writes
`central-sampler-session-diagnostics.json` for this mismatch too, retaining full History events
and their boundary-record request/reply bodies. That evidence must distinguish an empty
exec-server reply from loss after the sampler receives a chunk before choosing a backend fix.
The stdout assertion remains unchanged.

## Identity Pod image after VM input cleanup

[CI invocation 36453d32](https://app.buildbuddy.io/invocation/36453d32-4bd0-5347-9662-41ae84180c2b)
failed before sampler startup: the separate `test-central-sampler` identity Pod had
`ErrImageNeverPull` for `git.allegedly.works/ducktape-ci/runner:unset`. Removing the Pod
runner layout from VM tests exposed this helper's hidden dependency. Earlier passing runs
had the image available and did not establish that the VM test's image inputs were complete.

The helper now uses the running app container's image, already imported for both Pod and VM
stacks, and executes `/bin/sleep` from its Debian base. It keeps its own service account,
waits for Pod readiness, and anchors the same UID-bound tokens; TokenReview coverage is
unchanged. Ordinary VM tests still omit the Pod runner image.
