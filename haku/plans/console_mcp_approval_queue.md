# Plan: console MCP approval follow-ups

Status: open follow-ups only. Implemented behavior belongs in:

- `haku/console/README.md` for the trusted console API, approval ledger, operator OAuth,
  and console UI shell.
- `haku/state_template/tool_requests/README.md` for authored request files.
- `haku/state_template/procedures/tool_calls.md` and `haku/state_template/procedures/garden.md`
  for Haku's external tool-call proposal pass, direct approval RPC use, and `<tool-call>`
  affordance authoring.

## Open follow-ups

1. Live haku-state rollout and smoke test.
   After the template PR lands, sync the live haku-state repo/template wiring, roll out haku-ui,
   and prove the real path: authored request -> haku-ui backend -> haku-console approval -> MCP
   execution -> console audit read. Use a harmless call first; only add Grocy writes when the
   exact arguments are known.

2. Audit sweep in Haku's normal pass.
   Treat haku-console's `GET /api/tool-calls` audit log as another bookmark/evidence source Haku
   sweeps. Terminal records can update ordinary state files when useful, but haku-console remains
   the source of truth for tool-call authorization, execution, audit, and results. Do not add a
   `tool_results/` git mirror.

3. Console-as-MCP airlock.
   Haku can already use haku-console's HTTP API to request approval-gated calls. Later, expose the
   same approval ledger and execution path as an MCP proxy too. Future auto-allow policy can execute
   immediately for allowed calls; all other calls stay approval-gated or get punted to the
   asynchronous haku-ui affordance flow.

4. Additional server onboarding.
   Add more connected MCP servers as concrete use cases arrive, such as a kubectl MCP server for
   "restart stuck rollout." Server config should name reachable servers; tool schemas still come
   from live MCP reflection, not duplicated config.
