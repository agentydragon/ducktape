# Sandbox Service retirement and caller rollout inventory

## Quiesce retry coverage audit, 2026-10-15 UTC

The retry implementation (#10580) and nonblocking writer (#10609) close the retry-code audit.
Writer rollout and legacy-reader removal remain separate gates in the task DAG; idle suspension
still waits for writer rollout verification. This audit made no cluster changes.

- The controller persists the attempt before calling the runner and enforces the per-incarnation
  retry deadline. `TestFailedQuiesceWaitsAcrossNoStageEventsAndControllerRestart` covers watch
  events, restart, repeated suspend/resume intent, a stalled flush and prompt Lease-driven completion.
  `TestFailedQuiesceAttemptStatusWriteDoesNotCallRunner` and
  `TestQuiesceAttemptDoesNotThrottleANewRunnerIncarnationDuringDeletion` cover conflict and identity
  boundaries in [controller tests](../../sandbox_controller/controller_test.go).
- [Runner control tests](../../runner/test_control.py) hold ingress acknowledgement to prove that
  sealing alone stays `Flushing`, acknowledge final cursors before `Quiesced`, and cover drain failure,
  repeated requests and cancellation/reconnect. #10609's final CI passed all 92 test targets.
- [Ingress tests](../../runner/test_ingress.py) cover refused-session backoff without blocking other
  Sessions, replay from the acknowledged cursor, persisted seal acknowledgement across restart,
  and an unreachable ingress that does not block journaling or sealing. The shared
  [reconnect loop](../../runner/reconnect.py) applies capped jittered exponential backoff to failures
  without progress; operator-only refusals wait the maximum. This is per-stream backoff, not a
  global ingress circuit breaker.

No additional global circuit breaker or live outage drill is required to establish this contract:
the named failure boundaries have deterministic coverage. A future aggregate retry-load problem
would justify separate measured work. This closes the old open-ended outage-audit gate, without
waiving writer image/VM-wheel or live-incarnation compatibility checks.

## Initial retirement inventory, 2026-10-14 PDT

Read-only inventory after #10524, #10525, #10529 and #10533 merged:

- Testing and staging have no Sandbox Service Deployment or Pods. #10529 removed deployment
  wiring; #10524 removed its source, proto and remaining clients. History Service's independently
  owned `sandbox_service` database is retained; no database deletion is part of retirement.
- All five SandboxTemplates declare `stateVolume.layout.type: DirectoryTree`: testing `runner`
  and `runner-vm`; staging `runner`, `runner-ducktape` and `runner-vm`. Both environments run
  controller source `31e0b37`, which accepts the explicit declaration and guards retained PVC
  identity/layout. No Sandbox CRs existed at this inventory. Refresh before any data disposition.
- The actual Ready app Pods (one testing, two staging) run `devel-20261014232043-102101f`, containing
  #10517's app-owned Session defaults and queued-opening implementation. Testing's app ConfigMap
  sets `session_dispatcher_open_sessions: true`; staging activation remains pending.
- Actual Ready notification Pods run source `142ea35` in both environments, containing #10506's
  strict Dispatcher-only command sender. The old notification direct-send caller is gone.
- Actual Ready Dispatcher Pods run `31e0b37` (one testing, two staging). Testing's wake caller
  allowlist contains the app and notifications accounts. Configuration readiness does not establish
  wake-on-send or queued-opening acceptance: those bounded checks and staging activation remain.

These observations complete implementation/rollout nodes only for the paths named above. They do
not complete VM fresh-format/remount acceptance, authorize deleting retained data, or remove the
app's direct native-resume path. Each remaining compatibility removal keeps its caller/image gate.

## Subsequent cleanup and rollout evidence, 2026-10-15 UTC

- #10582 removed the obsolete per-Sandbox defaults and Gateway override mask after the caller/data
  inventory. Its corrected notification fixture passed all eight cases and full PR CI. No data was deleted.
- #10589 requires successful bootstrap before Ready; #10590 rejects Welcome without an incarnation
  UID. The audit covered both selectable template wheels (`0a0e1373f9c2`) and the older running VM
  wheel (`a13d9a7d58ff`): both report bootstrap facts and understand incarnation UIDs. The deployed
  Gateway `721cab8` sends the UID. These are merged removals; historical seal-schema cleanup retains
  its separate writer-rollout and retained-journal disposition gates.
- Both audited wheels still omit `QuiesceResponse.stage`; current source still awaits sealing before
  returning. Non-blocking response implementation and its rollout must precede legacy-reader removal.
- #10593 adds explicit suspended markers to the sidebar, Thread, title and favicon. The retained
  history/revival surfaces remain. At that point, composer activation still followed wake-on-send
  acceptance.
- #10592 removed the opening flag from generated configuration. Both live app ConfigMaps omitted it
  before #10598 removed the accepted-unused field and its obsolete tests. #10598 merged with Bazel,
  Nix and pre-commit checks passing; the opening rollout flag is fully removed from source.
- #10596 merged staging wake-caller configuration. The subsequent read-only audit found the live
  ConfigMap allows exactly `agentplane-staging/agentplane-app` and
  `agentplane-staging/agentplane-notifications`, with Dispatcher Ready 2/2 on `0a593c0`.
  [Testing wake acceptance](../../session_dispatcher/debug/deployment_acceptance.md#testing-send-triggered-wake)
  preceded activation. Deterministic Dispatcher tests cover deletion precedence and redelivery of
  a message aborted during sealing. The permanent per-Sandbox `wakeOnSend` policy still requires
  explicit user opt-in; #10600 merged the app checkbox with passing tests and reviewed visuals.
- #10588 merged the packaged fresh-workspace creation helper and native-supervisor privilege drop.
  Distinct-user Git tests and full PR CI passed; existing workspace ownership is preserved. A subsequent
  read-only audit verified published wheel `b7f2fca414f9` contains the helper and both live VM
  templates select image `devel-20261015052552-c3949a5`, built with that wheel.
  The merged #10604 [fresh-workspace acceptance](../../acceptance/README.md#vm-workspace-ownership)
  passed on testing in [invocation aef53a32-d86e-4a86-ba26-40ae9dc97708](https://app.buildbuddy.io/invocation/aef53a32-d86e-4a86-ba26-40ae9dc97708).
  It proved the target Session directory was absent before open, then both it and `.git` were owned
  by `107:107`, with Git init/rev-parse succeeding and no `safe.directory` override. The fixture
  Sandbox UID `00a94aaf-8312-4e5c-a9a7-1381d45a451b` and its PVC were removed by scoped teardown.
  This was a model-free setup check on testing, not a new staging model-turn acceptance.

## App wake policy and suspended composer rollout, 2026-10-15 UTC

- #10600 (`e380e62`) adds the default-off `wakeOnSend` checkbox to new and existing Sandbox UI,
  retaining UID/resource-version checks and requiring an explicit user action to change a policy.
  The per-Sandbox value is permanent user policy, not a rollout shim or cleanup target.
- A read-only deployment audit found testing generation/observed generation 520 and staging
  generation/observed generation 565 running app image
  `devel-20261015060159-7eb6b79`. All three Ready app Pods use digest
  `ce44cd071dbc33e12000ed10a40f84c687a2135e4ce13f475ff62c80df47d366`; source ancestry includes
  #10600. This clears the app-image gate for the checkbox.
- #10608 merged as `cd4b4352623f47ffa9a420bef035aabca7e04b08`. It keeps a retained Session's
  composer usable after a normal stopped/ended seal when fresh Sandbox inventory confirms suspension
  and setup succeeded. The existing authenticated Dispatcher route queues input; wake-on-message
  requests a wake, while the default-off policy leaves the message queued and tells the user to resume
  manually. Stale or missing inventory, archived Threads and failed/interrupted/unfinished setup
  remain blocked. Focused UI tests cover the sealed projection, both wake policies and refusals; app
  bridge tests cover Dispatcher queueing without a ready runner or Gateway session lookup. PR CI and
  desktop/mobile visual review passed.
- A read-only rollout audit found testing at generation/observed generation 521/521 and staging at
  566/566. Testing's one app Pod and staging's two app Pods are Ready on
  `devel-20261015063016-cd4b435`, all with digest
  `sha256:689df9e143e769475992a64e6a02cf6836513636cdeb7b3a7747615cf79c8d31`. The image tag identifies
  the merged #10608 commit.
- The prior testing [Dispatcher wake acceptance](../../session_dispatcher/debug/deployment_acceptance.md#testing-send-triggered-wake)
  passed the opted-in scenario: concurrent allowed app `OpenSession` and notification `SubmitInput`
  produced one wake generation and both reached runner admission. Together with the deterministic
  composer and app-bridge tests, this covers the changed contract; no separate live browser send is
  required. No message was sent to a user Session for this rollout audit.
