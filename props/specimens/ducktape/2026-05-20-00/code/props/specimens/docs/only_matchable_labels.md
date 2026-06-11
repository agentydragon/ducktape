# match_file_restriction Labeling Dataset

Purpose: Human-labeled examples to train pattern recognition for safe `match_file_restriction` assignment.

## Key Semantic Distinction

**`critic_scopes_expected_to_recall`**: TRAINING SIGNAL. Some known files such that IF a critic is shown these files, THEN we want it to catch this issue. NOT exhaustive - does not enumerate all possible detection sources.

**`match_file_restriction`**: GRADING OPTIMIZATION. Restricts which critique outputs can match this occurrence. If set, a critique reporting issues only in files OUTSIDE this set will be skipped during matching (assumed non-match without semantic comparison).

- **NULL** = allow matching from any file. Conservative default when we haven't determined the closed set, OR for genuinely cross-cutting issues.
- **Non-empty set (≥1 file)** = we know the closed set; skip matching if critique's files don't overlap.
- **Empty set** = INVALID. Not allowed.

These are independent concepts:

- An issue might be detectable from file A (`critic_scopes_expected_to_recall: [[A]]`)
- But once detected, it could be validly reported in files A, B, or C (`match_file_restriction: [A, B, C]`)

## Validation Test

**To check if a proposed `match_file_restriction` value is correct:**

Can you produce a valid critique phrasing that accurately describes this issue but tags a file outside the set?

- **If yes** → the set is too narrow (see Negative Examples)
- **If no** → the set is safe to use (see Positive Examples)

## Positive Examples (OK to set)

### 1. redundant-compositor-names / occ-1

- **File:** `ducktape/2025-12-04-00/issues/redundant-compositor-names.yaml`
- **files:** `adgn/src/adgn/agent/mcp_bridge/compositor_factory.py: 45`
- **critic_scopes_expected_to_recall:** `[[compositor_factory.py]]`
- **Issue:** Instantiating `Compositor("global")` with explicit name when default would suffice
- **Proposed value:** `[adgn/src/adgn/agent/mcp_bridge/compositor_factory.py]`

### 2. state-in-separate-field / occ-0

- **File:** `ducktape/2025-12-04-00/issues/state-in-separate-field.yaml`
- **files:** `adgn/src/adgn/mcp/compositor/server.py` (lines 89, 306-307, 314)
- **critic_scopes_expected_to_recall:** `[[server.py]]`
- **Issue:** Pinned server tracking uses separate `_pinned_servers` set instead of `pinned: bool` field in `_MountState`
- **Proposed value:** `[adgn/src/adgn/mcp/compositor/server.py]`
- **Reasoning:** `_MountState` exists in the same server.py. Someone reading just this file has sufficient context to verify. Error is fully contained to this file.

### 3. unnecessary-cyclic-dependency / occ-0

- **File:** `gmail-archiver/2025-12-17-00/issues/unnecessary-cyclic-dependency.yaml`
- **files:** `gmail_archiver/core.py` (lines 57-58, 68-69)
- **critic_scopes_expected_to_recall:** `[[core.py]]`
- **Issue:** `PlannedAction` and `Plan` reference `Planner` type but only use `planner.name`. Creates cyclic dependency.
- **Proposed value:** `[gmail_archiver/core.py]`
- **Reasoning:** PlannedAction, Plan, and Action all exist in this file. The cyclical dependency is fully realized within this file. No other file hits it.

### 4. mcp-tool-comments-not-descriptions / occ-1

- **File:** `ducktape/2025-12-04-00/issues/mcp-tool-comments-not-descriptions.yaml`
- **files:** `adgn/src/adgn/mcp/exec/seatbelt.py` (lines 52-53)
- **critic_scopes_expected_to_recall:** `[[seatbelt.py]]`
- **Issue:** Comment explaining `env` field semantics on MCP tool input model should be `Field(description="...")` instead
- **Proposed value:** `[adgn/src/adgn/mcp/exec/seatbelt.py]`
- **Reasoning:** This issue could not be found by anyone not looking at the source code of this class.

### 5. inline-oneoff-command / occ-0

- **File:** `ducktape/2025-09-03-00/issues/inline-oneoff-command.yaml`
- **files:** `llm/adgn_llm/src/adgn_llm/mini_codex/mcp_manager.py` (lines 61-71)
- **critic_scopes_expected_to_recall:** `[[mcp_manager.py]]`
- **Issue:** Variables `shell`, `args_for_shell`, `env` are assigned then immediately passed to `StdioServerParameters()`. Should inline.
- **Proposed value:** `[llm/adgn_llm/src/adgn_llm/mini_codex/mcp_manager.py]`
- **Reasoning:** Encapsulated to this specific function. No external context needed.

