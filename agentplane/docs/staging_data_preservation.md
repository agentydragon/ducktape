# Staging data preservation during service extractions

**Deviation** from the [disposable deployed state](../AGENTS.md#deployed-state-is-disposable) rule:
when ownership of Agentplane state moves between services (the Sandbox Service extraction, the
History Service move), preserve existing staging data where feasible. Do not drop and recreate it as
the easy ownership cut. This does not promise that every staging resource keeps its identity through
every topology change.

Before changing staging:

1. **Inventory state and owners.** Sandbox/workspace volumes, runner session storage and native
   harness history, command journals and receipts, archived Events and cursors, Thread IDs, names and
   archive state, and the identity/configuration/grant records involved. Separate irreplaceable
   source or user-authored data from projections that can be regenerated, and leave unrelated
   services alone.
2. **Define migration and rollback.** Prefer adopting existing resources and explicit schema/data
   migrations. Preserve Session/Thread IDs, causal command/Event identities, checkpoints and access
   associations. A resource that cannot keep its UID or location gets a defined, tested mapping,
   never a silent replacement. Record expected disruption, and ask before a destructive or
   identity-breaking exception.
3. **Back up and prove restore.** Use the approved backup paths and verify recovery in isolation.
   Define a consistency boundary across PostgreSQL and retained runner volumes: snapshots taken while
   writers advance are not automatically a recoverable cut. Keep credentials and private content out
   of logs and PR artifacts.
4. **Hand ownership over once.** Old and new owners never write concurrently. A one-time migration
   may read or copy the old owner's state; that is not permission for a steady-state dependency on
   it. The new owner must own its data and recover with the old one stopped.
5. **Validate before retiring old state.** Check retained histories, identities and correlations,
   cursors, Thread metadata, workspace content and Session resume. Preserve original Events rather
   than replaying commands, tool side effects or Actions to reconstruct history. Rollback must handle
   writes made after cutover: never restore an old backup over new work. Retire old copies only after
   validation and the agreed rollback window.

If preservation proves infeasible, report exactly what would be lost, the alternatives considered
and the operational impact, and get explicit operator approval before deleting or resetting anything.
