---
name: branch-splitter
description: Split a large branch with many changes into independent, reviewable PRs. Use when preparing a messy development branch for code review, when asked to "split this into PRs", "make this reviewable", "break this up", or when a branch does too many unrelated things. Produces a DAG of branches/PRs that can be reviewed and merged independently.
---

# Branch Splitter

Transform a large, messy branch into a DAG of independent, reviewable PRs. Think like an expert software engineer preparing work for smooth code review and trunk merge.

## 1. Goals

Split a branch so that:

- Each PR does one logical thing and can be reviewed in isolation
- PRs can be merged in any valid topological order without conflicts
- The union of all PR diffs exactly equals the original branch diff (nothing lost, nothing added)
- No PR introduces regressions beyond what already exists on the base branch

## 2. Constraints

These properties must hold for every split PR:

1. **Atomic changes** — Each PR does one logical thing
2. **No dead code** — If a PR adds code, it must be used in that same PR (or already used in base + earlier PRs in the DAG)
3. **No orphaned deletions** — If a PR removes the last user of code, delete that code too
4. **Sound intermediate states** — After merging any valid prefix of the DAG, the codebase must build/pass tests. The final state is identical to the original branch (content invariant), but every intermediate state along the way must also be healthy
5. **Truthful documentation** — If behavior changes, docs must change in the same PR
6. **Documentation claims must be true** — If docs claim something is done (✅, "complete", "implemented"), the implementation must exist in or before that PR in the DAG. See Documentation Consistency in the Cookbook
7. **Tests with implementation** — If tests exist in the source branch, they accompany their implementation
8. **Complete coverage** — The union of all split PRs MUST equal the original branch diff (validated programmatically)

### Faithful Decomposition

The split is a **decomposition**, not an improvement pass. The content invariant requires the union of all PRs to exactly equal the original branch diff — nothing added, nothing removed. If the original branch adds dead code, has style issues, or lacks tests, the split will faithfully reproduce those properties. Constraints 2–3 above apply relative to what the original branch does: if the original adds function `foo()` in file A and calls it in file B, then the PR containing file A must also contain file B (or depend on a PR that does). But if the original adds `foo()` and never calls it, the split PR that adds `foo()` will also add dead code — that's faithful reproduction, not a defect of the split.

### De Novo Splitting

**Critical**: Treat the original branch as a single monolithic diff to split de novo. Do NOT slavishly follow the original commit structure.

- **Ignore original commits**: Only the final diff matters
- **Split within commits**: A single original commit may become parts of multiple PRs
- **Combine across commits**: Changes from multiple original commits may combine into one PR
- **Extract sub-patches**: If commit X changes files A, B, C but only A is independent, extract just the A changes

Example — original branch has 3 commits:

```
commit 1: "Refactor auth + update STYLE.md"    → auth.py (200 lines), STYLE.md (2 lines)
commit 2: "Add feature + tests"                → feature.py (100 lines), test_feature.py (50 lines)
commit 3: "Fix bug in auth"                    → auth.py (10 lines)
```

Correct split (by logical grouping, not commit history):

```
PR1: STYLE.md changes only (2 lines from commit 1)
PR2: auth.py refactor (200 lines from commit 1 + 10 lines from commit 3)
PR3: feature.py + test_feature.py (from commit 2)
```

### Splitting a File Across PRs

When the original branch makes logically independent changes to the same file, those changes may belong in different PRs. For example, `models.py` might have both a new enum value (for feature A) and a renamed field (for refactor B).

To split: include the file in both PRs, but apply only the relevant hunks in each. The planning phase must identify which hunks of each file belong to which PR and provide them to the extraction subagent. Git can auto-merge independent edits to the same file (non-overlapping, non-adjacent hunks), so same-file edits don't automatically require a DAG edge. Only add an edge when the hunks overlap or are adjacent enough to cause merge conflicts.

## 3. Output Artifacts

The skill produces:

