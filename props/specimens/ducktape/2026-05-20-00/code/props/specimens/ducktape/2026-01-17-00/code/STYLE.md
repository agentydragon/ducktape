# Code Style Guide

Style and convention rules for this repository. Package-specific elaborations belong in that package's AGENTS.md, but general rules belong here.

## Repository Documentation Structure

### File Purposes

| File      | Purpose                                       | Audience          |
| --------- | --------------------------------------------- | ----------------- |
| README.md | Descriptions (what is this, how to run)       | Humans and agents |
| AGENTS.md | Transcludes README + agent-only prescriptions | LLM agents        |
| CLAUDE.md | Always just `@AGENTS.md`                      | Claude Code       |
| STYLE.md  | Repository-wide style and convention rules    | Everyone          |

### Rules

- **README.md**: What it is, how to set up, how to run. No agent-specific instructions.
- **AGENTS.md**:
  - First line: `@README.md` (if README exists)
  - Then: Agent-specific instructions (idioms, "always do X", "never do Y", conventions)
  - Package-specific elaborations of STYLE.md rules go here
- **CLAUDE.md**: Always exactly one line: `@AGENTS.md`
- **STYLE.md**: Repository-wide rules. If a rule applies across packages, it belongs here, not in a package's AGENTS.md.

### @-Transclusion Syntax

- Must be on its own line: `@AGENTS.md`
- Supports relative paths: `@../../STYLE.md`
- Must be the only content on that line

## General

