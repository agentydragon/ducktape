# Agentplane CI timeouts on BuildBuddy

Observed 2026-10-15 UTC. This investigation distinguishes test execution from RBE
startup and input-fetch phases for the reported Agentplane failures. It does not
establish that every timeout has one cause.

## Evidence

The strongest common signal is delay or absence of the Firecracker
`workspacefs` drive attach on timed-out TestRunner actions. A focused inventory
timeout never emitted a pytest log, and its VM log contains no
`PATCH /drives/workspacefs`. A completed, instrumented action for the same
target/configuration attached that drive 4.26 seconds after VM resume and ran
all 20 collected tests in 0.27 seconds. Its one failure was a separate fixture
`KeyError`. Another test VM logged a 41.9-second gap between resume and the
workspacefs attach. This is strong evidence of workspace attachment stalls on
some workers, not proof that all test timeouts share that cause.

The root cause limits are material: BuildBuddy target history shows nearby
passes on other commits, but does not expose an exact source-plus-action-key
comparison for these failures. The branch/commit in an invocation identifies
the `bb remote` base, not necessarily the submitted worktree tree; `bb remote`
then applies a local patchset. The `ae8d…` inventory invocation's outer runner
log confirms checkout at `c50a2805`, then application of patch
`61a3ce3b5578bd7032eb1a6b438b842f5744db2f80be61d79ad17d269db3a16c`, including
`agentplane/sandboxes/test_inventory_mutations.py`.

### Direct VM and phase observations

- **Focused inventory timeout after the fixture correction.** Outer runner
  invocation `17ed1b73-35d3-4149-93f6-eda2ef8cc760`; BuildBuddy invocation
  `ae8d2a18-3d1f-406b-a451-ba54cb027709`; target
  `//agentplane/sandboxes:test_inventory_mutations`. RBE execution
  `/uploads/3b169135-4f98-405f-8ef0-e5062ba20dd7/blobs/60b76116367317785c96f087ce99b7194b97520eaf9c29d475605b91f1ce08a2/498`
  ran on worker `fc339064-2543-436c-8340-d13265712c62`, host
  `sjc-prod-r2204m06vm3gtf`, VM `b331079f-b9d2-4efc-b826-0d388395bca3`.
  Queue-to-worker-start was 64 ms; input fetch took 6.23 s; execution then
  ended at its 60 s deadline. The test log is empty. VM log artifact
  `04d4532b8610d2dadaddcc7da7f2f212ec5a840c3fc8ee9e08fa68dac6ceb39b/4674`
  shows snapshot resume at `01:19:25.119Z`, but no workspacefs drive patch
  before the deadline; its last guest message reports the workspace block
  shrinking to zero. The reported 64.8 s `fileDownloadDurationUsec` is an
  accumulated I/O metric, not the 6.23 s wall-clock input-fetch phase.

- **Instrumented completed inventory action, for comparison.** Invocation
  `e49f3722-c021-4bec-af9f-ca32144e2dcc`; action
  `/uploads/086504d2-db49-4d53-957e-516bf1174de8/blobs/2be9fbea8200cf88bc9672482db195856913775fa875582b94a83afefe311279/500`;
  worker `4ba31ed4-54e0-457b-b4da-b6aacf8267fc`. It used
  `PYTHONPROFILEIMPORTTIME=1`, `-s`, `-vv`, and
  `-o faulthandler_timeout=30`. Input fetch took 0.28 s and execution 11.44 s;
  pytest collected 20 items and finished their bodies in 0.27 s (19 passed,
  one fixture `KeyError` at `test_inventory_mutations.py:431`). The VM log
  `61b90b8b9adb31a542d68867dfe429b79fd4776ddb197925b5f721a2fb545bba/7996`
  records resume at `01:18:16.475Z` and workspacefs patch at `01:18:20.734Z`.
  Import profiling changes execution cost, so its duration is only useful as a
  phase comparison. This run predates the fixture initializer correction and
  is not post-fix test validation.

