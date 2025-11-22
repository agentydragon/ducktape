# Instructions for Authoring Specimen Files

## File Structure

```
specimen-name/
├── manifest.yaml       # VCS source, commit ref, scope (include patterns)
├── README.md          # Brief overview with issue list (no detailed descriptions)
├── CLAUDE.md          # This file - authoring instructions
└── issues/
    ├── 001.libsonnet  # Detailed issue with rationale, properties, locations
    ├── 002.libsonnet
    └── ...
```

## Critical: Specimens are Frozen Snapshots

**Specimens are training/evaluation data representing code quality issues at a specific commit.**

- Each specimen is pinned to a specific commit (see `manifest.yaml` `ref` field)
- Issue files (`.libsonnet`) describe what was **wrong at that commit**
- **NEVER** update issue files to record resolution status or mark issues "COMPLETED"
- Issue files should remain accurate descriptions of problems as they existed
- Fixes happen on separate branches; specimens remain unchanged historical records
- Think of specimens like labeled training data: the label describes the frozen state

**Example violations:**
- ❌ Adding "Status: COMPLETED" or "Note: This was fixed in commit X"
- ❌ Updating rationale to say "This issue has been resolved"
- ❌ Removing or modifying issue descriptions after fixes are made

**Correct approach:**
- ✅ Record issues as they exist at the snapshot commit
- ✅ Fix issues on separate branches without modifying specimen files
- ✅ Create new specimens for new commits if you want to capture improvements

## Authoring Rules

### 1. Single Source of Truth: Jsonnet Files

**All detailed issue information belongs in `issues/*.libsonnet` files only.**

Each `.libsonnet` file contains:
- **Rationale**: Full explanation of what's wrong and why
- **Properties violated**: List of property IDs from `props/`
- **File locations**: Exact paths and line ranges
- **GAP notes** (optional): Missing/unclear properties that should exist
- **Comments**: Inline comments at line ranges explaining context

**Do NOT duplicate this information in README.md or other files.**

### 2. README.md: Brief Overview Only

README.md should contain:
- **Purpose**: 1-2 sentence specimen description
- **Issues**: Bullet list with issue numbers and one-line summaries
  - Format: `- **NNN**: Brief title (affected symbol/file)`
  - Example: `- **001**: Normalization function for type that cannot occur (_normalize_call_arguments)`
- **Scope**: High-level scope description
- **Reference line**: "See `issues/*.libsonnet` for detailed rationale, properties violated, and file locations."

**Do NOT include**:
- Full rationale or problem explanations
- Code snippets or examples
- Properties violated lists
- "Correct behavior" sections
- Detailed analysis

### 3. Jsonnet Issue File Template

**IMPORTANT**: Keep comments MINIMAL. Use only a one-line title comment. All details belong in the structured fields (`rationale`, `properties`, `filesToRanges`).

```jsonnet
local I = import '../../specimens/lib.libsonnet';

// iss-NNN: Brief one-line title

I.issueOneOccurrence(
  rationale=|||
    Full explanation of the problem.

    Why it's wrong and what the correct approach should be.
    Include specific details, code patterns, and reasoning.

    All context, properties violated, fix recommendations go HERE,
    not in top-of-file comment blocks.
  |||,
  properties=['property-id', 'category/property-id'],
  filesToRanges={
    'path/to/file.py': [
      123,              // Single line with brief context
      [200, 210],       // Range with brief note
    ],
    'other/file.py': [
      [45, 50],         // Multiple locations OK
    ],
  },
  gap_note=|||
    Optional: Missing/unclear properties that should exist.
  |||,
)
```

### 4. Comments: What's Allowed vs Duplication

**✅ ALLOWED - Inline comments at line ranges:**
```jsonnet
filesToRanges={
  'foo.py': [
    [86, 89],   // --mcp-config: silent fallback
    [92, 93],   // --initial-policy: same pattern
  ],
}
```
These are brief labels helping locate code quickly. Not duplication.

**❌ FORBIDDEN - Large top-of-file comment blocks:**
```jsonnet
// Context:
// - User provides --mcp-config path
// - Code checks if file exists
// - If not: silently falls back without error
//
// Properties violated:
// 1. truthfulness: Silent failure masks error
// 2. no-swallowing-errors: Error ignored
//
// Fix: Remove exists() check or raise error
```
This duplicates the `rationale` field. **Delete these blocks.**

**Rule**: If information appears in structured fields (`rationale`, `properties`, `gap_note`), do NOT repeat it in comments.

### 5. When to Use GAP Notes

Use `gap_note` to document **gaps in the property taxonomy** - when a finding relates to existing properties but also represents a generalizable principle that deserves its own property definition.

Use `gap_note` when:
- Issue is covered by existing property (list it in `properties`)
- But the finding represents a more specific pattern that deserves its own property
- You want to document what property SHOULD exist without creating it yet
- The gap note describes the abstraction gap between existing and ideal properties

**What to include in gap_note:**
- Description of the generalizable principle
- Suggested property name (e.g., "fail-fast-on-missing-explicit-inputs")
- How it differs from/refines existing properties

**What NOT to use gap_note for:**
- General recommendations or notes (those go in `rationale`)
- Location-specific details (those go in occurrence `note`)
- Property violations (those go in `properties` array)

Example:
```jsonnet
properties=['no-swallowing-errors'],  // Existing property that covers this
gap_note=|||
  This pattern deserves a more specific property like "fail-fast-on-missing-explicit-inputs"
  to distinguish between:
  - Intentionally-missing optional files (acceptable to ignore)
  - User-explicitly-provided file paths that are missing (should fail-fast)

  The existing "no-swallowing-errors" is too generic to capture this distinction.
|||
```

## Why This Structure?

1. **DRY**: One authoritative description per issue (in Jsonnet)
2. **Tooling-friendly**: Jsonnet is machine-readable for analysis tools
3. **Human-friendly**: README provides navigation, Jsonnet provides depth
4. **Maintainable**: Updates happen in one place only
5. **Composable**: Tools can combine/aggregate issues from multiple specimens

## When Adding New Issues

1. Create `issues/NNN.libsonnet` with full details
2. Add one-line summary to README.md issue list
3. Commit with message: `feat(props): add issue NNN - brief-title`
4. **DO NOT** copy rationale/analysis into README.md or commit message details
