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

- **Modern Python**: `|` unions, `:=`, `match`.
- **Enter async early**: a single `asyncio.run(async_main(...))` at the top of `main()`;
  never scattered or nested deeper in the call stack.
- **No large code blobs in YAML/JSON**: any embedded script/config block longer than ~5
  lines lives in its native file (`.py`, `.sh`) and is mounted via `configMapGenerator`
  or a `ConfigMap` file reference, so it stays lintable and type-checkable.
- **In-function imports** only to break a proven circular dependency, with a one-line
  comment naming the cycle (ruff E402 already pins imports to module top).
- **No suspicious nullability**: an optional field must represent an intentional,
  defined absent state (or a named transition). Absence is `None` — never `""`/`[]`
  zero values. A field is either required (no default) or `| None = None`.
- **No dead code**: remove unused code and historical comments (ruff F401 catches
  unused imports).
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
  - **Degrade loud, not silent**: where a fallback genuinely is correct (best-effort
    cache write, optional prefill, non-critical fetch, graceful UI degradation), the
    `catch` still **logs the exception** at `warning`/`error` via a module-level logger.
    No empty `catch {}`, comment-only catch, or `.catch(() => {})` — unless the failure
    already surfaces elsewhere (the caller re-throws with the detail), where a second
    log is noise.
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
  past them. **Deviation, where the writer may be a newer release of this same system**
  — a value read back from our own storage, or off a payload between replicas of a
  rolling deployment: there an unrecognised value is expected rather than invalid, and
  raising takes down the rows the reader does understand along with the one it does
  not. Such a reader decodes it to a **named** unknown variant every consumer must
  dispatch on (never `None`, never a nearby member, never a silent skip), and the
  writer of a new value still waits a release wherever ignoring it is not the correct
  answer. Which readers those are, and how to tell: <haku/console/README.md>
  § Vocabularies across a roll.
- **Prefer functional style**: comprehensions and idiomatic patterns over loop-and-append.
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
**tombstone**: a marker recording what was removed and the verifiable condition for
deleting the marker itself:

```python
# CLEANUP(added 2026-03-22): Remove field once all props clients are on ≥v2.3
#   (commit abc123 drops the last reader).
old_field: str | None = None
```

- The tag date is **when the tombstone was added** — it is never a deadline. The
  removal trigger lives entirely in the condition text.
