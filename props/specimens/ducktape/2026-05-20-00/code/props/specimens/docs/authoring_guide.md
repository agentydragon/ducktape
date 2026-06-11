# Instructions for Authoring Snapshot Issue Files

## Overview

This guide explains how to author issue files for code review snapshots. Snapshots are frozen code states with labeled issues used as training/evaluation data for the LLM critic.

**For the broader context** on how snapshots fit into the training strategy (per-file examples, `critic_scopes_expected_to_recall` filtering, optimization approaches), see [Training Strategy](../../docs/training_strategy.md).

## File Structure

See <../README.md> for the directory layout. Issue files use descriptive slugs (lowercase with hyphens), not numerical indices. **Prefer shorter names when meaning is preserved** — slugs should be 0-30 characters. See <format_spec.md> for canonical slug names.

## Critical: Snapshots are Frozen Code States

**Snapshots are training/evaluation data representing code quality issues at a specific commit.**

- Each snapshot is pinned to a specific commit (see provenance comments in `BUILD.bazel`)
- Issue files (`.yaml`) describe what was **wrong at that commit**
- **NEVER** update issue files to record resolution status or mark issues "COMPLETED"
- Issue files should remain accurate descriptions of problems as they existed
- Fixes happen on separate branches; snapshots remain unchanged historical records
- Think of snapshots like labeled training data: the label describes the frozen state

**Example violations:**

- Adding "Status: COMPLETED" or "Note: This was fixed in commit X"
- Updating rationale to say "This issue has been resolved"
- Removing or modifying issue descriptions after fixes are made

**Correct approach:**

- Record issues as they exist at the snapshot commit
- Fix issues on separate branches without modifying snapshot files
- Create new snapshots for new commits if you want to capture improvements

## Authoring Rules

### 1. Single Source of Truth: YAML Issue Files

**All detailed issue information belongs in `issues/*.yaml` files only.**

Each `.yaml` file contains:

- **Rationale**: Full explanation of what's wrong and why
- **File locations**: Exact paths and line ranges
- **critic_scopes_expected_to_recall** (TPs): Files required to catch the issue
- **relevant_files** (FPs): Files that make the FP relevant

**Do NOT duplicate this information in README.md or other files.**

### 2. Verify File Paths Match Bundle Structure

**File paths in issue files must match the hydrated bundle structure exactly.**

When a snapshot bundle is created with `include: [adgn/]`, the hydrated snapshot will have paths like:

- `adgn/src/adgn/agent/agent.py`
- `adgn/tests/props/test_foo.py`

**NOT** like:

- `src/adgn/agent/agent.py` ❌
- `tests/props/test_foo.py` ❌

**Verification steps:**

1. **Check the provenance comments** in `BUILD.bazel` for include/exclude paths

2. **Verify paths match** by hydrating the snapshot and listing files:

   ```bash
   adgn-properties snapshot exec <snapshot-slug> -- ls -la
   ```

3. **Use paths as they appear** in the hydrated bundle - include all directory prefixes from the `include` patterns.

**Common mistake:** Writing issue files for `ducktape/` snapshots without the `adgn/` prefix when the bundle includes `adgn/`.

### 3. Issue File Templates

**True Positive (issue that should be caught):**

```yaml
rationale: |
  Dead code should be removed. Lines 145-167 define a function
  that is never called anywhere in the codebase.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      src/cli.py:
        - [145, 167]
    # critic_scopes_expected_to_recall auto-inferred for single-file issues
```

**Multi-file issue (requires explicit critic_scopes_expected_to_recall):**

```yaml
rationale: |
  Duplicated enum definitions. Both files define the same Status enum,
  creating a maintenance burden and potential for drift.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      src/types.py:
        - [6, 10]
      src/persist.py:
        - [54, 58]
    critic_scopes_expected_to_recall:
      - [src/types.py] # Catch from either file
      - [src/persist.py]
```

**Note on `critic_scopes_expected_to_recall`:** This field specifies which minimal file sets are needed to detect the issue. It's used to generate focused training examples per-file rather than only full-snapshot reviews. See [Training Strategy](../../docs/training_strategy.md) for details on how this enables the per-file examples approach and tighter optimization feedback loops.

**Multiple occurrences (with notes):**

