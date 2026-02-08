---
description: Write and iterate on a design document for the discussed feature/change
---

You've been asked to create a design document for the feature or change being discussed.

## Phase 1: Requirements and Initial Doc

First, **gather requirements**:

- Ask clarifying questions about scope, goals, and constraints
- Confirm what problem this solves and for whom
- Understand success criteria and non-goals
- Clarify any ambiguities in the request

Then, **research context** by investigating:

- Call sites and callers of affected code
- Implementation details of relevant libraries/patterns
- Existing similar implementations or alternatives
- Sequencing and dependencies
- Any other adjacent context needed for a complete picture

Then write a design document with these sections (organize in whatever order makes sense):

### Required Sections

1. **Acceptance Criteria / Definition of Done**
   - Clear, testable criteria for when this work is complete
   - What must be true when finished

2. **Non-goals / Out of Scope**
   - Explicitly state what is NOT being done
   - Set clear boundaries to prevent scope creep

3. **Breaking Changes** (if any)
   - Explicitly call out what existing behavior/APIs will break
   - What code/configs will stop working
   - **MUST ask user to confirm breaks are acceptable**
   - Default assumption: clean breaks, no backcompat shims or migrations

4. **Open Questions / Risks / Research Needed**
   - Unknowns that need answers
   - Potential risks or blockers
   - Areas requiring investigation

5. **Existing Options / Alternatives**
   - Current approaches or similar patterns in the codebase
   - Alternative implementation strategies considered
   - Trade-offs between options

6. **Execution Plan (DAG)**
   - Tasks organized as a dependency graph
   - Show which tasks can run in parallel across 5 subagents working on the same repo
   - Mark dependencies: tasks MUST NOT run simultaneously if they:
     - Write to the same files
     - Would otherwise conflict/collide
   - Indicate which tasks are blocked by which other tasks
   - Show file ownership: which agent/subtask writes which files

### Default Assumptions (unless user explicitly requests otherwise)

**Testing:**

- NOT exhaustive - write high-value tests for main flows only
- Prefer integration tests over unit tests
- Use real objects where easy, not mocks
- NOT complicated e2e browser tests or similar
- Goal: confidence in main paths, not 100% coverage

**Documentation:**

- NO separate documentation files (README, design docs, etc.)
- Only brief inline docs/comments for non-obvious code
- If it's clear from reading the code, don't document it
- Comments should add value beyond what's immediately obvious

**Backward Compatibility:**

- Clean breaks by default - NO backcompat shims or migration code
- NO migration paths unless explicitly requested
- Call out breaks clearly and ask user to confirm they're acceptable

**Style Guidelines:**

- Keep it DRY and concise
- Well-structured, not an append-only log
- Clear enough for implementation

## Phase 2: Interactive Refinement

After writing the initial doc, guide the user interactively to refine it:

**Each iteration:**

1. Present remaining **open questions** (prioritized, actionable)
2. Offer **alternatives** that need decisions
3. Ask about **tradeoff preferences**
4. Request confirmation on **implementation details** or sketches
5. Provide **breadcrumbs**: clear next steps toward a solid plan

**When the user responds:**

1. Research/test/explore as requested
2. Integrate findings and decisions into the doc
3. Keep the doc **logically structured** (reorganize as needed, don't just append)
4. **Rethink** if plan/sequencing/dependencies/alternatives/context needs updates
5. Update the open questions list
6. Check if doc is ready for implementation (all non-nitpicky questions closed)
7. If ready: tell the user and offer to start implementation
8. If not ready: continue with next iteration

## Phase 3: Implementation Tracking

Once implementation starts, maintain the design doc as a living document:

- Every time you complete a subtask, mark it done (✅) in the plan
- When you discover new information, integrate it into relevant sections
- Keep the doc synchronized with actual progress
- Update risks/questions as they're resolved or new ones emerge

The design doc should always reflect the current state and serve as the source of truth for the implementation effort.
