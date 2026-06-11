---
description: Verify documentation claims against actual code, finding and fixing stale or incorrect docs
---

Systematically verify that documentation, comments, and docstrings accurately describe the actual codebase behavior. This is NOT a heuristic grep sweep - it requires actually reading, understanding, and cross-referencing documentation claims against real code.

## What This Command Does

1. **Maps the codebase structure** - Understands the logical organization
2. **Identifies all documentation** - README.md, AGENTS.md, docstrings, inline comments, config examples
3. **Extracts verifiable claims** - Statements about behavior, structure, APIs, workflows
4. **Verifies each claim against code** - Actually reads and traces through implementations
5. **Reports discrepancies** - With specific line references and evidence
6. **Fixes issues** - Updates stale documentation to match reality

## Scope

Can be invoked in two modes:

1. **Full codebase verification** (default): `verify-docs`
   - Analyzes entire codebase
   - Spawns multiple subagents for parallel verification
   - Comprehensive cross-referencing

2. **Scoped verification**: `verify-docs <path or component>`
   - Example: `verify-docs agent_server/`
   - Focuses on specific directory or component
   - Faster, targeted verification

## Phase 1: Codebase Mapping

**Discover the documentation landscape:**

```bash
# Find all documentation files
fd -e md -t f .

# Find Python files with docstrings (potential doc sources)
fd -e py -t f .

# Identify key config files that may have documented schemas
fd -t f -e yaml -e toml -e json .
```

**Build a structural map:**

- Directory structure and purpose of each top-level directory
- Key entry points (main modules, CLI entry points, servers)
- Build system (Bazel targets, package definitions)
- Test locations and patterns

**Identify documentation types to verify:**

| Type              | Location                 | Verification Method                               |
| ----------------- | ------------------------ | ------------------------------------------------- |
| README.md         | Per-directory            | Cross-reference with actual files and commands    |
| AGENTS.md         | Per-directory            | Verify agent instructions match codebase reality  |
| Docstrings        | Python functions/classes | Compare to actual signatures and behavior         |
| Inline comments   | Throughout code          | Verify comment describes adjacent code accurately |
| Config examples   | Docs, README             | Validate against actual schemas                   |
| CLI examples      | Docs                     | Run or trace to verify commands work              |
| Architecture docs | docs/ directories        | Trace described flows through actual code         |

## Phase 2: Decomposition and Agent Spawning

**For large codebases, decompose into logical chunks:**

Chunk boundaries should follow:

- Package/module boundaries
- Functional areas (e.g., "server", "client", "persistence")
- Ownership patterns (code that changes together)

**Spawn subagents with clear mandates:**

Each subagent receives:

1. **Chunk scope**: Which directories/files they own
2. **Verification mandate**: Read all docs in scope, verify all claims
3. **Cross-reference protocol**: How to flag claims about other chunks
4. **Output format**: Structured report of findings

**Example agent spawning:**

```
Subagent 1: agent_server/
- Verify agent_server/AGENTS.md, agent_server/README.md
- Verify all docstrings in agent_server/src/
- Flag any claims about mcp_infra/ for cross-verification

Subagent 2: mcp_infra/
- Verify mcp_infra/AGENTS.md, mcp_infra/README.md
- Verify all docstrings in mcp_infra/src/
- Handle cross-verification requests from Subagent 1
```

## Phase 3: Claim Extraction and Verification

**For each documentation file, extract claims:**

A "claim" is any statement that can be verified against code:

| Claim Type     | Example                               | Verification                          |
| -------------- | ------------------------------------- | ------------------------------------- |
| File existence | "See config.yaml for settings"        | Does config.yaml exist?               |
| API shape      | "Takes a dict with 'name' and 'args'" | Check actual function signature       |
| Behavior       | "Retries 3 times on failure"          | Read implementation, find retry logic |
| Workflow       | "Run `bazel test //...` to test"      | Verify command works                  |
| Architecture   | "Uses Redis for caching"              | Find actual cache implementation      |
| Schema         | "Config has 'timeout_secs' field"     | Check Pydantic model or schema        |
| Default values | "Defaults to 5 seconds"               | Read default in code                  |

**Verification depth requirements:**

