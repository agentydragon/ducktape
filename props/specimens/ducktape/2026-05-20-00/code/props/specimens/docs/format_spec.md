# Specimens Format Specification

Technical reference for the specimens dataset format. This document defines the canonical structure for snapshots, issues, and related metadata.

## Overview

Specimens use:

- **Bazel BUILD files** for snapshot metadata (`slug`, `split`, provenance comments)
- **YAML** for issue definitions (issue files in `issues/`)
- **Git commits** for code snapshots (via local directories or `http_archive`)

## Directory Structure

See <../README.md> for the directory layout with concrete examples.

## Snapshot Metadata (BUILD.bazel)

Each snapshot has a `BUILD.bazel` file that calls `specimen_targets()` from `//props/specimens:defs.bzl`. This defines the specimen's `slug`, `split`, and code source. Source provenance (commit SHA, include/exclude paths) is captured as natural language comments.

### Structure

```python
load("//props/specimens:defs.bzl", "specimen_targets")

# Provenance comments (optional, for traceability)
# Snapshot of {repo} at {commit_sha}.
# Included: {paths}
# Excluded: {paths}
specimen_targets(
    name = "specimen",
    code_srcs = glob(["code/**/*"]),  # or external repo label
    slug = "{project}/{YYYY-MM-DD-NN}",
    split = "{train|valid|test}",
)
```

### Examples

**Local source with provenance comments:** See <../ducktape/2025-11-26-00/BUILD.bazel>

**Minimal local source:** See <../gmail-archiver/2025-12-17-00/BUILD.bazel>

**External repo source:** See <../crush/2025-08-30-internal_db/BUILD.bazel>

### Fields

- **`slug`** (string, required): Specimen identifier in `{project}/{YYYY-MM-DD-NN}` format
- **`split`** (string, required): Dataset split assignment
  - `train`: Training data (full access to labels and execution traces)
  - `valid`: Validation data (can evaluate, but cannot read labels)
  - `test`: Test data (reserved for final holdout evaluation)
- **`code_srcs`** (label list, required): Source code files — `glob(["code/**/*"])` for local, external repo label for remote
- **Provenance comments** (optional): Natural language comments capturing source commit SHA, included/excluded paths for traceability

## Issue File Format (YAML)

Each `.yaml` file in the `issues/` directory defines a single issue (true positive or false positive).

### True Positive (should_flag: true)

Issues that should be caught by a critic.

```yaml
rationale: |
  Multi-line explanation of what's wrong and why.
  Describe the problem, its impact, and optionally the fix.

should_flag: true

occurrences:
  - occurrence_id: occ-0
    files:
      path/to/file.py:
        - [10, 20] # Line range (inclusive)
        - [42, 42] # Single line
    note: "Optional note for this occurrence" # Required if multiple occurrences
    critic_scopes_expected_to_recall:
      - [path/to/file.py] # File sets that should detect this
    match_file_restriction: # Optional: restrict grader matching
      - path/to/file.py
```

### False Positive (should_flag: false)

Patterns that look wrong but are actually acceptable.

```yaml
rationale: |
  Critics might flag [X] because [Y looks problematic].
  However, our ground truth is that it's acceptable because [Z].

should_flag: false

occurrences:
  - occurrence_id: occ-0
    files:
      path/to/file.py:
        - [10, 20]
    note: "Optional note"
    relevant_files:
      - path/to/file.py
```

### Line Range Formats

The `files` field maps file paths to line specifications. **Line specs must always be a list of `[start, end]` pairs.**

```yaml
files:
  # Single line - use [N, N]
  file_a.py:
    - [42, 42]

  # Single range
  file_b.py:
    - [10, 20]

  # Multiple ranges
  file_c.py:
    - [30, 40]
    - [50, 60]

  # Multiple single lines
  file_d.py:
    - [10, 10]
    - [25, 25]
    - [42, 42]
```

**Format rules:**

1. **Always use `list[[start, end], ...]` format** - no bare integers, no inline ranges:

   ```yaml
   # ❌ INVALID: bare integer
   file.py: 42

   # ❌ INVALID: inline range
   file.py: [10, 20]

   # ❌ INVALID: bare integers in list
   file.py:
     - 10
     - 20

   # ✅ CORRECT: list of [start, end] pairs
   file.py:
     - [42, 42]

   file.py:
     - [10, 20]
   ```

2. **Each range must have exactly 2 elements** `[start, end]`:

   ```yaml
   # ❌ INVALID: 1 element
   file.py:
     - [42]

   # ❌ INVALID: 3 elements
   file.py:
     - [10, 20, 30]

   # ✅ CORRECT: 2 elements per range
   file.py:
     - [10, 10]
     - [20, 20]
     - [30, 30]
   ```

3. **Single lines use `[N, N]`** (start equals end):
   ```yaml
   # Line 42 only
   file.py:
     - [42, 42]
   ```

All line numbers are 1-indexed (first line is 1, not 0). Ranges are inclusive on both ends.

