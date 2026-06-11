# Organize Specimen Issues Command

Reorganize specimen issues to follow CLAUDE.md rules: group by logical problems, not locations.

## Usage

```bash
/organize_issues [task description]
```

**Default task (if no description provided):** "Apply CLAUDE.md rules to current specimen"

**Examples:**

- `/organize_issues` - Full reorganization of current specimen
- `/organize_issues fix subjective phrasing in issues 010-020`
- `/organize_issues split multi-problem issues into logical groups`
- `/organize_issues merge duplicate issue types across files`

## What This Command Does

Analyzes specimen issues and reorganizes them according to CLAUDE.md principles:

1. **Groups by LOGICAL PROBLEM** (not location)
   - One issue type = one issue file (with N occurrences)
   - Example: "trivial aliases" across all files, not "problems in app.py"

2. **Splits mixed-problem issues**
   - If one file contains: type safety + dead code + useless comments
   - Creates 3 separate issues, each grouping that problem type across ALL locations

3. **Merges duplicate logical issues**
   - Example: Separate issues for "imports not at top" → one issue with all occurrences

4. **Removes subjective phrasing**
   - "User mentioned X" → objective technical description

## Parallel Pipeline Architecture

The command uses a 5-agent maximum parallel pipeline with 3 computation depths:

### Phase 1: Analysis (5 parallel agents, depth=1)

Split specimen into 5 roughly equal chunks, each agent analyzes independently:

- **Agent 1**: Issues 001-010 → identify logical problem types + mixed issues
- **Agent 2**: Issues 011-020 → identify logical problem types + mixed issues
- **Agent 3**: Issues 021-030 → identify logical problem types + mixed issues
- **Agent 4**: Issues 031-040 → identify logical problem types + mixed issues
- **Agent 5**: Issues 041-051 → identify logical problem types + mixed issues

Each reports:

```
Chunk N findings:
- Logical problems found: [list]
- Mixed issues: [issue numbers with sub-problems]
- Potential merges: [similar issues across files]
- Subjective phrasing: [list]
```

### Phase 2: Strategy (1 agent, depth=2)

Single coordinator agent receives all 5 reports and creates reorganization plan:

- Identifies duplicate logical problems across chunks
- Plans splits (one mixed issue → multiple logical issues)
- Plans merges (multiple similar issues → one with occurrences)
- Assigns new issue numbers (maintain 001-N sequence)
- Creates work units for Phase 3

Outputs:

```
Reorganization strategy:
- Splits: [issue X → X1 (type A), X2 (type B), X3 (type C)]
- Merges: [issues Y, Z, W → new issue for problem type D]
- Renumbering: [old → new mappings]
- Work units: [5 independent batches for parallel execution]
```

### Phase 3: Execution (5 parallel agents, depth=3)

Execute reorganization plan in parallel with non-overlapping work:

- **Agent 1**: Work unit 1 (splits + writes new issues 001-010)
- **Agent 2**: Work unit 2 (splits + writes new issues 011-020)
- **Agent 3**: Work unit 3 (merges + writes new issues 021-030)
- **Agent 4**: Work unit 4 (merges + writes new issues 031-040)
- **Agent 5**: Work unit 5 (cleanup + writes new issues 041-N)

Each agent:

- Reads ONLY the issues assigned to them
- Writes ONLY the new issue files assigned to them
- Does NOT commit (reports completion only)
- Does NOT touch issues outside their work unit

## Load Balancing Strategy

**Problem:** Specimen has 51 issues, agents can handle variable work:

- Splits: Read 1 issue → Write 2-5 issues (more work)
- Merges: Read 3-5 issues → Write 1 issue (less work, but complex)
- Simple fixes: Read 1 → Write 1 (minimal work)

**Solution:** Phase 2 coordinator balances by:

1. Counting total read/write operations per work unit
2. Mixing splits (heavy) with simple fixes (light) in same unit
3. Distributing merges evenly
4. Ensuring no agent writes to same file as another

**Example balanced distribution:**

