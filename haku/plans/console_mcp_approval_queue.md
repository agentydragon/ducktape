# Plan: console MCP approval follow-ups

Status: open follow-ups only. Implemented behavior belongs in:

- `haku/console/README.md` for the trusted console API, approval ledger, operator OAuth,
  and console UI shell.
- haku-state's `tool_requests/README.md` for authored request files.
- haku-state's `procedures/tool_calls.md` and `procedures/garden.md`
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

3. Console-as-MCP airlock — fleshed out (operator, 2026-07-11), still not built.

   **Why now:** every haku-console call from Haku's web session today goes through a short-lived
   `haku-sandbox` pod (`kubectl run` a curl container, write the JSON body via a ConfigMap, read
   `kubectl logs`, delete the pod) — the workaround `haku/base/instructions.md` documents for
   reaching any cluster-internal Service from a web session that has no cluster-internal network
   path. It works, but it's slow, easy to fumble, and 2026-07-11's session hit it repeatedly (a
   Grocy shopping-list refresh, several tool-call submissions, a permission probe — each its own
   pod). A native MCP connection (the same mechanism this session already uses for `tana-mcp-ro`
   and `grocy-mcp-sf`) replaces all of that with a normal tool call.

   **The constraint that shapes this:** `haku.allegedly.works` is not a usable base — it's 100%
   an Authentik **proxy provider** (browser SSO forward-auth, policy-restricted to the operator's
   own account), with no bearer/API path at all. This needs its own public route, not a carve-out
   of that hostname.

   **Precedent to follow, not invent:** `tana-mcp-ro.allegedly.works` is exactly this pattern
   already shipped — a public `HTTPRoute` + a static bearer (`haku-tana-ro-token`), whose own
   annotation states the reason verbatim: it "exists so Haku's Claude Code web home can reach the
   facade directly... [since] there is no cluster-internal path from the home." A
   `haku-console-mcp` route would be the same shape: public HTTPRoute, static bearer — reusing the
   **existing** `haku-console-agent-api` token (already scoped to exactly "request/read tool
   calls, not approve them" — the right scope for this) rather than inventing new auth. The
   OAuth-DCR pattern `grocy-sf`/`tana-rw` use doesn't fit here: that authenticates the _operator's_
   own browser-linked identity for servers _he_ wants to use through the console; there's no
   analogous "Haku's own OAuth identity" anywhere in this repo, and inventing one is out of scope
   for what's really just replacing a pod-curl workaround with a normal MCP transport.

   **What it wraps:** haku-console is a plain FastAPI app, not an MCP server — it uses FastMCP
   only as an in-process _client_ (per-tool `FastMCP` instances registered in
   `haku/console/mcp_config.py`'s `InProcessServers`, reflecting the operator's connected tools
   outward). This plan is the reverse direction: a new `FastMCP` instance wrapping
   `mcp_approval.py`'s three operations Haku already calls today —
   submit (`POST /api/tool-calls`), list/status (`GET /api/tool-calls`), and the audit sweep — as
   `@mcp.tool`s, mounted alongside the existing FastAPI app (FastMCP supports co-mounting with
   FastAPI in one ASGI app) or as a sibling route on the new hostname.

   **Where it belongs:** `haku/docs/security.md`'s own invariant #3 settles this — "does it hold
   a secret, perform a privileged action, or define the trust boundary? If not, it belongs to
   Haku." The approval ledger and its execution path are exactly that boundary, so this MCP
   surface lives in haku-console, not in Haku's own state or a new service.

   Once live, auto-allow policy (already the plan for allowed calls) can return `ok`/`error`
   directly in the tool response; everything else still lands `pending_approval` for the
   operator, same as today — this only changes the transport Haku uses to reach the queue, not
   the approval semantics.

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
   - **google-workspace-mcp** — Gmail (send, drafts, labels), Google Drive (organize/save files), Google Calendar
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
