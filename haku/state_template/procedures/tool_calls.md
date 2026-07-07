# External tool-call proposals

haku-console exposes connected MCP servers as approval-gated actions. These calls are for useful
external side effects Haku is not allowed to execute autonomously during a run. In v1 every call
needs operator approval, so Haku's job is to discover high-value operations, author exact requests,
and choose the experience that creates the most value for the operator.

Use ducktape as the source of configuration: it owns the haku-console deployment/service, the shared
agent API token wiring, and the non-secret connected MCP server catalog (currently
`cluster/k8s/haku/console/config.yaml`). Use live haku-console reflection as the source for each
server's tools and input schemas when reachable.

The `<tool-call>` button is only the v0 affordance. Haku may also call haku-console directly during
its own run, or build a bespoke haku-ui frontend/backend workflow that submits calls, reads console
results, branches, and advances the task while Haku is asleep. The important primitive is
console-mediated consent for an exact external operation, not the shape of the UI control.

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

3. Pick the best experience, not the easiest widget.
   Use a direct haku-console RPC when a fast approval could let the current run continue. Use
   `<tool-call>` when the operator only needs to approve one exact action in context. Build a
   bespoke haku-ui flow when the operator needs to review/edit multiple rows, supply runtime knobs,
   or step through result-dependent branches. If a lightweight backend endpoint makes the flow
   clearer, add it; Haku owns this UI service.

4. Author exact calls only when grounded.
   For asynchronous UI requests, create `tool_requests/<state_request_id>.yaml` with the target
   `server_id`, `tool_name`, `title`, `rationale`, and schema-valid `arguments`. Direct RPCs and
   bespoke flows submit the same console request shape at runtime. Arguments may contain secrets
   when the operation really needs them; haku-state and haku-console are private stores. If a
   required argument is missing or ambiguous, ask for it in the UI first instead of guessing.

5. Choose the consent path.
   If Haku is currently running and the result could let it continue useful work in the same pass,
   submit the call directly to haku-console with the shared agent credential and a short
   `wait_for_ms`. The trusted console may prompt the online operator; if the response returns
   `ok`, `error`, or `denied`, continue from that result. If it returns `pending_approval`, do not
   poll forever or work around approval; leave the action as pending and sweep the audit log later.

6. Put async affordances next to the decision.
   Embed `<tool-call request="..." label="..."></tool-call>` in the item or garden note that gives
   the operator enough context to approve or decline. For multi-row workflows, build a bespoke
   Haku-owned UI surface that collects operator edits and submits one or more authored requests.
   This path is best when the operator may not be online, the action needs review in context, or the
   user-facing UI should gather final values before submitting.

7. Let haku-ui advance bounded workflows.
   haku-ui can read tool-call results from haku-console. When Haku can pre-plan a useful partial
   workflow, let the UI run that sequence while Haku sleeps: collect knobs, submit an approved call,
   read the result, show the next known branch, or stop at the point where actual judgment is needed.
   It does not need to complete the whole task. Advancing the state of the world and leaving a clean
   result for the next Haku run is already valuable.

8. Sweep the console audit log.
   Treat haku-console's tool-call audit as another evidence source, like a bookmark source. During
   a later run, query terminal tool calls and reduce useful outcomes into ordinary state: close the
   item, record that an action happened, or create a follow-up for an error/denial. Do not mirror
   `tool_results/` into git; haku-console remains the source of truth.

## Experience patterns

- Single exact action: one grounded operation with enough context for approval. A `<tool-call>`
  button is enough.
- Same-run continuation: Haku asks haku-console for approval with a short wait, receives a terminal
  result if the operator is online, and continues the run from that result.
- Review/edit table: Haku builds a UI surface where the operator adjusts generated rows, then the UI
  submits one or more approved tool calls and displays per-row results.
- Staged partial workflow: Haku pre-plans the next few safe branches. haku-ui runs them through
  console approvals and result reads until the workflow completes that segment or reaches a branch
  that needs fresh Haku judgment.
- External edit proposal: Haku drafts mutations to a privileged system, lets the operator inspect
  and edit them, then sends exact approved calls. This fits knowledge bases, email, calendars,
  tickets, billing systems, and home/inventory tools.

## Examples

- Delivery-to-inventory: if a delivery manifest says a box is arriving and the operator can confirm
  what physically arrived, build a small check-in surface. Let the operator adjust quantities,
  missing items, lot/expiry dates, or product matches, then submit Grocy MCP requests to add stock
  or products. Later, sweep console audit records to learn which stock changes executed.
- Prepared Tana edits: if notes/tasks should change, build a review surface with proposed node
  edits, tags, fields, due dates, and links. The operator can edit the patch; haku-ui submits Tana
  MCP calls through haku-console, reads apply results, and leaves Haku a clean audit trail.
- Gmail cleanup or reply: group threads that should be archived, labeled, drafted, or sent. Let the
  operator uncheck rows and edit draft text. haku-ui submits Gmail calls through haku-console and
  advances row-by-row from results, stopping for ambiguous replies or send decisions.
- Paperwork follow-through: collect runtime knobs such as preferred appointment windows or claim
  category, then run approved search/hold/upload/email calls until the flow reaches a confirmation,
  error, or branch needing fresh judgment.
- Operations fix: if a rollout is stuck and a kubectl MCP server is connected, use direct RPC for a
  quick "Restart rollout" approval during Haku's run, or leave an incident panel with reconcile /
  restart / rollback options when the operator is not online.
- Account association: if a server requires operator OAuth and the operator has not connected it,
  surface that prerequisite through the console panel flow. Do not replace operator-approved
  execution with an autonomous credential.
