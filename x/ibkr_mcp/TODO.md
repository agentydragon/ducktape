# ibkr_mcp TODO

Items that can only be settled once the gateway is live (a paper login is
active). Until then the server is built and tested but unexercised against real
IBKR responses.

## Reactivation check

- [ ] **2026-07-31:** Check whether the IBKR paper-account application has
      become active. If it has, unsuspend the `ibkr-mcp` Flux Kustomization and
      `ibkr-mcp-server` ImageRepository, restore the `interactive_brokers` entry in
      `cluster/k8s/haku/console/config.yaml`, complete one interactive paper login,
      and verify `session_status` plus a delayed quote end to end before declaring
      the service live. Then pin IBeam to the verified image digest.

## Once the gateway is running

- **Free-tier data go/no-go**: confirm delayed quotes actually flow for the
  instruments in scope. If the Web API's free data doesn't cover them, fall back
  to the socket API behind the same tool surface.
- **Verify the OpenAPI response schemas** are reliable enough to keep as tool
  output schemas (we currently keep them; `server._customize_component` no longer
  strips them). Only strip if they demonstrably misbehave.
- **Trim `spec_fixup`**: the Swagger 2.0 → OpenAPI 3.1 transcode + read-only
  filter are required (IBKR ships 2.0; FastMCP needs 3.x). Exercise the live API
  and drop any remaining conversion (ref-rewrite edge cases, empty-enum drop,
  response reshaping) that turns out to be cargo-culted from grocy and isn't
  actually needed — modern schema shapes should already be handled by
  fastmcp/pydantic.
- **Try richer lookups**: bond parameter lookups (`secdef_info` / `secdef_search`)
  and a bond scanner (`scanner_run` + `scanner_params`) — check they return
  useful data and whether any need a dedicated wrapper.
- **Session keepalive**: IBeam maintains the session, so no server-side tickler
  is wired. If IBeam's maintenance proves insufficient in practice, add a
  background task that POSTs `/tickle` on an interval while authenticated.
- **Pin IBeam** to its running image digest (drop the `:latest` + CLEANUP
  tombstone in `../cluster/k8s/ibkr/deployment.yaml`).

## Candidate tools to consider (read-only)

Grouped from the Web API surface. Reconcile against the gateway's _current_ spec
when we refresh it — several below are in the newer Web API but absent from the
pinned older Swagger 2.0 mirror, so they'll only appear once the spec is
re-dumped from the running gateway. Add to the `route_policy` allowlist per group
as they prove useful.

- **Contract / reference discovery**: `/trsrv/stocks`, `/trsrv/futures`,
  `/trsrv/secdef` (bulk symbol→conid), `/iserver/contract/rules`,
  `/trsrv/secdef/schedule` (trading schedule). Cheaper bulk resolution than
  per-symbol `secdef_search`.
- **FX / currency**: `/iserver/exchangerate`, `/iserver/currency/pairs` (newer
  Web API).
- **Deeper history**: `/hmds/history`, `/hmds/scanner` (the historical
  market-data service — more lookback than `/iserver/marketdata/history`; newer
  Web API).
- **Watchlists (read)**: `/iserver/watchlists`, `/iserver/watchlists/{id}` — read
  the operator's saved watchlists to drive quote pulls.
- **Read-only account/portfolio (scope decision)**: `/portfolio/{accountId}/`
  `{summary,ledger,positions}`, `/iserver/account/pnl/partitioned`,
  `/pa/{summary,performance}`, `/iserver/account/trades`. All read-only but
  account-scoped — exposing them widens the server past pure market data to "let
  Haku see the paper account." Decide deliberately before adding; the read-only
  guard still holds (no order routes), but it's a different data class.
