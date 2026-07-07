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

   Pattern (established by `grocy-sf` and `tana-rw`, added 2026-07-06): a server needs an
   MCP-OAuth authorization-server front — the `mcp-oauth-facade` image
   (`mcp_infra.authentik_auth`, which speaks DCR + PKCE and delegates user auth to Authentik) —
   plus an Authentik OAuth2 provider and an access group containing the console operator
   (`agentydragon`). Then it is a single `mcp.servers` entry in
   `cluster/k8s/haku/console/config.yaml` with `operator_oauth`. Servers behind a plain Authentik
   **proxy provider** (forward-auth) or a static bearer do not match the console's DCR client
   flow and need the facade treatment first. Candidates:
   - **google-workspace-mcp** — Gmail (send, drafts, labels — beyond the narrow `gmail-labeling`
     always_allow path, which stays), Google Drive (organize/save files), Google Calendar
     (add/edit events). One upstream server covers all three. Already deployed at
     `cluster/k8s/agents/google-workspace-mcp/` (`ghcr.io/taylorwilsdon/google_workspace_mcp`,
     does its own Google OAuth) and fronted today by an Authentik proxy provider
     (`cluster/k8s/authentik/app/blueprints/google-workspace-mcp-sso.yaml`, "authentik Admins").
     Blocker: it does not speak MCP OAuth/DCR — front it with `mcp-oauth-facade` (or confirm an
     MCP-OAuth mode), then add the console entry.
   - **habitify** — habit data store (mark habits done via `set_habit_status`, etc.). MCP server
     code exists at `llm/mcp/habitify/` (FastMCP with write tools) but is not deployed. Needs a
     container/Bazel image + k8s deployment + an auth front (facade or static bearer), then the
     console entry.
