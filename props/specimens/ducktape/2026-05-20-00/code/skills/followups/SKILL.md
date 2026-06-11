---
name: followups
description: >
  Surface pending followups, natural extensions, incomplete migrations, code
  quality issues, and "what's next" suggestions. Verify session work is on disk.
  Use when wrapping up a task or session, or when user asks "what's next",
  "anything else", "what did we miss".
---

Surface pending followups, natural next steps, and quality improvements. Verify session work is on disk.

## Purpose

**"What's next?" advisor** — Proactively find natural extensions, incomplete migrations, unwired components, and quality improvements related to the session's work. Think like a colleague who sees what you just did and says "oh, and you probably also want to..."

**Save user time and cognitive load** — If there's >10-20% chance the user wants to do something, surface it for them to select with a key press. Much cheaper than having to remember/type it themselves.

**Catch loose threads** — Both agent and user loose threads: things discussed 15 minutes ago but abandoned when we pivoted, half-finished migrations, components built but not wired up.

**Verify persistence** — Double-check work done in this session is actually on disk (not stashed/reverted by parallel process).

## Process

### Phase 1: Check Session Work Status

Run `git status` and `git log --oneline origin/HEAD..HEAD` to determine whether session work is:

- **Uncommitted** — suggest committing
- **Committed but not pushed** — suggest pushing
- **Pushed but not deployed** — note if relevant (e.g., Flux reconciliation pending)

Brief output, one line per state.

### Phase 2: Extract What We Talked About But Didn't Do

**Consider delegating** conversation analysis for large sessions:

- Good for: Scanning long conversations, extracting patterns
- Can provide session log access (see Phase 1)

Scan conversation for:

- "should...", "could...", "TODO", "later", "next time"
- "maybe add...", "consider...", "might want to..."
- Incomplete actions ("let's do X" → did it actually get done?)
- Questions asked but not fully answered

**Check `/later` items**: Scan `TODO.md` files and `plans/` for items added during this session (via `/later` or manually). Surface any that haven't been addressed yet.

### Phase 3: Find Natural Followups

**Consider delegating** independent discovery tasks:

- Good for: Code pattern searches, workflow checks, cleanup scans
- Can split into distinct scopes with separate files-allowed-to-edit blocks

**Code propagation analysis:**

- If created new helper/class → search for hand-rolled equivalents
- If DRYed pattern → search for remaining duplicates
- If fixed bug → search for similar bugs
- If added validation → find sites missing it

**Workflow completion:**

- Git: Any modified files? → suggest commit message
- Tests: Did code change? → suggest test command
- Docs: Did behavior change? → check if docs updated
- Pre-commit: Any pre-commit hooks to run?

**Cleanup opportunities:**

- Dead code from this session's changes
- Newly unused imports
- Outdated comments referencing old code
- Inconsistencies introduced

### Phase 4: Natural Extensions and Incomplete Migrations

Look beyond what was explicitly discussed. Based on what the session actually changed, search for natural next steps the user might not have thought of yet.

**Incomplete migrations:**

- If the session moved field/config/pattern A→B for one instance, search for remaining instances still on A
- If the session replaced one tool/library/approach with another in one place, check if the old approach is still used elsewhere

Examples:

- "Moved `storageClass` from default to `lvm-proxmox-hdd` for grocy — 3 other PVCs still use the default class."
- "Replaced Vault/ESO with SOPS for authentik secrets — harbor and grafana still use ESO."
- "`ghcr-build-push` action now uses `github.token` directly instead of a `github_token` input — are any callers still passing the old input?"
- "Removed `master` from branch triggers in 5 workflows — any others still referencing `master`?"

**Extracted patterns not yet applied everywhere:**

- If inline logic was extracted into a shared action/helper/utility, check for remaining copies of the inline version
- "Extracted digest-pinning into `pin-image-digest` action for rbe-image and freecad-test — openclaw-image and tana-mcp-image still inline the same logic (or skip pinning entirely)."
- "Added Docker layer caching (`cache_from`/`cache_to`) to 4 workflows — openclaw-image got caching but doesn't use the shared pin action yet."

**Multi-layer version/config alignment:**

- If the session fixed a version mismatch or config inconsistency in one layer, check all other layers that might have the same issue

Examples:

- "Fixed protobuf version in nix to match Bazel gencode — pip layer and MODULE.bazel still have the old version. All three need to agree."
- "Updated Docker mTLS env vars in `py_test` macro — but the firewall doesn't allow port 2376 yet, and the test fixture isn't wired to the new env var name."

