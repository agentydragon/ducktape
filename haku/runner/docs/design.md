# Bridge decision record

Constraints and rejected alternatives behind the bridge's shape: how it survives a console roll,
and where the harness seam sits. Check here before redesigning recovery, replay, bridge
versioning, or the harness seam. The current frame-log shape is
<../../console/docs/harness_frame_log_v3.md>; the live protocol invariants are docstrings in
<../protocol.py>, <../neutral_operations.py> and <../operation_journal.py>.

## Re-adoption: keep the CLI alive (design B), not `--resume` (design A)

What a dying console replica kills is the CLI process, not the sandbox: the sandbox is a separate
SandboxClaim on its own pod, and the runner dials out, so a reconnect lands on whichever replica
the Service picks.

**Built: design B.** The runner keeps the CLI alive across the dropped socket, buffers, and
redials; an adopting console picks the conversation up mid-flight, preserving the in-flight turn.
The lease (`expire_stale_leases`, <../../console/session/store.py>) is what makes a dropped
session **observable** — another replica notices the holder stopped renewing — and, because an
expired lease means unowned rather than dead, **adoptable**: the returning runner takes it over,
and the sweep fails the session only when nobody does.

**Rejected: design A** — let the CLI die and start a fresh one in the surviving sandbox with
`--resume` (session state is on disk there under `CLAUDE_CONFIG_DIR`). No runner protocol change,
but it loses the in-flight turn. Still a candidate fallback tier for when adoption fails. Either
way, resume beats re-awakening (reconstructing context from a transcript tail): it restores the
actual context rather than an approximation of it.

## The replay hazard is side effects, not inbound traffic

This deployment's control channel is effectively outbound-only: sessions launch with
`bypassPermissions` and no `setting_sources`, register no hooks and no `can_use_tool`, and reach
MCP as an external HTTP server the CLI contacts itself. Where inbound control traffic does exist,
the runner can buffer `control_request`s nobody answered and re-deliver them on adopt — the
adopting console answers late and the CLI never knows. That is safe exactly when answering twice
is harmless, which covers a read-only surface completely.

It does **not** cover `can_use_tool` or hooks: those gate an action, a replayed approval is a
second authorization, and a synthesized denial silently changes what the turn did. **Adding a
permission callback or a hook makes re-adoption qualitatively harder — whoever does should come
back to this note.**

## Rejected: replay by payload identity

Bridge v3 replaced it with positional identity: the runner numbers every frame it sends
(`runner_seq`, dense and monotonic per session), the console records the exact opaque JSON and
deduplicates `(session_id, runner_seq)`, and `Checkpoint.HOLD` (<../../console/x/runtime.py>)
keeps the durable projection cursor before provider-private partial state until replay can
reproduce it. The constraints that killed identifying frames by their payloads:

- **A cursor cannot be the correctness argument.** The console can die between recording a frame
  and acknowledging it, so delivery is at-least-once no matter how exact the cursor — something
  downstream must tolerate a duplicate regardless.
- **Deltas have no payload identity.** Two identical `stream_event` deltas are legitimately
  distinct, so payload-keyed dedupe double-appends on replay, forcing a special never-replayed
  delta class. A runner-owned position numbers deltas like everything else; v3 has no delta
  exception.

## Rejected: exact-match protocol version + `extra="forbid"`

Right while both bridge ends lived and died together; adoption breaks it. A runner's image is
fixed when its claim is created and its process outlives many console releases, while the console
rolls several times a day — so an exact-match version with no range kills every live session on
the first release after it ships, and `extra="forbid"` turns every additive field into a
fleet-wide breaking change.

What replaced it is built, and its reasoning lives at the code site (<../protocol.py>): the
runner speaks first with a frozen `hello` carrying the versions it can speak, the console picks
the highest in common, an unknown _field_ in a known kind is ignored (an additive change is a
no-op for older peers), and an unknown _kind_ fails the union parse — so anything a peer must
understand to stay correct arrives as a new kind, fail-closed. A cost falls due if
`SUPPORTED_VERSIONS` ever widens past one element: contract tests at both ends of the range, or
"we support N" quietly becomes a claim nobody checks.

## The runner seam: `main` lifecycle, shared library, per-harness `run()`

The seam over-abstracted on two axes at once. **Transport** — subprocess plus newline-delimited
JSON stdio — was hardcoded in the runner's stdout pump and process launch, so a harness that spoke
anything else had nowhere to say so. **Interaction shape** — the six `HarnessDriver` hooks
(`initialize`, `compose_prompt`, `compose_interrupt`, `answer_control_request`, `observe`,
`admit`) — pins one call vocabulary every harness must fit, even where its native protocol brackets
a turn or streams a result differently.

**Target.** The runner `main` owns the harness-invariant lifecycle: dial, the two handshakes, the
reconnect/roll, selecting the harness implementation, wiring it, starting it, and the shared
teardown. What does not vary by harness is a library — the <../communicator.py> `Communicator` (the
console side: WebSocket client, `Hello`/launch and `RunnerHello`/`ConsoleResume` handshakes,
tenacity reconnect, roll replay), the <../session_api.py> `SessionApi` (one sequence, retention,
the `OperationJournal` fold, the abort rewrite, admission idempotency), and the neutral-operation
vocabulary (<../neutral_operations.py>). Each harness owns its run-loop behind a narrow
`async def run(self, launch, session) -> None` (<../backend.py> `Harness`): it starts its binary,
speaks its native protocol, projects it, and emits neutral operations through the `SessionApi` the
lifecycle handed it. The shared subprocess-stdio pump lives beside the seam in <../backend.py>, used
by both harnesses but reached through `run()`, so a harness that is neither subprocess-stdio nor
six-hooks-shaped is expressible instead of excluded. The `Communicator` and `SessionApi` were
extracted first (#5056), Claude driven through them unchanged; the per-harness `run()`, Claude's
port onto it, and the Codex harness landed on top (#4667 stage 5).

### Rejected: extend the six `HarnessDriver` hooks

Add hooks for whatever a new harness needs and keep the runner driving them. It keeps the
interaction-shape abstraction — the runner still owns the turn loop and calls fixed hooks — and
still assumes stdio frames, since the hooks are defined in terms of native frames the runner reads
from stdout and writes to stdin. Every genuinely different harness grows the hook surface for all
of them. Wrong level: what varies is the whole run-loop, not a widening set of callbacks inside one
runner-owned loop.

### Rejected: a synchronous blocking handshake shim

Keep the `HarnessDriver` and let a harness needing a multi-step handshake drive it inside
`initialize()`, reading the CLI's stdout directly until it settles. It fights `observe()`'s
ownership of stdout — two readers of one pipe — and hides per-connection handshake state on a hook
the contract calls one-per-process and otherwise stateless. It entrenches the seam this refactor
exists to move: the harness-specific run-loop stays smeared across runner-owned hooks instead of
living behind one boundary the runner does not reach through.
