# Where Haku's standing instructions live

**Archived 2026-08-11. Superseded by what actually happened, which went further.**

This proposed splitting the manual by writability — roughly 210 lines of authority staying in
ducktape, roughly 370 lines of craft moving to `haku-state`. That is not what was done. The
whole manual moved: goals, persona, voice, hard boundaries, run procedure and credential
discovery all live in haku-state's root cards now, base-sync and its pin are gone, and
<haku/README.md> § _Where things live_ records the outcome.

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
mitigation the proposal floated — a run that touches its own manual saying so — was never built,
and is the live question this document leaves behind.

The original proposal text is in git history.