1. **Branch set** — Named branches pushed to remote, ready for PR creation
2. **DAG description** — JSON file mapping branch names to their dependencies (input for the validation script)
3. **Validation report** — Script output showing all orderings tested and content invariant verified
4. **PR descriptions** — Draft text for each PR
5. **Merge guide** — Recommended merge order and notes

Every branch must have a clear, specific scope describable in one sentence. If you can't precisely describe what a branch does, you haven't finished analyzing it.

## 4. Order of Operations

### Phase 1: Analysis (Parallel Subagents)

Launch subagents to analyze the source branch concurrently:

| Subagent | Task                                                                                 |
| -------- | ------------------------------------------------------------------------------------ |
| 1        | Identify all modified files, group by subsystem                                      |
| 2        | Find pure refactors (renames, moves, style fixes)                                    |
| 3        | Find documentation-only changes                                                      |
| 4        | Find test-only changes (new tests for existing code)                                 |
| 5        | Identify schema/migration changes and their dependents                               |
| 6        | Map import/dependency relationships between changes                                  |
| 7        | Scan documentation for completion claims (see Documentation Consistency in Cookbook) |

Each subagent produces a report without making file edits.

### Phase 2: Planning

Synthesize subagent reports into a split plan:

1. **Identify independent atoms**: style/lint fixes, documentation, refactors, dependency updates, new tests for existing code
2. **Identify dependent clusters**: feature + its tests, schema change + code using it, API change + callers
3. **Process documentation claims** (from Subagent 7): for each claim marked "complete/done", identify the implementing code and add a DAG edge so the docs PR depends on the implementation PR
4. **Build the DAG**: independent atoms become leaves, dependent clusters form chains, documentation claims add edges
5. **Check for transitional needs**: does splitting require intermediate states not in the original? Document any added transitional commits

### User Review Checkpoint

Present the proposed DAG to the user before starting extraction. Include: branch names with one-sentence scope descriptions, dependency edges with rationale, and any known tight couplings. Extraction is expensive (worktrees, subagents, cherry-picks) — get agreement on the split boundaries first.

### Phase 3: Extraction (Parallel Subagents in Worktrees)

Use separate git worktrees for each PR branch to avoid conflicts:

```bash
git worktree add ../split-pr1 -b pr1-style-fixes origin/main
git worktree add ../split-pr2 -b pr2-refactor-x origin/main
git worktree add ../split-pr3 -b pr3-feature-a pr1-style-fixes  # depends on PR1
```

After creating each worktree, install commit hooks (`pre-commit install` or equivalent).

Assign each worktree to a subagent. Each extraction subagent's prompt must include:

- **Branch scope**: one-sentence description of what this PR does
- **File list with hunks**: the specific changes from the original diff that belong in this PR (file paths + relevant diff hunks, not just file names — a file may be split across PRs)
- **Base state**: what branch/commit the worktree is based on (base branch for leaves, dependency branch for non-leaves)
- **Dependencies**: which other PR branches this one builds on, so the subagent understands what already exists
- **Baseline failures**: known pre-existing test/lint failures to ignore

Subagents must:

- Only modify files in their assigned worktree
- Commit with clear messages referencing original commits
- Verify no new failures introduced (compare against baseline)
- Report any conflicts or issues

**Save each subagent's agent ID** for later resumption during maintenance or querying.

### Phase 4: Validation

Run validation in this order:

1. **Establish baseline** on base branch (record pre-existing test/lint failures)
2. **Individual PR validation** — launch validation subagents in parallel, one per branch. Each runs build+test and compares against baseline
3. **DAG order validation** — run `validate-dag-split.py` to test all valid topological orderings (merges, tests, content invariant)
4. **Manual consistency checks** — dead code, orphaned deletions, docs matching behavior, tests accompanying implementation

If validation fails, iterate (see Handling Validation Failures in the Cookbook).

### Phase 5: Documentation

Produce a summary: DAG diagram (mermaid), per-PR descriptions, validation results, pre-existing issues, recommended merge order.

## 5. Validation Script