- The condition must name a **verifiable gate**: an explicit date ("remove after
  2026-07-01"), a version ("once nixpkgs ships libqmi ≥1.38"), a tracked change
  ("once nixpkgs#510952 merges"), or a last-reader check ("once nothing imports X") —
  never "when safe" or "once upstream fixes it" with no reference.
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

### TypeScript: one `ts_library` per module

**`ts_library` (`//devinfra/js:ts_library.bzl`) is how TypeScript is built here.** One target
per module, `srcs` listing that module's file(s), `tsconfig` naming the package tree's shared
`ts_config`:

```python
ts_library(
    name = "client",
    srcs = ["client.ts"],
    tsconfig = TSCONFIG,  # a package-level constant, e.g. ":tsconfig" or "//pkg/frontend:tsconfig"
    deps = [":operator_login", ":schema", "//:node_modules/openapi-fetch"],
)
```

It wraps `ts_project`: tsc emits the `.js` and `.d.ts` in the same action that type-checks them,
so **the type check is the build step** — `bazel build` fails on a type error, and a module's
dependencies are what it declares. Bundlers (`spa_bundle`, `esbuild`) take the emitted `.js`, so
their `entry_point` is `main.js`, not `main.tsx`. vitest runs the emitted `.test.js`, so every
spec is its own `ts_library` too and `vitest.config.ts`'s `include` is `["**/*.test.js"]`.

**Never `js_library` for `.ts`/`.tsx`, and never a whole-project `tsc_test`.** `js_library` only
stages files; a codebase built from it needs a second target re-listing every file to check it,
the two lists drift, and what falls through is checked by nothing — silently, which is how three
haku-console fixtures rendered as raw JSON (#3599, #3604, #3610). `js_library` remains right for
`.mjs`/`.js` and for staging generated declarations.

Three consequences worth knowing before they surprise you:

- **A generated `.ts` needs compiling like any other.** rules_js classifies it as a source, not a
  type, and `ts_project` pulls only its deps' types into the compile — so a `js_library`-wrapped
  generated module reads as "cannot find module". Macros that emit TypeScript
  (`js_openapi_zod`, `data_uri_module`) take a `tsconfig` and emit a `ts_library`.
- **Every `.tsx` needs `//:node_modules/@types/react`**, including a file that imports no React
  symbol itself — JSX element types and inferred component types come from there (TS2742).
- **A whole-program tool still needs the sources.** Type-aware ESLint reads `.ts`, which a
  `ts_library` does not propagate. Feed it a `filegroup` glob and set `no_copy_to_bin` on the
  test, or `ts_project`'s copy-to-bin and the staging copy become two actions writing one path.
  A glob is right here: the tool runs over the whole directory, and unlike a hand-written list it
  cannot fall behind a new file.

**Svelte packages keep `svelte_check_test`.** `ts_project` cannot process `.svelte`, and
`svelte_check` already type-checks components and their `.ts` as one program driven by the
tsconfig's globs off the `:app` library graph — there is no second hand-maintained list, so the
drift this pattern prevents does not arise there.

### System CA certificates in distroless images

`rules_distroless` ships no `/etc/ssl/certs/ca-certificates.crt` bundle, so a
system-trust TLS client in a distroless image fails to verify. Add the shared
`//third_party/debian_slim:cacerts` layer to the `oci_image` `tars` — never
re-assemble a bundle at runtime. Per-client details (`pygit2` ignores
`SSL_CERT_FILE`; `requests`/`reqwest` need no layer at all):
<docs/distroless_ca_certificates.md>.

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
- **No pure change-detector tests**: every expectation must encode a durable rule, not
  duplicate the artifact's current state. Reading a checked-in YAML/JSON/XML file and copying
  its current values, shape, roster, or whole contents into assertions is not coverage — an
  intentional edit must change the test in exactly the same way, so the test can distinguish
  no correct state from an incorrect one. Moving the duplicate into a fixture or test constant
  does not help.
  - Independence is **semantic, not physical**. The source of truth may be another artifact,
    an external contract, or an invariant authored in the test about relationships within the
    same file. A test that says two LiteLLM routes must name the same downstream model and key
    enforces a real rule even though both routes live in one YAML file; a test that copies the
    current route roster into Python does not.
  - Prefer relations (a configured path names a mounted file; paired fields stay equal; a
    Service targets a declared container port), schemas or invariants that admit many valid
    inputs, and logic/runtime behavior. Ask whether a plausible wrong edit would fail without
    updating expected values in lockstep.
  - Generated-output snapshots are valid when the test runs the generator. Exact wire-format
    or compatibility pins are valid only when an external specification or still-live consumer
    independently requires that value; name that contract in the test.
  - Delete or rewrite assertions such as `config["session_ttl_seconds"] == 7200`, a copied
    manifest dict, a copied enum/roster, or `version_num == "0011"`. Prefer, respectively, a
    timeout behavior test, semantic correspondence within or across manifests, behavior for
    each mode, or migrating a fresh database to ORM parity and proving head re-application
    idempotent.
- **No lint silencing without approval**: no ignore rules or per-line silencing unless
  explicitly approved.
- **Use pre-commit**: `pre-commit run --all-files` over invoking individual tools.
- **`textwrap.dedent`** for inline multiline strings (YAML, JSON, scripts) so test
  indentation stays readable.

## Documentation

**Remove**: docstrings/comments that restate the name, signature, or next line; Args/
Returns sections echoing types; trivial class docstrings; obvious docstrings longer than
one line; historical "used to" comments; **prose arguing that the current code is correct** —
what was here before, why the change was right, what alternative was rejected; `# === Section ===`
banners; changelog comments; self-referential counts of an adjacent list ("the three steps
below") — they drift silently as rows are added, so let the list speak or derive the count;
**process narration** — the order the work landed in, which step or milestone a change belongs
to, what it replaced, what it is waiting on. An actionable transition is a **tombstone** (above);
nothing else about the sequence earns a line.

That justifying register belongs in the commit message, the PR, or the reply handed back with the
work — wherever someone is deciding whether to accept it. It does not survive into the file,
because once the change has landed nobody is deciding any more.

**What ships is the artifact, not an account of building it.** A television leaves the factory
with a service sticker inside, not a book about designing televisions. A reader arriving at a file
wants the system as it works now, and every sentence about how it came to work that way is one
they must read and discard first. Git already holds that story for the rare reader who wants it,
which is exactly why the file does not have to.

**Keep**: TODOs/FIXMEs near their context; non-obvious behavior (edge cases, invariants,
preconditions, contracts); why-comments; system/integration context not visible locally
(action at a distance, shared-state mutation); disambiguation of ambiguous names
("container-side path" vs "host-side"); test intent comments naming the edge case under
test.

**A past state earns a comment only when a future editor has to act on it** — a migration still
in flight, a compatibility requirement that still binds, a roll-safety constraint that still
holds. Those are instructions wearing a historical tense. A past state that only explains why
the present code is right is not: the present code being right is not news.

**Heuristics**: if deleting it loses zero information, delete it. "Why" earns its place;
"what" rarely does. Public API boundaries tolerate more verbosity than internal code. Judge the
file and not only the line: every comment can pass the test above while the file still reads as
narration with some code in it. If prose is most of what a reader scrolls past, keep cutting.

**Then say what is left in fewer words.** Deletion is not the only edit: prose that carries real
information can still take more clauses and more sentence structure than the information needs,
and that survives the test above because nothing would be lost by keeping it. Prefer the
parsimonious, to-the-point form over the elaborate one — plain sentences over stacked subordinate
clauses, one em-dash aside rather than three. When tightening, the information content must be
unchanged; dropping a caveat to shorten a sentence is a deletion and needs a deletion's
justification.

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

### Fenced Code Blocks

- **Always specify a language** on fenced blocks (markdownlint MD040 enforces it):
  ` ```python `, ` ```bash `, ` ```yaml `; use ` ```text ` for ASCII art/diagrams.
- **Split logically separate content** (different tools, APIs, or categories) into
  separate fenced blocks under markdown headings — don't mix categories or use prose
  headers as lines inside one block.
- Wrap structured output (JSON/YAML/command output) in a fenced block with the right
  language; never leave raw JSON bare in prose.

### Brace-Expansion Shorthand for Lists

Prefer `gitea-{namespace,secrets,db,admin-token}` over spelling out each item. Only with
2+ suffixed variants — single-item braces (`foo-{bar}`) are worse than `foo-bar`.
