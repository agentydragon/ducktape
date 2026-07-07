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
- The **security model** (threat model, enforcement inventory, invariants) lives in
  <../docs/security.md> — not in base, since it spans console and cluster wiring too.
- `AGENTS.md` — instructions for agents that **edit** this directory (not
  Haku's runtime manual).
- `sources/` — Haku's **information sources** (the operator-linked channels it reads:
  gmail, calendar, drive, tasks, tana, plaid, ducktape) — what each channel tells you +
  how to read it. Inputs/reference, not a checklist.

Haku's **method** — the procedures (passes) it runs, the UI it serves, and whatever
format that UI presents (the current "items" board is one example) — is
**not here.** It lives in, and only in, Haku's `haku-state` repo.
(The full "what lives where" is in `AGENTS.md`.)

The step-by-step run procedure lives in `haku/run.md` (environment-neutral;
per-environment entrypoints like `haku/runtime/claude_web_env/run.md` just layer setup
and defer to it), which reads this manual; `base/` holds the durable contracts,
not the imperative steps.

No `.mcp.json` permission surface: Haku's source MCPs are called explicitly over HTTP where needed
(see `sources/`), and privileged external actions go through haku-console tool-call requests rather
than local agent auto-allow config. Plaid is plain `psql` (via a `haku-sandbox` pod) and
Gmail/Calendar reads are REST calls.

There is **no `.claude/` permission config** (it's gitignored, and
unnecessary): the Job runs `claude --dangerously-skip-permissions`, so Bash and
every tool are auto-allowed. In-agent gating is not the boundary — Haku runs in
a Pod behind the mitmproxy egress with only read-only creds and scoped RBAC,
and that perimeter is what limits it (see `haku/PLAN.md`).

Haku's **state** (memory, log, its UI + procedures, intake, and whatever working format it
presents) lives in the separate `haku-state` repo — the only thing Haku writes, cloned
into Haku's home during a run (the web home puts it at `~/haku-state`). The repo is the
live artifact with no seed template behind it (`haku/state_template/` was retired
2026-07-07 — haku-ui and the method live in haku-state); an empty remote is an incident
to surface, not a first run. Design and roadmap: `haku/PLAN.md`.