### 6. empty-message-check / occ-0

- **File:** `ducktape/2025-11-26-00/issues/empty-message-check.yaml`
- **files:** `adgn/src/adgn/git_commit_ai/cli.py` (lines 588-590)
- **critic_scopes_expected_to_recall:** `[[cli.py]]`
- **Issue:** Redundant empty message check - Git already rejects empty commit messages
- **Proposed value:** `[adgn/src/adgn/git_commit_ai/cli.py]`

### 7. mcpmanager-startup-leak / occ-0, occ-1

- **File:** `ducktape/2025-09-03-00/issues/mcpmanager-startup-leak.yaml`
- **files:** `mcp_manager.py` (occ-0: 191-224, occ-1: 226-258)
- **critic_scopes_expected_to_recall:** `[[mcp_manager.py]]`
- **Issue:** `from_config()` and `from_servers()` don't cleanup already-started servers if a later one fails

## Negative Examples (NOT OK to set)

### 1. duplicate-proposal-types / occ-0

- **File:** `ducktape/2025-11-21-00/issues/duplicate-proposal-types.yaml`
- **files:** `persist/__init__.py` (82-87), `server.py` (47-54, 163-174)
- **critic_scopes_expected_to_recall:** `[[server.py], [persist/__init__.py]]` (OR)
- **Issue:** `ProposalDetail` in server.py duplicates `PolicyProposal` in persist/**init**.py
- **Why NOT OK:** Cross-file duplication. A valid critique could mention either file. Cannot restrict to singleton.

### 2. dead-constants-runs-context / occ-0

- **File:** `ducktape/2025-12-04-00/issues/dead-constants-runs-context.yaml`
- **files:** `runs_context.py` (15-19)
- **critic_scopes_expected_to_recall:** `[[runs_context.py]]` (singleton)
- **Issue:** Constants like `EVENTS_JSONL` defined but unused; strings hardcoded in cluster_unknowns.py, cli_app/shared.py, etc.
- **Why NOT OK:** Despite singleton `critic_scopes_expected_to_recall`, a valid critique could flag the hardcoded strings in cluster_unknowns.py etc. The issue spans multiple files conceptually.

### 3. abort-method-not-implemented / occ-0

- **File:** `ducktape/2025-11-20-00/issues/abort-method-not-implemented.yaml`
- **files:** `agent.py` (547-552), `agents.py` (656)
- **critic_scopes_expected_to_recall:** `[[agents.py]]` (singleton)
- **Issue:** agents.py:656 calls `agent.abort()` which doesn't exist on MiniCodex
- **Why NOT OK:** Despite singleton `critic_scopes_expected_to_recall`, a valid critique has dual framing:
  - Tag agents.py: "This calls .abort() which doesn't exist"
  - Tag agent.py: "MiniCodex is missing abort() that callers expect"
- **Key insight:** Detection source ≠ valid reporting targets. Both files in `files:` are valid places to report this issue.

### 4. has-inflight-always-false (original structure)

- **File:** `ducktape/2025-11-22-00/issues/has-inflight-always-false.yaml`
- **Original structure:** Two occurrences, each with narrow `match_file_restriction`:
  - occ-0: `files: {runtime.py}`, `match_file_restriction: [runtime.py]`
  - occ-1: `files: {status_shared.py}`, `match_file_restriction: [status_shared.py]`
- **Issue:** `mcp_has_inflight` always False in runtime.py makes `TOOLS_RUNNING` unreachable in status_shared.py
- **Why NOT OK:** Producer/consumer relationship creates dual framing:
  - Tag runtime.py: "Hardcoded False makes TOOLS_RUNNING unreachable"
  - Tag status_shared.py: "TOOLS_RUNNING is dead code because callers always pass False"
- **Fix applied:** Merged into single occurrence with both files in `match_file_restriction`

### 5. nonexistent-ws-approvals (original structure)

- **File:** `ducktape/2025-11-22-02/issues/nonexistent-ws-approvals.yaml`
- **Original structure:** Two occurrences, each with narrow `match_file_restriction`:
  - occ-0: `files: {ApprovalTimeline.test.ts}`, `match_file_restriction: [test.ts]`
  - occ-1: `files: {app.py}`, `match_file_restriction: [app.py]`
- **Issue:** Tests reference `/ws/approvals` endpoint that's commented out in app.py
- **Why NOT OK:** Consumer/provider relationship creates dual framing:
  - Tag test.ts: "Tests call endpoint that doesn't exist"
  - Tag app.py: "Commented-out routes break dependent tests"
- **Fix applied:** Merged into single occurrence with both files in `match_file_restriction`

## Pending Review

<!-- Issues awaiting human label -->
