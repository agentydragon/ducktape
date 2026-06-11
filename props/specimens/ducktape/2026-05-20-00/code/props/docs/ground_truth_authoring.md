# Ground Truth Authoring Guide

This guide explains how to author ground truth issues (true positives and false positives) for Props evaluation.

## File Format

Ground truth issues are stored as YAML files in the specimen `issues/` directory. Each file represents one logical issue with one or more occurrences.

## Basic Structure

```yaml
rationale: |
  Full explanation of what makes this a TP or FP.
  Why should (or shouldn't) a critic flag this?
should_flag: true # true for TP, false for FP
occurrences:
  - occurrence_id: occ-0
    files:
      path/to/file.py:
        - [42, 45] # lines 42-45
    note: Occurrence-specific explanation
```

## Specifying Line Ranges

The `files` field maps file paths to line specifications. All line numbers are 1-based (first line is 1). Ranges are inclusive on both ends.

### Supported Formats

**1. List of `[start, end]` pairs** (most common):

```yaml
files:
  file.py:
    - [10, 20] # lines 10-20
    - [30, 40] # lines 30-40
    - [50, 50] # single line 50 (start equals end)
```

**2. Inline single range** (shorthand when only one range):

```yaml
files:
  file.py: [10, 20] # equivalent to - [10, 20]
```

**3. Null** (whole file anchor, no specific lines):

```yaml
files:
  file.py: # null/omitted = no specific lines
```

**4. List of dicts with notes** (for per-range annotations):

```yaml
files:
  file.py:
    - start_line: 10
      end_line: 15
      note: "First issue location"
    - start_line: 20
      end_line: 25
      note: "Related manifestation"
```

**5. Single dict** (shorthand for one range with note):

```yaml
files:
  file.py:
    start_line: 42
    end_line: 45
    note: "The problematic code"
```

### Invalid Formats

```yaml
# INVALID: bare integer
file.py: 42

# INVALID: empty list
file.py: []

# INVALID: single-element list
file.py:
  - [42]

# INVALID: three-element list
file.py:
  - [10, 20, 30]
```

### Per-Range Notes

Use per-range notes when:

- A single occurrence spans multiple distinct locations that serve different purposes
- You want to explain why each range matters independently (e.g., "definition site" vs "call site")
- The ranges represent different aspects of the same logical issue

**Example:**

```yaml
rationale: |
  SQL injection vulnerability via string concatenation.
should_flag: true
occurrences:
  - occurrence_id: occ-sql-001
    files:
      src/auth/login.py:
        - start_line: 13
          end_line: 14
          note: "User input concatenated into SQL query (injection point)"
        - start_line: 20
          end_line: 22
          note: "Query executed without parameterization"
    note: |
      This occurrence demonstrates multiple injection points stemming from
      the same root cause: lack of parameterized queries.
```

## Notes Hierarchy

Props supports two levels of notes:

1. **Occurrence-level notes** (`note` at occurrence level): Explain the overall occurrence and context
2. **Range-level notes** (`note` within each range dict): Explain why specific line ranges matter

Both are optional, but:

- Multi-occurrence issues **must** have occurrence-level notes on all occurrences
- Per-range notes are optional but recommended when ranges serve distinct purposes

## True Positives (TPs)

For true positives:

```yaml
rationale: Explanation of the issue
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      path/to/file.py:
        - [start, end]
    note: Occurrence-specific details (required for multi-occurrence issues)
    critic_scopes_expected_to_recall:
      - [file1.py, file2.py] # Files needed to detect this occurrence
    match_file_restriction:
      - file1.py # Files where critique must be reported (optional)
```

### critic_scopes_expected_to_recall

Defines which file scopes contribute to **recall denominator**:

- Outer list = alternatives (OR logic)
- Inner list = required together (AND logic)
- If ANY alternative is a subset of the critic's reviewed files, the occurrence counts toward recall denominator
- Can be omitted for single-file occurrences (auto-inferred)

**NOTE:** This is a soft expectation - critics CAN find issues outside expected scopes (recall >100% possible).

### match_file_restriction

**HARD CONSTRAINT** on where graders can give credit:

- `null` (default): Critique can match from any file (unrestricted)
- Non-empty list: Grader may only give credit if critique flagged overlapping files (file-restricted)

This is distinct from `critic_scopes_expected_to_recall` - see ground_truth.md.mako for the full explanation.

- Independent of critic_scopes (detection source ≠ valid reporting targets)

## False Positives (FPs)

For false positives:

```yaml
rationale: Why this should NOT be flagged
should_flag: false
occurrences:
  - occurrence_id: occ-0
    files:
      path/to/file.py:
        - [start, end]
    note: Why this specific case is acceptable
    relevant_files:
      - file1.py # Files that make this FP relevant
    match_file_restriction:
      - file1.py # Optional: restrict matching scope
```

## Complete Example

```yaml
rationale: |
  Function parameter should use walrus operator to combine assignment and conditional check,
  reducing line count without harming readability.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      adgn/agent/agent.py:
        - start_line: 358
          end_line: 358
          note: "Variable assignment"
        - start_line: 360
          end_line: 360
          note: "Immediate use in conditional - could be combined with := above"
    note: Variable cid assigned then used in conditional check
    critic_scopes_expected_to_recall:
      - [adgn/agent/agent.py]
    match_file_restriction:
      - adgn/agent/agent.py
```

## Auto-Inference Rules

**For true positives:**

- Single file in occurrence → `critic_scopes_expected_to_recall` auto-inferred as `[[that_file]]`
- Multiple files in occurrence → Must provide explicit `critic_scopes_expected_to_recall`

**For false positives:**

- `relevant_files` auto-inferred from keys of `files` if not provided

## Best Practices

1. **Rationale**: Write for human readers who will understand the context. Explain "why" not just "what".
2. **Occurrence notes**: Use for location-specific guidance; avoid repeating the rationale.
3. **Range notes**: Use sparingly, only when individual ranges need distinct explanations.
4. **Multi-occurrence issues**: Always provide occurrence-level notes to distinguish each occurrence.
5. **File paths**: Use paths relative to the specimen root.
6. **Line numbers**: 1-based (matching most editors).
