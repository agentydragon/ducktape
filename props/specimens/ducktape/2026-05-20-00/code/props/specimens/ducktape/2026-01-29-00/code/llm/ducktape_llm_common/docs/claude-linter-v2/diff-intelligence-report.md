# Claude Linter Diff-Based Intelligence Report

## Executive Summary

This report analyzes how Claude's edit patches are represented in the claude-linter v1 logs and proposes a design for implementing diff-based intelligence to distinguish between in-diff violations (code Claude just added), near-diff violations (context around changes), and out-of-diff violations (unrelated existing code).

## Claude Edit Tool Representations

### 1. Edit Tool Structure

The Edit tool provides a `tool_response` with a `structuredPatch` field containing the diff information:

```json
{
  "tool_name": "Edit",
  "tool_input": {
    "file_path": "/path/to/file.py",
    "old_string": "original content",
    "new_string": "modified content",
    "replace_all": false
  },
  "tool_response": {
    "filePath": "/path/to/file.py",
    "oldString": "original content",
    "newString": "modified content",
    "originalFile": "<full file content>",
    "structuredPatch": [
      {
        "oldStart": 1,
        "oldLines": 1,
        "newStart": 1,
        "newLines": 1,
        "lines": ["-old line", "+new line"]
      }
    ],
    "userModified": false,
    "replaceAll": false
  }
}
```

### 2. MultiEdit Tool Structure

MultiEdit provides similar structure but with arrays:

```json
{
  "tool_name": "MultiEdit",
  "tool_input": {
    "file_path": "/path/to/file.py",
    "edits": [
      {
        "old_string": "first old",
        "new_string": "first new",
        "replace_all": false
      },
      {
        "old_string": "second old",
        "new_string": "second new",
        "replace_all": false
      }
    ]
  },
  "tool_response": {
    "filePath": "/path/to/file.py",
    "edits": [...],
    "originalFileContents": "<full original>",
    "structuredPatch": [
      {
        "oldStart": 29,
        "oldLines": 6,
        "newStart": 29,
        "newLines": 12,
        "lines": [...]
      },
      {
        "oldStart": 39,
        "oldLines": 8,
        "newStart": 45,
        "newLines": 14,
        "lines": [...]
      }
    ],
    "userModified": false
  }
}
```

### 3. Write Tool Structure

Write tool doesn't provide patches but contains the full new content:

```json
{
  "tool_name": "Write",
  "tool_input": {
    "file_path": "/path/to/file.py",
    "content": "full new content"
  }
}
```

## Key Observations

1. **Structured Patch Format**: The `structuredPatch` array contains hunks with:
   - `oldStart`/`oldLines`: Position and count in original file
   - `newStart`/`newLines`: Position and count in new file
   - `lines`: Array of diff lines prefixed with `-`, `+`, or space

2. **Line Prefixes**:
   - `-` prefix: Removed line
   - `+` prefix: Added line
   - Space or no prefix: Context line
   - `\ No newline at end of file`: Special marker

3. **Multiple Hunks**: MultiEdit can produce multiple hunks in the structuredPatch array

4. **Line Number Shifts**: After first hunk, subsequent hunks must account for line number changes

## Data Structures Needed

### 1. Diff Parser

```python
@dataclass
class DiffLine:
    line_number: int  # Line number in final file
    content: str
    change_type: Literal["added", "removed", "context"]
    hunk_index: int  # Which hunk this belongs to

@dataclass
class DiffHunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[DiffLine]

@dataclass
class ParsedDiff:
    file_path: str
    hunks: List[DiffHunk]
    added_lines: Set[int]  # Line numbers in final file
    removed_lines: Set[int]  # Line numbers in original file
    context_lines: Set[int]  # Unchanged lines near changes
```

### 2. Violation Categorizer

```python
@dataclass
class CategorizedViolation:
    line_number: int
    message: str
    category: Literal["in-diff", "near-diff", "out-of-diff"]
    distance_from_change: Optional[int]  # For near-diff
```

## Algorithm for Categorizing Violations

