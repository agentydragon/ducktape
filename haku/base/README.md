# haku base

The **base** layer of Haku, a personal background agent: its instructions and config —
the durable **job and judgment**, independent of how Haku currently works. This directory
is baked into the `haku-scanner` container image and is the read-only project root at run
time — Haku never writes here. Changing Haku's behaviour means editing this directory in
ducktape and letting the image rebuild (Flux image automation bumps the CronJob tag).

- `instructions.md` — Haku's operating manual: who it is, its objective (make the
  operator's life go well), how it reasons, the perimeter/credential model, hard rules,
  continuity, and that it maintains its own UI and procedures. **Item-agnostic** — no item
  schema, no board spec; how Haku presents what it surfaces is its own implementation, in
  its state. Haku reads this as itself at run time.
- `AGENTS.md` — instructions for agents that **edit** this directory (not
  Haku's runtime manual).
- `SECURITY.md` — the **security model**: threat model, the full enforcement-mechanism
  inventory (RBAC, egress, closures, UI containment), and the invariants any change must
  preserve. The index a security review starts from.
- `sources/` — Haku's **information sources** (the operator-linked channels it reads:
  gmail, calendar, drive, tasks, tana, plaid, ducktape) — what each channel tells you +
  how to read it. Inputs/reference, not a checklist.

Haku's **method** — the procedures (passes) it runs, the UI it serves, and whatever
format that UI presents (the starter kit's "items" board + schema is one example) — is
**not here.** It is seeded from `haku/state_template/` and owned/evolved by Haku in its
state. (The full "what lives where" is in `AGENTS.md`.)

The step-by-step run procedure lives in `haku/run.md` (environment-neutral;
per-environment entrypoints like `haku/runtime/claude_web_env/run.md` just layer setup
and defer to it), which reads this manual; `base/` holds the durable contracts,
not the imperative steps.

No `.mcp.json`: v0 has no MCP servers — Plaid is plain `psql` (via a
`haku-sandbox` pod) and Gmail/Calendar are read-only REST calls.

There is **no `.claude/` permission config** (it's gitignored, and
unnecessary): the Job runs `claude --dangerously-skip-permissions`, so Bash and
every tool are auto-allowed. In-agent gating is not the boundary — Haku runs in
a Pod behind the mitmproxy egress with only read-only creds and scoped RBAC,
and that perimeter is what limits it (see `haku/PLAN.md`).

Haku's **state** (memory, log, its UI + procedures, intake, and whatever working format it
presents) lives in the separate `haku-state` repo — the only thing Haku writes, cloned
into Haku's home during a run (the web home puts it at `~/haku-state`). The repo starts
empty; on first run Haku scaffolds it from `haku/state_template/` (placeholder stubs plus
the `ui/`, `procedures/`, and `k8s/` starters), then owns and evolves it. Design and
roadmap: `haku/PLAN.md`.
