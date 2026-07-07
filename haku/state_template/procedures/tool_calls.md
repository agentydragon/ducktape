# External tool-call proposals

haku-console exposes connected MCP servers as approval-gated actions. These calls are for useful
external side effects Haku is not allowed to execute autonomously during a run. In v1 every call
needs operator approval, so Haku's job is to discover high-value operations, author exact requests,
and either ask for immediate consent through haku-console or hand the operator a clear affordance
for later.

Use ducktape as the source of configuration: it owns the haku-console deployment/service, the shared
agent API token wiring, and the non-secret connected MCP server catalog (currently
`cluster/k8s/haku/console/config.yaml`). Use live haku-console reflection as the source for each
server's tools and input schemas when reachable.

## Pass

1. Discover the connected tools.
   Query haku-console's MCP capability endpoint when available. If HTTP access is not available
   during a run, read ducktape's haku-console config only to learn which servers are configured.
   The live MCP `tools/list` response is the schema source; do not duplicate tool schemas in
   haku-state.

2. Match tools to the operator's active goals.
   For each current item, bookmark/source, or implicit next step, ask whether a connected MCP tool
   could turn known facts into an operator-approved action. Include explicit requests and quiet
   opportunities: "one action you might want to take here is..." is useful even when Haku is not
   certain enough to preselect every argument.

3. Author exact requests only when the call is grounded.
   Create `tool_requests/<state_request_id>.yaml` with the target `server_id`, `tool_name`,
   `title`, `rationale`, and schema-valid `arguments`. Arguments may contain secrets when the
   operation really needs them; haku-state and haku-console are private stores. If a required
   argument is missing or ambiguous, build an intake/choice surface first instead of guessing.

4. Choose the consent path.
   If Haku is currently running and the result could let it continue useful work in the same pass,
   submit the call directly to haku-console with the shared agent credential and a short
   `wait_for_ms`. The trusted console may prompt the online operator; if the response returns
   `ok`, `error`, or `denied`, continue from that result. If it returns `pending_approval`, do not
   poll forever or work around approval; leave the action as pending and sweep the audit log later.

5. Put async affordances next to the decision.
   Embed `<tool-call request="..." label="..."></tool-call>` in the item or garden note that gives
   the operator enough context to approve or decline. For multi-row workflows, build a bespoke
   Haku-owned UI surface that collects operator edits and submits one or more authored requests.
   This path is best when the operator may not be online, the action needs review in context, or the
   user-facing UI should gather final values before submitting.

6. Sweep the console audit log.
   Treat haku-console's tool-call audit as another evidence source, like a bookmark source. During
   a later run, query terminal tool calls and reduce useful outcomes into ordinary state: close the
   item, record that an action happened, or create a follow-up for an error/denial. Do not mirror
   `tool_results/` into git; haku-console remains the source of truth.

## Patterns

- Delivery-to-inventory: if a delivery manifest says a box is arriving and the operator can confirm
  what physically arrived, build a small check-in surface. Let the operator adjust quantities,
  missing items, lot/expiry dates, or product matches, then submit Grocy MCP requests to add stock
  or products. Later, sweep console audit records to learn which stock changes executed.
- Operations fix: if a rollout is stuck and a kubectl MCP server is connected, present a "Restart
  stuck rollout" request with the exact namespace/workload arguments and the evidence that makes it
  reasonable.
- Account association: if a server requires operator OAuth and the operator has not connected it,
  surface that prerequisite through the console panel flow. Do not replace operator-approved
  execution with an autonomous credential.
