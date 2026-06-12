# Code Style Guide

Repository-wide style and convention rules. Package-specific elaborations belong
in that package's AGENTS.md. Rules are stated tersely and assume fluency with the
standard tools; examples appear only where they define a repo-specific format.

## Repository Documentation Structure

### File Purposes

| File      | Purpose                                       | Audience          |
| --------- | --------------------------------------------- | ----------------- |
| README.md | Descriptions (what is this, how to run)       | Humans and agents |
| AGENTS.md | Transcludes README + agent-only prescriptions | LLM agents        |
| CLAUDE.md | Always just `@AGENTS.md`                      | Claude Code       |
| STYLE.md  | Repository-wide style and convention rules    | Everyone          |

### Rules

- **README.md**: what it is, how to set up, how to run. No agent-specific instructions.
  Keep it lean — everything in a README ships to agents via transclusion; deep reference
  and historical context belong in `docs/` behind links.
- **AGENTS.md**: first line `@README.md` (if a README exists), then agent-only
  prescriptions. A sub-folder AGENTS.md must not repeat anything a parent AGENTS.md
  already covers — document only what is new or different at this level.
- **CLAUDE.md**: always exactly one line: `@AGENTS.md`.
- **STYLE.md**: rules that apply across packages live here, not in a package AGENTS.md.

### @-Transclusion Syntax

`@path/to/file.md` alone on its own line; relative paths supported. Transclude only
content every reader of the host file needs — task-specific docs get a `<path>` pointer
instead, so they load on demand.

## General

- **Modern Python (3.13+)**: `|` unions, `:=`, `match`.
- **Enter async early**: a single `asyncio.run(async_main(...))` at the top of `main()`;
  never scattered or nested deeper in the call stack.
- **No large code blobs in YAML/JSON**: any embedded script/config block longer than ~5
  lines lives in its native file (`.py`, `.sh`) and is mounted via `configMapGenerator`
  or a `ConfigMap` file reference, so it stays lintable and type-checkable.
- **Imports at module top**; in-function imports only to break a proven circular
  dependency, with a one-line comment naming the cycle.
- **No suspicious nullability**: an optional field must represent an intentional,
  defined absent state (or a named transition). Absence is `None` — never `""`/`[]`
  zero values. A field is either required (no default) or `| None = None`.
- **No dead code**: remove unused code, unused imports, and historical comments.
- **No redundant derived fields**: don't return a collection plus a trivially computable
  function of it (a list and its `len()`). Exception: pagination `total_count`.
- **Every field needs a reader**: a set-but-never-read field is dead payload. Authoring
  provenance goes in inert `#` comments next to the data, not schema fields (`note:`);
  delete write-only fields that survived refactors.
- **No unnecessary aliasing**: no import renames, fixture re-assignment, trivial
  parameter aliasing, or convenience re-exports (`AgentEvent = EventType`). Aliases only
  at public API boundaries (`__init__.py` re-exports) or to avoid collisions, with a
  comment.
- **Import from the defining module**, never from a module that happens to re-export
  the symbol.
- **No dynamic attribute probing** (`getattr`/`hasattr`/`setattr`) unless justified and
  documented; in tests, assert attributes directly.
- **Exceptions**:
  - **Never swallow**: no bare/broad `except` as a silent fallback, no defaulting to
    empty values on parse/IO errors — real errors must surface. Broad catch is fine
    only for cleanup-then-`raise` (e.g. `__exit__`).
  - **Raise, don't return error lists**, from validation/precondition checks.
  - **Let them propagate** to the single error boundary (CLI wrapper, request
    middleware, FastMCP handler — FastMCP already converts unhandled exceptions to MCP
    errors). Catch only to transform, add context, or genuinely handle.
  - **Don't restate parser/IO exceptions**: no wrappers that just say "invalid file" —
    the original `OSError`/parser error/`ValidationError` already carries the details.
  - **Not control flow**: query preconditions first and execute without catch; don't
    execute-catch-and-parse-the-fault for foreseeable states.
  - **Bugs crash**: don't catch errors that indicate programming bugs.
- **Express code concisely**: every line does work; inline single-use subexpressions;
  no no-op short-circuits; no single-use intermediate variables.
- **Strict data mapping**: invalid enum/typed inputs raise early; never `continue`
  past them.
- **Prefer functional style**: comprehensions and idiomatic patterns.
- **Functions over classes** when there's no state to manage.
- **Use framework features** (e.g. typer `exists=True`) over manual checks.
- **Precise types**: discriminated unions, Protocols, TypedDicts, concrete models.
  `Any`/`object` only when truly anything is allowed; document such cases.
- **Make invalid states unrepresentable**: a union of variant types over flag+optional
  combos that permit nonsense (`hook_installed=False, pid=42`). Dispatch on variants
  with `isinstance` (mypy narrows), not discriminator-string compares — except in
  Mako/Jinja templates, where `kind` strings are acceptable.
- **Typed concurrency messages**: dataclasses/models for actor/mailbox messages and
  results, never `dict[str, T]`.
- **Dataclasses for internal types, Pydantic at boundaries**: `@dataclass` for purely
  internal typed objects; `BaseModel` where you need (de)serialization, validation,
  JSON schema, or Field validators.
- **Pydantic as typed objects**: direct attribute access (`model.field`), parse dicts
  into models at the boundary; `dict.get(...)` only for truly untyped external payloads.
  Construct models, not schema-shaped dicts, in tests; never `Mock()` a Pydantic model;
  no `model_dump()` except at I/O boundaries.
- **`Field(description=...)`** for per-field docs, not a class docstring listing fields.
- **Shorten obvious docstrings** to one line; drop Args/Returns sections that echo the
  signature.