- **Imports at module top**: Place all imports at the top of files. Only use in-function imports to break a proven circular dependency, and add a one-line comment at that import explaining the cycle it avoids.
- **No suspicious nullability**: If a field is optional, it must be for a clear transitional reason or represent an intentional, valid state with defined behavior. Otherwise, model as non-nullable and remove guards.
- **No dead code**: Remove unused code, unused imports, and historical comments that no longer reflect the behavior.
- **No unnecessary aliasing**: Avoid renaming imports (`import foo as bar`), assigning fixtures to local variables, or any other form of aliasing unless it adds real value in readability or is required to avoid a collision. Include a comment when an alias is genuinely needed.
- **No dynamic attribute probing**: Avoid `getattr`/`hasattr`/`setattr` unless justified and documented. In tests, prefer direct attribute access with precise expectations.
- **No exception swallowing**: Never use bare `except:` or broad `except Exception:` as a silent fallback. Catch specific exception types, let exceptions propagate, or re-raise with precise context. Do not default to empty values on error. **Real errors must surface** - if a config file has invalid syntax, a source file won't parse, or I/O fails, that's a bug the user needs to know about. Silently returning empty defaults hides the problem.

  Bad examples (do not do these):

  ```python
  # ❌ Invalid config silently ignored - user thinks they have no config
  try:
      data = tomllib.loads(config_path.read_text())
  except (OSError, tomllib.TOMLDecodeError):
      data = {}  # Pretend config doesn't exist

  # ❌ Syntax errors in source code silently skipped
  try:
      tree = ast.parse(source)
  except (OSError, SyntaxError):
      continue  # Skip broken files without warning

  # ❌ Narrowing exception type doesn't fix the swallowing problem
  except tomllib.TOMLDecodeError:
      data = {}  # Still wrong - invalid TOML is a real error
  ```

  The fix is usually to let the exception propagate, or at minimum log a warning so the user knows something is wrong.

  **Exception**: Broad `except Exception:` is acceptable for cleanup-before-reraise patterns (like a context manager's `__exit__`). If you catch broadly, perform cleanup (rollback, close resource), then immediately `raise`, that's fine—no information is lost.

- **Prefer exceptions over error lists**: Functions that validate or check preconditions should raise exceptions on failure, not return lists of errors. Exceptions provide immediate control flow, clear error types, and standard patterns for callers.
- **Let exceptions propagate**: Self-explanatory exceptions with actionable messages should propagate to the existing error boundary (CLI wrapper, request handler, FastMCP tool handler) rather than being caught and reformatted at each call site. Define error boundaries once (e.g., in a CLI entry point or request middleware), not repeatedly throughout the code. FastMCP already converts unhandled exceptions to MCP errors with the exception message - use this pattern. Only catch exceptions when you need to transform them, add context, or handle them differently than the default boundary.
- **Strict data mapping**: When parsing enums/typed values from persistence or inputs, do not ignore invalid values. Validate early or raise; do not `continue` on exceptions.
- **Prefer functional style**: Use concise comprehensions and idiomatic patterns. Keep public interfaces typed with Pydantic where appropriate.
- **Prefer precise types**: Use discriminated unions, Protocols, TypedDicts, or concrete Pydantic models for heterogeneous values. `Any`/`object` is acceptable only when a field truly allows any value and no stronger contract exists; document such cases.
- **Typed concurrency messages**: Actor/mailbox patterns should use explicit dataclasses or Pydantic models for messages and result types—never `dict[str, T]`.
- **Use Pydantic as typed objects**: Access fields directly (`model.field`), not via `dict.get(...)` or `model["field"]`. Parse dicts into typed models at the boundary. Only use `dict.get(...)` for truly untyped external payloads (raw DB rows, HTTP headers, environment vars).
- **No unnecessary model_dump()**: Use typed attributes on Pydantic objects directly. Dump only at I/O boundaries (logging/serialization), not to re-parse fields for logic.
- **Explicit keyword arguments**: Instantiate Pydantic models and call functions with explicit keyword arguments (`Model(field=value)`) rather than `**kwargs` unpacking when the arguments are known. Prefer `Model.model_validate(data)` over `TypeAdapter(...).validate_python(...)` unless you explicitly need adapter semantics.
- **Use enum values directly**: Reference enum values as `EnumClass.VALUE`, not as string literals. For StrEnum, use the value directly without `.value` (StrEnum instances are already strings): `f"{AgentType.CRITIC}_suffix"` not `f"{AgentType.CRITIC.value}_suffix"` or `f"critic_suffix"`.
- **Compact CLI output**: CLI output should preserve vertical space. Merge related information onto single lines instead of spreading across multiple lines without good reason. Vertical space is at a premium.
- **Logging**: In modules that log, declare a module-level logger at the top: `logger = logging.getLogger(__name__)`. Do not call `logging.getLogger(...)` inside functions/classes. Do not store the logger on `self`.
- **Paths**: Prefer `pathlib.Path` objects; only call `str(path)` when an external API requires a string.
- **No string forward references**: Avoid string-based forward references in type annotations. Reorder classes or split files to remove cycles. When cross-module cycles exist, use `if TYPE_CHECKING:` imports with real symbols (not quoted names). Do not rely on `model_rebuild()` where reordering can avoid forward refs.

## Testing

- **DRY test fixtures**: Extract shared setup logic into pytest fixtures. Avoid duplicating fixture definitions across test files. Prefer conftest.py for fixtures used by multiple test modules.
- **Concise test bodies**: Keep test functions focused on assertions. Delegate setup to fixtures.
- **Update tests with production code**: When editing production code, check what tests use the interfaces you touched and propagate edits. Type signature changes, parameter changes, renamed functions, or changed behavior require corresponding test updates.
- **No lint silencing without approval**: Do not add ignore rules or silence individual lint errors unless explicitly approved.
- **Use pre-commit**: Prefer `pre-commit run --all-files` over manually running individual tools (ruff, mypy, etc.) since pre-commit is configured correctly for this repository.

## Documentation

### What to Remove

- **Restating docstrings**: Docstrings that just repeat function name, parameter names, or return type without adding insight
- **Restating comments**: Comments that describe what the next line does when the code is self-explanatory
- **Parameter echoing**: Args sections that just list parameter names and types already in the signature
- **Returns echoing**: Returns sections that restate the return type annotation
- **Trivial class docstrings**: Docstrings like "A class that represents X" where X is the class name
- **Historical comments**: Comments about removed code, old behavior, or "used to be X"
- **Section banners**: `# ========== Section ==========` comments that add visual noise without information
- **Changelog comments**: `# Added in v1.2` or `# Modified 2024-01-15` that belong in version control

### What to Keep

- **TODOs/FIXMEs**: Valid work items belong in code near the relevant context, not just in issue trackers
- **Useful module-level docstrings**: Those that concisely summarize the file's purpose when not redundant with other docs
- **Non-obvious behavior docs**: Edge cases, error conditions, invariants, contracts, preconditions ("caller must ensure..."), important caveats
- **Why comments**: Comments explaining rationale, not what the code does
- **External context docs**: Comments/docstrings explaining why something exists, how it integrates into the broader system, or its role in architecture not obvious from local context
- **System context**: What the function does in wider system context, action at a distance, mutations to shared state
- **Disambiguation docs**: Docstrings that clarify ambiguous naming (e.g., "container-side path" vs "host-side path", "UTC timestamp" vs "local time"). If a name could be misinterpreted, either rename it to be unambiguous or keep documentation that clarifies
- **Test intent comments**: Comments in tests that describe what specific edge case, subtlety, or behavior the test is verifying. These clarify the test's purpose beyond what the test name conveys and help future readers understand why the test exists

### Decision Heuristics

- **Delete test**: If removing the doc/comment loses zero information, remove it
- **Signature coverage**: If signature + types tell the whole story, docstring is redundant
- **Why vs what**: Comments explaining "why" are valuable; comments describing "what" are usually redundant
- **Non-obvious behavior**: Keep docs that explain edge cases, error conditions, or non-intuitive behavior
- **API boundaries**: Public API docs may justify more verbosity; internal code should be minimal

### Local File Links in Markdown

For references to local files in documentation:

- **For LLM agents**: Use `@path/to/file.md` transclusion syntax (on its own line)
- **For clickable links without custom text**: Use angle brackets: `<path/to/file.md>`
- **For links with custom text**: Use standard markdown: `[custom text](path/to/file.md)`

**Do NOT use** `[path/to/file.md](path/to/file.md)` - this duplicates the path unnecessarily.

```markdown
# Good

@docs/architecture.md
See <docs/schema.md> for details.
See the [architecture guide](docs/architecture.md) for details.

# Bad - duplicates path

See [docs/schema.md](docs/schema.md) for details.
```
