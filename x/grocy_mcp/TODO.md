# grocy_mcp follow-ups

Running backlog of improvements surfaced by Haiku eval audits and review
feedback. Items here are cross-cutting or non-trivial enough that they
shouldn't get lost between sessions.

## Verification pending

### Does claude.ai's MCP connector forward `initialize.instructions` to the LLM?

Commit that added `server_instructions.md` publishes the markdown via
`FastMCP.from_openapi(instructions=…)`. `agent_framework` **does not**
forward it (verified by reading `agent_framework/_mcp.py`: it calls
`session.initialize()` and discards the result); the eval harness
prepends the markdown to the system prompt manually to compensate.
Once claude.ai is connected to a deployed `grocy-mcp`, check whether
the model spontaneously demonstrates awareness of the conventions (e.g.
mentions `amount_opened` semantics, picks `stock_set` for a "we counted
and have X" framing). If it doesn't, follow the same manual-prepend
approach at the connector side — or file a bug upstream on whichever
client dropped the field.

## Tool naming

### Finish the `<entity>_<verb>` rename

The first pass renamed the CRUD / stock / shopping-list families. The
remainder needs a bit more care:

- **Stock views** — `get_expiring_stock`, `get_below_minimum_stock`,
  `get_expired_stock`, `list_volatile_stock`. `stock_expiring_list` etc.
  works mechanically but "stock views keyed on a query" doesn't feel like
  a true entity; a separate "queries" namespace
  (`stock_query_expiring` / `stock_query_expired` / …) or grouping under
  `volatile_stock_*` might read better. Decide before renaming.
- **Singletons** — `get_system_info`, `get_db_changed_time`,
  `get_current_user`. These are zero-argument reads on singleton
  resources; dropping the verb (`system_info`, `db_changed_time`,
  `current_user`) reads more naturally as MCP resources than as tools.
  May warrant promoting them to actual MCP resources (see `TOOL_OVERRIDES`
  `resource=True` flag) rather than just renaming.
- **Product stock helpers** (OpenAPI) — `get_product_stock`,
  `open_product_stock`, `transfer_product_stock` (already disabled). The
  `<entity>_<verb>` rename is mechanical (`product_stock_get`,
  `product_stock_open`) but the `product_stock` name implies it's a
  standalone entity, which it isn't — it's a derived view over
  `stock_entries` filtered by product. Worth thinking about whether to
  promote to a batch tool that groups with `stock_*`.

## Operational safety

### `shopping_list_clear` checks each DELETE (landed in #1345)

Tombstone — keep until the next design pass so we remember it was an
intentional fix rather than an oversight.