The split must be validated by `validate-dag-split.py` in this skill directory. Copy it to your repo and customize as needed.

### What It Validates

1. **Merge cleanliness** — All branches merge without conflicts in every valid DAG ordering
2. **No regressions** — No new test failures compared to baseline (skip with `--skip-tests`)
3. **Content invariant** — The union of all PR diffs exactly equals the original branch diff

### DAG Input Format

```json
{
  "base": "origin/devel",
  "original_branch": "origin/claude/my-feature-branch",
  "test_command": "bazel test //...",
  "build_command": "bazel build --config=check //...",
  "branches": {
    "pr1-style-fixes": [],
    "pr2-refactor-auth": [],
    "pr3-feature-a": ["pr1-style-fixes", "pr2-refactor-auth"],
    "pr4-feature-b": ["pr2-refactor-auth"],
    "pr5-integration": ["pr3-feature-a", "pr4-feature-b"]
  }
}
```

**Required fields**: `base`, `original_branch`, `branches` (map of branch name → list of dependency branch names).

**Optional fields**: `test_command` (default: `true` = skip), `build_command` (default: `true` = skip).

### Running

```bash
./validate-dag-split.py dag.json              # Full validation with tests
./validate-dag-split.py dag.json --skip-tests # Just merges + content invariant
```

### Content Invariant Failures

If the content invariant fails, the script reports which file diffs are in the original but missing from the split. Add the missing file changes to the appropriate PR branch.

### Large DAGs

For DAGs with many valid orderings, sample instead of exhaustive testing: canonical ordering, reverse, random samples (10–100), and orderings maximizing parallel merges.

## 6. Subagent Usage

Use subagents liberally — they enable parallelism and preserve context.

### Coordination Rules

1. **Worktree isolation** — Each subagent works in a separate git worktree
2. **Shared files may need DAG edges** — Multiple PRs can edit the same file. Git auto-merges non-overlapping, non-adjacent hunks, so same-file edits don't automatically require ordering. Add a DAG edge only when edits overlap or are adjacent enough to cause merge conflicts
3. **No shared state** — Subagents communicate via reports, not shared files
4. **Sequential git ops** — Only one agent commits to a branch at a time

### Communication Pattern

```
Main Agent
│
├── Phase 1: Spawn Analysis Subagents (parallel, read-only)
│   ├── Analyzer 1–7 → Reports
│
├── Phase 2: Synthesize Plan (main agent)
│
├── Phase 3: Spawn Extraction Subagents (parallel, separate worktrees)
│   ├── Extractor 1 → Branch 1  [save agent ID]
│   ├── Extractor 2 → Branch 2  [save agent ID]
│   └── Extractor N → Branch N  [save agent ID]
│
├── Phase 4: Spawn Validation Subagents (parallel per PR)
│   ├── Validator PR1 → Pass/Fail  [save agent ID]
│   └── Validator PRN → Pass/Fail  [save agent ID]
│
└── Phase 5: Generate Documentation (main agent)
```

### Subagent Resumption

**Save agent IDs** when spawning extraction and validation subagents. When maintenance is needed later, resume the original subagent rather than spawning a fresh one — it retains full context of what was done, conflicts resolved, and design decisions made.

Track IDs in a map:

```json
{
  "pr1-style-fixes": { "extractor": "abc123", "validator": "def456" },
  "pr2-refactor": { "extractor": "ghi789", "validator": "jkl012" }
}
```

### Querying Branch Subagents

Resume branch subagents to ask analytical questions cheaply (they already have full context):

- **Splittability**: "Does this branch have pieces that would make sense to extract separately?"
- **Claim verification**: "If merged onto devel, would this add documentation claims not substantiated by code in this branch?"
- **Style/quality**: "Does this branch introduce style violations or dead code?"
- **Dependency analysis**: "What files does this branch touch that other branches might also touch?"

Run these queries in parallel across all branch subagents to surface issues early, before the full validation script.

