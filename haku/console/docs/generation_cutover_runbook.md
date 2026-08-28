# Runtime transport generation cutover — operator runbook

The one-time, maintenance-gated cut from the v3 bridge fold to the neutral-operation generation
(`runner_projection_v1`), issue #4667. The runner interprets native harness frames and emits an
acknowledged journal of neutral conversation operations; the Console stops parsing native payloads.
There is **no dual-protocol period** — old and new peers fail closed against each other — so the cut
happens inside one drained maintenance window. This is a single-operator deployment: draining means
simply not using the app until the window is over.

## What arms the cut

Migration `0109_generation_cutover` **executes on the next deploy's migration Job** and:

- **refuses to apply while the window is not drained** — it RAISEs, and nothing changes, if any
  session is live, any turn is open, any runner sandbox is claimed, or any prompt is pending in the
  old `conversation_prompt` queue or the `submitted_prompt` inbox;
- otherwise closes the remaining idle sessions (non-launchable; history stays readable), relaxes
  the `session_frames` runner-seq direction constraint, and repoints the Matrix ingress dedup rows
  at the inbox. `session_frames` stays durable and is **not** marked legacy.

The exact-generation peering is what makes the cut atomic, and the images carry it themselves: an
old bridge-v3 peer finds no protocol version in common with a v4 peer, and a v4 peer of any other
generation fails the journal hello (`neutral_operations.GENERATION`). No old runner and new Console
(or the reverse) ever serve one conversation.

**Merging the PR that adds `0109` is the act of scheduling the window.** Do not merge it until you
intend to run the window: on the next deploy it either cuts or fails the deploy's migration step.

## The window

1. **Drain.** Stop using the app and let every live session end. Confirm zero live sessions, open
   turns, claimed sandboxes, and pending prompts — the migration re-checks and refuses otherwise.
   Take the pre-cutover DB backup here.
2. **Merge + deploy.** Merge the cutover PR and let it deploy. The migration Job **cuts the
   generation or refuses**. A refusal means the window was not drained: nothing changed — clear the
   remaining work and re-deploy.
3. **Roll the images.** The new Console and runtime-specific runner images roll; the peering above
   fails any old/new pairing closed.
4. **Health gate.** Run `bazel test //haku/console/x:test_generation_cutover_e2e` (the full-stack
   harness: handshake, a streamed message over journal batches, a tool call and result, prompt
   admission, cumulative ACK, and a console-restart resume) and/or drive one live test session
   through the same flow. If it fails, do not resume use — roll back (below).

## Rollback

The cut is one-way at the schema level: the new images speak only the new protocol. Recovery from a
failed health gate is **image rollback plus the pre-cutover database backup**, not a schema
downgrade. Restore the pre-cutover DB snapshot, roll the images back, verify a v3 session serves,
and only then resume use.

- Take the DB backup **before** step 2.
- On any failure in steps 2–4, keep the app idle: restore the backup, roll images to the pre-cut
  tags, verify a v3 session serves.

## After the cut (stage 5)

The Codex runner-side projector lands against the same boundary, and the Console-side native
projectors, the v3 turn loop, and the legacy `conversation_prompt` queue are deleted. Until then
they remain in-tree, dead at the cut, deletion-scheduled.
