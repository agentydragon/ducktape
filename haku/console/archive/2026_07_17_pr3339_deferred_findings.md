# PR #3339 audit — deferred findings

**Archived 2026-08-16, unchanged.** Every item below was a judgment call the audit **declined**,
not work it scheduled — so it sat in `plans/` reading as a backlog it never was. Each was still
factually true when this moved (`_now()`/`timeout=10.0` are still inlined in
`mcp_operator_oauth.py`; `OperatorConnectionChangedEvent` and `McpOperatorAuthChangedEvent` are
still separate models), which is the point: they were declined on value, not on staleness. Kept so
the reasoning is findable if any of them is proposed again.

Full-content STYLE + duplication audit of the airlock→per-Operator-Google-connections change.
The **must-fix** and **worth-doing** findings have been applied — the pre-existing-code ones in
PR #3351 (merged: operator-OAuth test relocation, the migration-ledger change-detector, the
self-referential doc counts, the `[path](path)` link), and the rest in PR #3339 itself (shared
`oauth_token_support` token helpers, the degraded-gate/auth-mode `ServerAuthMode` classifier, the
`google_service` client builder, the `ConnectionCard` component, the dead-`kind`/precise-`provider`/
invalid-combo-validator/garbled-comment STYLE fixes, the security-SSOT + `gmail.settings.basic`
scope corrections, and the conftest identity-store test fixtures).

Only the deliberately-deferred items remain below. The shared `oauth_token_states` extraction
subsequently resolved DF2, DF3, and DF5.

## DEFER — judgment calls / low value / intentional parallelism

- **DF1** `OperatorConnectionChangedEvent` ≈ `McpOperatorAuthChangedEvent` (`console_events.py`) — a
  shared base carrying `status: Literal["connected","disconnected"]` + `extra="forbid"` would remove
  one copied field, but the distinct event types/keys are intentional. Low value.
- **DF6** `_now()` / `timeout=10.0` are named in `provider_connection.py` but still inlined in
  `mcp_operator_oauth.py`. Could fold `now()`/`TOKEN_ENDPOINT_TIMEOUT_SECONDS` into the shared
  `oauth_token_support` module — or accept the local inconsistency.
- **DF7** frontend `connect`/`connectProvider` + `disconnect`/`disconnectProvider` handlers and the
  three generation-guarded fetch triples in `load()` (`settings_panel.tsx`) share structure — small
  helpers / a `useTrackedResource` hook are possible but low value; single-sourcing the generation
  guard is the only real argument.
- **DF8** frontend `disconnect*` client fns' return values are fetched but discarded
  (`client.ts`) — could be typed `Promise<void>`. Minor; the return type mirrors the backend
  response faithfully.
- **DF9** `test_mcp_approval.py` vs `test_mcp_server.py` `_STATIC_AGENTS` + autouse
  `_static_agent_env` static-agent wiring is parallel, but the values deliberately differ per
  module's routing matrix. Low value.