**Unwired components:**

- Session created a Dockerfile/image but no CI workflow builds it
- Session created a k8s manifest but nothing applies/reconciles it
- Session created a library/module but nothing imports it yet
- Session added a config option but no documentation mentions it
- Session added infrastructure (fixture, helper, env var) but the consumers aren't connected yet

Example: "Created Docker mTLS test fixture and env var setup — but the firewall port isn't open, and no tests actually use the fixture yet."

**Generalizations:**

- If a fix or improvement was made to one instance of a pattern, check if similar instances would benefit. Example: "Added `requires_docker = True` to one test — 4 other tests also use Docker fixtures but don't have it."
- If a new utility/helper was created, search for places that hand-roll the same logic

**Better ways to do what we're doing manually:**

- If the session used a manual/ad-hoc approach, check if there's a more standard or automated way. Example: "Passing `github_token` as an explicit input — GitHub Actions provides `github.token` automatically, no need to thread it through."

**Stale names after renames:**

- If the session renamed a class, function, or concept, check whether file names, test file names, variable names, and documentation still use the old name

Example: "Renamed `FooClass` to `BarClass` but it still lives in `foo.py` with test in `test_foo.py` — should be `bar.py` / `test_bar.py`."

**Tests must move with the code they test:**

- If session refactored config, APIs, or function signatures, check whether tests still compile and match the new interfaces. Don't leave test fixes for later commits.

Example: "Deleted `HookConfig` class and switched to standalone profile YAML — 5 test files still import `HookConfig` and pass it to `configure()`. These will break."

**Optimizations and correctness:**

- If session added functionality, are there edge cases not handled?
- If session touched performance-sensitive code, are there obvious improvements?
- If session changed data formats, are there consumers that need updating?

Surface these with enough context for the user to judge value, not just "you could also do X." Include what specifically would change and why.

### Phase 5: Prevent Recurrence Analysis

If the session involved debugging, diagnosing, or working around a problem, ask:

**Has this happened before?**

- Search recent Claude session logs for similar symptoms, error messages, or affected components
- Check `debug/` directories and `lessons_learned/` for prior investigations of the same area
- If recurring: this is a higher-priority followup — the pattern needs a structural fix, not another one-off diagnosis

**Can we prevent it from happening again?**

For each significant problem encountered, consider whether any of these would be worth the effort:

- **Pre-commit check or CI test**: Catches the problem before it ships (e.g., lint rule, validation script, regression test)
- **Automated guard**: Code-level assertion, type constraint, or invariant that makes the bad state unrepresentable
- **Better diagnostics**: More logging, metrics, or transparency that would make the _next_ occurrence faster to diagnose (e.g., structured error messages, health check endpoints, undeclared test outputs)
- **Easier workflow**: A CLI command, script, or alias that automates the manual steps we had to do (e.g., `bbapi target history --failures-only` was built this session because manual API calls were painful)
- **Documentation**: A `lessons_learned/` entry, `AGENTS.md` update, or troubleshooting section that captures the diagnosis path so future sessions don't start from scratch

Surface these as followup suggestions with concrete proposals, not vague "consider adding tests." Example:

```
B. **Add pre-commit check for unquoted URLs in pnpm lockfiles**
   - We spent time diagnosing a check-yaml failure caused by pnpm's YAML output
   - A targeted check could catch this on lockfile regeneration
```

### Phase 6: Code Quality Audit of Changed Files

**Delegate to subagent(s)** — these are read-only searches well suited for parallel execution.

**Duplicate code detection:**

- For each function/class added or substantially modified this session, search the repo for similar logic elsewhere
- Look for: copy-pasted blocks, reimplemented stdlib/library functionality, patterns that exist in shared utilities (`util/`, `mcp_infra/`, etc.) but were hand-rolled instead
- Surface as refactoring opportunities with specific file:line references

**Refactoring opportunities:**

- If a pattern was repeated 3+ times across the session's changes, suggest extracting it
- If session changes introduced a new abstraction, check whether older code could use it
- If session touched a module with known code smells (long functions, deep nesting, god classes), note the opportunity but at MAYBE priority

**STYLE.md compliance audit:**

Read `STYLE.md` and check all files modified this session against its rules. Common violations to scan for:

