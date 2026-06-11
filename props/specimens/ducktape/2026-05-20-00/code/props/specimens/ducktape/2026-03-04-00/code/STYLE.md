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
  - **Nesting/inheritance**: Agents read AGENTS.md files from the repo root down to the current package. A sub-folder AGENTS.md must not repeat instructions already covered by a parent AGENTS.md. If `foo/AGENTS.md` says X, `foo/bar/AGENTS.md` should not repeat X. Only document what is new or different at this level.
- **CLAUDE.md**: Always exactly one line: `@AGENTS.md`
- **STYLE.md**: Repository-wide rules. If a rule applies across packages, it belongs here, not in a package's AGENTS.md.

### @-Transclusion Syntax

- Must be on its own line: `@AGENTS.md`
- Supports relative paths: `@../../STYLE.md`
- Must be the only content on that line

## General

- **No large code blobs embedded in YAML/JSON**: Do not inline Python scripts, shell scripts, or large configuration blocks as string values in Kubernetes manifests, Helm values, or other YAML/JSON files. Embedded code cannot be linted, formatted, or type-checked by standard tools (ruff, shellcheck, mypy). Instead, keep code in its native file format (`.py`, `.sh`) and mount it into containers via `configMapGenerator` (kustomize) or `ConfigMap` with file references. This applies to any embedded block longer than ~5 lines.
- **Imports at module top**: Place all imports at the top of files. Only use in-function imports to break a proven circular dependency, and add a one-line comment at that import explaining the cycle it avoids.
- **No suspicious nullability**: If a field is optional, it must be for a clear transitional reason or represent an intentional, valid state with defined behavior. Otherwise, model as non-nullable and remove guards.
- **No dead code**: Remove unused code, unused imports, and historical comments that no longer reflect the behavior.
- **No redundant derived fields**: API responses should not include both a collection and a trivially computable function of it (e.g., returning both a list and its `len()`). The consumer can compute `len(items)` themselves. Exception: pagination, where `total_count` represents the full count before offset/limit slicing — this is not derivable from the returned page.
- **No unnecessary aliasing**: Avoid renaming imports (`import foo as bar`), assigning fixtures to local variables, trivial parameter aliasing (`slug = snapshot_slug`), or any other form of aliasing unless it adds real semantic value or is required to avoid a collision. Prefer using variables under their actual names. Include a comment when an alias is genuinely needed. Do not create "convenience" re-export aliases like `AgentEvent = EventType` — import from the module that actually defines the symbol. Aliases are only justified at public API boundaries (e.g., `__init__.py` re-exports for external consumers) or when adapting between internal/external naming conventions.
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
- **Exceptions are for exceptional things**: Don't use exceptions for expected control flow. Prefer: query preconditions first, then execute without catch. Avoid: execute with catch, then parse what went wrong from the exception.

  ```python
  # ❌ Using exception for expected control flow
  try:
      info = supervisor.get_process_info(name)
      return info.state == "RUNNING"
  except Fault as e:
      if e.faultCode == BAD_NAME:
          return False  # Service doesn't exist - but this was foreseeable
      raise

  # ✓ Query preconditions first
  if not supervisor.service_exists(name):
      return False
  info = supervisor.get_process_info(name)
  return info.state == "RUNNING"
  ```