```yaml
rationale: |
  Imperative list building should use comprehensions. Replace
  `result = []; for x in items: result.append(f(x))` with
  `[f(x) for x in items]` for cleaner, more Pythonic code.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      src/agents.py:
        - [50, 59]
    note: "In _convert_pending_approvals()"
    critic_scopes_expected_to_recall:
      - [src/agents.py]

  - occurrence_id: occ-1
    files:
      src/bridge.py:
        - [64, 108]
    note: "In list_approvals()"
    critic_scopes_expected_to_recall:
      - [src/bridge.py]
```

**Rationale vs Occurrence Notes:**

Rationale can be specific—include file names, line numbers, concrete details. The `note` field exists to **distinguish multiple occurrences** of the same issue type.

- **Single occurrence:** Rationale contains all details. No note needed.
- **Multiple occurrences:** Rationale describes the shared pattern. Notes identify each instance.

**Single occurrence (specific rationale):**

```yaml
rationale: |
  `_build_host_config` (lines 122-130) silently ignores `opts.volumes` when it's
  a list[str], despite the type allowing it (line 54). Handle both cases or
  remove list[str] from the type.
occurrences:
  - occurrence_id: occ-0
    files:
      container_session.py:
        - [54, 54]
        - [122, 130]
```

**Multiple occurrences (notes distinguish instances):**

```yaml
rationale: |
  Multiple fields use str instead of AgentID newtype. Use proper type.
occurrences:
  - occurrence_id: occ-0
    files:
      container.py: [[45, 45]]
    note: "agent_id field in AgentContainer"
  - occurrence_id: occ-1
    files:
      registry.py: [[89, 89]]
    note: "id parameter in get_agent()"
```

**False Positive:**

```yaml
rationale: |
  Critics might flag this duplication as problematic because the button styles
  are repeated across components. However, our ground truth is that this is
  intentional for visual consistency - we want all interactive elements to
  have identical hover/active states for UX coherence.
should_flag: false
occurrences:
  - occurrence_id: occ-0
    files:
      src/Button.svelte:
        - [45, 60]
      src/Link.svelte:
        - [32, 47]
    # relevant_files auto-inferred from files keys
```

**False Positive Rationale Format:**

The rationale should explain why something that LOOKS like a problem is actually acceptable. Typical structure (not a strict template):

**Pattern:** "Critics might say X is bad because Y, but our ground truth is that it's acceptable because Z."

