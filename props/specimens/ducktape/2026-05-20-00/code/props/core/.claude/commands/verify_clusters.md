# Interactive Cluster Verification Workflow

**Purpose:** Guide interactive verification and integration of clustered unknown issues from critic runs into specimen issue files.

## Prerequisites: Research Phase (MUST be completed before interactive verification)

**IMPORTANT:** Before starting the interactive verification session, you MUST complete a research phase that:

1. **Reads actual code for every cluster** using `props snapshot exec`
2. **Identifies exact line ranges** for each issue occurrence
3. **Proposes critic_scopes_expected_to_recall** based on detection logic (following `docs/authoring.md` section 5)
4. **Prepares detailed summaries** including:
   - Synthesized rationale (combining all instances)
   - Exact file paths and line ranges
   - Proposed issue file name (following naming conventions)
   - Proposed YAML structure (single occurrence vs multi-occurrence)
   - Verification commands ready to copy-paste

**Output:** Enriched verification data file (`verification_order_enriched.json`) with all research completed.

**Why:** The interactive session should present pre-researched information immediately, not do live code reading. This saves context switching during verification and ensures all proposals are based on actual code inspection.

**How to run research phase:**
Use the Task tool to spawn a research agent that processes all clusters:

- Reads `verification_order.json` and `analysis_input.json`
- For each cluster: reads actual code, identifies line ranges, proposes framing
- Writes `verification_order_enriched.json` with complete proposals
- Reports any clusters that need manual attention

## Context

You are helping verify clustered unknown issues from a clustering run. The issues were flagged by critic agents but have not yet been integrated into canonical specimen issue files.

**Input data location:** `runs/cluster/20251205_234403/`

**Key files:**

