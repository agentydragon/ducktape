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
