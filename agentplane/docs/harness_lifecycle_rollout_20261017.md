# Harness lifecycle rollout, 2026-10-17

## Release and deployment

- Core PR #10950 merged as `2893ec865aa16c6be938f08d0314d7353496468f`.
  Its exact-head suite passed 163 tests; desktop and phone visual review passed.
- Runner wheel `agentplane-runner-70b9a3bb287b` was pinned by
  `e7fd6c3830b86463ad1dd3c27f2a42db250d515d`.
- Promotion #10961 merged as `d0391aa5f2b55c44209eb83514b054ff43351aee`.
  With all staging Sandboxes suspended, ordinary service rollouts completed and
  old service Pods disappeared before the retained-session canary started.
  No service scale-to-zero or Flux suspension was used.
- The promotion restored image setters. A subsequent `all-images` reconciliation
  reported `repository up-to-date`; Flux Image Automation owns the pins.

Observed staging revisions after promotion:

| Component              | Image tag                      |
| ---------------------- | ------------------------------ |
| App and Runner Gateway | `devel-20261018010815-2893ec8` |
| History                | `devel-20261018010813-2893ec8` |
| Notifications          | `devel-20261018010838-2893ec8` |
| Sandbox controller     | `devel-20261018010828-2893ec8` |
| Standard runner        | `devel-20261018011710-e7fd6c3` |
| Ducktape runner        | `devel-20261018013530-e7fd6c3` |
| Runner VM              | `devel-20261018011904-e7fd6c3` |

## Deployed acceptance

The launch-preset test passed in 74.6 seconds, including a real model/tool turn,
bootstrap and instructions, durable opening deduplication, suspension until Pod
deletion, and queued opening followed by wake and native activation.
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/5fe70f9e-ccb9-4096-bf80-afe394e147a9).

The suspend/resume suite passed both cases in 378.34 seconds:
Claude and Codex together in a Pod, and Codex in a VM. Each retained its Sandbox
identity and state/workspace PVC UIDs and bound PVs across a new incarnation.
Open created dormant Sessions without processes; input activated assigned native
processes. After resume, dormant Sessions retained their native IDs without
starting processes until input. Resumed model turns recalled their private
markers; the Pod case checked isolation between the two Sessions. Archived
history prefixes remained byte-for-byte unchanged, and journal source sequences
continued after reopening.
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/39fb47e3-f63d-4d4a-ac90-10109fd3fed3).

The live run used the acceptance deadline change from #10962. During execution,
the testing client's return annotation and unused import were corrected; these
were type-only changes. Bazel reported both cases passing but declined to upload
test outputs because an input file changed during execution. The local output
artifacts recorded `passed: true` for both cases.

Earlier attempts exposed a Pod-deletion observation race corrected by #10959,
intermittent controlled-host network timeouts, a Pod taking longer than its grace
period to disappear, and a VM whose scheduling/image pull consumed its startup
budget. The final passing run did not increase those budgets. Tests used a
task-local mount namespace to resolve the existing HTTPS names to a verified
Nebula endpoint, preserving TLS validation and the original Kubernetes identity.
No host networking or resolver configuration changed.

## Retained staging state

The canary used existing Sandbox `public-coder-1`, UID
`4f4df26b-2dc6-4371-9013-10308f27a6c6`, and archived Codex Session
`416958c6-0e13-46d7-a0eb-fa176a890f79`. It resumed the Sandbox, observed the
Session dormant with no process, temporarily unarchived it, and submitted a
fresh change-reasoning-effort command matching its existing `low` setting.
No message or model turn was submitted.

The runner activated a Codex process, resumed the same native Session ID,
reconciled the conversation, and settled the command as `CommandNoop`.
The Session reached `READY`, idle with no active turn. It was re-archived and
the Sandbox suspended until its Pod disappeared. The suffix contained
`JournalReopened`, admission/native handshake, `HarnessStarted(resumed=true)`,
`ConversationReconciled`, `CommandNoop`, and orderly `HarnessExited`.

All 184 preexisting entries through cursor 648 retained their exact canonical
JSON hash, `5bfe4b752f3a51a30f98b8881f3a7d0c40d85dcd3ae22c4c4e546d22c5790212`.
Their final origin remained source
`bf40d06b-2518-4259-a609-de9105372fc6`, sequence 648. The suffix continued
through cursor and source sequence 663. Authored content was not saved to the
investigation artifacts.

After the canary, all four staging Sandbox UIDs and all eight Sandbox state and
workspace PVC UIDs, PV bindings and owner UIDs matched the pre-cutover inventory.
All four Sandboxes were suspended again.

The rollout hold temporarily changed `public-coder-2`'s `wakeOnSend` from `true`
to `false`, fenced to UID `9daaf99f-38a3-4e2a-9fac-93a45d9c3003`.
Its original `true` value was restored after acceptance with UID and old-value
checks. Desired `running: true` and the idle suspension generation were unchanged.
All four Sandboxes remained suspended, with no runner Pods or VM instances. A final
PVC inventory matched every pre-cutover UID, PV binding and owner UID.

