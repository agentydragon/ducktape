# TODO — Agentplane environment

## Prove VM guest boot and console autologin

The VM system suite boots a test guest using shared Runtime units, not the production NixOS guest.
The production image's checks evaluate its mount table and rendered VM resources but do not boot it
or prove Runtime readiness. Add production-guest coverage that waits for Runtime readiness and
verifies the serial console shows a login prompt when `console_autologin` is disabled and a root
shell when it is enabled.

## Reduce Firecracker checkpoint and restore disk I/O

The production-guest acceptance captured RAM in seconds but spent minutes copying and hashing
the backing disks, including the 8.5 GiB root image. Sparse copies already preserve holes; bulk
content hashes have since been removed from local checkpoints. Restore now copies every backing
disk from the committed generation, without hashing or reusing live contents. The
[storage probe](../debug/2026_11_04_firecracker_storage_probe.md) measured the worker's I/O and
built and booted a variant without the pre-commit bundle, reducing closure bytes by 67.2%, with
reduced tool availability. The separate Bazel-built CI guest now passes RAM-resume acceptance
with a 733 MB root disk; production remains unchanged. Measure remaining populated-disk copy/sync
latency, including the writes from unconditional restore. Establish required tools before selecting a smaller production guest;
consider keeping heavy tools in an immutable disk. In every case,
preserve the paused disk/RAM boundary, durable manifest commit and identity fencing. The
[system-test scenario](../system_tests/scenarios/firecracker.py) exercises the current CI guest. The
[production Nix guest probe](../debug/2026_11_04_firecracker_storage_probe.md) records its separate
image measurements and reduced-tool experiment.

## Explore keeping control services outside Firecracker guests

Consider keeping the runner/Environment Runtime, Codex exec-server and other control services
outside Firecracker, with the guest limited to workload execution and its filesystem environment.
Map process ownership and choose a minimal guest agent and gRPC/vsock boundary; determine which
parts of Codex exec-server can move given its current ownership of native sessions and processes.
Preserve RAM-resumable guest processes, host/guest state coordination, identity fencing and no
replay, with explicit session parking, reattachment and terminal cleanup.
Probe thaw without reattachment for longer than Codex's 30-second detached-session grace;
define parking and durable terminal release before promising indefinite process retention.

## Simplify Environment Runtime reconciliation and coordination

- Map lifecycle transitions and mutation ownership across Sandbox Controller, Gateway, launcher,
  Runtime and native executor: admission, checkpoint/thaw, connection replacement and terminal
  cleanup. Include `PrepareSuspend`/`ResumeAdmission` generations, lost replies, controller restart,
  concurrent wake and deletion. Then simplify one coordination path under the race tests,
  preserving identity fencing, cleanup ownership and no replay. Use this map to evaluate the
  control-services-outside-Firecracker proposal above.
- Systemd observations are typed; process start, exit and stop still use a revision wakeup and
  process-owned result slots. A start-fact prototype retained the live worker slot and required
  additional reads to reconcile COSI observations with completion and teardown. Revisit when a
  typed fact can replace coordination or serve another consumer. Preserve completion ordering,
  teardown finalizers, fencing and no replay.
- Measure Session reconcile fan-out and contention before optimizing: Environment lifecycle updates map to
  every Session, app-server updates map to every Codex Session, and snapshot publication uses a
  manager-wide mutex. If significant, narrow subscriptions or locking without losing the
  pre-fence settlement status and wakeups needed by the drain barrier.
- Profile aggregate shutdown scans before adding cached inventory or generation-scoped scan state.
  The [shutdown scan note](runtime/debug/shutdown-scan.md) counts per-Session and process reads but
  records no profile. Measure a real shutdown with many retained Sessions and process attempts; use
  a Go CPU profile to separate inventory listing, sorting and COSI reads, and record resource counts
  and blocker transitions. Preserve the full membership and recovery proof if optimizing.

## Simplify ownership and process fencing with COSI lifecycle primitives

Runtime holds the COSI backing database's process lock through cleanup. Pod PID1 descendants rely
on the private PID namespace and its outer runtime fence. Execution uses Runtime ownership and the
COSI Execution controller's serial, joined native fence; its independently supervised Pod companion
retains the generation lease and namespace fence.

Use COSI's [exclusive controller outputs, strong inputs and finalizers](https://github.com/cosi-project/runtime/blob/v1.16.2/README.md#controllers)
and teardown phases to express ownership and cleanup barriers where they can replace private
coordination. The [Process controller](runtime/supervision/controller.go) already holds a finalizer
through its native fence; build on that model and consider whether persisted lifecycle resources or
another COSI backend can improve recovery across Runtime death.
COSI controller ownership and finalizers describe reconciliation, but the current engine and its
in-memory state cannot themselves stop a stale external process after the engine dies. Identify
what enforces each external effect and how a successor proves cleanup before accepting work.

