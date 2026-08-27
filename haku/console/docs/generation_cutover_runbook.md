# Runtime transport generation cutover — operator runbook

The one-time, maintenance-gated cut from the v3 bridge fold to the neutral-operation generation
(`runner_projection_v1`), issue #4667. The runner interprets native harness frames and emits an
acknowledged journal of neutral conversation operations; the Console stops parsing native payloads.
There is **no dual-protocol period** — old and new peers fail closed against each other — so the cut
happens inside one drained maintenance window.

## What arms the cut

Migration `0109_generation_cutover` **executes on the next deploy's migration Job** and:

- **refuses to apply while the window is not drained** — it RAISEs, and nothing changes, if any
  session is live, any turn is open, any runner sandbox is claimed, or any prompt is pending in the
  old `conversation_prompt` queue or the `submitted_prompt` inbox;
- otherwise sets the active generation to `runner_projection_v1`, creates the admission switch
  **open**, closes the remaining idle sessions (non-launchable; history stays readable), and relaxes
  the `session_frames` runner-seq direction constraint. `session_frames` stays durable and is
  **not** marked legacy.

The exact-generation peering — the protocol-version intersection plus the generation on the journal
hello — is what makes the cut atomic, so the switch is a drain/traffic control, not the safety
mechanism. The operator closes it through the API for the health-gate window once it exists.

**Merging the PR that adds `0109` is the act of scheduling the window.** Do not merge it until you
intend to run the window: on the next deploy it either cuts or fails the deploy's migration step.

## The window

1. **Drain.** Stop sending prompts and let every live session end. The admission switch does not
   exist until `0109` applies, so pre-cut the drain is operational: stop the channels and wait.
   Confirm zero live sessions, open turns, claimed sandboxes, and pending prompts — the migration
   re-checks and refuses otherwise. Take the pre-cutover DB backup here.
2. **Merge + deploy.** Merge the cutover PR and let it deploy. The migration Job **cuts the
   generation or refuses**. A refusal means the window was not drained: nothing changed — clear the
   remaining work and re-deploy. The switch now exists, open.
3. **Close admission.** `POST /api/runtime/admission {"admission_open": false}` — now available — so
   the health gate runs without general traffic. New channel/SPA prompts are refused with
   `admission_closed` while it is closed.
4. **Roll the images.** The new Console and runtime-specific runner images roll. Each peer presents
   the exact active generation and a mutually supported neutral protocol version; an old bridge-v3
   peer finds no common protocol version and fails closed, so no old runner and new Console (or the
   reverse) ever serve one conversation — true whether or not admission is open.
5. **Health gate.** Run `bazel test //haku/console/x:test_generation_cutover_e2e` (the full-stack
   harness: handshake, a streamed message over journal batches, a tool call and result, prompt
   admission, cumulative ACK, and a console-restart resume) and/or drive one live test session
   through the same flow. If it fails, keep admission closed and roll back (below).
6. **Reopen admission.** `POST /api/runtime/admission {"admission_open": true}` only after the health
   gate passes. Confirm with `GET /api/runtime/admission` — it also reports the active `generation`.

## Rollback

The cut is one-way at the schema level: the new images speak only the new protocol. Recovery from a
failed health gate is **image rollback plus the pre-cutover database backup**, not a schema
downgrade. Keep admission closed throughout a failed cut, restore the pre-cutover DB snapshot, roll
the images back, and only then reopen.

- Take the DB backup **before** step 3.
- On any failure in steps 3–5, admission stays closed. Restore the backup, roll images to the
  pre-cut tags, verify a v3 session serves, then reopen.

## The admission switch

A single `runtime_control` row, flipped by `POST /api/runtime/admission` (operator-authenticated) and
read on every prompt admission. It exists only after `0109` applies; flipping it before the cut
returns 409. The escape hatch, for when the API is unreachable, is a direct
`UPDATE runtime_control SET admission_closed = <bool>, updated_at = now() WHERE id = 1;` — the same
row the API writes.

## After the cut (stage 5)

The Codex runner-side projector lands against the same boundary, and the Console-side native
projectors, the v3 turn loop, and the legacy `conversation_prompt` queue are deleted. Until then
they remain in-tree, dead at the cut, deletion-scheduled.