- Unit 1: 2 splits + 3 simple fixes → ~15 file operations
- Unit 2: 1 merge + 5 simple fixes → ~12 file operations
- Unit 3: 3 splits + 0 fixes → ~14 file operations
- Unit 4: 2 merges + 2 simple fixes → ~13 file operations
- Unit 5: 1 merge + cleanup tasks → ~11 file operations

## Preventing Agent Conflicts

**Rule 1: Non-overlapping writes**

- Each agent assigned exclusive output issue number ranges
- No two agents write same file
- Phase 2 coordinator enforces this in work unit assignments

**Rule 2: Read-only source material**

- Agents only READ original issues
- Write only to NEW issue files
- Old files deleted in final cleanup (phase 3, agent 5)

**Rule 3: No inter-agent communication**

- Phase 1: agents report to user only
- Phase 2: coordinator reads all phase 1 reports
- Phase 3: agents execute pre-assigned work units independently
- No agent depends on another agent's output during same phase

## Command Execution Flow

```
User: /organize_issues [optional task]
↓
Claude: Read CLAUDE.md rules + scan specimen
↓
[Phase 1: Analysis - 5 parallel agents]
  Agent 1-5 analyze chunks → report findings
↓
[Phase 2: Strategy - 1 coordinator]
  Coordinator creates reorganization plan
  Prints plan summary for user review
↓
[Phase 3: Execution - 5 parallel agents]
  Agent 1-5 execute work units in parallel
  Each reports completion
↓
Claude: Verify all files written
        Delete old issues
        Report final statistics
↓
User: Review changes (NOT committed by agents)
```

## Implementation Notes

### Phase 1 Agent Template

```
Read issues {start}-{end} from specimen.
Identify:
1. Logical problem types (list unique problem categories)
2. Mixed issues (issues with 2+ unrelated problems)
3. Duplicate problems (same type across multiple issues)
4. Subjective phrasing (quote specific lines)

Report findings in structured format.
DO NOT write any files.
DO NOT commit anything.
```

### Phase 2 Coordinator Template

```
Read all 5 analysis reports.
Create reorganization strategy:
1. List all unique logical problems found
2. Plan splits for mixed issues
3. Plan merges for duplicate problems
4. Assign new issue numbers (001-N sequence)
5. Create 5 work units with balanced load

Ensure:
- No work unit writes same file as another
- Each unit has roughly equal file operations
- Splits/merges distributed evenly

Output work unit assignments.
DO NOT write any files.
DO NOT commit anything.
```

### Phase 3 Executor Template

```
Execute work unit {N}:
- Read assigned old issues
- Write assigned new issues (numbers {start}-{end})
- Follow CLAUDE.md rules for:
  * Logical grouping
  * Objective phrasing
  * Code citation limits

Report completion with file counts.
DO NOT commit anything.
DO NOT touch files outside your range.
```

## Minimum Computation Depth

**Theoretical minimum:** 3 phases (cannot be reduced)

- Phase 1 requires reading all issues (parallelizable)
- Phase 2 requires all phase 1 outputs (serial bottleneck)
- Phase 3 requires phase 2 plan (parallelizable)

**Actual depth:** O(log N) for large specimens

- With 5-agent limit: ⌈N/5⌉ rounds in phase 1 if N > 50
- For 51 issues: depth = 1 + 1 + 1 = 3 rounds total

**Time estimate:**

- Phase 1: ~2-3 min (parallel)
- Phase 2: ~1-2 min (serial)
- Phase 3: ~3-5 min (parallel)
- **Total:** ~6-10 minutes for 51-issue specimen

## Exit Conditions

Command completes when:

1. All new issue files written
2. Old issue files deleted
3. Issues renumbered 001-N consecutively
4. All issues follow CLAUDE.md logical grouping
5. No subjective phrasing remains
6. Files NOT committed (left for user review)

Reports final statistics:

- Issues before: X
- Issues after: Y
- Splits: Z issues split into A new issues
- Merges: B issues merged into C new issues
- Fixes: D issues had subjective phrasing removed