## Egress acceptance and permission correction

The bounded egress rerun completed with **four cases passing and two failing** in
788.66 seconds. All three Codex cases passed: bound-policy reachability and
credential substitution, model ingress, and granting a policy to a running
Sandbox. Claude model ingress passed. Both Claude tool scenarios hit the
300-second whole-turn deadline, including the initial unbound request in the
policy-grant case, so that case never reached its grant assertion.
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/bafed821-ef82-4db0-a759-7599bc4b95d4).

The Claude bound-policy Session's native init reported CLI 2.1.296 and
`permissionMode: auto`. It emitted two `permission_denied` frames and two
checking/done pairs before the deadline. This does not establish that the egress
proxy admitted or denied the intended requests: native tool classification
prevented the probe from completing. Setup also logged model-warmup connection
timeouts; these were warnings, not the reported test failures. Fixture cleanup
completed, and a fresh inventory found no testing Sandboxes.

An earlier diagnostic attempt was stopped after the old driver failed to impose
a whole-turn deadline despite continuing SSE traffic. #10962 merged as
`15512183b2a5333a3bb99625362afd4849e416f4`, with its offline deadline/stream-closing
regression and exact-head CI passing. The bounded deployed run now returned both
whole-turn timeout failures without manual interruption.

The operator selected explicit `bypassPermissions` for deployed harnesses in
#10965, merged as `a9505163b20e6c6e322693b0367610043ffa4bf5`.
Pod harnesses run as UID/GID 1000; VM harness children drop to UID/GID 107.
The earlier claim that deployed harnesses needed manual mode because they ran
as root was incorrect. Native scripted scenarios retain manual mode to exercise
host approval/denial responses. `CLAUDE_PERMISSION_MODE_REVIEW` defers revisiting
native approval or classifier policy until explicitly wanted.

The first bypass-mode CI head exposed root execution in the RBE integration
tests: native Claude rejected the mode before handshake. Test execution now uses
UID/GID 1000 with writable non-root workspaces; database-backed native tests use
their VM-local Docker TCP endpoint. The separate cross-UID supervisor coverage
retains its privileged test contract. Exact head
`62a863165f1937894963ce2f08be1bb4a9f2ed2b` passed all 1,463 tests, including the
eleven integration targets changed to run non-root. Devel CI passed as well.
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/05dbf478-9a8f-5ed8-a253-e855d767d984).

The selected mode was released in wheel `agentplane-runner-f58ebdd3e329`,
from merge `a9505163b20e6c6e322693b0367610043ffa4bf5`, SHA256
`f58ebdd3e329a3f734aa636b94161759d26a17ae07fcdfe075333253b0209d18`.
Normal artifact synchronization pinned it in
`810a43155b51984ffcd7aad3daed4ae37d33da19`. All three image build/smoke/publish
jobs succeeded. Flux Image Automation propagated the pins; its final observed
push was `b23d44f899151ae7a32913c3cb53f6457d072eaa`, with `Ready=True` and
`repository up-to-date`. All four testing/staging service/delivery Kustomizations
were Ready. Staging templates contained:

| Runner   | Image tag                      |
| -------- | ------------------------------ |
| Standard | `devel-20261018034344-810a431` |
| Ducktape | `devel-20261018035032-810a431` |
| VM       | `devel-20261018034555-810a431` |

The two previously failing Claude tool cases passed on this standard runner:
**2 passed, 4 deselected in 359.05 seconds**. They exercised bound-policy access,
public Git refs, GitHub bot credential substitution, denial of an unlisted host,
and granting policy to an already running Sandbox.
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/77b93c8d-230c-4419-9e7f-8229c1102d75).
The actual Pod image digest was
`sha256:e86fbb21bd3cfb4f2346b5f564de90e0680e616dc3488aa3f46a70be1e4f292b`.
Its native process reported UID 1000 in `/proc`, and native init reported
`permissionMode: bypassPermissions`, Claude Code 2.1.296. Fixture cleanup left
no testing Sandboxes. The other four egress cases passed on the earlier lifecycle
release; the complete six-case suite was not rerun on this wheel.

**Lifecycle rollout acceptance is complete.** The temporary wake hold is restored,
staging starts may resume, and Flux continues owning image pins. The retained-state
canary and Pod/VM context acceptance above ran on the initial lifecycle release;
the final permission correction received exact-head CI and the two deployed Claude
checks described here.

The operator's staging auto-suspend/send-to-resume UI drive remains separate.
Idle Claude reaping is v2. Historical stop-frame/receipt cleanup remains tracked
by `HARNESS_STOP_HISTORY_CLEANUP`; no executable old lifecycle API is retained.
