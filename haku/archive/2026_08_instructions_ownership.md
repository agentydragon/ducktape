# Where Haku's standing instructions live

**Archived 2026-08-11. Superseded by what actually happened, which went further.**

This proposed splitting the manual by writability — roughly 210 lines of authority staying in
ducktape, roughly 370 lines of craft moving to `haku-state`. That is not what was done. The
whole manual moved: goals, persona, voice, hard boundaries, run procedure and credential
discovery all live in haku-state's root cards now, base-sync and its pin are gone, and
<../base/README.md> records the outcome.

Two things are worth carrying forward from it, which is why it is kept rather than deleted.

**The line that mattered was config, not prose.** The proposal spent its length deciding which
paragraphs Haku may rewrite. What actually stayed behind in ducktape is `agent_shared.yaml` —
model and tool grants — because that is the part a Haku-writable repo must not hold. The
document's own best argument pointed here already ("in-agent prose is not the boundary, the
pod, RBAC and mitmproxy egress are"); the execution simply took it seriously enough to stop
line-splitting a file whose contents were never the enforcement.

**Self-drift is still unsolved, and now unbounded.** The proposal bounded it by keeping the
hard rules under human review. The wholesale move gives that up: a change to Haku's objective
is now one commit among many in a run rather than a ducktape PR an operator reads. The
mitigation floated below — a run that touches its own manual saying so — was never built, and
is the live question this document leaves behind.

The original text follows unchanged.

---

Today Haku's operating manual is <../base/instructions.md> in ducktape (52 KB), read from a
live ducktape checkout at run time, with the run procedure in <../run.md> and per-environment
entrypoints under <../runtime/>. Haku's `haku-state` repo holds its method — `procedures/`,
`ui/`, `memory/`, `log/`. The question is whether the manual should move to `haku-state` too.

## What raises it

The Matrix runtime (<matrix_chat_runtime.md> § Phase 2) clones `haku-state` into the session's
sandbox. If the manual stays in ducktape, that sandbox needs a **second** repo and a second
credential path purely to tell the agent who it is — or the console has to render 52 KB into a
system prompt on every session start. Both are worse than the thing they buy.

## What is actually in the way

Not tidiness. <../docs/security.md> carries a row — _"Operator-owned base vs. canonical
autonomous-write inventory: Haku cannot modify its base"_ — and it is enforced at the
credential layer, not by convention: Haku has a Forgejo token for `haku-state` and no ducktape
write credential. Move the manual as-is and Haku can rewrite its own _Hard rules_, because it
pushes `haku-state` on every run.

**That costs less than it sounds, and more than nothing.** <../base/README.md> already argues
the honest version: in-agent prose is not the boundary, the pod, RBAC and mitmproxy egress are.
Rewriting _Hard rules_ grants Haku no credential and no RBAC — it changes what Haku believes it
may do, and the perimeter still refuses. What is genuinely lost is **human review over intent**,
and with it the defence against slow self-drift: today a change to Haku's objective is a
ducktape PR the operator reads, and afterwards it is one commit among forty in a run.

## Proposal: split by writability, not by topic

| Stays in ducktape (Haku cannot write) | Moves to `haku-state` (Haku's to evolve) |
| ------------------------------------- | ---------------------------------------- |
| _Hard rules_ (44 lines)               | _How you reason_ (124)                   |
| _Setup: discover credentials_ (117)   | _Your own UI service_ (93)               |
| _base vs. state_ (32)                 | _Continuity_ (49)                        |
| _Adopting base updates_ (19)          | _Propagation discipline_ (35)            |
|                                       | _Environment self-check_ (33)            |
|                                       | _Information sources_ (20)               |
|                                       | _The run cycle_ (9), _Tone_ (6)          |

Roughly 210 lines of authority stay; roughly 370 lines of craft move, plus the run procedure.

**This is not a new boundary — it is the one already written down.** The manual has a section
titled _Your method lives in your state, not here_, and `base/`'s own README says base holds
"the durable job and judgment… it does not fix _how_ you work." A 93-line chapter on operating
a UI service is method sitting on the wrong side of a line the document itself draws. The split
enforces the existing contract rather than renegotiating it.

### What the split buys that a wholesale move does not

- **The authority core becomes small enough to render into a system prompt.** That is
  _stronger_ than today's read-only checkout, not weaker: the agent cannot edit a system prompt
  at all, and cannot be argued out of a file it never sees as a file. It also settles Phase 2's
  first open question in a way neither recorded option managed — not `setting_sources` at the
  clone, not the whole manual rendered by the console, but a small rendered core plus a cloned
  repo that stays authoritative over its own conventions.
- **Multi-agent survives it.** One state repo per agent (<2026_08_multi_agent.md>) means per-agent
  craft over a shared authority core. A wholesale move forks the hard rules per agent, which is
  the opposite of what a shared perimeter wants.
- **Base-sync mostly disappears.** Step 2 of every run diffs `haku/base` + `haku/run.md`
  against a pin in `memory/base-sync.md` and migrates state to match. Content that lives in
  `haku-state` is always already current — no pin, no diff, no adoption ceremony. The ceremony
  survives for what stays, so the smaller the core, the cheaper every run gets.

### The entrypoint the routine points at does not move

The Claude Code web environment's project checkout **is** ducktape, so before anything is
cloned ducktape is the only place a scheduled routine can point. The
[web entrypoint](../runtime/claude_web_env/run.md) therefore keeps its path exactly; what
changes is its body — bootstrap (kubeconfig, `~/.netrc`, clone `haku-state`), then "read the
manual in your state and follow it." **The routine's prompt does not change at all.** The
general line: ducktape owns how this environment brings Haku up; `haku-state` owns what Haku
then does.

## Risks and open items

- **Self-drift with no reviewer** is not solved by the split, only bounded by it. Worth pairing
  with something that makes Haku editing its own manual visible — a run that touches it saying
  so, at minimum. Open.
- **<../docs/security.md> must be rewritten, not silently invalidated.** Its enforcement row
  would stop being true of the moved content the moment it moves.
- **The pin crosses the move.** During migration `memory/base-sync.md` refers to files that no
  longer exist. Either handle one run of "the pin advanced past the move" explicitly, or reset
  the pin and note it in the log.
- **Which side does `haku/run.md` land on?** Argued above as craft, since it is the shape of the
  run — but it is also the file every entrypoint defers to, so moving it makes every entrypoint
  depend on the clone having succeeded. Bootstrap already fails loudly when it hasn't, so this
  is probably fine; it is the one item in the table not settled.