```python
def categorize_violations(
    violations: List[Violation],
    parsed_diff: ParsedDiff,
    context_distance: int = 3
) -> List[CategorizedViolation]:
    """
    Categorize violations based on their proximity to changes.

    Args:
        violations: List of violations with line numbers
        parsed_diff: Parsed diff information
        context_distance: Lines away from change to consider "near"
    """
    categorized = []

    # Build set of all changed lines and their neighbors
    changed_lines = parsed_diff.added_lines
    near_lines = set()

    for line in changed_lines:
        for offset in range(-context_distance, context_distance + 1):
            if offset != 0:  # Don't include the changed line itself
                near_lines.add(line + offset)

    for violation in violations:
        if violation.line_number in changed_lines:
            category = "in-diff"
            distance = 0
        elif violation.line_number in near_lines:
            category = "near-diff"
            # Calculate minimum distance to any changed line
            distance = min(
                abs(violation.line_number - changed_line)
                for changed_line in changed_lines
            )
        else:
            category = "out-of-diff"
            distance = None

        categorized.append(CategorizedViolation(
            line_number=violation.line_number,
            message=violation.message,
            category=category,
            distance_from_change=distance
        ))

    return categorized
```

## Edge Cases to Handle

### 1. Multiple Sequential Edits

When Claude makes multiple edits to the same file, line numbers shift:

- First edit at lines 10-15 adds 5 lines
- Second edit originally at line 30 is now at line 35
- Must track cumulative line shifts

### 2. New File Creation

- Write tool with non-existent file path
- All violations are "in-diff" since entire file is new

### 3. Line Number Mapping

After edit patches are applied:

- Violations report line numbers in the final file
- Must map these back to understand if they're in added lines

### 4. Whitespace-Only Changes

- Changes that only modify whitespace
- Should still be considered "in-diff"

### 5. Replace All

- When `replace_all: true`, multiple hunks may be generated
- Each occurrence creates a separate hunk

### 6. Context Lines

- Diff includes context lines (unchanged lines around changes)
- These should not be considered "in-diff"

### 7. Adjacent Line Changes

- When changes are on consecutive lines
- May appear as single hunk or multiple hunks

### 8. File Moves/Renames

- Not represented in current Edit/MultiEdit tools
- Would need special handling if added

## Implementation Plan

### Phase 1: Diff Parser

1. Create diff parser that extracts structured patch data
2. Build line number mappings (original → final)
3. Identify added, removed, and context lines

### Phase 2: Violation Parser

1. Parse pre-commit output to extract violations with line numbers
2. Handle different linter output formats
3. Create standardized violation representation

### Phase 3: Categorization Engine

1. Implement categorization algorithm
2. Add configurable context distance
3. Handle edge cases

### Phase 4: Reporting

1. Group violations by category
2. Prioritize in-diff violations
3. Provide actionable feedback to Claude

### Phase 5: Hook Integration

1. Integrate with existing hook system
2. Modify response messages based on categories
3. Consider different handling for each category

## Example Implementation

```python
class DiffIntelligence:
    def __init__(self, context_distance: int = 3):
        self.context_distance = context_distance

    def parse_tool_response(self, tool_response: dict) -> ParsedDiff:
        """Parse Edit/MultiEdit tool response into ParsedDiff."""
        # Implementation here

    def parse_violations(self, precommit_output: str) -> List[Violation]:
        """Parse pre-commit output for violations."""
        # Implementation here

    def categorize(
        self,
        tool_response: dict,
        precommit_output: str
    ) -> Dict[str, List[CategorizedViolation]]:
        """Main entry point for categorization."""
        parsed_diff = self.parse_tool_response(tool_response)
        violations = self.parse_violations(precommit_output)
        categorized = self.categorize_violations(violations, parsed_diff)

        # Group by category
        return {
            "in-diff": [v for v in categorized if v.category == "in-diff"],
            "near-diff": [v for v in categorized if v.category == "near-diff"],
            "out-of-diff": [v for v in categorized if v.category == "out-of-diff"],
        }
```

## Recommendations

1. **Start Simple**: Begin with basic in-diff detection for Edit tool
2. **Test Thoroughly**: Create test cases for all edge cases
3. **Iterate**: Refine context distance based on real usage
4. **User Experience**: Provide clear, actionable messages for each category
5. **Performance**: Cache parsed diffs if processing multiple times
6. **Extensibility**: Design to support future tool types (e.g., file moves)

## Next Steps

1. Review this design with the team
2. Create proof-of-concept implementation
3. Test with real Claude edit logs
4. Refine based on testing results
5. Integrate into claude-linter v2