### Auto-Inference Rules

**For true positives:**

- Single file in occurrence → `critic_scopes_expected_to_recall` auto-inferred as `[[that_file]]`
- Multiple files in occurrence → Must provide explicit `critic_scopes_expected_to_recall`
- Multiple occurrences → `note` field required on all occurrences

**For false positives:**

- `relevant_files` auto-inferred from keys of `files` if not provided

### Complete Examples

**True Positive (Single File, Single Occurrence):**

```yaml
rationale: |
  Lines 67-100 and 108-135 duplicate identical logic for computing AgentInfo.
  Fix: extract helper function.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      adgn/src/adgn/agent/mcp_bridge/servers/registry_bridge.py:
        - [67, 100]
        - [108, 135]
    critic_scopes_expected_to_recall:
      - [adgn/src/adgn/agent/mcp_bridge/servers/registry_bridge.py]
```

**True Positive (Multiple Files, Multiple Occurrences):**

```yaml
rationale: |
  Three functions build lists imperatively using append() instead of comprehensions.
  Replace with list comprehensions for cleaner, more Pythonic code.
should_flag: true
occurrences:
  - occurrence_id: occ-0
    files:
      adgn/src/adgn/agent/mcp_bridge/servers/agents.py:
        - [50, 59]
      adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py:
        - [64, 65]
        - [71, 80]
    note: "In _convert_pending_approvals()"
    critic_scopes_expected_to_recall:
      - [adgn/src/adgn/agent/mcp_bridge/servers/agents.py]
      - [adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py]

  - occurrence_id: occ-1
    files:
      adgn/src/adgn/agent/server/runtime.py:
        - [267, 267]
        - [274, 274]
    note: "In runtime proposals building"
    critic_scopes_expected_to_recall:
      - [adgn/src/adgn/agent/server/runtime.py]
```

**False Positive:**

```yaml
rationale: |
  A past critique flagged the two reads surrounding the permission gate as an
  "unnecessary re-read". This is a false positive. The first read is a lightweight
  early equality check; the subsequent read populates oldContent for canonical
  diff/history recording. If permission.Request blocks, the file may change,
  so re-reading ensures recorded history reflects state at write time.
should_flag: false
occurrences:
  - occurrence_id: occ-0
    files:
      internal/llm/tools/write.go:
        - [148, 151]
        - [161, 167]
        - [174, 182]
    relevant_files:
      - internal/llm/tools/write.go
```

## Match File Restriction (`match_file_restriction`)

The `match_file_restriction` field is a **hard constraint** on where graders can give credit for finding an occurrence.

### Semantics

- **`null`/omitted (unrestricted)**: Critique can match from any file. This is the default when we haven't determined the closed set of valid reporting files, or for issues that aren't bound to specific files.
- **Non-empty list (file-restricted)**: Grader may only give credit if the critique flagged overlapping files.

### Why This Is Separate from `critic_scopes_expected_to_recall`

These two fields control different aspects:

| Field                              | Purpose                                                     | Enforcement      |
| ---------------------------------- | ----------------------------------------------------------- | ---------------- |
| `critic_scopes_expected_to_recall` | Which file sets contribute to **recall denominator**        | Soft expectation |
| `match_file_restriction`           | Where the issue **actually is** and can be validly reported | Hard constraint  |

**Example:** `wrapper.py` calls APIs in `core.py`. The issue is dangerous code in `core.py`.

```yaml
occurrences:
  - occurrence_id: occ-0
    files:
      core.py:
        - [10, 20]
    critic_scopes_expected_to_recall:
      - [wrapper.py] # Reviewing wrapper.py should lead to finding this
      - [core.py] # Reviewing core.py directly also works
    match_file_restriction:
      - core.py # But credit only if critique mentions core.py
```

**Result:**

- A critic reviewing `wrapper.py` that flags `core.py` → gets credit (good diligence!)
- A critic reviewing `wrapper.py` that only complains about `wrapper.py` → zero credit
- The occurrence counts toward recall denominator when reviewing either file

### When to Use

- **Omit** for issues not bound to specific files, or when you haven't determined the valid reporting scope
- **Set** when you know the specific file(s) where the issue should be reported

### Dead Code Note

For dead code issues, `match_file_restriction` is typically the dead file itself — the issue _is_ in that file regardless of how it's detected. However, `critic_scopes_expected_to_recall` may include other files: when existing code duplicates logic that the dead helper would simplify, a reviewer of those files could discover the dead code by searching for existing helpers. The two fields diverge in this case:

```yaml
# Dead helper that would DRY up existing code
occurrences:
  - occurrence_id: occ-0
    files:
      utils/format_helpers.py:
        - [1, 30]
    note: "Dead helper; cli.py lines 80-95 duplicate this formatting logic"
    critic_scopes_expected_to_recall:
      - [utils/format_helpers.py] # Direct detection
      - [cli.py] # Reviewer would search for helpers, find this dead one
    match_file_restriction:
      - utils/format_helpers.py # Issue is in the dead file
```

