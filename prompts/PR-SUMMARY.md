# PR: Code Quality Improvements and Antipattern Scan Prompts

## Branch
`claude/explore-repo-structure-01FC6GZeYG1RZUiNnRHT6UJj` → `devel`

## Summary

This PR contains two main components:
1. **Code cleanups**: Removal of antipatterns identified during analysis
2. **Scan prompts**: Systematic documentation for finding and fixing similar antipatterns

## Code Changes (Commits 1-4)

### 1. Remove unnecessary type annotations and casts (72fa28f)
- Removed 7+ instances of mypy-appeasing code that added no value
- Eliminated unnecessary `cast()` on `model_dump()` returns
- Removed intermediate variables that existed only for type annotation
- Removed redundant `isinstance` assertions

### 2. Create text extraction consolidation module (036c68c)
- Created `adgn/openai_utils/text_extraction.py` with unified utilities
- Functions: `first_assistant_text()`, `try_first_assistant_text()`, `all_assistant_text()`, `concatenate_assistant_text()`
- Replaced duplicate text extraction patterns across codebase

### 3. Remove unnecessary casts from Pydantic model_dump() (c66066c)
- Verified that Pydantic's `model_dump()` already returns `dict[str, Any]`
- Removed casts that were appeasing mypy unnecessarily

### 4. Remove trivial wrappers and javadoc-style documentation (0f8a930)
- Removed `dump_response()`, `dump_error()`, `dump_usage()` trivial wrappers
- Updated call sites to use `model_dump(mode="json")` directly
- Stripped verbose Args/Returns docstrings that merely repeated signatures
- **Net**: Removed 106 lines of redundant code

## Documentation (Commit 5)

### 5. Add code quality scan prompts (b3573e4)

Created systematic scan prompts in `prompts/`:

#### Scan Prompts (`prompts/scans/`)
- **trivial-forwarders.md**: Detect functions that just forward to others
- **mypy-appeasing-code.md**: Find unnecessary casts, typed variables, isinstance checks
- **pydantic-antipatterns.md**: Identify manual serialization instead of using Pydantic features
- **useless-documentation.md**: Scan for javadoc-style docs that repeat signatures
- **library-type-misuse.md**: Find code that doesn't use library types properly

#### Findings Documents (`prompts/`)
- **findings-type-stubs.md**: Available type stubs for sqlalchemy and pygit2
  - Both `types-sqlalchemy` and `types-pygit2` exist on PyPI but aren't installed
  - Could eliminate some remaining casts

- **findings-to-db-payload.md**: Analysis of `to_db_payload()` antipattern
  - Currently uses repetitive manual `model_dump(mode="json")` per field
  - Recommended fix: Use Pydantic serialization aliases (`Field(serialization_alias=...)`)
  - Alternative: Rename database columns to match model fields

#### Shared Context (`prompts/shared-context.md`)
- Philosophy: Type safety without ugliness, trust library types, read source
- Common library type patterns (OpenAI SDK, Pydantic, SQLAlchemy)
- Tools available: mypy, ruff, vulture, AST analysis

## Impact

**Lines changed**: +1,216 additions, -106 deletions
- Code cleanup: -106 lines (removed redundancy)
- Documentation: +1,216 lines (scan prompts and findings)

**Files modified**: 6 (code), 8 (documentation)

## Testing

All code changes:
- Maintain existing functionality (no behavior changes)
- Pass type checking (mypy confirms type safety)
- Replace verbose patterns with idiomatic Python/Pydantic usage

## Follow-up Work

Based on the scan prompts, suggested next steps:
1. Install `types-sqlalchemy` and `types-pygit2` to improve type inference
2. Fix `to_db_payload()` antipattern using Pydantic serialization aliases
3. Run scans across codebase to find remaining instances of these patterns
4. Consider database migration to rename `*_json` columns to match model fields

## Benefits

**Immediate**:
- Cleaner, more maintainable code
- Better use of Pydantic's built-in features
- Improved type safety without ugliness

**Long-term**:
- Systematic approach to identifying antipatterns
- Documentation for future code reviews
- Guidance for AI agents performing code quality scans
