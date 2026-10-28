# Integrate worker results

Use the [adapter contract](../skills/shared/workflow.md#adapter-contract) and
[execution evidence](../skills/shared/workflow.md#execution-evidence).
Use dedicated isolated integration state at the selected receiving revision.
The adapter supplies the expected conflict policy and whether pushing is allowed.
Record the intended final target ref separately from temporary integration and
PR base refs; they can differ.

1. Before applying a worker patch, verify that receiving graph, source, spec,
   and engine identity match the worker's starting snapshot. A mismatch requires
   a fresh snapshot and rerun after rebasing or redispatching; the old checks do
   not validate the receiving inputs.
2. Apply the reviewed relative YAML patches in order. Resolve routine conflicts
   under the policy below, preserving intended changes. Regenerate emitted JS
   through the canonical adapter command.
3. Validate the combined pipeline, gate, selector uniqueness, and applicable
   runtime checks. Green individual workers do not prove the combined result.
4. If green, commit the combined result if required. Push only when explicitly
   requested. Report the receiving/final SHA, per-branch validation results,
   conflict resolutions, diagnostics, and follow-up assignments.
5. Before reporting work as landed, fetch the intended final target and verify
   that the accepted YAML and generated output are present there. A merged
   worker PR establishes a merge into its recorded base, which may be a
   temporary branch. Recheck a child PR's live base after its parent merges;
   retarget and rebase when it still points at the temporary branch. Compare
   target-tree content or patch equivalence when squash or cherry-pick changes
   commit IDs. Revalidate changed receiving inputs through the checks above.

If the combined result fails, restore the pre-train isolated integration state,
apply patches individually or by halves, and run the same checks to isolate
incompatible work. Retain accepted patches and return exact diagnostics for
failed ones. Do not silently redesign a worker's spec change.

Conflict policy:

- Disjoint members added to one coherent module, or disjoint patch-stream
  removals, can be combined.
- Competing changes to one member's semantics require reporting a real conflict.
- An export rename can break another branch's selector template without a Git
  conflict: templates refer to entities by export name. Run source-only
  `spec validate` on the merged spec and fix the template on the renaming side.
- Resolve generated-output conflicts by regeneration.

Do not use an edited worker's starting `snapshot verify` as an acceptance check.
The receiving comparison uses its baseline identity; combined checks validate
what integration produces.