- **X** = What was flagged (describe the pattern critics noticed)
- **Y** = Why it looks problematic (the critic's reasoning)
- **Z** = Why it's actually fine (your reasoning: intentional choice, acceptable trade-off, makes sense with context, etc.)

The exact phrasing can vary - the key is to acknowledge what looks problematic while explaining why it's actually acceptable.

**Examples:**

- "Critics might flag this duplication as a DRY violation because the validation logic is repeated. However, our ground truth is that this is acceptable because each validation context has slightly different error handling requirements and merging them would reduce clarity."
- "Critics might say this type annotation is missing because the function signature has no return type. However, our ground truth is that it's acceptable because this is a decorator that preserves the wrapped function's type, and explicit annotation would be less accurate than the inferred type."
- "Some critics flagged this as a resource leak, but this is intentional - the handle lifetime is managed by the parent context manager which ensures cleanup in its `__exit__` method."

### 4. Detection Standard for `critic_scopes_expected_to_recall`

See format_spec.md for line range formats and auto-inference rules.

**The key question:** "If I gave a high-quality critic this file set to review, and they failed to find this issue, would that be a failure on their part?"

**What "reviewing a file" includes:**

- Reading the file thoroughly line by line
- Following imports and calls to check APIs
- Searching the codebase for existing helpers/patterns
- Looking for duplication or similar patterns
- All normal thorough code review activities

**What it does NOT mean:**

- "Can you detect this reading ONLY these files in complete isolation?"
- "Without following any imports or doing any searches?"

**Examples:**

**Example 1: Test creating `Compositor("comp")` directly**

- File: `test_notifications.py` with `comp = Compositor("comp")`
- Question: "Should this use a pytest fixture?"
- Expected action: Critic searches for existing fixtures and patterns
- Result: Finds 12 other instances, flags duplication
- `critic_scopes_expected_to_recall: [[test_notifications.py]]` ✓

**Example 2: Wrapper calling implementation with silent fallback**

- Files: `cli.py` (wrapper) and `local_tools.py` (implementation with fallback logic)
- From `cli.py` alone: See wrapper name, but not fallback behavior
- From `local_tools.py` alone: See the silent fallback directly
- Result: Only detectable from implementation file
- `critic_scopes_expected_to_recall: [[local_tools.py]]` ✓

**Example 3: Unused CLI flag**

- File: `cli.py` defines `--ui-port` flag with logs saying "Management UI available"
- Question: "Does this flag actually work?"
- Expected action: Critic traces code to verify the flag is properly wired up
- Result: Discovers server serves only stubs, flag misleads users
- `critic_scopes_expected_to_recall: [[cli.py]]` ✓

**Example 4: Cross-file duplication of enum definitions**

- Files: `types.py` and `persist.py` both define same enum
- From either file alone: Cannot detect duplication (only see one instance)
- Need both files: See duplicate definitions
- `critic_scopes_expected_to_recall: [[types.py, persist.py]]` ✓ (AND logic)

**General principle: Include problem code, not reference/solution code**

When an issue is about "absence of use" or "should use existing X":

- **Include:** Code that needs to change (the violators/problems)
- **Don't include:** Code that's already correct and just needs to be used/referenced

Examples:

- Tests not using fixtures → Include test files, not conftest
- Code not using helper function → Include duplicated code, not the helper module
- Code not using base class → Include implementations, not the base class
- Code duplicating utility logic → Include duplicators, not the utility module
- Code reinventing stdlib feature → Include reinvention, not stdlib
- Code with hardcoded values → Include literals, not the constants file
- Code not following pattern → Include non-conforming code, not the exemplar
- Code not calling cleanup → Include leaky code, not the cleanup util

**Exceptions - include the "solution" file when:**

- It itself has a problem (broken/misleading API, incomplete implementation, missing docs)
- The issue is about improving the solution, not just using it
- **Internal contradiction:** File's docstring/comments promise something the code doesn't deliver
  - Example: `server.py` docstring says "provides Management UI" but code only has stubs
  - The file IS the problem (broken promise), not just "unused affordance"
  - Contrast with: Tests not using `server.py` fixture → fixture is fine, tests are the problem

**Dead code with potential callers:**

Dead code issues have two possible fixes: (1) delete the dead code, or (2) wire it up where it should be used. When existing code _should_ be calling the dead helper (i.e., there's duplicated/manual logic that the helper would simplify), the dead code is also detectable from those caller files — a reviewer seeing the manual logic would search for existing helpers and discover the unused one.

- Dead helper with no plausible callers → detect from the dead file only
- Dead helper that would DRY up existing code → detect from both the dead file AND files that should call it
- `match_file_restriction` still includes only the dead file (the issue _is_ in the dead file), but `critic_scopes_expected_to_recall` can include caller files as alternative detection scopes

When the plausible fix is "wire it up," note this in the occurrence's `note` field so graders understand the dual-fix nature (e.g., "Dead helper; `cli.py` lines 80-95 duplicate this logic and should call it instead").

### 5. Setting `match_file_restriction` (Optional)

**Purpose:** This field is a grading optimization. When set, critiques that report issues only in files OUTSIDE this set are skipped during matching (assumed non-match without semantic comparison).

**Relationship to `critic_scopes_expected_to_recall`:**

- `critic_scopes_expected_to_recall` = where the issue can be DETECTED from (training signal)
- `match_file_restriction` = where the issue can be validly REPORTED (grading optimization)

These are independent. An issue detectable from file A might be validly reported in files A, B, or C.

**When to set it:**

- Single-file issues where the problem is fully contained → set to that file
- Issues that can only be described from specific files → set to those files

**When to leave it NULL (omit):**

- Cross-cutting issues that could be reported from many files
- When unsure about the complete set of valid reporting locations
- Issues with dual framing (e.g., "X calls missing method" vs "Y is missing method callers expect")

**Validation test:** Can you produce a valid critique phrasing that accurately describes this issue but tags a file outside the set?

- **If yes** → the set is too narrow, expand it or use NULL
- **If no** → the set is safe to use

**Example - safe to set:**

```yaml
# Issue: Useless docstring that restates the function signature
# Can only be reported from the file containing the docstring
match_file_restriction:
  - src/persist.py
```

**Example - must include multiple files or use NULL:**

```yaml
# Issue: agents.py calls agent.abort() which doesn't exist
# Valid framings:
#   - "agents.py calls nonexistent method" (tag agents.py)
#   - "agent.py missing abort() that callers expect" (tag agent.py)
match_file_restriction:
  - src/agents.py
  - src/agent.py
```

**Antipattern - splitting producer/consumer issues:**

Don't split a single logical issue into separate occurrences by file with narrow `match_file_restriction`. Example of what NOT to do:

```yaml
# WRONG: Split into two occurrences with narrow sets
occurrences:
  - occurrence_id: occ-0
    files:
      runtime.py:
        - [249, 253] # passes hardcoded False
    match_file_restriction: [runtime.py] # TOO NARROW
  - occurrence_id: occ-1
    files:
      status_shared.py:
        - [42, 56] # has unreachable code
    match_file_restriction: [status_shared.py] # TOO NARROW
```

This fails because a critique like "status_shared.py has dead code because runtime.py passes False" could validly tag either file. Instead, merge into one occurrence:

```yaml
# CORRECT: Single occurrence with both files
occurrences:
  - occurrence_id: occ-0
    files:
      runtime.py:
        - [249, 253]
      status_shared.py:
        - [42, 56]
    match_file_restriction:
      - runtime.py
      - status_shared.py
```

See @only_matchable_labels.md for more labeled examples.

### 6. Issue Organization: Logical Problems, Not Locations

**CRITICAL PRINCIPLE: Group by LOGICAL ISSUE, not by location.**

Each issue file should describe ONE logical problem type, which may occur in multiple locations:

**CORRECT - One logical issue:**

- "Trivial alias functions that should be inlined" -> lists 5 occurrences across different files
- "Imports not at top of file" -> lists 8 occurrences in different components
- "Dead code that should be removed" -> lists all unused functions

**WRONG - One location:**

- "Problems in ServersPanel.svelte" -> mixing thin wrappers + manual parsing + duplicate styles
- "Issues in app.py lines 100-200" -> mixing type annotations + dead code + useless comments

**Issue organization rules:**

1. **One logical problem** = one issue file (may have N occurrences)
2. **Multiple problems in one location** = separate issue files (one per problem type)
3. **Same problem across locations** = single issue with multiple occurrences
4. **Different problems** = separate issues even if in adjacent lines

### 7. Rationale Detail: Calibrate to Obviousness

**The amount of explanation needed depends on how obvious the improvement is.**

#### Obvious Pareto Improvements (Minimal Rationale)

When the issue is an unambiguous improvement with no cost, just state what's wrong. Don't explain why the fix is better—any experienced engineer knows.

**Heuristics for "obviously better" (all else equal):**

- Less code > more code
- More type-safe > less type-safe
- Early errors > delayed/obscured errors
- Typed > untyped
- Less complex > more complex
- Specific types (`Path`, `AgentID`) > generic (`str`, `dict[str, Any]`)
- Readable > obscure
- Less magic > more magic (dunders, getattr, reflection)

**Examples of minimal rationales:**

```yaml
# Dead code - obvious
rationale: |
  `format_legacy_output()` is never called. Remove it.

# Wrong type - obvious
rationale: |
  Line 197 types `_policy_gateway` as `Any | None` with a comment saying
  it's `PolicyGatewayMiddleware`. Use the proper type annotation.

# Stdlib replacement - obvious
rationale: |
  Lines 45-52 manually split on first occurrence of "=". Use `str.partition()`.

# Trivial inline - obvious
rationale: |
  `get_name()` just returns `self._name`. Inline it.

# Duplicate code - obvious
rationale: |
  Lines 67-100 and 108-135 duplicate identical logic. Extract a helper.
```

#### Non-Obvious Issues (Substantive Rationale)

When reasonable engineers might disagree, or there's a tradeoff, explain your reasoning:

**When more detail is needed:**

- **Complex tradeoffs**: "This uses algorithm X but we have n<100 in-memory, so simpler O(n²) is fine"
- **Cross-cutting design**: "API here inconsistent with API there"
- **Judgment calls**: "Is this 15-line type-safe pattern worth the complexity vs simpler but less safe approach?"
- **Non-obvious costs**: "This looks like deduplication but would create tight coupling"
- **Context-dependent**: "This pattern is usually fine but problematic here because..."

**Examples of substantive rationales:**

```yaml
# Tradeoff requiring explanation
rationale: |
  Lines 200-250 use a trie for prefix matching. The dataset has <50 entries
  loaded once at startup. A simple list scan is 30 lines shorter, equally
  fast for this size, and easier to debug. The trie adds complexity without
  measurable benefit.

# Design inconsistency requiring context
rationale: |
  `AgentContainer.get_agent()` returns `Agent | None` but `Registry.get_agent()`
  raises `KeyError`. Callers must know which they're using. Pick one pattern
  (preferably Optional return since it's the newer code).

# Judgment call with reasoning
rationale: |
  The `TypedDict` with 12 optional fields (lines 45-70) duplicates the
  Pydantic model. Critics might suggest deduplicating. However, keeping them
  separate is intentional: the TypedDict is the wire format, the model adds
  validation. They may diverge as the API evolves.
```

#### The Key Test

**Can you delete the "why it's better" explanation and the issue is still clear?**

- Yes → it's obvious, keep it minimal
- No → it needs substantiation

### 8. Issues Must Be Standalone

**Never reference other issues by name, ID, or number.** Each issue file must be understandable in isolation—assume readers cannot see other issues in the snapshot.

**Wrong:**

```yaml
rationale: |
  This is related to Issue 037 (notifier pattern bugs). See also Issue 038.
```

**Wrong:**

```yaml
rationale: |
  **Related:** Issues 036, 037, 038. All solved by compositor pattern.
```

**Right:** If context from another issue is relevant, incorporate the necessary details directly:

```yaml
rationale: |
  ApprovalPolicyEngine has only ONE notifier slot. When agents.py:855 wires its
  notifier, it replaces any previously-wired notifier, breaking notifications.
```

### 9. Objectivity in Issue Descriptions

**Avoid subjective phrasing** - describe problems objectively:

**Wrong:**

- "User mentioned 'pretty mechanism for parsing Pydantic models'"
- "This is a nice pattern"
- "Would be better to..."

**Correct:**

- "Manual `isinstance()` validation instead of Pydantic `TypeAdapter`"
- "This pattern duplicates validation logic"
- "Use `TypeAdapter` for automatic validation"

Present facts and technical rationale, not opinions or attributed suggestions.

### 10. Research First: No Open Questions

**Snapshots must not leave open research questions.** All investigation should be completed before authoring the issue.

**WRONG - Leaving research questions open:**

```yaml
rationale: |
  Lines 700-704 manually discover the git directory. Check if `pygit2.Repository()`
  can discover automatically.

  **Investigation needed:** Check if either of these works...
```

**CORRECT - Research completed, findings documented:**

```yaml
rationale: |
  Lines 700-704 manually discover the git directory using `pygit2.discover_repository()`.
  Per pygit2 docs, `Repository()` accepts a path and auto-discovers the .git directory,
  making manual discovery unnecessary.
```

### 11. Verifiable External References

**When referencing specific tools, APIs, or implementation details, provide verifiable links. Well-known frameworks/standards don't need URLs.**

**DO need URLs:**

- Specific tools/packages: npm packages, PyPI packages, CLI tools
- APIs and library methods: Specific API endpoints, method documentation
- Commit references: Full 40-character SHAs or GitHub/GitLab permalinks

**DON'T need URLs:**

- Common frameworks: React, Vue, Angular, Tailwind CSS
- Standard libraries: Python stdlib, Node.js core modules
- Well-known tools: pytest, Jest, Docker, PostgreSQL

### 12. Code Citation Guidelines

**IMPORTANT**: Do NOT include long code blocks in rationale. Readers have snapshot code open - cite file paths and line ranges, briefly summarize what's there.

- Brief summary: "Button styles duplicated across 6 components (AgentsSidebar lines 355-360, GlobalApprovalsList lines 118-146, etc.)"
- Short example (3-5 lines) when illustrating pattern
- Avoid long blocks (10+ lines) copied from source
- Assume reader can look up exact code at cited lines

**Per-range context:** Notes are supported at the occurrence level. For per-range context within an occurrence, use YAML comments:

```yaml
files:
  file.py:
    - [10, 20] # definition site
    - [30, 40] # call site
```

Note: These comments help human readers but aren't parsed into the data model.

@quality_checklist.md

## Why This Structure?

1. **DRY**: One authoritative description per issue (in YAML)
2. **Tooling-friendly**: YAML is machine-readable for analysis tools
3. **Human-friendly**: YAML provides full detail in a structured format
4. **Maintainable**: Updates happen in one place only
5. **Composable**: Tools can combine/aggregate issues from multiple snapshots
