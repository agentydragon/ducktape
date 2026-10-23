# Staging MCP edge handoff

## Read-only preflight

On 2026-10-23, Actions was Ready 2/2 and the staged edge was scaled to zero. Source counts:
one Connection, three grants, four enrollments; no OAuth clients, authorizations or tokens.
Credential values were not read. These eight records remain owned by Actions until handoff.

The readiness PR starts one edge replica using its existing Goose init container. It changes
neither the app endpoint nor public routes; the edge has no Gateway ingress or app egress rule.
Before copying, verify edge health, migration 1 and empty destination tables. Recheck source
counts rather than treating this inventory as a permanent fact.

## Execution gate

The operator requested the staging switch. Automatic approval review rejected preparing the
combined activation/route/ownership change before verified transfer and readiness. Execution
needs an approved staging Actions/MCP maintenance window to stop writers and preserve state.
The readiness PR does not execute this window.

## Coordinated handoff

1. Land and publish the app endpoint split (#11729). Verify Flux selected its image. Keep both
   endpoint settings on Actions until the handoff configuration is ready.
2. Hold staging Flux reconciliation during manual maintenance so it cannot restart the old
   writer. Stop Actions and the staged edge; wait for their Pods and migrators to exit. The
   rest of the app, Sandboxes and storage remain intact. Actions execution/review and external
   MCP/OAuth are unavailable while their owner is stopped.
3. Run `//agentplane/mcp_edge/schema_transfer/cmd:mcp-edge-transfer` with the source/destination
   database URLs supplied through the existing environment variables and
   `--confirm-single-writer-cutover`. Verify the six table counts and successful field comparison.
   The command rejects nonempty destination tables and rolls back failures before commit.
4. Apply the reviewed handoff configuration: Actions has `integrated_connection_service=false`
   and no external OAuth settings; the edge serves the existing MCP/OAuth hostname; the app's
   Connection endpoint uses the edge. Add Gateway ingress and app egress explicitly. Keep
   Actions' upstream CIMD and the app's upstream OAuth callback on their existing owners.
5. Start the edge and Actions core under the new ownership. Verify health, operator Connection
   calls, retained bindings/grants and external MCP/OAuth routing. Reconnect behavior remains
   unverified until exercised; no restoration drill is required.
6. Confirm the Git manifests encode the running ownership and resume Flux. Retire integrated
   runtime and old source tables only in later reviewed cleanup changes.

Do not restart the legacy Connection/OAuth writer after transfer. Source tables remain as a
preserved copy, not an automatically safe rollback once the new edge accepts writes. A failure
before transfer commit can restore the original owner after the destination transaction rolls back;
after new writes, rollback needs an explicit state reconciliation decision.