- Exception swallowing (`except Exception: ... = {}`)
- Unnecessary aliasing (`import foo as bar`, `x = param`)
- String forward references instead of reordering
- `model_dump()` used for logic instead of field access
- Missing `Field(description=...)` on Pydantic models (docstring listing fields instead)
- Verbose docstrings that restate the signature
- `dict` construction instead of Pydantic model construction
- `getattr`/`hasattr` usage without justification
- Grab-bag module names (`utils.py`, `constants.py`, `core.py`)
- `list` used where `set` is semantically correct
- Manual iterator patterns where `more_itertools.one()`/`first()` fits

Only flag **actual violations found in the diff**, not hypothetical ones. Include the file path, line number, the STYLE.md rule violated, and a concrete fix.

### Phase 7: Verify Suggestions Are Actionable

Before surfacing any suggestion, verify it's actually actionable right now:

- **Git push/commit**: Re-run `git status` and `git log --oneline origin/HEAD..HEAD` fresh — the user may have committed, pushed, or staged files since the last check. Don't suggest pushing if already pushed, don't suggest committing if nothing is staged/modified
- **Run tests**: Confirm the test target exists and the test runner is available
- **Code changes**: Confirm the file/function still exists and hasn't been changed by a concurrent agent
- **Cleanup**: Confirm the dead code / unused import is actually still there

Drop suggestions that fail verification. A stale or impossible suggestion wastes more attention than omitting it. If a suggestion is borderline (e.g., "bench.py might need updating" but you haven't checked), either verify it or drop it — don't surface uncertain claims as actionable items.

### Phase 8: Probabilistic Action Suggestions

For each verified action, estimate probability user wants it:

**>80% probability - DO NOW category:**

- Commit modified files (if changes were made)
- Fix breaking changes introduced
- Complete half-finished work

**40-80% probability - LIKELY category:**

- Run tests after code changes
- Propagate new pattern to obvious sites
- Update related documentation
- Push committed work

**20-40% probability - MAYBE category:**

- Add tests for new feature
- Refactor similar code
- Improve error messages
- Add logging

**10-20% probability - OPTIONAL category:**

- Performance optimizations
- Nice-to-have cleanups
- Documentation improvements for edge cases

**<10% probability - omit** (don't waste user's attention)

## Output and Interaction

### Phase output (text)

Print verification results and a brief summary of findings as text:

```markdown
## Verification

✅ All session work verified on disk

- src/feature/ (3 files modified)
- config/settings.yaml (new validation added)

## Summary

Found 3 immediate actions, 4 likely followups, 2 optional items.
```

### Action selection (AskUserQuestion)

Present followup suggestions using AskUserQuestion. Each item needs a tri-state response:

- **Yes**: Do it now.
- **Skip**: Not now, but resurface if `/followups` is called again this session.
- **No**: Don't do it, and don't suggest it again this session. Track in conversation context only — do not save to memory.

Items not explicitly addressed default to **Skip**.

**Priority indicators** (include in option descriptions):

- 🔴 = DO NOW (>80% probability)
- 🟡 = LIKELY (40-80%)
- 🟢 = MAYBE (20-40%)
- 🔵 = OPTIONAL (10-20%)

**AskUserQuestion constraints:**

- 1-4 questions per call, 2-4 options per question
- Labels: 1-5 words. Details go in description.
- Headers: max 12 chars (chip/tag)
- `multiSelect: true` allows multiple selections
- "Other" freeform option is auto-provided
- Can call the tool multiple times sequentially

**Presentation strategy**: Choose whatever interaction pattern best fits the specific suggestions being presented. Options include:

- Multi-select by topic (selected=yes, unselected=skip), then a follow-up to capture explicit "no" items
- Per-item single-select with Yes/Skip/No options (when items are few or need individual attention)
- Batched by priority, processing DO NOW items first before presenting lower-priority ones
- Multiple sequential calls to work through more items than fit in one call

Use judgment — optimize for the user making quick decisions with minimal friction.

## Implementation Requirements

### 1. Consider Delegation

Delegate read-only tasks (verification, code search, git status) to subagents
when there are multiple independent checks. Spawn in parallel when possible.

### 2. Concrete Commands

Every suggestion includes the exact command in the description:

- ✅ `git push origin devel`
- ✅ `bbr test //path/to:target`
- ❌ "consider committing changes"

### 3. Probability Calibration

- 90%: User explicitly said "do this next"
- 70%: Standard workflow step (commit after edits)
- 50%: Natural followup (tests after code change)
- 30%: Improvement opportunity (refactor similar code)
- 15%: Nice-to-have (documentation polish)
- <10%: Omit entirely

### 4. Zero False Omissions

Better to show 5 low-probability items than miss the one action user wanted.
Err on side of over-suggesting rather than under-suggesting.