- **Earlier egress timeouts in #10562.** For
  `//agentplane/egress:test_platform_routes`, execution
  `/uploads/2689eeef-1be6-4830-822c-c5491074d4c4/blobs/9a8e9d882fcdddff2f7274b4c046b7c1868de53f0571b384b0bfa4abc83a8d53/498`
  ran on worker `973e8650-fab1-4c46-a685-8903eea9eb40`. Input fetch took
  117.4 s, followed by 64.9 s of test execution before timeout. Its VM log
  `94f4f83bc532c761252c01456765ceb7226fe592473abad8c60eb1b90c3ce0fd/4659`
  records VM resume but no workspacefs patch. For
  `//agentplane/egress:test_main`, execution
  `/uploads/2bd41ca7-1283-4504-b4c1-89fcedc94f16/blobs/434d7044253327c26455d0b4c358610059024f450cee632db2097f9b9e270faa/498`
  ran on worker `1276c228-b068-430f-adfd-54c5add993f4`: input fetch was
  0.28 s, execution lasted 118.2 s, and VM log
  `19e6a89c829f4f0a03a24d6af3afb1aff3dd11c196bf095d2506cb0f3c257e7e/4642`
  has no workspacefs patch. The long fetch in the first action is a distinct
  delay; it does not explain the later 64.9 s execution deadline.

- **Delayed attach in #10561.** `//agentplane/runner:test_control` execution
  `/uploads/bf704ce4-141d-483a-b258-4e3ccc53f2c4/blobs/82f63d3023ccc55fc7c0a661bdd580822e95462bce48e945b5a303c5829fde87/498`
  ran on worker `eb9f4115-a7ae-4fb8-895c-d3c5c1266d86`. VM log
  `d0fbd205c33c3117a7ac80df6be08cd0831bd6e6b1bd09891747b724f95ea161/8172`
  records resume at `00:53:46.475Z` and workspacefs patch at
  `00:54:28.363Z`, a 41.9 s gap. The execution timed out after about 62 s.
  Pytest logged `collected 24 items` and 19 dots but emitted no Python stack
  trace. This run shows a real delayed attach; its test log cannot identify
  whether a remaining test also stopped making progress.

Across #10562 (`0d362ba1-cfcc-58ff-8abe-f5e5e8c486a4`), #10560
(`fd8486fc-37b6-52f3-b9d8-94169dd76c0e`) and #10561
(`90bcea59-bd9f-5316-8b2e-428332c846c9`), the execution metadata contains 19,
7 and 7 RBE code-14 failures respectively; each includes TestRunner actions,
and the rest span lint, mypy, TypeScript, and other action types. Representative
messages say Firecracker did not create its API socket before a context
deadline or that a VM health check hit `DeadlineExceeded`. These are executor
startup failures across action kinds, independent of pytest, but do not prove
the cause of every in-execution timeout.

## Other failures in #10561

These were failures rather than Bazel timeouts and should be tracked
separately:

- `//agentplane/notification_service:test_acceptance` had 3 failures and 5
  passes. Two Claude parameter cases hit the test helper's 5 s
  `channel.channel_ready()` timeout while starting the local Dispatcher; a
  third got `UNAVAILABLE: destination unavailable; no offline admission` on
  Runner Gateway `CreateSession`, with logs saying the Runner Control stream
  ended. The Postgres fixture reached ready state. The test log does not prove
  a bad fixture or distinguish delayed Dispatcher startup from a stuck
  startup dependency; the 5 s gate at
  `notification_service/test_acceptance.py:104-105` is a concrete point to
  instrument if this recurs.
- `//agentplane/runner:test_attach` had one failure among 22 cases: the Claude
  post-shutdown reattach returned `UNAVAILABLE: the tunnel ended before the
callee answered` after the Runner Control stream ended. This is a separate
  Runner/tunnel lifecycle failure candidate.
- `//agentplane/app:test_main` passed 8 tests and then failed while Testcontainers
  created Ryuk: Docker's `POST /containers/create` hit a 60 s read timeout.
  This points to the test worker's Docker path, not an assertion regression.

The #10562 run also had an unrelated mypy error: `SandboxPhase.UNSPECIFIED` is
not defined in `agentplane/app/test_sandbox_models.py` at lines 38 and 49.

## Limits and next diagnostic

Several timeout logs contain only the wrapper and pytest command; the browser
shards show a test filename but no case result. No timeout log inspected here
contains a faulthandler stack. Target history has adjacent passing runs (for
example, notification acceptance at 100.727 s on different commits), but does
not identify a matching source tree and action key; `--failures-only` also
returned no entries for these invocations. Do not infer that every timeout is
an executor stall or a test deadlock from those histories.

For the next naturally occurring timeout, retain the exact TestRunner action
metadata, VM log tail, and test log together. If the workspacefs patch and
mount complete promptly, add bounded phase logging or a 30 s faulthandler
dump around the test's waiting gates. Do not change timeout or size based on
these observations. See [#10405](https://github.com/agentydragon/ducktape/issues/10405)
for the existing BuildBuddy slow-host and workspace-attach investigation.
