# Code Style Guide

Repository-wide style rules. Package-specific elaborations go in that package's AGENTS.md.

## Documentation Structure

| File      | Purpose                                 | Audience          |
| --------- | --------------------------------------- | ----------------- |
| README.md | What it is, how to run                  | Humans and agents |
| AGENTS.md | `@README.md` + agent-only prescriptions | LLM agents        |
| CLAUDE.md | Always just `@AGENTS.md`                | Claude Code       |
| STYLE.md  | Repository-wide style rules             | Everyone          |

- AGENTS.md nesting: sub-folder must not repeat parent instructions. Only what's new/different.
- `@`-transclusion: own line, supports relative paths (`@../../STYLE.md`)

### Markdown Conventions

- **Local file links**: use `@path` (transclusion), `<path>` (clickable), or `[text](path)`. Never `[path](path)`.
- **Inline code**: backtick all code tokens in prose (variables, flags, paths, env vars, globs).

## General

- **Enter async early**: single `asyncio.run()` at the top level, not scattered deep in the call stack.
- **No large code blobs in YAML/JSON**: keep scripts in native files (`.py`, `.sh`), mount via ConfigMap. Applies to blocks >5 lines.
- **No suspicious nullability**: optional fields need a clear reason. For external inputs, use `None` for absence, never `""` or `[]`.
- **No exception swallowing**: catch specific types, let exceptions propagate. Broad `except Exception:` only for cleanup-before-reraise. Real errors (bad config, I/O failure) must surface, never silently default to empty values.
- **Let exceptions propagate** to error boundaries (CLI wrapper, request handler, FastMCP tool handler). Don't catch-and-reformat at every call site.
- **Exceptions for exceptional things**: query preconditions first, don't use try/except for expected control flow.
- **Make invalid states unrepresentable**: discriminated unions over flags + optional fields. Dispatch with `isinstance`, not string comparisons (except in templates).
- **No unnecessary aliasing**: don't rename imports, alias parameters, or create re-export aliases. Import from the defining module.
- **No redundant derived fields**: don't return both a list and its `len()`. Exception: pagination `total_count`.

## Pydantic

- Access fields directly (`model.field`), not via dict access. Parse dicts at the boundary.
- Construct real models in tests, never `Mock()` or `MagicMock()`.
- Use `Field(description=...)` for per-field docs, not class docstrings listing fields.
- Use `Model.model_validate(data)` over `TypeAdapter`. Use `model_dump()` only at I/O boundaries.
- Explicit keyword arguments, not `**kwargs` unpacking.

## Itertools

- `more_itertools.one()` for exactly-one extraction (instead of `next(iter(x))` when multiple is a bug)
- `more_itertools.first()` for first-of-many
- `itertools.batched` (stdlib 3.12+) for chunking

## Build System (Bazel)

Use `main_module` for `py_binary` when source is shared with a `py_library` (avoids dep duplication for mypy):

```python
py_library(name = "foo_lib", srcs = ["foo.py"], deps = [...])
py_binary(name = "foo", main_module = "pkg.foo", deps = [":foo_lib"])
```

## Testing

- **Fixture imports in conftest.py**, not test files (avoids F811). Ruff ignores F401 in conftest via `ruff.toml`.
- **Update tests with production code**: propagate signature/behavior changes.
- **`textwrap.dedent`** for inline multiline strings in tests.
- **No lint silencing** without explicit approval.
- Use `pre-commit run --all-files` over individual tools.

## Documentation Heuristic

**Cut** anything that just restates function names, signatures, types, or obvious behavior. Keep: non-obvious edge cases, "why" comments, system context, disambiguation, test intent, TODOs/FIXMEs.

## SQLAlchemy

Prefer ORM over raw SQL for standard queries. Raw SQL acceptable for complex PostgreSQL features, performance-critical paths, or admin operations.