- **Strict data mapping**: When parsing enums/typed values from persistence or inputs, do not ignore invalid values. Validate early or raise; do not `continue` on exceptions.
- **Prefer functional style**: Use concise comprehensions and idiomatic patterns. Keep public interfaces typed with Pydantic where appropriate.
- **Prefer precise types**: Use discriminated unions, Protocols, TypedDicts, or concrete Pydantic models for heterogeneous values. `Any`/`object` is acceptable only when a field truly allows any value and no stronger contract exists; document such cases.
- **Make invalid states unrepresentable**: Prefer algebraic types (discriminated unions) over flags and optional fields that allow nonsensical combinations. A model with `hook_installed: bool` and `pid: int | None` permits `hook_installed=False, pid=42` — instead use a union of distinct result types where each variant carries exactly the fields that make sense for it. In Python code, dispatch on union variants with `isinstance` checks (which let mypy narrow the type), not string comparisons on discriminator fields. In templates (Mako/Jinja) where `isinstance` is unavailable, `kind` string checks are acceptable.
- **Typed concurrency messages**: Actor/mailbox patterns should use explicit dataclasses or Pydantic models for messages and result types—never `dict[str, T]`.
- **Use Pydantic as typed objects**: Access fields directly (`model.field`), not via `dict.get(...)` or `model["field"]`. Parse dicts into typed models at the boundary. Only use `dict.get(...)` for truly untyped external payloads (raw DB rows, HTTP headers, environment vars).
- **Construct Pydantic models, not dicts**: When creating test data or structured values, construct typed Pydantic models directly (`BaseExecResult(exit=Exited(exit_code=0), ...)`) rather than building dicts that match the schema. This ensures mypy catches field renames and type changes at every construction site, instead of silently producing invalid dicts.
- **Never mock Pydantic models**: Do not use `Mock()` or `MagicMock()` to fake Pydantic models in tests—construct real instances with test data. Mocking bypasses validation and hides type errors.
- **No unnecessary model_dump()**: Use typed attributes on Pydantic objects directly. Dump only at I/O boundaries (logging/serialization), not to re-parse fields for logic.
- **Explicit keyword arguments**: Instantiate Pydantic models and call functions with explicit keyword arguments (`Model(field=value)`) rather than `**kwargs` unpacking when the arguments are known. Prefer `Model.model_validate(data)` over `TypeAdapter(...).validate_python(...)` unless you explicitly need adapter semantics.
- **Use enum values directly**: Reference enum values as `EnumClass.VALUE`, not as string literals. For StrEnum, use the value directly without `.value` (StrEnum instances are already strings): `f"{AgentType.CRITIC}_suffix"` not `f"{AgentType.CRITIC.value}_suffix"` or `f"critic_suffix"`.
- **Compact CLI output**: CLI output should preserve vertical space. Merge related information onto single lines instead of spreading across multiple lines without good reason. Vertical space is at a premium.
- **Use `{x=}` f-string debugging format**: In error messages, log messages, and debug strings, prefer `f"{x=}"` over `f"x={x!r}"` or `f"x={x}"`. The `=` specifier is more concise and automatically includes the variable name and `repr()`.
- **Logging**: In modules that log, declare a module-level logger at the top: `logger = logging.getLogger(__name__)`. Do not call `logging.getLogger(...)` inside functions/classes. Do not store the logger on `self`.
- **Paths**: Prefer `pathlib.Path` objects; only call `str(path)` when an external API requires a string.
- **No string forward references**: Avoid string-based forward references in type annotations. Reorder classes or split files to remove cycles. When cross-module cycles exist, use `if TYPE_CHECKING:` imports with real symbols (not quoted names). Do not rely on `model_rebuild()` where reordering can avoid forward refs.
- **No unnecessary `__init__.py`**: Do not create `__init__.py` files for packages that only contain Bazel targets. Bazel auto-generates stub `__init__.py` files via `imports = [...]` in `py_library`/`py_test` rules. Only create `__init__.py` when you need to expose a public API or configure the package namespace.
- **Avoid `__all__` unless required**: Do not define `__all__` in modules unless there's a specific need (e.g., controlling `from module import *` behavior for a public API, or suppressing lint warnings for re-exports in `__init__.py`). Most modules don't need it.
- **Prefer sets for unordered collections**: When a collection's order is semantically irrelevant (changed files, unique IDs, tags), use `set[T]` instead of `list[T]`. Sets make the "no duplicates, order doesn't matter" intent explicit and provide O(1) membership testing. Use lists only when order matters or duplicates are valid.
- **Prefer `more_itertools` over manual iterator patterns**: Use `more_itertools` when it expresses intent more clearly than raw Python. Key utilities:
  - `one(iterable)` — extract the single element, raise if zero or multiple. Use instead of `next(iter(x))` when exactly one item is expected. Prefer over length checks followed by indexing. Also use for searching a collection for exactly one expected match (e.g., `one(x for x in items if x.id == target_id)`) instead of handrolled filter-and-check loops.
  - `first(iterable)` / `first(iterable, default=...)` — get the first element. Use instead of `next(iter(x))` or `next(iter(x), default)` when taking the first of potentially many.
  - Use `itertools.batched` (stdlib since 3.12) instead of `for i in range(0, len(x), n): batch = x[i:i+n]`.
  - Choose `one()` vs `first()` based on semantics: if receiving multiple items is a bug, use `one()` to enforce the invariant. If multiple items are valid and you want the first, use `first()`.

## SQLAlchemy

