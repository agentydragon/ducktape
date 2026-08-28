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

**Correct, then short.** Short is not terminal: map the code to the domain, test what
matters to pin down, comment what reading alone won't yield, and spend no tokens or
attention past that.

**Claim only what you have observed — and observe the behavior itself, not a proxy for
it.** Never call a change a "fix" — in a commit message, PR, doc, `debug/` note, comment,
or status update — unless you have watched the specific failure the user described stop
happening, **end to end, under the conditions where it was reported**. Verifying a
_precondition_ is not verifying the fix: that the harness now reports the right context
window, that the config renders, that a component initializes, that a green check
passed — each can hold while the reported problem persists. "My car runs weird" is not
answered by "the tank has gas, the oil's clean, the lights come on" — you have to drive
it. A change whose preconditions check out but whose end-to-end behavior you have not
exercised is a **candidate fix**: state exactly what you observed, and state that the real
behavior was not run. The distinction is load-bearing across "works", "resolved",
"passing" too — "it builds" and "the unit test passes" are not "the reported problem is
gone." Underclaiming costs a sentence; overclaiming sends the next person past the live
bug.

- **Enter async early**: a single `asyncio.run(async_main(...))` at the top of `main()`;
  never scattered or nested deeper in the call stack.
- **`main()` returns `None`**: an entry point raises on failure and lets the traceback
  and Python's own exit code say so — no `return 0`/`return 1`, no `sys.exit(main())`,
  no catching an exception to turn it into a number. Where a specific code genuinely is
  the contract (a shell gate, a `git` driver, a linter's "found findings" convention),
  `raise SystemExit(2)` at the point that decides it and name the caller that reads it;
  the signature still says `None`, because the code is raised and not returned.
- **No large code blobs in YAML/JSON**: any embedded script/config block longer than ~5
  lines lives in its native file (`.py`, `.sh`) and is mounted via `configMapGenerator`
  or a `ConfigMap` file reference, so it stays lintable and type-checkable.
- **In-function imports** only to break a proven circular dependency, with a one-line
  comment naming the cycle (ruff E402 already pins imports to module top).
- **No suspicious nullability**: an optional field must represent an intentional,
  defined absent state (or a named transition). Absence is `None` — never `""`/`[]`
  zero values. A field is either required (no default) or `| None = None`.
- **No footgun defaults**: require a parameter when every correct production caller must
  supply real configuration or dependency wiring. A fallback value must represent a valid
  omission in the contract, not merely spare callers from plumbing the authoritative value.
- **Every field needs a reader**: a set-but-never-read field is dead payload. Authoring
  provenance goes in inert `#` comments next to the data, not schema fields (`note:`);
  delete write-only fields that survived refactors.
- **No redundant derived fields**: don't return a collection plus a trivially computable
  function of it (a list and its `len()`) — storing or returning `x` alongside
  `trivial_function(x)` invites drift and leaves open which layer of the stack adds the
  derivation.
- **No unnecessary aliasing**: no import renames, fixture re-assignment, or convenience
  re-exports (`AgentEvent = EventType`). Aliases only at public API boundaries
  (`__init__.py` re-exports) or to avoid collisions, with a comment.
- **Import from the defining module**, never from a module that happens to re-export
  the symbol.
- **Reuse before minting**: before adding a helper, type, or mechanism, search for the
  existing one solving the same shape; a near-duplicate dedupes into the original
  rather than landing beside it.
- **`typing.Protocol` is a smell by default**: name the concrete type or a union of the
  real types; a Protocol earns its place only to break a proven circular dependency or
  in genuinely structural metaprogramming, never as indirection over implementers you
  can name.
- **No dynamic attribute probing** (`getattr`/`hasattr`/`setattr`) unless justified and
  documented; in tests, assert attributes directly.
- **Exceptions**:
  - **No silent fallbacks**: no bare/broad `except` swallowing, no defaulting to empty
    values on parse/IO errors. Broad catch only for cleanup-then-`raise` (e.g. `__exit__`).
  - **Degrade loud**: where a fallback genuinely is correct (best-effort cache write,
    optional prefill, graceful UI degradation), the `catch` still **logs the exception**
    at `warning`/`error` via a module-level logger — no empty or comment-only `catch` —
    unless the failure already surfaces elsewhere (caller re-throws with the detail).
  - **Raise, don't return error lists**, from validation/precondition checks.
  - **Let them propagate** to the single error boundary (CLI wrapper, request
    middleware, FastMCP handler — FastMCP already converts unhandled exceptions to MCP
    errors). Catch only to transform, add context, or genuinely handle. Don't wrap
    parser/IO exceptions in "invalid file" restatements — the original carries the
    details.
  - **Not control flow**: query preconditions first and execute without catch; don't
    execute-catch-and-parse-the-fault for foreseeable states.
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
- **Make invalid states unrepresentable**: a union of variant types over flag+optional
  combos that permit nonsense (`hook_installed=False, pid=42`). Dispatch on variants
  with `isinstance` (mypy narrows), not discriminator-string compares — except in
  Mako/Jinja templates, where `kind` strings are acceptable.
- **One concept, one name across representations**: a concept's Pydantic model, ORM
  class, and table share the concept-name; representation-role suffixes (`…Row`,
  `…Body`, `…View`) only where two representations of one concept must coexist in a
  namespace, and the concept half stays identical. The disambiguating suffix lives **on
  the definition**, never re-minted per import (no `X as XRow` repeated across sites).
- **Directory-as-namespace, no redundant prefix**: inside a domain package the package
  name is the namespace — drop it from both filenames and the entities they define
  (`grants/kubernetes/models.py` defines `Grant`, not `kubernetes_grant_models.py` /
  `KubernetesGrant`). Three seams keep meaningful names, never by rebaking the prefix: at a
  cross-package collision, module-qualify (`kubernetes.Grant` vs `http.Grant`) or
  alias-with-comment; cross-cutting primitives (`HttpMethod`, `RequestAttributes`) live in
  a shared home, they are not domain-specific entities; and a class name that is a
  published schema-component key renames only as a coordinated wire change, never as a
  package-move rider. Worked example and the console reorg it governs:
  <haku/console/docs/naming_and_layout.md>.
- **Identifiers carry their type**: a UUID travels as `UUID` end to end, the
  conversions absorbed by boundary adapters (Pydantic validators, ORM column types) —
  no scattered `UUID(x)`/`str(y)` in code. Where a str-typed library surface can't be
  adapter-absorbed, keeping `str` beats conversion churn.
- **Typed concurrency messages**: dataclasses/models for actor/mailbox messages and
  results, never `dict[str, T]`.
- **Dataclasses for internal types, Pydantic at boundaries**: `@dataclass` for purely
  internal typed objects; `BaseModel` where you need (de)serialization, validation,
  JSON schema, or Field validators.
- **Pydantic as typed objects**: direct attribute access, parse dicts into models at
  the boundary; `dict.get(...)` only for truly untyped external payloads. Construct
  models, not schema-shaped dicts, in tests; never `Mock()` a Pydantic model; no
  `model_dump()` except at I/O boundaries.
- **`Field(description=...)`** for per-field docs — not a class docstring listing
  fields, and not bare attribute docstrings, which Python discards at runtime and no
  schema consumer sees.
- **Explicit keyword arguments** when arguments are known; `Model.model_validate(data)`
  over `TypeAdapter(...)` unless adapter semantics are needed.
- **Enums**: `EnumClass.VALUE`, not string literals. StrEnum is already a string — no
  `.value` in f-strings. A vocabulary occurring more than once is a StrEnum, not
  repeated `Literal["…", "…"]` unions — unless an external API dictates the Literal
  shape.
- **Compact CLI output**: merge related information onto single lines; vertical space
  is at a premium.
- **`f"{x=}"`** in error/log/debug strings, not `f"x={x!r}"`.
- **Logging**: module-level `logger = logging.getLogger(__name__)` only — never inside
  functions or stored on `self`.
- **No string forward references**: reorder/split files to remove cycles; for
  cross-module cycles use `if TYPE_CHECKING:` with real symbols, not quoted names or
  `model_rebuild()`.
- **No unnecessary `__init__.py`**: Bazel auto-generates stubs via `imports = [...]`;
  create one only to expose a public API or configure the namespace. **No `__all__`**
  without a specific need.
- **No grab-bag modules** (`core.py`, `utils.py`, `constants.py`): name modules by what
  they do; organize by domain, not by role. **Flat over nested**: a subdirectory with
  <3 files gets flattened.
- **Sets for unordered collections** (`set[T]`); lists only when order or duplicates
  matter.
- **Don't reinvent the wheel**: a solved problem uses the library the repo already
  carries, never hand-rolled arithmetic — retry/backoff is `tenacity`; iteration
  shapes are `more_itertools` (`one()` when more than one match is a bug, `first()`
  when many are valid and you want the first) or `itertools.batched` over manual
  slicing.

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

**Store facts, derive state.** A status column computable from stored facts
(`revoked_at`, `valid_until`) is derived in queries or properties, never stored — the
persistence form of no-redundant-derived-fields (§ General): a materialized status is a
cache that can lie and demands a sweeper. Materialize only for a measured query-cost
reason, stated where it happens.

## Build System (Bazel)

### Gazelle-managed Python BUILD files

Gazelle owns Python `py_library`/`py_test` rules; CI fails on drift, so run it
(devshell `gazelle`, or `bb run //devinfra:gazelle`) instead of hand-editing managed
`srcs`/`deps`. Mechanism, escape hatches, and limitations: <devinfra/docs/gazelle.md>.

- **One `py_library` per `.py` file, named exactly the module stem.** Rules list only
  own-package files; a subdirectory's `.py` files get per-file rules in the
  subdirectory's own BUILD.
- **`py_binary`: hand-written, `main_module`, no `srcs`, `deps = [":<stem>"]`**, named
  `<stem>_bin` — no module stem, so gazelle never touches it. The library declares all
  deps once (keeping the mypy aspect single-sourced). Where an aspect image twin
  already holds `_bin`, the twin moves to `<stem>_image_bin`.

```python
py_binary(
    name = "session_start_bin",
    main_module = "devinfra.claude.session_start",
    imports = ["../.."],
    deps = [":session_start"],
)
```

- **`test_*.py` / `*_test.py` filenames are reserved for `py_test` targets.** Shared
  test helpers live in `<pkg>/testing/` packages (`default_testonly`), never in
  test-glob-named files; a non-test file whose glob-matching name is part of an
  external contract gets `# gazelle:exclude`.
- **`conftest.py` never appears in `py_test.srcs`**: the plugin generates a
  per-package `:conftest` library and deps each test on the whole ancestor conftest
  chain (including `//:conftest`).
- **Runtime-only deps the import scan cannot see get a local escape** —
  `# gazelle:include_dep` in the owning `.py`, or a dep-level `# keep: <reason>` where
  the source cannot carry the annotation. The category inventory is in
  <devinfra/docs/gazelle.md> § Escape hatches.

### TypeScript: one `ts_library` per module

**`ts_library` (`//devinfra/js:ts_library.bzl`) is how TypeScript is built here.** One
target per module, `srcs` listing that module's file(s), `tsconfig` naming the package
tree's shared `ts_config`:

```python
ts_library(
    name = "client",
    srcs = ["client.ts"],
    tsconfig = TSCONFIG,  # a package-level constant, e.g. ":tsconfig" or "//pkg/frontend:tsconfig"
    deps = [":operator_login", ":schema", "//:node_modules/openapi-fetch"],
)
```

It wraps `ts_project`: the type check is the build step, and a module's dependencies are
what it declares. Bundlers (`spa_bundle`, `esbuild`) take the emitted `.js` (entry point
`main.js`, not `main.tsx`); vitest runs the emitted `.test.js`, so every spec is its own
`ts_library` and `vitest.config.ts` includes `["**/*.test.js"]`.

**Never `js_library` for `.ts`/`.tsx`, and never a whole-project `tsc_test`** — files
fall through the second hand-maintained list and end up checked by nothing, silently.
`js_library` remains right for `.mjs`/`.js` and staging generated declarations.

Gotchas: a generated `.ts` needs compiling like any other (macros that emit TypeScript
take a `tsconfig` and emit a `ts_library`); every `.tsx` needs
`//:node_modules/@types/react` even without importing a React symbol (TS2742);
whole-program tools (type-aware ESLint) need a `filegroup` glob of the sources plus
`no_copy_to_bin`, since `ts_library` does not propagate `.ts`.

**Svelte packages keep `svelte_check_test`** — `ts_project` cannot process `.svelte`,
and `svelte_check` already checks components and their `.ts` as one program with no
second hand-maintained list.

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
- **Use the real test environment, not local platform shims**: configure the test
  runner with an implementation of the platform APIs the code depends on. Mock only
  test-specific behavior or genuine external boundaries; do not reimplement the
  platform piecemeal inside a spec.
- **Test externally meaningful behavior, not implementation text**: assert observable
  behavior, public API contracts, schemas, and durable invariants — never inspect
  implementation source text, count or forbid source fragments, or pin method identity.
  Negative coding guidance belongs in this file, a header comment, or a lint rule when
  genuinely enforceable; source-pattern tests are brittle and not a substitute for
  design.
- **No pure change-detector tests**: every expectation encodes a durable rule, not the
  artifact's current state. Copying a checked-in file's values, shape, or roster into
  assertions is not coverage — an intentional edit changes the test in lockstep, so it
  cannot distinguish correct from incorrect state; moving the duplicate into a fixture
  does not help.
  - Independence is **semantic, not physical**: an invariant about relationships within
    one file ("two LiteLLM routes name the same downstream model") is a real rule; a
    copied roster is not.
  - Prefer relations (a configured path names a mounted file; a Service targets a
    declared container port), schemas/invariants that admit many valid inputs, and
    runtime behavior. Ask whether a plausible wrong edit would fail without updating
    expected values in lockstep.
  - Generated-output snapshots are valid when the test runs the generator — including
    the inverted form where the test _builds_ the artifact in code (loops and functions
    beating repetitive YAML) and asserts the checked-in file equals it: there the test
    is the source of truth and the file is generated output pinned to it (the LiteLLM
    config pattern). Exact
    wire-format pins are valid only when an external contract or still-live consumer
    requires that value; name that contract in the test.
- **Test value: what must break for this to fail?** Judge every test by that
  question. If the answer is bread-and-butter behavior of a standard library —
  pydantic parsing a plain `foo: int`, a StrEnum equalling its string, a
  yaml-load echo — or the test restates the declarations it exercises (field
  aliases, a default compared against the same imported constant), delete it:
  libraries' documented basics get no tests here; our choices and edges do.
  - **Bulk is the multiplier**: a short trivial test is a minor sin; a file
    full of them, or 150 lines of JSON `model_validate`d onto a plain model, is
    the real cost. A cheap one-liner may stay on judgment.
  - **Library spikes are not trivia**: where a library's usage had to be
    figured out (sharp edges, non-obvious wiring), a test pinning the working
    pattern is executable documentation CI keeps honest — keep it, and say so
    in the test or file docstring, unless local context (a README, an `x/`
    folder) already does. The axis is triviality of the usage, not the
    presence of a library.
  - **Change-detector guarding a real constraint → comment at the
    declaration**: when a test's only enforcement is "the editor must update a
    mirrored literal in a second file" _and_ the guarded thing can only break
    by editing that one declaration, move the constraint onto the declaration
    itself (append-only, values persisted, edits need a migration) and delete
    the test. A relation against a second live artifact (the running
    database's enum, a generated config) stays a test.
  - **Multi-site constraints**: when several places must agree (the same rule
    in JS and Python, a value mirrored across configs), prefer in order: an
    integration/e2e test of the shared behavior; a test tying the sites
    together (parse both files, assert the values agree); and only where both
    are impractical or degenerate into pure change detectors, a concise sync
    comment at _every_ site naming what must stay in sync and why — a long
    why lives in one central place the other comments point to.
  - **Pin defaults where they act**: best is asserting the behavior the value
    produces; else assert the boundary artifact carries the value, as literals
    (the token request asks for the full scope list) — not
    `field default == the same imported constant`, which passes even when the
    constant changes.
  - **Anchors and guards inherit their unit's value**: a positive anchor keeps
    its negatives from passing vacuously; an anti-vacuity assert keeps an
    invariant honest. Judge the unit, not the line.
- **No lint silencing without approval**: no ignore rules or per-line silencing unless
  explicitly approved.
- **Use pre-commit**: `pre-commit run --all-files` over invoking individual tools.
- **`textwrap.dedent`** for inline multiline strings (YAML, JSON, scripts) so test
  indentation stays readable.

### Waiting

**Never sleep for a duration; wait for the condition.** A blind delay is wrong in both
directions at once: too short on a loaded CI runner, where it flakes, and too long on every
run that did not need it. It also hides what is being awaited — the number is a guess nobody
can check, so it only ever ratchets up.

Wait for the thing itself. In Puppeteer that is `waitForSelector` (including
`{ hidden: true }`), `waitForFunction`, `waitForNetworkIdle`, or `waitUntil: "networkidle0"`.
For "the page finished rendering what it has", use `waitForStable` from
<util/testing/frontend_visual/capture.mjs> — `document.fonts.ready`, images decoded, a painted
frame — rather than a delay after mount.

When the condition is app-internal (data arrived, a component mounted lazily), expose it as a
flag, attribute or event and wait on that. Having nothing to wait on is the thing to fix, not
to sleep past.

In tests, assert **ordering, not elapsed time**. `assert(Date.now() - started >= 10)` after a
10 ms wait measures the platform's timer rather than the code under test, and fails whenever
`setTimeout` lands a hair early — measured, 25 of 4000 iterations.

**Gotcha:** do not await `document.getAnimations()` in visual tests here.
`DISABLE_ANIMATIONS_CSS` pins animations with `animation-play-state: paused`, and a paused
animation's `finished` never settles, so awaiting it hangs rather than capturing.

## Documentation

**Remove**: docstrings/comments that restate the name, signature, or next line; Args/
Returns sections echoing types; trivial class docstrings; historical "used to"
comments; **prose arguing that the current code is correct** — what was here before,
why the change was right, what alternative was rejected (a litigated rejection moves to
the design doc, never stays inline — § Decision records); `# === Section ===` banners;
changelog comments; self-referential counts of an adjacent list ("the three steps
below") — they drift as rows change; **process narration** — the order work landed in,
what it replaced, what it is waiting on. An actionable transition is a **tombstone**
(above); nothing else about the sequence earns a line. That justifying register belongs
in the commit message or PR — where someone is deciding whether to accept the change —
not in the file, where nobody is deciding any more.

**Keep**: TODOs/FIXMEs near their context; non-obvious behavior (edge cases,
invariants, preconditions, contracts); why-comments; system/integration context not
visible locally (action at a distance, shared-state mutation); disambiguation of
ambiguous names ("container-side path" vs "host-side"); test intent comments naming the
edge case under test.

**A past state earns a comment only when a future editor has to act on it** — a
migration still in flight, a compatibility requirement that still binds, a roll-safety
constraint that still holds. Those are instructions wearing a historical tense.

**Heuristics**: if deleting it loses zero information, delete it. "Why" earns its
place; "what" rarely does. Judge the file and not only the line: every comment can pass
the test above while the file still reads as narration with some code in it. If prose
is most of what a reader scrolls past, keep cutting.

### Decision records

The rejected-alternative ban above targets inline commentary, not the knowledge. A
rejection that was genuinely litigated — an approach that looks right, was tried or
seriously costed, and lost for a non-obvious reason — goes in the component's design
doc (`<dir>/docs/design.md`, or a rejects section of an existing one), stated as the
constraint that killed it. That is where future work checks before re-walking a
months-old rabbithole; nobody searches git history for it.

**The Y-test** decides what qualifies — there, and in every "X rather than Y" sentence
anywhere: would a competent future author actually try Y? If yes, the rejection earns a
durable record, and a contrast clause naming it earns its place. If nobody would ever
try Y ("queries the API rather than hardcoding the list"), the clause is padding —
state X and stop.

### Documentation lifecycle

- Live docs describe the present, active work, durable invariants, and lessons a future
  maintainer needs. Git already records superseded behavior, completed plans, and
  incident timelines.
- Delete resolved `debug/` notes and completed plans only after moving any surviving
  workaround, fragility warning, or recurrence clue beside the current code, contract,
  or design — short, with a link to the upstream bug or fixing commit when it helps
  recognize the same failure.
- If an upstream fix made a workaround unnecessary, retain at most a one- or two-line
  warning where the area remains unusually fragile. No archives or indexes merely to
  preserve history.

**Then say what is left in fewer words.** Prose that survives the deletion test can
still take more clauses than the information needs — prefer the parsimonious form.
When tightening, the information content must be unchanged; dropping a caveat is a
deletion and needs a deletion's justification.

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
  separate fenced blocks under markdown headings; wrap structured output in a fenced
  block with the right language — never raw JSON bare in prose.

### Brace-Expansion Shorthand for Lists

Prefer `gitea-{namespace,secrets,db,admin-token}` over spelling out each item. Only with
2+ suffixed variants — single-item braces (`foo-{bar}`) are worse than `foo-bar`.
