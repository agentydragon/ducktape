# Kitchen and household stocking subagent

**Deferred.** Operator, 2026-07-11. Keep the shopping list current, plan reasonably cookable
meals from what's on hand, and keep fresh fruit in stock that the operator likes. Process
changes since the last run in one batch. Grocy owns the shopping list. The July design
records `haku-state:kitchen/board.yaml:shopping_list` as a UI mirror and `make_now` as an
example of the intended meal planning output.

## Decisions before implementation

- **Runtime and trigger.** Select a new runner and decide whether Haku reads Grocy's
  `stock_log` at each pass or a separate collector polls it and starts the worker. Decide
  who owns the bookmark. Verify webhook support if considering event-driven collection.
  This trigger can be decided independently of Haku's proposed general intake sensors.
- **Data and model.** The operator proposed z.ai for this workload. Decide which household
  data may reach the chosen provider, and enforce that boundary in the runner and its egress.
  See the [information trust design](information_trust_tiers.md).
- **Grocy authority.** Give the worker bounded stock reconciliation and shopping-list edits.
  The [household MCP server](../../grocy_mcp/README.md#tool-surface) runs with the user's
  Grocy permissions and exposes broader writes, including `product_delete`. Choose a
  separate MCP service or a scoped credential and enforced tool allowlist. Keep credentials
  server-side, block arbitrary entity writes and irreversible operations, and decide who
  approves worker writes before connecting a household.
- **Inputs.** Verify a useful presence and guest signal, and whether Thrive Market has a usable
  catalog feed. The July design lists location and calendar as partial occupancy signals and
  `haku-state:kitchen/board.yaml:thrive_order` and `incoming` as examples of catalog-related data.

## Acceptance evidence

- Extend the isolated [Grocy eval cases](../../grocy_mcp/eval/cases.py), starting from
  `shopping_planning` and `post_cook_logging`, with a realistic stock-change batch that
  requires reconciling the shopping list. Score final state against the case criteria and
  compare candidate models before choosing a tier. The [runner](../../grocy_mcp/eval/run.py)
  records final state, transcript, and an agent postmortem. Choose a grader for the new case.
- Verify end-to-end trace attribution, access, contents, and retention with a representative
  kitchen run. The [shared LiteLLM integration](../../cluster/cdk8s/litellm/docs/langfuse_tracing.md)
  can provide model spans. Decide whether the workload needs a separate trace project once
  the runtime and household data boundary are known.