## Detection Standard (`critic_scopes_expected_to_recall`)

The key question for `critic_scopes_expected_to_recall`: **"If I gave a high-quality critic this file set to review, and they failed to find this issue, would that be a failure on their part?"**

### What "reviewing files" includes:

- Reading files thoroughly line by line
- Following imports and calls to check APIs
- Searching the codebase for existing helpers/patterns
- Looking for duplication or similar patterns
- All normal thorough code review activities

### What it does NOT mean:

- "Can you detect this reading ONLY these files in complete isolation?"
- "Without following any imports or doing any searches?"

### Semantics

`critic_scopes_expected_to_recall` is a list of alternative file sets (OR logic):

```yaml
critic_scopes_expected_to_recall:
  - [file_a.py] # Detectable from file_a alone
  - [file_b.py, file_c.py] # OR detectable from both b AND c together
```

- **Outer list**: OR logic (any of these file sets works)
- **Inner list**: AND logic (all files in set required together)

### Examples

**Single-file issue:**

```yaml
# Unused import in server.py - obvious from the file itself
critic_scopes_expected_to_recall:
  - [src/server.py]
```

**Either-file issue (duplication):**

```yaml
# Enum duplicated in types.py and persist.py
# Seeing EITHER file should trigger "search for duplication"
critic_scopes_expected_to_recall:
  - [src/types.py]
  - [src/persist.py]
```

**Multi-file required (missing abstraction):**

```yaml
# Client duplicates logic that exists in utils
# Need to see both to notice the redundancy
critic_scopes_expected_to_recall:
  - [src/client.py, src/utils.py]
```

## Data Model (Python)

The YAML structures are validated by these Pydantic models:

### Issue (True Positive)

```python
class TruePositive(BaseModel):
    rationale: str              # 10-5000 characters
    should_flag: Literal[True]
    occurrences: list[TruePositiveOccurrence]

class TruePositiveOccurrence(BaseModel):
    occurrence_id: str
    files: dict[Path, list[LineRange] | None]
    note: str | None = None     # Required if multiple occurrences
    critic_scopes_expected_to_recall: set[frozenset[Path]]
    match_file_restriction: set[Path] | None = None

class LineRange(BaseModel):
    start_line: int             # 1-based, >= 1
    end_line: int | None        # 1-based, inclusive, None for single line
```

### FalsePositive

```python
class FalsePositive(BaseModel):
    rationale: str              # 10-5000 characters
    should_flag: Literal[False]
    occurrences: list[FalsePositiveOccurrence]

class FalsePositiveOccurrence(BaseModel):
    occurrence_id: str
    files: dict[Path, list[LineRange] | None]
    note: str | None = None     # Required if multiple occurrences
    relevant_files: set[Path]
    match_file_restriction: set[Path] | None = None
```

## Validation Rules

### Snapshot Slugs

- Format: `{project}/{YYYY-MM-DD-NN}`
- Date is snapshot creation date
- NN is zero-padded sequence number for that day (00, 01, ...)

### File Paths

- Must match hydrated bundle structure exactly
- Use forward slashes (Unix-style paths)

### Line Ranges

- Always use `list[[start, end], ...]` format
- 1-indexed (first line is 1, not 0)
- Inclusive on both ends: `[10, 20]` means lines 10-20
- Single line: `[10, 10]` (no bare integers)

### Rationale

- Must be 10-5000 characters (after whitespace stripping)
- Use `|` for multi-line YAML strings

### critic_scopes_expected_to_recall

- Each inner list must be a subset of files mentioned in `files` (for that occurrence)
- Cannot be empty list
- At least one alternative file set must be provided

### Multi-occurrence Issues

- All occurrences MUST have `note` field when there are multiple occurrences
- If total unique files across ALL occurrences > 1, EVERY occurrence must have explicit `critic_scopes_expected_to_recall`

## File Naming

Issue files use descriptive slugs (lowercase with hyphens), not numerical indices:

- ✅ Good: `dead-code.yaml`, `missing-types.yaml`, `duplicate-logic.yaml`
- ❌ Bad: `issue-001.yaml`, `iss-032.yaml`

**Prefer shorter names when meaning is preserved.** Verbose names add noise without value.

@canonical_slugs.md

**General examples:**

- ✅ `swallowed-exceptions.yaml` not `ui-swallowed-exceptions.yaml`
- ✅ `unused-params.yaml` not `unused-function-parameters.yaml`

Slugs should be 0-30 characters and convey the issue type.

## YAML Style

- Use `|` for multi-line rationale strings
- Line ranges: always `list[[start, end], ...]` format, single lines as `[N, N]`
- Minimal comments: prefer structured fields over comments

## Related Documentation

- [Authoring Guide](authoring_guide.md) - How to write good specimens
- [Quality Checklist](quality_checklist.md) - Pre-commit verification
- System integration: See <../../README.md>