```
# Example: parallel claim verification
Resume PR1 subagent: "Would merging this add unsubstantiated claims?"
Resume PR2 subagent: "Would merging this add unsubstantiated claims?"
Resume PR3 subagent: "Would merging this add unsubstantiated claims?"
# → PR3 reports: "Yes - docs claim get_db was renamed, but rename is in PR2"
# → Fix: add PR2 as dependency of PR3 in DAG
```

## 7. Admonitions

Things to watch out for that are **not automatically checked** by the validation script:

### Verify Branch Diffs After Extraction

After applying changes to a branch — whether by bulk `git checkout`, cherry-pick, or hunk extraction — **review the actual diff of the branch before committing**. Do not trust that the extraction was correct just because the right files were targeted.

Specifically: run `git diff origin/devel...HEAD` on each branch and read every hunk. Verify each hunk belongs to this branch's stated scope. A bulk `git checkout <commit> -- <file>` grabs the entire file, which silently includes changes from other logical groups when a file is shared across PRs. This is the most common source of leaked changes.

Common failure mode: file `client.ts` has both an enum value change (group A) and an API endpoint change (group B). Checking out the whole file from the main branch puts both changes into whichever branch runs first. The API endpoint change then silently disappears from group B's branch during rebase because it already exists in the base. **Result: one branch has a change it shouldn't, and another branch is missing a change it needs.**

### No Catchall "Misc" Branches

Every branch must have a precisely described, narrow scope. A split that produces "PR1: style fixes, PR2: refactor auth, PR3: everything else" is a failed split — "everything else" means you stopped analyzing. If a branch's description is vague ("remaining changes", "misc updates", "other fixes"), break it down further. The whole point of splitting is that each piece is independently reviewable; a grab-bag branch defeats this entirely. When two changes seem inseparable, explain the specific coupling (e.g., "migration adds column that this query requires") rather than lumping them into a catch-all.

**Recursive decomposition**: After extracting the obvious independent atoms (style fixes, docs, single-concern refactors), don't treat the remainder as one branch. Apply the same splitting analysis to the remaining cluster: what distinct logical changes does it contain? A 90-file "DB refactoring" branch that also adds new API routes, restructures dependency injection, changes test fixtures, and introduces a new config system is really 3–4 branches wearing a trench coat. Each sub-concern should be nameable in one sentence; if you need "and" to describe a branch, it's probably two branches.

### Dead Code and Orphaned Deletions

The validation script checks merge cleanliness and content invariants, but not symbol usage. Manually verify:

- Every function/class/constant added in a PR is used in that same PR
- If the last user of code is removed in a PR, the code itself is also removed

### Documentation Claims

The validation script doesn't parse documentation for truthfulness. When a PR contains docs with progress markers (✅, "done", "complete"), verify the implementation exists in the same PR or an earlier one in the DAG. Use Subagent 7's report and branch subagent queries to catch these.

### Style Consistency

Splitting can introduce subtle style issues: imports reordered differently across PRs, inconsistent naming if a rename is split across PRs, etc. Run the repo's linter on each PR branch.

### When NOT to Split

Some branches shouldn't be split:

- Single atomic change (already reviewable)
- Tightly coupled changes where split adds no review value
- Time-sensitive fixes where review speed matters more
- When the "split" would be artificial (one PR = rename, next PR = use new name)

Document why splitting wasn't done.

## 8. Cookbook

### Imperfect Baselines

Real codebases have pre-existing issues. Before splitting, record the base branch's health:

```bash
git checkout origin/devel
bazel test //... 2>&1 | tee baseline-test-results.txt
bazel build --config=check //... 2>&1 | tee baseline-lint-results.txt
grep "FAILED" baseline-test-results.txt > known-failures.txt
```

For each split PR: must pass everything that passed on base, allowed to fail on pre-existing failures, must not introduce new failures.

When baseline has issues, document them in PR descriptions:

```markdown
## Known Issues (Pre-existing)

- `//foo:test_bar` — Flaky, fails ~10% of runs
- `//baz:lint` — Has 3 pre-existing lint warnings

