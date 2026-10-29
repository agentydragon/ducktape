# Decision cockpit contract

Use this contract when the candidate inventory is too large for comfortable row-by-row review.

## Evidence row

Keep one current record per branch with at least:

```json
{
  "branch": "topic/example",
  "sha": "live remote SHA",
  "compare_url": "https://github.com/OWNER/REPO/compare/DEFAULT...topic/example",
  "subject": "tip subject",
  "ahead": 3,
  "behind": 18,
  "diff_paths": ["path/one", "path/two"],
  "pr_history": [{ "number": 123, "state": "MERGED", "url": "..." }],
  "diffstate": "landed/successor",
  "topic": "subsystem",
  "rationale": "The exact behavior landed in PR #123; this ref has no remaining unique objective.",
  "evidence_links": [{ "label": "PR #123", "url": "..." }],
  "probability": 97,
  "premise_pack": "migration-complete"
}
```

Derived overlap metrics may help triage, but keep them visibly secondary to `rationale` and `evidence_links`. Reject missing or duplicate branch rows, and reject a judgment inventory that does not exactly match the live candidate inventory.

## Rationale quality

Good rationales identify the objective and its disposition:

- “The branch's credential rotation landed in PR #123; compare shows only the pre-merge formulation.”
- “The implementation targets the removed `old_service/`; current code uses `new_service/handler.py` from commit `abc123`.”
- “This is an intermediate stack checkpoint; its unique commits are contained in the later sibling branch and the final behavior is on the default branch.”

Reject rationales such as “old,” “probably Claude work,” “many commits behind,” or “looks merged.”

## Review surfaces

Include:

- summary counts by decision and lane;
- premise packs with exact counts, average probability, and an inspect action;
- a topic by diffstate matrix for navigation;
- an ambiguous-first exception queue;
- branch rows with compare, PR, commit, and current-code links;
- `D`, `K`, and `R` controls persisted in `localStorage`;
- search and filters for topic, diffstate, lane, pack, and decision;
- export of exact delete names and complete decision JSON.

If showing “forecasted objections,” calculate `sum(1 - P(delete))` for the selected set and label it as a calibration aid, not an independence claim or guarantee.

## Interaction invariants

Changing one row's decision must not eject the user from the current context. Preserve:

- active premise-pack or matrix scope;
- all select/search filters;
- scroll position across rerender;
- decisions for branches that remain in the inventory.

Do not carry decisions onto a different SHA unnoticed. Key persisted state by repository and inventory version, or surface SHA changes before reusing it.

Keep deletion outside the HTML. The cockpit reviews and exports decisions; the agent separately revalidates live refs and executes guarded Git commands.
