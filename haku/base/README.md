# haku base

The **base** layer of Haku, a personal background agent: its instructions,
config, and item schema. This directory is baked into the `haku-scanner`
container image and is the read-only project root at run time — Haku never
writes here. Changing Haku's behaviour means editing this directory in ducktape
and letting the image rebuild (Flux image automation bumps the CronJob tag).

- `instructions.md` — Haku's operating manual: who it is, how it reasons, the
  perimeter/credential model, hard rules, the item contract, the `items.md`
  spec, and tone. Haku reads this as itself at run time.
- `AGENTS.md` — instructions for agents that **edit** this directory (not
  Haku's runtime manual).
- `playbooks/` — **example** playbooks (not a closed set); starting points Haku
  adapts and grows from.
- `schema/item.json` — JSON Schema for items, validated at write time.

The step-by-step run procedure lives in the runtime entrypoint
(`haku/claude_web_env/run.md`), which reads this manual; `base/` holds the
durable contracts, not the imperative steps.

No `.mcp.json`: v0 has no MCP servers — Plaid is plain `psql` (via a
`haku-sandbox` pod) and Gmail/Calendar are read-only REST calls.

There is **no `.claude/` permission config** (it's gitignored, and
unnecessary): the Job runs `claude --dangerously-skip-permissions`, so Bash and
every tool are auto-allowed. In-agent gating is not the boundary — Haku runs in
a Pod behind the mitmproxy egress with only read-only creds and scoped RBAC,
and that perimeter is what limits it (see `haku/PLAN.md`).

Haku's **state** (items, intake, memory, log) lives in the separate
`haku-state` repo — the only thing Haku writes, cloned into Haku's home during a
run (the web home puts it at `~/haku-state`). The repo starts empty (no seed);
Haku creates the structure on first run. Design and roadmap: `haku/PLAN.md`.
