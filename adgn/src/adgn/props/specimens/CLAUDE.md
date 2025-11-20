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

Use `gap_note` when:
- Issue is covered by existing property (e.g., `no-dead-code`)
- But lacks a more specific/precise property definition
- You want to document the abstraction gap without creating new property yet

Example:
```jsonnet
gap_note=|||
  Both flags exhibit the same anti-pattern: user provides file path,
  file doesn't exist, code silently ignores it. This could be a distinct
  property: "fail-fast-on-missing-explicit-inputs" rather than generic
  "no-swallowing-errors".
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