- **Prefer SQLAlchemy ORM over raw SQL**: In projects using SQLAlchemy, prefer the ORM query interface over raw SQL strings. The ORM provides type safety, IDE support, and protection against SQL injection. Use raw SQL only when the ORM would make the query significantly less readable (e.g., complex window functions, CTEs, or database-specific features not well-supported by the ORM).

  ```python
  # ✓ Prefer ORM queries
  session.query(User).filter(User.email == email).first()
  session.query(TruePositiveOccurrenceORM).filter_by(snapshot_slug=slug).all()

  # ❌ Avoid raw SQL for simple queries
  session.execute(text("SELECT * FROM users WHERE email = :email"), {"email": email})
  ```

  Raw SQL is acceptable when:
  - Using complex PostgreSQL-specific features (window functions, recursive CTEs, LATERAL joins)
  - Performance-critical queries where the ORM generates suboptimal SQL
  - Database administrative operations (GRANT, CREATE FUNCTION, etc.)
  - The equivalent ORM query would be significantly harder to read or maintain

## Build System (Bazel)

- **Use `main_module` for `py_binary` targets**: When a `py_binary` needs the same source file as a `py_library`, use `main_module` instead of duplicating `srcs`. The `py_library` declares all deps; the `py_binary` just references the library. This avoids dep duplication and ensures the mypy lint aspect checks deps in one place.

  ```python
  py_library(
      name = "session_start_lib",
      srcs = ["session_start.py"],
      imports = ["../.."],
      deps = [":settings", "@pypi//mako"],
  )

  # CORRECT - main_module, no srcs
  py_binary(
      name = "session_start",
      main_module = "tools.claude_hooks.session_start",
      imports = ["../.."],
      deps = [":session_start_lib"],
  )

  # WRONG - duplicates srcs and requires duplicating deps for mypy
  py_binary(
      name = "session_start",
      srcs = ["session_start.py"],
      deps = [":session_start_lib", "@pypi//mako"],
  )
  ```

## Testing

- **Test file placement**: Tests for module `a/b/c.py` should be in `a/b/test_c.py`. Keep test files adjacent to the modules they test. This makes it easy to find tests and ensures test coverage is visible in directory listings. Integration tests that span multiple modules can use descriptive names like `test_agent_mcp_integration.py`.
- **DRY test fixtures**: Extract shared setup logic into pytest fixtures. Avoid duplicating fixture definitions across test files. Prefer conftest.py for fixtures used by multiple test modules.
- **Fixture imports belong in conftest.py, not test files**: Never import pytest fixtures directly in `test_*.py` files. Instead, add fixture imports to the nearest `conftest.py`. Ruff ignores F401 in conftest files via `per-file-ignores` in `ruff.toml` (`"**/conftest.py" = ["F401"]`), so no `# noqa` comments are needed. Importing fixtures in test files causes F811 (redefinition) when the same name appears as a test function parameter.
- **Concise test bodies**: Keep test functions focused on assertions. Delegate setup to fixtures.
- **Update tests with production code**: When editing production code, check what tests use the interfaces you touched and propagate edits. Type signature changes, parameter changes, renamed functions, or changed behavior require corresponding test updates.
- **No lint silencing without approval**: Do not add ignore rules or silence individual lint errors unless explicitly approved.
- **Use pre-commit**: Prefer `pre-commit run --all-files` over manually running individual tools (ruff, mypy, etc.) since pre-commit is configured correctly for this repository.
- **Use `textwrap.dedent` for inline multiline strings**: When embedding multiline strings in tests (YAML, JSON, scripts), use `textwrap.dedent()` to maintain proper indentation in the test file while removing leading whitespace from the string content:

  ```python
  from textwrap import dedent

  # ✓ Readable indentation with dedent
  yaml_content = dedent("""
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: test
  """)

  # ❌ Flush-left strings break visual flow
  yaml_content = """
  apiVersion: v1
  kind: ConfigMap
  metadata:
    name: test
  """
  ```

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

### Inline Code in Prose

Use backtick inline code (`` `...` ``) or fenced code blocks for any code-like token in markdown prose: variable names, function names, CLI flags, file paths, config keys, env vars, glob patterns, hostnames, and similar. Do not write code tokens as plain text — formatters like prettier will escape special characters (e.g., `*` → `\*`), and unformatted code tokens are harder to read.

```markdown
# Good

- Reduce `file-read*` to the minimum required
- Set `JUPYTER_*` dirs and `HOME`
- Map `fs.read_paths` → ro binds

# Bad — code tokens as plain text (prettier will mangle the \*)

- Reduce file-read\* to the minimum required
- Set JUPYTER\_\* dirs and HOME
- Map fs.read_paths → ro binds
```