This PR does not make these worse.
```

### Documentation Consistency

Documentation that claims something is done must not be mergeable before the implementation.

**The problem**: A plan document says "Phase 1: Renamed get_db to get_admin_db ✅" in branch `pr-cred-plan`, but the actual rename is in `pr-db-refactor`. Merging docs first commits a false claim.

**Solutions** (in order of preference):

1. **DAG constraint**: Make `pr-cred-plan` depend on `pr-db-refactor`
2. **Intermediate states**: Split docs into "planned" and "complete" versions in separate PRs
3. **Combined PR**: Keep tightly coupled docs and implementation together

**Detection**: Subagent 7 scans for progress markers (✅, "done", "complete", "implemented"), claims about specific code ("renamed X to Y", "added function Z"), and status tables. For each claim, it reports what code changes are required and whether they exist in the branch.

### Tightly Coupled Changes

When two changes seem inseparable, identify the specific coupling. Common cases:

- **Schema + code**: Migration adds a column that new code queries — keep in one PR, or split as migration PR → code PR chain
- **Rename + callers**: A function rename and all its call site updates must be in one PR (splitting as "rename" then "use new name" is artificial)
- **Interface + implementation**: A new interface/protocol and its first implementation often belong together

The key distinction: "these are coupled because X depends on Y at the type/schema level" is a valid reason to keep changes together. "I couldn't figure out how to separate these" is not — analyze harder or ask a branch subagent for splittability analysis.

### Schema Migrations

Database migrations create dependencies:

1. Migration must come before code using the new schema
2. Consider: migration PR → code PR chain
3. Or: combined PR if separation adds no review value

### Circular Dependencies

If analysis reveals circular deps:

1. Identify the cycle
2. Find a cut point (what can be split first?)
3. May need a transitional state (interface, feature flag)
4. Document the resolution

### Missing Tests

If original branch has implementation without tests:

1. Note in PR description: "Tests to be added in follow-up"
2. Or: add tests in same PR if scope is reasonable

### Handling Validation Failures

Validation failures are expected during initial splitting. Iterate until all orderings pass.

**Merge conflict in some orderings** → The DAG is missing a dependency edge. Add an edge so the conflicting PR comes after the one it conflicts with.

**Test failure in some orderings** → A test depends on code from another PR not yet merged in that ordering. Add a dependency edge, or move the test to the PR containing the code it tests.

**Test failure in all orderings** → The split introduced a bug or a PR is missing necessary changes. Check for incomplete cherry-picks or accidentally omitted files.

**Content invariant mismatch** → The union of PRs doesn't equal the original diff. Find commits not assigned to any PR, or cherry-picks resolved differently than the original.

Iteration loop:

```
while validation fails:
    1. Run validation script
    2. Identify failure type (conflict, test, invariant)
    3. Apply fix (add edge, move test, fix PR content, find missing changes)
    4. Update branches, re-run validation
```

When iteration reveals fundamental issues (too many edges needed, circular dependencies, test coverage gaps), reconsider the split boundaries. Try different cut points, merge specific PRs that have proven coupling, or ask branch subagents for splittability analysis. Do not fall back to a vague catchall branch — if two things must be together, name the specific coupling.

### Ongoing Maintenance

Splits are not one-time operations. Maintenance is needed when:

- A split branch is merged → rebase remaining branches on updated base, remove merged branch from DAG
- User requests edits to the original → propagate to affected split branches
- Base branch moves forward → rebase all split branches
- New changes are added → fit into existing branches or create new ones

**Use subagent resumption** — resume the original extractor/validator subagents rather than spawning fresh ones. They retain context about the branch history, conflict resolutions, and design decisions.

After any maintenance operation, update `dag.json` (remove merged branches, adjust dependencies) and re-run validation.

## Activation Triggers

Use this skill when:

- User says "split this into PRs", "make this reviewable", "this branch is too big"
- Branch has > 500 lines changed across > 10 files
- Branch touches > 3 unrelated subsystems