NOT acceptable:

- Grepping for keywords and assuming match
- Checking file existence without reading contents
- Trusting import statements without tracing

REQUIRED:

- Read the actual implementation
- Trace through function calls
- Verify semantic meaning, not just syntactic presence
- Check that documented behavior matches actual behavior

**Example verification process:**

```
Claim: "Policy evaluation runs in a sandboxed container with network disabled"

Verification:
1. Find policy evaluation code (policy_eval/runner.py)
2. Read container creation logic
3. Check HostConfig: {"NetworkMode": "none"}  ← VERIFIED
4. Check for other sandbox claims (ReadonlyRootfs, Memory limits)
```

## Phase 4: Cross-Chunk Verification

**When a claim references another chunk:**

1. Subagent flags the claim with context:

   ```
   CROSS-REF NEEDED:
   - Source: agent_server/AGENTS.md:26
   - Claim: "See docker/AGENTS.md for image build"
   - Target chunk: docker/
   - Question: Does docker/AGENTS.md exist and describe image building?
   ```

2. Coordinator routes to appropriate subagent or verifies directly

3. Results flow back to original subagent for report

**Coordination patterns:**

- **Interface claims**: "Module A calls Module B's foo() function" - verify both sides
- **Flow claims**: "Request goes A → B → C" - trace actual flow
- **Dependency claims**: "Uses library X" - check requirements and actual imports

## Phase 5: Report Generation

**Each subagent produces a structured report:**

````markdown
## Verification Report: <chunk name>

### Summary

- Files verified: N
- Claims extracted: M
- Verified correct: X
- Discrepancies found: Y
- Needs cross-verification: Z

### Discrepancies

#### 1. <File:Line> - <Claim summary>

**Documented:**

> <quoted claim>

**Actual:**
<what the code actually does, with file:line references>

**Evidence:**

```python
# From actual_file.py:123
<relevant code snippet>
```
````

**Suggested fix:**
<proposed correction to documentation>

---

#### 2. ...

### Verified Correct

<Brief list of claims that were verified as accurate>

### Cross-Verification Requests

<Claims that need verification against other chunks>
```

## Phase 6: Synthesis and Fixes

**Coordinator synthesizes subagent reports:**

1. Collect all discrepancy reports
2. Resolve cross-verification requests
3. Prioritize fixes by severity:
   - **Critical**: Completely wrong (would cause errors if followed)
   - **Misleading**: Partially wrong or outdated
   - **Minor**: Trivial inaccuracies or typos

**Apply fixes:**

- Update documentation files to match reality
- Prefer concise, accurate docs over verbose outdated ones
- If unsure whether doc or code is wrong, flag for human review
- Don't "fix" docs by adding complexity - if something is confusing, simplify

## Verification Quality Checklist

Before marking verification complete:

- [ ] Every README.md and AGENTS.md in scope was read fully
- [ ] Every verifiable claim was traced to code
- [ ] File/path references were validated to exist
- [ ] Command examples were verified to work (or at least parse)
- [ ] Schema/API claims were compared to actual definitions
- [ ] Cross-references between chunks were resolved
- [ ] Fixes were applied and re-verified

## Anti-Patterns to Avoid

**Shallow verification:**

- ❌ "Found 'timeout' in the file, claim verified"
- ✅ "Read function at line 45, timeout parameter defaults to 30, not 5 as documented"

**Grep-and-assume:**

- ❌ `grep -r "Redis" . | wc -l` → "Redis is used"
- ✅ Read the caching module, trace actual cache backend instantiation

**Existence-only checks:**

- ❌ "File docker/AGENTS.md exists, claim verified"
- ✅ "docker/AGENTS.md exists and contains image build instructions as claimed"

**Trusting docs about docs:**

- ❌ "AGENTS.md says to see README.md, and README.md exists"
- ✅ "Verified README.md actually contains the information AGENTS.md claims it does"

## Output

Final output should be:

1. **Summary**: Total docs verified, discrepancies found, fixes applied
2. **Fixed files**: List of documentation files that were corrected
3. **Remaining issues**: Any claims that couldn't be verified or fixed automatically
4. **Recommendations**: Suggestions for documentation improvements beyond just fixing errors
