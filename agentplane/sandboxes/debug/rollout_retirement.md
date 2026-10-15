# Sandbox Service retirement and caller rollout inventory

Read-only inventory on 2026-10-14 PDT, after #10524, #10525, #10529 and #10533 merged:

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
  history/revival surfaces remain. Composer activation still follows wake-on-send acceptance.
- #10592 removed the opening flag from generated configuration. Both live app ConfigMaps omitted it
  before #10598 removed the accepted-unused field and its obsolete tests. #10598 merged with Bazel,
  Nix and pre-commit checks passing; the opening rollout flag is fully removed from source.
- #10596 merged staging wake-caller configuration. The subsequent read-only audit found the live
  ConfigMap allows exactly `agentplane-staging/agentplane-app` and
  `agentplane-staging/agentplane-notifications`, with Dispatcher Ready 2/2 on `0a593c0`.
  [Testing wake acceptance](../../session_dispatcher/debug/deployment_acceptance.md#testing-send-triggered-wake)
  preceded activation. Deterministic Dispatcher tests cover deletion precedence and redelivery of
  a message aborted during sealing. The permanent per-Sandbox `wakeOnSend` policy still requires
  explicit user opt-in; #10600 exposes it in the app and remains under review.
- #10588 merged the packaged fresh-workspace creation helper and native-supervisor privilege drop.
  Distinct-user Git tests and full PR CI passed. Fresh VM Session acceptance remains gated on the
  runner wheel and guest image rollout; existing workspace ownership is preserved. A subsequent
  read-only audit verified published wheel `b7f2fca414f9` contains the helper and both live VM
  templates select image `devel-20261015052552-c3949a5`, built with that wheel. This clears the
  image gate for fresh testing acceptance, not the acceptance itself.