- **Explicit keyword arguments** when arguments are known; `Model.model_validate(data)`
  over `TypeAdapter(...)` unless adapter semantics are needed.
- **Enums**: `EnumClass.VALUE`, not string literals. StrEnum is already a string — no
  `.value` in f-strings.
- **Compact CLI output**: merge related information onto single lines; vertical space
  is at a premium.
- **`f"{x=}"`** in error/log/debug strings, not `f"x={x!r}"`.
- **Logging**: module-level `logger = logging.getLogger(__name__)` only — never inside
  functions or stored on `self`.
- **`pathlib.Path`** throughout; `str(path)` only when an external API requires it.
- **No string forward references**: reorder/split files to remove cycles; for
  cross-module cycles use `if TYPE_CHECKING:` with real symbols, not quoted names or
  `model_rebuild()`.
- **No unnecessary `__init__.py`**: Bazel auto-generates stubs via `imports = [...]`;
  create one only to expose a public API or configure the namespace.
- **No `__all__`** without a specific need (public `import *` surface, `__init__.py`
  re-export lint).
- **No grab-bag modules** (`core.py`, `utils.py`, `constants.py`): name modules by what
  they do; organize by domain, not by role.
- **Flat over nested**: a subdirectory with <3 files gets flattened.
- **Sets for unordered collections** (`set[T]`); lists only when order or duplicates
  matter.
- **Prefer `more_itertools`**: `one()` when more than one match is a bug, `first()`
  when many are valid and you want the first; `itertools.batched` over manual slicing.

## Tombstones

When a removal can't be atomic — downstream consumers need a transition period — leave a
**tombstone**: a dated marker recording what was removed and the verifiable condition for
deleting the marker itself:

```python
# CLEANUP(2026-03-22): Remove field once all clients are on ≥v2.3
#   (commit abc123 drops the last reader).
old_field: str | None = None
```

- The condition must be specific and verifiable ("after YYYY-MM-DD", "once commit X
  ships"), not "when safe".
- Delete the tombstone (and what it guards) once the condition is met — it's an active
  work item, not a historical comment ("used to do X" gets deleted, not tombstoned).
- Cross-cutting cleanup spanning many files goes in `TODO.md`; tombstones are for
  localized removals next to the thing being cleaned up.

## SQLAlchemy

**ORM over raw SQL.** Raw SQL only for DB-specific features the ORM handles poorly
(window functions, recursive CTEs, LATERAL joins), performance-critical queries,
administrative DDL, or when the ORM equivalent would be markedly less readable.

## Build System (Bazel)

**Use `main_module` for `py_binary` targets** that share a `py_library`'s source — the
library declares all deps once (keeping the mypy aspect single-sourced); the binary has
no `srcs`:

```python
py_binary(
    name = "session_start",
    main_module = "devinfra.claude.session_start",
    imports = ["../.."],
    deps = [":session_start_lib"],
)
```

## Testing

- **Placement**: tests for `a/b/c.py` go in `a/b/test_c.py`, adjacent to the module.
  Cross-module integration tests get descriptive names
  (`test_agent_mcp_integration.py`).
- **Fixtures**: shared setup in fixtures; fixtures used by multiple modules in
  `conftest.py`. Never import fixtures in `test_*.py` files — import them in the
  nearest `conftest.py` (ruff ignores F401 there via `per-file-ignores`; test-file
  imports cause F811 collisions with parameter names).
- **Concise test bodies**: assertions in tests, setup in fixtures.
- **Update tests with production code**: signature/behavior changes propagate to the
  tests that use them, in the same change.
- **No pure change-detector tests**: don't assert a checked-in literal equals itself
  copied into the test. Test semantics: invalid values rejected, invariants hold,
  behavior differs by mode.
- **No lint silencing without approval**: no ignore rules or per-line silencing unless
  explicitly approved.
- **Use pre-commit**: `pre-commit run --all-files` over invoking individual tools.
- **`textwrap.dedent`** for inline multiline strings (YAML, JSON, scripts) so test
  indentation stays readable.

## Documentation

**Remove**: docstrings/comments that restate the name, signature, or next line; Args/
Returns sections echoing types; trivial class docstrings; historical "used to" comments;
`# === Section ===` banners; changelog comments.

**Keep**: TODOs/FIXMEs near their context; non-obvious behavior (edge cases, invariants,
preconditions, contracts); why-comments; system/integration context not visible locally
(action at a distance, shared-state mutation); disambiguation of ambiguous names
("container-side path" vs "host-side"); test intent comments naming the edge case under
test.

**Heuristics**: if deleting it loses zero information, delete it. "Why" earns its place;
"what" rarely does. Public API boundaries tolerate more verbosity than internal code.

### Deviations, Not Re-explanations

When this repo uses a standard mechanism with a repo-specific difference, document only
the difference, labeled **Deviation:**, with a brief pointer to the standard mechanism —
never re-explain the standard behavior. Example: "Standard Flux image automation;
deviation: register the `ImageRepository` with the webhook receiver." House vocabulary:
**deviation** = intentional divergence from stock; **gotcha** = surprising behavior that
bites.

### Local File Links in Markdown

- `@path/to/file.md` (own line) — transclusion, for content agents must always load
- `<path/to/file.md>` — clickable link without custom text
- `[custom text](path/to/file.md)` — link with custom text
- Never `[path](path)` — duplicates the path

### Inline Code in Prose

Backtick every code-like token in markdown prose: flags, paths, config keys, env vars,
globs, hostnames. Plain-text code tokens are harder to read and prettier escapes their
special characters (`*` → `\*`).

### Brace-Expansion Shorthand for Lists

Prefer `gitea-{namespace,secrets,db,admin-token}` over spelling out each item. Only with
2+ suffixed variants — single-item braces (`foo-{bar}`) are worse than `foo-bar`.
