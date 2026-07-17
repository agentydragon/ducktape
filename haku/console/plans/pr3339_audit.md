# PR #3339 audit — deferred findings

Full-content STYLE + duplication audit of the airlock→per-Operator-Google-connections change.
The **must-fix** and **worth-doing** findings have been applied — the pre-existing-code ones in
PR #3351 (merged: operator-OAuth test relocation, the migration-ledger change-detector, the
self-referential doc counts, the `[path](path)` link), and the rest in PR #3339 itself (shared
`oauth_token_support` token helpers, the degraded-gate/auth-mode `ServerAuthMode` classifier, the
`google_service` client builder, the `ConnectionCard` component, the dead-`kind`/precise-`provider`/
invalid-combo-validator/garbled-comment STYLE fixes, the security-SSOT + `gmail.settings.basic`
scope corrections, and the conftest identity-store test fixtures).

Only the deliberately-deferred items remain below.

## DEFER — judgment calls / low value / intentional parallelism

- **DF1** `ProviderConnectionChangedEvent` ≈ `McpOperatorAuthChangedEvent` (`console_events.py`) — a
  shared base carrying `status: Literal["connected","disconnected"]` + `extra="forbid"` would remove
  one copied field, but the distinct event types/keys are intentional. Low value.
- **DF2** `ProviderConnection`/`ProviderConnectionFlow` parallel the `McpOperatorOAuth*` tables
  (`database_schema.py`) — a token-column mixin is extractable, but the divergence (fixed client vs
  DCR columns, different PKs) is largely intentional. Mixin-only, if ever.
- **DF3** `ProviderConnection.token_type` + `updated_at` are write-only (`database_schema.py`;
  written in `provider_connection.py`) — but faithfully mirror the pre-existing
  `McpOperatorOAuthAssociation.token_type`/`updated_at`. Decide whether to trim the pattern repo-wide
  (out of this PR's scope) or accept it.
- **DF5** `access_token_for` refresh **write-tail** (7 lines) is still parallel between the two
  stores (`provider_connection.py` ≡ `mcp_operator_oauth.py`) — `apply_refreshed_token(row,…)` over a
  small Protocol would share it. Lower payoff; the surrounding sequence is intentionally parallel.
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
