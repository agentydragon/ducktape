# haku base

The **base** layer of Haku, a personal background agent: its instructions,
config, and item schema. This directory is baked into the `haku-scanner`
container image and is the read-only project root at run time — Haku never
writes here. Changing Haku's behaviour means editing this directory in ducktape
and letting the image rebuild (Flux image automation bumps the CronJob tag).

- `AGENTS.md` — the operating manual: scan procedure, item contract, tone.
- `playbooks/` — **example** playbooks (not a closed set); starting points Haku
  adapts and grows from.
- `schema/item.json` — JSON Schema for items, validated at write time.

No `.mcp.json`: v0 has no MCP servers — Plaid is plain `psql` (via a
`haku-sandbox` pod) and Gmail/Calendar are read-only REST calls.

There is **no `.claude/` permission config** (it's gitignored, and
unnecessary): the Job runs `claude --dangerously-skip-permissions`, so Bash and
every tool are auto-allowed. In-agent gating is not the boundary — Haku runs in
a Pod behind the mitmproxy egress with only read-only creds and scoped RBAC,
and that perimeter is what limits it (see `haku/PLAN.md`).

Haku's **state** (items, intake, memory, log) lives in the separate
`haku-state` repo — the only thing Haku writes, cloned at `./state/` during a
run. The repo starts empty (no seed); Haku creates the structure on first run.
Design and roadmap: `haku/PLAN.md`.
