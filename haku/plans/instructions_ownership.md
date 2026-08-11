# Where Haku's standing instructions live

**Status:** decided (operator, 2026-08-11) and partly executed. **ducktape stops being Haku's
base, and stops being a thing Haku syncs from** — `haku-state` owns the manual outright.

This supersedes the earlier proposal in this file, which argued for splitting the manual by
writability: an authority core (hard rules, credential discovery) staying in ducktape, the craft
moving. That is not what is happening, and the reason it was dropped is worth keeping.

## Why not the split

The split existed to preserve one property: that Haku cannot rewrite its own hard rules, because
it holds a `haku-state` credential and no ducktape one. The operator's call is that **the property
was never worth what the two-repo arrangement cost**, because the enforcement that binds Haku is
the credential boundary — RBAC, the `haku-egress-proxy` fence, the haku-console approval queue —
not which repository the prose sits in. Rewriting a rule grants no credential and no RBAC; the
perimeter still refuses. What is genuinely given up is human review over a change of intent, and
that is accepted.

Two costs the split would not have removed:

- The Matrix runtime (<matrix_chat_runtime.md> § Phase 2) clones `haku-state` into the session
  sandbox. Any authority core left in ducktape means a **second** repo and a second credential
  path in that sandbox purely to tell the agent who it is.
- `haku/base/` is 108 KB — `instructions.md` at 52 KB plus `sources/` at 44 KB. Rendering it into
  a system prompt was never viable at that size, and the split only shrinks it.

## What is already true

- **`haku-state` carries the root cards.** `SOUL.md`, `USER.md`, `MEMORY.md`, and an `AGENTS.md`
  routing-card table plus a `## Tools` section — haku-state PR #103. They name **themselves** as
  the source: where a card and `haku/base/` disagree, the card wins. That is the reverse of the
  base-sync rule, and it is the load-bearing half of the decision.
- **ducktape's base is tombstoned, not deleted** — this PR. `haku/base/README.md` and
  `instructions.md` → _Adopting base updates_ both carry the gate, and the security model's
  enforcement row no longer claims Haku cannot modify its own rules.
- **The adoption ceremony is narrowed to one direction.** It may still pull across what
  `haku-state` does not yet carry; it may never overwrite a card.

## What is left, in order

1. **Port the remainder into `haku-state`** — _Setup: discover credentials_ (117 lines),
   `sources/` (44 KB, 15 files), and `tool_calling.md`. The only step that moves content, and
   what the deletion is gated on.
2. **Delete `haku/base/` from ducktape**, repoint <../runtime/claude_web_env/run.md> at the
   clone, remove the adoption ceremony, and drop both tombstones.
3. **Clean up `haku-state`** — delete `memory/base-sync.md` and strip the "`haku/base/` holds an
   older copy" transition notes from the cards.

**The ordering is not optional.** `haku-state` gains it, then ducktape drops it — cross-repo
expand/contract, the discipline a destructive migration needs. Deleting first would leave the
Claude Code web entrypoint, which reads `instructions.md` for credential discovery and `sources/`
for access mechanics, with nothing to read.

## What never moves

- **`haku/base/agent_shared.yaml`** and its `test_agent_config_ssot.py` guard. It looks like part
  of base and is not: it is deploy config for the two managed-agent surfaces, and it sets Haku's
  model and tool grants. Moving it would put those inside the boundary Haku writes.
- **The entrypoint paths under <../runtime/>.** The scheduled routine's prompt is literally
  `Execute haku/runtime/claude_web_env/run.md`, and the Claude Code web environment's project
  checkout **is** ducktape, so before anything is cloned that is the only place a routine can
  point. What changes is the body — bootstrap, then "read the manual in your state" — never the
  path, and never the routine's prompt.

## The risk this leaves

Self-drift with no reviewer: a change to Haku's objective used to be a ducktape PR the operator
read, and afterwards it is one commit among forty in a run. The decision accepts this. Worth
pairing with something that makes a run's edit to its own `SOUL.md` visible — at minimum, a run
that touches it saying so. Not designed. The obvious candidate is gone: OpenClaw's workspace hash
attestation (`.attested`, `.openclaw/workspace-state.json`) is legacy and upstream removed it, so
there is nothing to adopt there.