Also evaluate replacing the invocation-specific systemd lifetime marker with a simpler
systemd-native or COSI-coordinated fence. Preserve the `ConditionPathExists` guarantee that a
queued launch from a dead invocation cannot execute after its replacement's inventory scan.
Validate crash and restart, overlapping owners, late starts, uncertain fence outcomes and detached
descendants for each affected backend. Remove redundant mechanisms only after their guarantees have
an identified replacement; do not assume the existing set of locks and markers is minimal.

## Reconsider COSI persistence for the Session journal

Keep the SQLite journal as the authority for now, and revisit COSI if a disk-backed implementation
or a different resource model makes it a good fit. The upstream
[`state-sqlite`](https://github.com/cosi-project/state-sqlite) backend reads resources from SQLite
without requiring every resource to remain in COSI's in-memory state; controller caches and large
lists can still retain or load many resources. Compare transaction and replay semantics,
indexed and paged reads, startup and memory use, and write throughput at realistic journal sizes.
Any replacement must retain atomic command admission, dispatch and event/checkpoint updates,
commit-before-publication, ordered replay and reconciliation of uncertain native effects. A COSI
watch is a change notification, not by itself a durable journal. Avoid duplicating journal authority
in COSI merely to expose observations controllers already need.

## Reconsider the Session state model

Runtime owns Session management, retained Session records and History journal delivery.
`SessionRecords` are already protobuf-backed COSI resources, persisted in bbolt and held in COSI's
in-memory state. Session journals remain separate SQLite databases. Evaluate whether a different
disk-backed state implementation improves memory and indexed access at realistic Session counts;
controller caches and list operations may still load additional resources. Define ownership and
transaction boundaries with journals and command outcomes before changing storage, including
recovery of uncertain native effects.

The broader [Session/runtime model decision](../plans/task_dag.md#session_runtime_model_design--sessions-harness-placement-and-execution-attachments)
also covers Sessions independent of environments and harnesses inside them. Use `CreateSession` as a
concrete example: separate reservation, placement, native process startup and observable readiness
or failure without assuming all harnesses support the same placement.

## Use COSI primitives where they fit the lifecycle

COSI runtime is pinned in `third_party/go.mod`; evaluate its APIs against the actual ownership and
teardown contracts rather than treating every controller as a simple resource transform.

- `env/cosi.Write` also serves Bootstrap, Session, Codex app-server and Execution status writes,
  where `changed` wakes Runtime or app-server waiters. The Session aggregate's `WriteStatus`
  delegates to `cosi.Write` and queues another reconcile on change because it does
  not watch its own output. A pre-read is not an atomic change detector: a competing writer
  could change the resource between that read and Modify. These controllers declare exclusive
  outputs and serialize the relevant key; no competing production writer was found. Any replacement
  must handle creation and conflict retries and preserve those notifications without a wakeup loop.
  A pre-read that returns early also bypasses COSI output tracking; no environment controller
  currently enables `StartTrackingOutputs`.
- `cosi.Write` uses Go `==`, so its comparable constraint does not give protobuf values structural
  equality. Pointer-backed COSI protobuf specs need their own equality-aware path; no current
  `cosi.Write` caller uses those persisted protobuf specs.
- Keep the retryable _mutations_ and their outer barriers: Session input publication increments
  demand inside `StateModify` while `publishMu` serializes snapshots; Runtime admission accounting
  updates a delta under the admission lock; retained Session field updates patch an existing record
  with `StateUpdateWithConflicts`. Execution's `inputMu` also orders demand and completion against
  native handle registration; its completion callback has side effects and cannot simply move
  inside a retryable updater. COSI retries update conflicts, but its absent-resource Modify path can
  still return an AlreadyExists error if a concurrent creator wins.
- COSI's generic mappers are not drop-in replacements here. Session operation routing reads the
  operation to ignore its destruction; labels remain present on COSI's destroyed event, so a
  label-only mapper adds a wakeup after consumption. The Process host accepts a namespace at runtime,
  whereas `MapperSameID` derives it from the resource definition, which currently has no default.
- The current lifecycle controllers are not simple `qtransform` candidates. Its transform runs
  inside a retryable `Modify`, potentially before the output is committed or more than once; these
  controllers publish status before effects and notify waiters after writes. Its input finalizer and
  output teardown order also differ from the Process controller's retained `Exited` status and fence
  barrier. Revisit only if a proposed design preserves those contracts; do not move one-shot effects
  into a retryable transform.

## Prove central execution across Pod and VM lifetimes

The central sampler already reaches Runtime's embedded Executor through the Gateway. The Gateway
integration test exercises that route with PID 1 supervision, and the Pod system-test scenario
covers suspend and resume. Extend central execution coverage to the production PID1 Pod and VM
systemd paths, including ownership loss, executor replacement and descendant cleanup; keep the
Sandbox incarnation and uncertain-effect boundaries in those checks.

## Investigate VM executor health loss during tool OOM

The [VM OOM failure record](runtime/debug/2026_11_02_vm_executor_replacement_ci.md) shows a tool
OOM followed by an exec-server health deadline, native fencing and executor replacement; the reason
the local WebSocket handshake stalled is not established. On recurrence, capture per-probe timing
and exec-server process and socket evidence before the fence, then distinguish OOM effects from
guest scheduling, systemd contention and other causes. Do not replay uncertain work or change the
health deadline solely from the observed replacement.

## Protect resident harnesses from tool resource exhaustion

Claude's direct Bash children share the harness's Pod container or VM service cgroup. Codex commands
have a separate boundary from the app-server, but share the exec-server's budget and failure domain.
Evaluate a feasible tool-resource boundary for each deployment, then test that a tool OOM leaves the
resident harness and its journal usable. Any executor restart must report lost work and must not replay
uncertain commands. See [current isolation and options](runtime/execution/ISOLATION.md).

## Exercise the shipped harness binaries

The published [`environment-tools` image](images/image.nix) uses nixpkgs Claude and Codex, while
environment e2e tests use separately pinned native binaries. The [image smoke check](images/ducktape_image_smoke.sh)
verifies versions but does not run a turn through the shipped harnesses. Add a deterministic turn
through each image binary at the Runtime boundary, or establish an equivalent release gate that
tests the exact shipped versions.

## Decide whether Agentplane should own a base prompt

Runners preserve the harness's default base prompt and add platform and Session instructions.
Consider an explicit Agentplane base prompt if a production workflow needs one; evaluate it across
the supported harnesses and model routes before replacing their defaults.

## Verify Claude interrupted-turn reconciliation across transcript flush timing

The [native interrupted-turn test](../native/claude/test_turns.py) waits for the interrupt marker to
persist; it does not establish the no-flush case or the Runtime's reconciliation result. Compare
those boundaries against `runtime/claude/history.go` and `adapter.go` with real-binary tests;
retain unknown results when the transcript does not prove what survived.

## Reconcile conversations that were compacted

Codex history RPCs report `UNKNOWN` when a compaction item appears in the recovery window;
Claude's transcript reader does the same for `compact_boundary`. The [manual-compaction probe](../native/codex/test_history_projection.py)
shows that retained history items need not remain in the model context. Keep the `UNKNOWN` barrier
until real-binary evidence covers automatic compaction, interruption or process loss during
compaction, compaction outside the observed recovery window, and external rollback.

## Codex reconcile edge cases

- Tools other than `commandExecution` remain unknown until their API history can be compared with
  actual model continuation in real-binary tests.
- Failed or incomplete command executions remain unknown: their API display output does not establish the
  synthetic interruption output sent to the next model request.
- Reasoning recovery compares visible summaries, not the encrypted reasoning payload.

## Claude reconcile edge cases

- A non-streaming retry (a whole message in one assistant frame) followed by an interrupt has no
  runner-level test. The rule counts a frame holding thinking and text as answered.
- A native line that precedes its turn's `TurnStarted` is outside the turn's journal range and is
  not seen. That matters only for a turn whose first frame is a complete assistant frame.
- The interrupt reconcile reads the turn's native lines a second time. Its cost on a large turn is
  unmeasured.
- A `redacted_thinking` block ahead of an unanswered tool call is dropped from the resumed context.
  That was seen with the real binary but no committed test pins it.
- A tool call interrupted before its result is reported unknown on an ordinary interrupt, because
  Claude does not say whether it keeps it.

## Decide what to do with Claude Code's synthetic messages

After an interrupted or killed turn, Claude Code adds messages the operator never typed to the next
model request: `Continue from where you left off.` and `No response requested.` after a kill,
`[Request interrupted by user]` and `No response requested.` after a graceful interrupt. Whether the
thread view shows them is unchecked, and the project has not decided whether it should: surface them
as lifecycle rows, hide them, or treat them as part of the model's context.