- `verification_order.json` - Smart-ordered list of clusters (optimized for: value + file sparsity + context locality)
- `analysis_input.json` - Full issue details from database (rationales, files, occurrences)
- `cluster_analysis.json` - Analysis metadata (difficulty, value, subsystem)
- `verification_order_enriched.json` - **Research phase output** (exact line ranges, proposed framing)
- `verification_log.jsonl` - Append-only log of verification decisions (you'll create/update this)

**Ordering strategy:** Clusters are ordered to:

1. Prioritize high-value issues (big wins first)
2. Favor files with fewer existing TPs (less coarse datapoints, better metric granularity)
3. Minimize context switches (specimen > subsystem > file locality)

The user may deviate from the ordering at runtime (e.g., "let's look at foo.py"), so be prepared to adapt.

## Workflow State Management

**Load workflow state on startup:**

```python
import json
from pathlib import Path

run_dir = Path("runs/cluster/20251205_234403")
state_file = run_dir / "workflow_state.json"

if state_file.exists():
    with open(state_file) as f:
        state = json.load(f)
    current_index = state["current_index"]
    print(f"Resuming from cluster {current_index + 1} of {state['total_clusters']}")
else:
    current_index = 0
    with open(run_dir / "verification_order.json") as f:
        total = json.load(f)["total_clusters"]
    state = {"current_index": 0, "total_clusters": total}
```

**Save state after each cluster:**

```python
state["current_index"] += 1
with open(state_file, "w") as f:
    json.dump(state, f, indent=2)
```

## Per-Cluster Verification Flow

For each cluster in `verification_order.json` (starting from `current_index`):

### 1. Present Issue to User

**Load from enriched data** (`verification_order_enriched.json`):

**Format:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CLUSTER #{sequence} of {total}: {cluster_name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Specimen: {snapshot}
Subsystem: {primary_subsystem}
Instances: {instance_count}
Verification Difficulty: {verification_difficulty}/5
Estimated Value: {estimated_value}

WHAT'S WRONG:
{enriched.synthesized_rationale - from research phase}

AFFECTED FILES (with line ranges):
{enriched.file_line_ranges - exact ranges from code reading}

VERIFICATION COMMANDS (pre-generated):
{enriched.verification_commands - ready to copy-paste}

PROPOSED FRAMING:

Issue file: specimens/{snapshot}/issues/{enriched.proposed_slug}.yaml

Rationale:
  {enriched.final_rationale}

critic_scopes_expected_to_recall:
  {enriched.proposed_critic_scopes_expected_to_recall}

  Reasoning: {enriched.critic_scopes_expected_to_recall_justification}

Structure: {enriched.structure_type} (single vs multi-occurrence)

OCCURRENCES (if multi):
{enriched.occurrences - with files, line ranges, notes}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

What should we do with this cluster?
  [accept] - Create issue file as proposed
  [edit] - Accept with corrections (you'll specify changes)
  [fp] - Mark as false positive (intentional/acceptable pattern)
  [adjacent] - Accept AND capture additional related issues you noticed
  [skip] - Skip for now (won't log, will revisit later)
  [done] - Stop processing (save state and exit)
```

### 2. Process User Response

**If "accept":**

- Create YAML issue file at proposed path
- Use multi-occurrence format if multiple occurrences, single-occurrence if one
- Extract exact line ranges from issue details
- Log to `verification_log.jsonl`:

  ```json
  {
    "timestamp": "2025-12-06T...",
    "cluster_sequence": N,
    "cluster_name": "...",
    "snapshot": "...",
    "action": "accepted",
    "issue_file": "specimens/.../issues/xxx.yaml",
    "issue_ids": ["critique_id:tp_id", ...]
  }
  ```

**If "edit":**

- Ask user what to change (rationale, critic_scopes_expected_to_recall, file path, etc.)
- Apply corrections
- Create issue file with corrections
- Log to `verification_log.jsonl` with `"action": "accepted_with_edits"` and `"user_corrections": "..."`

**If "fp":**

- Create false positive issue file with `should_flag: false`
- Rationale should explain why this is NOT an issue (intentional pattern, acceptable design choice, etc.)
- Log to `verification_log.jsonl` with `"action": "marked_false_positive"`

**If "adjacent":**

- First process the main cluster (as "accept" or "edit")
- Then prompt user: "Describe the additional issue(s) you noticed"
- Create separate issue file(s) for adjacent issues
- Log both the main cluster and adjacent captures

**If "skip":**

- Do not log anything
- Move to next cluster (do not increment processed count in summary)

**If "done":**

- Save current state
- Print summary of session (clusters processed, files created, skipped count)
- Exit workflow

### 3. Move to Next Cluster

- Increment `current_index`
- Save workflow state
- Continue to next cluster

## Naming Conventions for Issue Files

**Read `docs/authoring.md` sections 1-2 and 6 for:**

- Issue file naming conventions (descriptive slugs, 0-30 chars)
- Issue organization principle: group by LOGICAL ISSUE, not by location
- When to merge multiple clusters into one multi-occurrence file

## YAML Issue Format

**Read `docs/authoring.md` sections 3-4 for:**

- YAML issue structure
- Range format specifications
- Rules for `critic_scopes_expected_to_recall` and `relevant_files`
- False positive rationale format (acknowledge what looks problematic, explain why acceptable)

## Tips for Efficient Verification

1. **Use snapshot exec liberally** - Don't guess, read the actual code
2. **Batch similar issues** - If you see multiple occurrences of the same pattern, use multi-occurrence format
3. **Be objective** - Avoid "user said..." or "this is nice", describe facts
4. **Trust the ordering** - It's optimized for context locality, don't jump around
5. **Use "adjacent"** - If you spot related issues while verifying, capture them
6. **Use "skip" sparingly** - Only if truly unclear, prefer "fp" if you disagree

## Session Management

**Start new session:**

```
User: "Let's verify clusters from runs/cluster/20251205_234403/"
Assistant: [Loads workflow state, shows first unprocessed cluster]
```

**Resume session:**

```
User: "Continue cluster verification"
Assistant: [Loads workflow state, resumes from current_index]
```

**Early exit:**

```
User: [types "done" or explicitly asks to stop]
Assistant: [Saves state, shows summary, explains how to resume]
```

## Output Files

**Created during workflow:**

- `specimens/{snapshot}/issues/{slug}.yaml` - Issue files (one per cluster or adjacent)
- `verification_log.jsonl` - Append-only log (one JSON object per line)
- `workflow_state.json` - Current position (overwritten each cluster)

**Final summary** (when workflow completes or user says "done"):

```
Cluster Verification Session Summary
=====================================

Processed: {N} clusters
Accepted: {M} issues created
False positives: {K}
Skipped: {S}
Remaining: {R}

Files created:
- specimens/ducktape/2025-11-20-00/issues/xxx.yaml
- specimens/ducktape/2025-11-20-00/issues/yyy.yaml
- ...

To resume: /verify-clusters
```
