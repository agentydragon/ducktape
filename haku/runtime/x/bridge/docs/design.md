# Bridge decision record — session re-adoption

Constraints and rejected alternatives behind the bridge surviving a console roll. Check here
before redesigning recovery, replay, or bridge versioning. The current frame-log shape is
<../../../console/docs/harness_frame_log_v3.md>; the live protocol invariants are docstrings in
<../protocol.py>.

## Re-adoption: keep the CLI alive (design B), not `--resume` (design A)

What a dying console replica kills is the CLI process, not the sandbox: the sandbox is a separate
SandboxClaim on its own pod, and the runner dials out, so a reconnect lands on whichever replica
the Service picks.

**Built: design B.** The runner keeps the CLI alive across the dropped socket, buffers, and
redials; an adopting console picks the conversation up mid-flight, preserving the in-flight turn.
The lease (`expire_stale_leases`, <../../../console/session/store.py>) is what makes a dropped
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
deduplicates `(session_id, runner_seq)`, and `Checkpoint.HOLD` (<../../../console/x/runtime.py>)
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
