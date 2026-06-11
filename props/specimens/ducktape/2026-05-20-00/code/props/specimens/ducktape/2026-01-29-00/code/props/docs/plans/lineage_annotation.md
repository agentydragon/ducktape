# LineRange Annotations Design

## Overview

This document describes the design for adding inline annotations (comments/notes) to individual line ranges within issue occurrences. Currently, notes can only be attached at the Occurrence level, but fine-grained per-line annotations would improve clarity and reduce verbosity in rationale text.

## Current State

### Pydantic Schema (models/issue.py)

```python
class LineRange(BaseModel):
    start_line: int = Field(..., ge=1, description="1-based start line number")
    end_line: int | None = Field(
        default=None, description="1-based end line number (inclusive); omit for single-line anchor"
    )
```

- No annotation field on LineRange
- Notes can only be attached at the Occurrence level via `Occurrence.note`

### YAML Format

Current specimens use YAML format. Line ranges are specified as integers or `[start, end]` arrays.

## Proposed Design

### 1. Pydantic Schema Changes

Add an optional `note` field to `LineRange`:

```python
class LineRange(BaseModel):
    start_line: int = Field(..., ge=1, description="1-based start line number")
    end_line: int | None = Field(
        default=None, description="1-based end line number (inclusive); omit for single-line anchor"
    )
    note: str | None = Field(
        default=None,
        description="Optional inline annotation/comment for this specific line range"
    )

    @model_validator(mode="after")
    def _validate_range(self) -> LineRange:
        if self.start_line < 1:
            raise ValueError("start_line must be >= 1")
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line when provided")
        return self

    model_config = ConfigDict(extra="forbid")
```

### 2. YAML Format Support

Support multiple formats for line ranges in YAML:

```yaml
# Single line (integer)
- 123

# Range (2-element array)
- [123, 155]

# Single line with note (object)
- start_line: 123
  note: "validation logic"

# Range with note (object)
- start_line: 78
  end_line: 92
  note: "API response handling"
```

### 3. Semantic Distinction

**Two levels of notes:**

1. **LineRange.note** (new): Inline annotation for a specific code location
   - Short, contextual comment about what's at that location
   - Example: "main validation logic", "error handling", "config parsing"
   - Keep brief - just enough to distinguish this location from others

2. **Occurrence.note** (existing): Explanation for this occurrence as a whole
   - Why this occurrence is significant or unique
   - How it differs from other occurrences of the same issue
   - When to use: multi-file occurrences, special context needed

**Usage guidelines:**

- Prefer LineRange notes for simple location labels
- Use Occurrence notes when multiple files/ranges need collective explanation
- Don't duplicate rationale - put global explanation in `IssueCore.rationale`

### 4. Example Usage

```yaml
rationale: |
  Manual JSON parsing without schema validation.
  Should use Pydantic TypeAdapter for automatic validation.
occurrences:
  - files:
      src/app.py:
        - start_line: 45
          note: "user config parsing"
        - start_line: 78
          end_line: 92
          note: "API response handling"
        - [120, 120] # Single line without note
        - [200, 210] # Range without note
```

With occurrence-level note:

```yaml
rationale: |
  Dead code that should be removed.
occurrences:
  - files:
      src/utils.py:
        - start_line: 45
          note: "unused helper function"
        - start_line: 78
          end_line: 92
          note: "dead import section"
    note: "These were part of the old caching system before refactor"
```

### 5. Migration Path

**Backward compatibility:**

- Existing specimens without line notes continue to work (note field is optional)
- Existing occurrence-level notes are unchanged
- No data migration required for existing issues

**Adoption:**

- New specimens can use line-level notes for clarity
- Existing specimens can be gradually enhanced if needed
- Consider line notes when occurrence note would just list locations

### 6. Implementation Tasks

1. **Pydantic schema** (models/issue.py):
   - Add `note: str | None` field to `LineRange`
   - Update tests to cover annotated line ranges

2. **YAML parsing**:
   - Ensure object-form line ranges parse correctly
   - Test mixed formats (integers, arrays, objects)

3. **Validation**:
   - Ensure line notes are preserved through load/dump cycles
   - Verify backward compatibility with existing specimens

4. **Documentation**:
   - Update specimens authoring guide with line annotation examples
   - Add usage examples to key documentation

## Alternatives Considered

### Alternative 1: Dict-based line specs only

Require dict form for all annotated ranges: `{start_line: 123, end_line: 155, note: "text"}`

**Rejected because:**

- More verbose for simple cases
- Loses conciseness of integer/array notation for unannotated ranges

### Alternative 2: Separate annotations dict

Keep LineRange simple, add `annotations: dict[tuple[int, int | None], str]` to Occurrence

**Rejected because:**

- Awkward tuple keys in YAML
- Complicates lookup and validation
- Less ergonomic for authoring

### Alternative 3: Keep everything in occurrence notes

Continue using only occurrence-level notes with inline text

**Rejected because:**

- Causes verbosity in rationale and occurrence notes
- Makes programmatic extraction harder
- Doesn't provide structured location context

## Open Questions

1. **Character limit for line notes?**
   - Recommendation: Keep them brief (< 100 chars), but don't enforce hard limit
   - Longer explanations should go in occurrence note or rationale

2. **Should line notes support markdown?**
   - Initial answer: Plain text only for simplicity
   - Can be enhanced later if needed

3. **UI rendering?**
   - Line notes should appear inline with code locations
   - Consider truncation/tooltip for long notes
   - Out of scope for this design (UI enhancement)

## References

- Current LineRange: props/core/models/issue.py
- Occurrence schema: props/core/models/issue.py
