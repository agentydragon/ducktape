# Agentplane control-surface retirement audit

Audit snapshot: 2026-10-15. This records confirmed callers and dispositions for overlapping
Session-control paths. It is evidence for the unfinished work in the [task DAG](../plans/task_dag.md#12-the-runner-gateway-serves-the-runners-own-service), not permission to remove a server API before its caller rollout gate.

## Confirmed overlaps

| Surface                     | Runtime callers and meaning                                                                                                                                                                                                                                                                                                                                                                                              | Disposition and gate                                                                                                                                                                                                                                                                                                                  |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session opening             | App `POST /sandboxes/{name}/sessions` calls Session Dispatcher `OpenSession` (`app/threads/bridge.py::create_session`). Notification does not open Sessions. Gateway `CreateSession` has no production caller in this repository; remaining references are its implementation, client helper and tests.                                                                                                                  | Keep Dispatcher as the opening authority. Remove Gateway `CreateSession` with `GATEWAY_WRAPPER_RETIRE` after checking pinned callers; remove its request/response and helper in the same proto/client change.                                                                                                                         |
| Commands                    | App and Notification use Dispatcher per-operation RPCs (`app/threads/dispatcher_client.py`, `notification_service/dispatcher_client.py`), which durably queue commands and report delivery state. Dispatcher then calls the matching runner operation. Gateway `SubmitCommand` → runner `DeliverCommand` has no production caller in this repository; remaining references are implementation, test fakes and tests.     | Keep Dispatcher for sender admission and the runner's per-operation methods for execution. Retire Gateway `SubmitCommand` and runner `DeliverCommand` after wrapper callers and pinned images are clear. Keep `command.proto`'s `Command`: Dispatcher rows and `CommandAdmitted` history still use it.                                |
| Live inventory              | The app archive guard (`app/api.py::archive_thread`) and explicit native resume (`app/threads/bridge.py::resume_thread`), Notification membership (`notification_service/service.py::subscribe`), and Dispatcher `has_session` (`session_dispatcher/runner_client.py`) call runner `ListSessions` through Gateway. Gateway validates the current Sandbox owner and UID before listing.                                   | Keep the live read until each caller has an approved replacement preserving its live-state and identity checks. History summaries are retained and eventually folded; they cannot stand in for this authorization or live-state read. Remove the Gateway wrapper independently if its other callers are migrated.                     |
| Session placement           | App `RunnerDirectory.sandbox_of` calls Gateway `BatchGetSessionSandboxes` for command and native-resume routing. The response includes the registry's Sandbox UID, but the app currently returns only the name. Explicit resume later reaches Gateway with the current UID; app commands go to Dispatcher with name/session ID, and Dispatcher records the current UID without checking the Session's prior association. | Do not replace this with History's current Sandbox reference, which contains namespace/name only. Decide the canonical Session-to-Sandbox-incarnation authority and how the command path preserves or verifies it. The name match alone cannot prove that an old Session belongs to the current Sandbox incarnation.                  |
| Find/list APIs              | History `FindSessions` supplies retained Threads to the app (`app/threads/summaries.py`). Gateway `FindSessions` searches the separate Gateway placement registry, including deleted Sandboxes, and has no production caller in this repository; its client helper and remaining calls are tests/fakes.                                                                                                                  | These APIs have different authority and results. Keep History. Gateway `FindSessions` is a removal candidate only after confirming no external or pinned caller; remove its service method, client helper and fixture-only support together.                                                                                          |
| Browser Sandbox session GET | The browser table reads retained History summaries. The old `GET /sandboxes/{name}/sessions` route still reads live runner inventory; the current frontend client has no call, and remaining route references are HTTP tests.                                                                                                                                                                                            | Remove the unused GET route, schema and test surface after the app rollout gate. Keep the POST opening route and `/{session_id}/opening`; keep `RunnerBridge.list_sessions` until archive/native-resume callers are migrated. Generate schema with `bazel build //agentplane/app/frontend:schema`; do not hand-edit generated output. |
| Explicit resume             | The app's user-requested native resume is a Gateway `ResumeSession` call using the runner's retained spec. Dispatcher input delivery may resume a stopped harness, but is not an explicit resume API with the same user-facing checks.                                                                                                                                                                                   | Keep explicit resume as a distinct operation until an owner and contract are selected. Do not infer that Dispatcher command delivery replaces it.                                                                                                                                                                                     |

## Caller-specific notes

`Runner.ListSessions` also has a Dispatcher opening fallback after `OpenSession` returns
`ALREADY_EXISTS`. The runner's `OpenSession` is idempotent for the same spec and setup script; it
returns the attachment on a matching retry and uses `ALREADY_EXISTS` for a conflicting spec or
setup. The current fallback checks only whether the same ID appears in the list, so it can treat a
conflict as a successful opening. `test_delivery.py` models a lost response as `ALREADY_EXISTS`,
although retrying the identical `OpenSession` request returns the existing attachment. Correct that
contract before removing this final Dispatcher caller; do not preserve a list RPC solely to settle
an impossible success case.

Archive safety and Notification membership both rely on current live state and identity. Archive
refuses a live running harness and refuses when a present Pod cannot be checked; Notification first
checks current Sandbox UID and owner, then verifies the Session is in that incarnation. A generic
runner proxy call addressed by Sandbox name does not itself preserve the Gateway wrapper's
destination UID check. Any migration must prove those checks or keep the wrapper; History's retained
namespace/name and last-observed harness state are insufficient.

The app currently drops the Sandbox UID returned by `BatchGetSessionSandboxes`. Its explicit resume
then uses the current Sandbox UID and the Gateway rejects a Session absent from that incarnation;
Dispatcher command submission carries Sandbox name and Session ID and binds the queued command to the
current UID. Whether that command can target a Session from a prior incarnation is an unresolved
identity contract. Review it before replacing the placement lookup or changing the authorization path.

## Reviewed surfaces with distinct semantics

- Dispatcher per-operation methods are sender-facing durable admission and delivery-state reads;
  runner per-operation methods execute the operation and journal its canonical admission. Similar
  method names are an intentional boundary, not duplicate queue APIs.
- Dispatcher `GetSessionOpening` reports the durable opening request; `GetDispatch` reports one
  command. `WatchDispatches`/`ListDispatches` expose commands, not session inventory.
- The inspected timeout settings are per-hop budgets: app and Notification caller deadlines,
  Dispatcher client and runner deadlines, Gateway admission and lifecycle deadlines, and runner
  stream/Quiesce bounds. Their names overlap but they bound different work; no timeout setting was
  identified as safe to delete in this pass.
- App and service Pydantic command-line options expose the same validated settings as environment
  and YAML inputs. No accepted-but-unused service flag was confirmed in this pass. Remove a setting
  only with its reader, deployment value and any pinned image gate together.

The remaining broad audit must still inspect every protobuf field and deployment-only CLI/config
key before `CONTROL_SURFACE_RETIREMENT_AUDIT` is complete. In particular, a field with no current
caller in one component is not dead if generated clients, fixtures, a guest wheel or stored
messages still read it.
