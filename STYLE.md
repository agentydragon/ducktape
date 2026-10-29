# Code Style Guide

Repository-wide rules; package-specific elaborations go in that package's AGENTS.md.

## Instruction files

| File      | Holds                                                        |
| --------- | ------------------------------------------------------------ |
| README.md | What it is, setup, how to run. Lean: it is transcluded.      |
| AGENTS.md | `@README.md` (if one exists), then agent-only prescriptions. |
| CLAUDE.md | Exactly `@AGENTS.md`.                                        |
| STYLE.md  | Cross-package style rules (never in a package AGENTS.md).    |

Deep reference goes in `docs/` behind links. A nested AGENTS.md states only what its parents don't. `@path` transclusion stands alone on its line; transclude only what every reader of the host file needs.

## General

**Correct, then short.** Every line does work.

**Claim only what you observed, end to end.** Never call a change a "fix" (commit, PR, doc, comment, status) unless you watched the reported failure stop happening under the conditions it was reported. Verified preconditions (config renders, component starts, a check is green) make a **candidate fix**: say what you observed and that the real behavior wasn't run. Same for "works", "resolved", "passing".

- **Async:** one `asyncio.run(async_main(...))` at the top of `main()`.
- **`main()` returns `None`** and raises on failure — no `return 1`/`sys.exit(main())`. Where an exit code is a contract (shell gate, git driver, linter convention), `raise SystemExit(n)` where it's decided and name the caller that reads it.
- **No code blobs >~5 lines in YAML/JSON**; put them in native files mounted via `configMapGenerator`/`ConfigMap`.
- **In-function imports** only to break a proven cycle, with a comment naming it.
- **Nullability:** optional means a defined absent state; absence is `None`, never `""`/`[]`. Fields are required or `| None = None`.
- **No footgun defaults:** require a parameter every correct production caller must supply; a default must be a valid omission.
- **Explicit policy:** configure independent policy decisions at their owning callsite. Do not infer authentication, trust, retry, placement, or lifecycle policy from incidental names, paths, unrelated fields, or ambient environment. Hidden coupling makes routine edits and refactors hard to review safely: changing a path or setting can unexpectedly change another policy, and callers accumulate dependencies on those undocumented meanings. Helpers consume explicit decisions; mechanical derivation is fine. A deliberately named preset may bundle related decisions when its name and contract expose the bundle.
- **Configuration consistency:** validate related settings together. Reject partial or contradictory configuration; do not silently discard supplied settings or interpret an incomplete feature configuration as disabled.
- **Certificate trust:** proxy configuration selects routing, not certificate trust. Use the established CA/trust mechanism and keep certificate verification enabled.
- **Every field needs a reader.** Delete write-only fields; provenance goes in `#` comments, not `note:` fields.
- **No dead code:** delete what no live path (shipped binary, image entrypoint, workflow, deployed generator, API a dependent repo imports) can actually call, in the change that orphans it. Tests don't make code live; test support is live for its tests. Exceptions: code staged for a named caller landing next; spikes under `x/`. Non-atomic removals get a tombstone.
- **No redundant derived fields** (a list plus its `len()`).
- **No aliasing:** no import renames, convenience re-exports, fixture re-assignment — except public `__init__.py` API or collision avoidance, with a comment. Import from the defining module.
- **Reuse before minting:** search for an existing helper/type/mechanism; dedupe near-duplicates into it.
- **`typing.Protocol`** only to break a proven cycle or for genuinely structural metaprogramming; otherwise name the concrete type or union.
- **No `getattr`/`hasattr`/`setattr` probing** unless justified in a comment; tests assert attributes directly.
- **Exceptions:**
  - No silent fallbacks: no broad `except` swallowing, no empty defaults on parse/IO errors. Broad catch only for cleanup-then-`raise`.
  - Where a fallback is correct (best-effort cache, optional prefill, UI degradation), log the exception at `warning`/`error` unless it surfaces elsewhere.
  - Validation raises; never returns error lists.
  - Let exceptions reach the single boundary (CLI wrapper, middleware, FastMCP handler). Catch only to transform, add context, or handle. No "invalid file" restatements of parser/IO errors.
  - A wrapping exception keeps what the original carried that a reader acts on (status code, errno, path), adds context, and claims nothing the original didn't.
  - Not control flow: check foreseeable states first instead of catching and parsing the fault.
- **Concise code:** inline single-use subexpressions and variables; no no-op short-circuits.
- **Strict data mapping:** invalid enum/typed input raises early, never `continue`. **Deviation:** where the writer may be a newer release of this system (our own stored data, payloads between replicas in a rolling deploy), decode unknown values to a **named** unknown variant every consumer dispatches on (never `None`, a nearby member, or a skip), and a writer of a new value waits a release unless ignoring it is correct. See <haku/console/README.md> § Vocabularies across a roll.
- **Invalid states unrepresentable:** a union of variant types, not flag+optional combos; dispatch with `isinstance`, not discriminator strings (except in Mako/Jinja templates).
- **Naming:** functions are verbs (`record_admission`) unless convention says otherwise (properties, framework hooks); classes are the singular noun an instance is (`SessionStore`).
- **Owned resources are context managers**, scoped with `with`/`async with`/`ExitStack`, never `finally: x.close()`. Keep a public `close()` only where callers end the lifetime early.
- **One concept, one name** across Pydantic model, ORM class and table; a role suffix (`…Row`, `…Body`) only where two representations share a namespace, defined on the class, never via per-import aliases.
- **Directory is the namespace:** `grants/kubernetes/models.py` defines `Grant`, not `KubernetesGrant`; the class a module exists for takes the module's name (`whatever_client.py` → `WhateverClient`). On cross-package collision, module-qualify or alias with a comment; shared primitives (`HttpMethod`) live in a shared home; a class name that's a published schema key changes only as a coordinated wire change.
- **Identifiers carry their type** (`UUID` end to end, converted by boundary adapters); keep `str` only where a str-typed library surface can't be adapted.
- **Typed concurrency messages:** dataclasses/models, never `dict[str, T]`.
- **`@dataclass` internally, Pydantic at boundaries** (serialization, validation, schema).
- **Pydantic:** attribute access; parse dicts at the boundary; `dict.get` only for untyped external payloads; construct models in tests, never `Mock()` one; `model_dump()` only at I/O. Per-field docs via `Field(description=...)`. Prefer `Model.model_validate` over `TypeAdapter`.
- **Keyword arguments** when arguments are known.
- **Enums:** `EnumClass.VALUE`, no `.value` in f-strings; a vocabulary used more than once is a `StrEnum`, not repeated `Literal` unions (unless an external API dictates).
- **Compact CLI output**: merge related info onto one line.
- **`f"{x=}"`** in error/log strings.
- **Logging:** module-level `logger = logging.getLogger(__name__)` only.
- **No string forward references**; restructure, or `if TYPE_CHECKING:` for cross-module cycles; no `model_rebuild()`.
- **No `__init__.py`** unless exposing API/configuring the namespace (Bazel generates stubs); no `__all__` without need.
- **No grab-bag modules** (`utils.py`, `core.py`, `constants.py`); organize by domain. Flatten subdirectories with <3 files.
- **Sets for unordered collections;** Pydantic fields typed `collections.abc.Set[T]`.
- **Use the library:** `tenacity` for retry/backoff; `more_itertools` (`one()` when >1 is a bug, `first()` otherwise); `itertools.batched`.

## Tombstones

A non-atomic removal leaves a marker naming what to remove and a verifiable gate:

```python
# CLEANUP(added 2026-03-22): Remove field once all props clients are on ≥v2.3
#   (commit abc123 drops the last reader).
old_field: str | None = None
```

The date is when it was added, never a deadline. The gate is a date, version, tracked change, or last-reader check — never "when safe". Delete it once met. Cross-cutting cleanup goes in `TODO.md`.

## SQLAlchemy

- ORM over raw SQL, except DB-specific features (window functions, recursive CTEs, LATERAL), measured performance, admin DDL, or markedly better readability.
- **Store facts, derive state:** never store a status computable from stored facts (`revoked_at`, `valid_until`) unless for a measured query cost, stated where it happens.

## Go

One library per job. An unlisted job reuses what other Go services import before adding a library, and adds its row.

| Job        | Use                                                                                                                                                            |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Logging    | `log/slog` JSON on stderr; controller-runtime via `ctrl.SetLogger(logr.FromSlogHandler(...))`. Agentplane runner uses `zap` directly because COSI requires it. |
| Settings   | `spf13/viper` layered; `yaml.v3` with `KnownFields(true)` for file-only settings with literal keys; `protoyaml` + `protovalidate` for shared protobuf schemas. |
| HTTP       | Stdlib `net/http`; no router.                                                                                                                                  |
| OpenAPI    | `danielgtaylor/huma/v2` with the `humago` adapter over `net/http.ServeMux` for typed app routes.                                                               |
| Tests      | Stdlib `testing`.                                                                                                                                              |
| PostgreSQL | `lib/pq`, `goose` migrations.                                                                                                                                  |
| UUIDs      | `google/uuid`.                                                                                                                                                 |
| YAML       | `gopkg.in/yaml.v3`.                                                                                                                                            |
| Retry      | `sethvargo/go-retry`; callers own error classification and progress resets.                                                                                    |
| D-Bus      | `coreos/go-systemd/v22/dbus` for systemd; `godbus/dbus/v5` for generic D-Bus.                                                                                  |
| GitHub     | `google/go-github` (REST) with `bradleyfalzon/ghinstallation` (App JWTs, installation tokens).                                                                 |
| Schemas    | `invopop/jsonschema` for Go type reflection; `santhosh-tekuri/jsonschema/v6` for validation, with an explicit loader (the default reads local files).          |
| TTL cache  | `apimachinery/pkg/util/cache.Expiring`; `x/sync/singleflight` shares a lookup between callers.                                                                 |
| Sets       | `apimachinery/pkg/util/sets`.                                                                                                                                  |
| Casefold   | `x/text/cases.Fold`.                                                                                                                                           |

## Build System (Bazel)

### Gazelle

Gazelle owns Python `py_library`/`py_test` rules (<devinfra/docs/gazelle.md>).

- One `py_library` per `.py` file, named its module stem; rules cover only own-package files.
- `py_binary` is hand-written, named `<stem>_bin` (`<stem>_image_bin` if an aspect image twin holds `_bin`), with `main_module` and no `srcs`; the library holds all deps:

  ```python
  py_binary(
      name = "session_start_bin",
      main_module = "devinfra.claude.session_start",
      imports = ["../.."],
      deps = [":session_start"],
  )
  ```

- Test-only is per package: a test-support umbrella (`testing/`, `e2e/`, `agentplane/system_tests/`) uses `package(default_testonly = True)`; other packages hold production code plus `test_*.py`/`*_test.py` targets. Shared test libraries go in `conftest.py` or an umbrella.
- `testing/` holds fakes, builders, fixtures, matchers — never tests of production code (those sit beside the code, or in `e2e/`/`system_tests/`). `testing/test_x.py` tests only `testing/x.py`.
- `test_*.py`/`*_test.py` names are for test targets only; a non-test file whose name is an external contract gets `# gazelle:exclude`.
- Hand-run demos/probes are `demo_<what>` (any language), beside their subject or under `x/`, in no test target. Promote to a test or production code if it earns it.
- Never list `conftest.py` in `py_test.srcs` (the plugin wires the conftest chain).
- Runtime deps the scan can't see: `# gazelle:include_dep` in the `.py`, or `# keep: <reason>` on the dep.

### TypeScript

Build `.ts`/`.tsx` with one `ts_library` (`//devinfra/js:ts_library.bzl`) per module, `tsconfig` naming the package tree's shared `ts_config`:

```python
ts_library(
    name = "client",
    srcs = ["client.ts"],
    tsconfig = TSCONFIG,  # package-level constant
    deps = [":operator_login", ":schema", "//:node_modules/openapi-fetch"],
)
```

- Bundlers take emitted `.js` (entry `main.js`). Every spec is a `vitest_test` (`//devinfra/js:vitest.bzl`; per-frontend wrapper `<frontend>/vitest.bzl`) run alone like any test. One `vitest_config` per package generates the config — never hand-write `vitest.config.*`; end the BUILD with `check_vitest_specs()`.
- Never `js_library` for `.ts`/`.tsx` and never a whole-project `tsc_test` (files slip through unchecked). `js_library` is for `.js`/`.mjs` and generated declarations.
- **Generated model types:** emit named types from the generator and import them directly from the generated module. Don't maintain schema lookup aliases or type-forwarding rosters in API clients; handwritten aliases should express meaningful projections or UI concepts.
- **Gotchas:** generated `.ts` needs compiling (macros emitting TS take a `tsconfig`); every `.tsx` needs `//:node_modules/@types/react` (TS2742); whole-program tools (type-aware ESLint) need a `filegroup` of sources with `no_copy_to_bin`.

### Runfiles

`_main` is the root module of the build, not necessarily this repo. Tests and repo-only tools may use `get_required_path("_main/...")`; library code a dependent repo imports (where this repo is `ducktape+`) uses `own_repo_rlocation` (<util/bazel/runfiles.py>).

### System CA certificates in distroless images

`rules_distroless` ships no CA bundle: add `//third_party/debian_slim:cacerts` to the `oci_image` `tars`, never assemble one at runtime. Per-client details: <docs/distroless_ca_certificates.md>.

## Testing

- `a/b/c.py` → `a/b/test_c.py`; cross-module tests get descriptive names.
- Shared fixtures in `conftest.py`; never import fixtures into `test_*.py` (F811), import them in the nearest `conftest.py`.
- Assertions in tests, setup in fixtures. Update tests in the same change as the code.
- **Test-only values** (names, hosts, URLs, paths) are unmistakably fake, never production identifiers. A test of the actual production config may assert a literal production value only when an outside system holds it, naming that consumer.
- **Code is not its deployment:** a code test never reads cluster manifests or synthesizes the cluster chart. A test beside the manifests may parse them with the code's model, but not exercise the code's behavior on them (<cluster/cdk8s/AGENTS.md>).
- **Use the real platform** in the test runner; mock only test-specific behavior or genuine external boundaries.
- **Test behavior, not structure:** assert observable behavior, public contracts, schemas, invariants. Never inspect source text, pin method identity, or test style/structure (decorators, imports, signatures, "flaw stays fixed") — prevent flaws via design, lint, or a comment. Exceptions: structure that is itself a contract (published schema, public API), the only way to hold a security boundary, an analyzer test, or a rare "importing `x` doesn't import heavy `y`".
- **Assert structured results, not wording** (prompt text, UI copy, errors, logs): assert the type/code/enum/state/field. Lint-required `match=` stays. Text is asserted only when a program parses it, an external protocol fixes it, it's legal/safety text, a model verifies the prompt end to end, the test is that a log was emitted, or a UI test checks a narrow user-visible snippet. Delete tests that only show a prompt says X; document intent at the template.
- **No change detectors:** each expectation encodes a durable rule. Copying a file's values/roster into assertions (or a fixture) is not coverage. Relations within or across files (a path names a mounted file; a Service targets a declared port), schemas and runtime behavior are. Don't pin an API to an exact route list unless a security boundary requires a closed world. Generated-output snapshots run the generator and compare (`//cluster/cdk8s:test_generate_manifests`); no second test reads the committed file. Exact wire pins only for a named external contract/consumer. Never compute expected values with the code under test or a copy.
- **What must break for this to fail?** Delete tests of library basics (pydantic parsing, StrEnum equality, yaml round-trip), tests restating declarations or what mypy guarantees, and assertions that can't fail. Keep our own logic (validators, clamps), published schemas, and smoke tests of otherwise unexercised paths (saying so). Bulk is the cost multiplier; a cheap one-liner may stay.
  - Parametrize over representative inputs instead of a case per input on the same path; drop assertions implied by stronger ones and tests duplicated at the same level. Distinct boundaries, branches and error paths stay, as do positive anchors that keep negatives non-vacuous.
  - **Library spikes** pinning a non-obvious working usage stay, labelled as such unless local context (README, `x/`) says so.
  - **External protocols** we don't control: tests feeding real observed payloads stay; name the source.
  - A change detector guarding a constraint that can only break at one declaration → replace with a comment at the declaration. A relation to a second live artifact stays a test.
  - **Multi-site agreement:** prefer one generator for all sites (no test), then an integration test, then a test parsing both sites, then sync comments at every site.
  - **Pin defaults where they act:** assert resulting behavior or the boundary artifact's literal values, not `default == same constant`.
  - **Removed stays removed:** delete a feature's tests with it; no "stays removed" tests (use a tombstone), except for auth/authz/approval boundaries.
  - **Regression tests** fail on pre-fix code (PR says so) or aren't called regression tests; delete one that passes pre-fix or whose premise is gone.
- No lint ignores or per-line silencing without approval.
- `pre-commit run --all-files` over individual tools.
- `textwrap.dedent` for inline multiline strings.

### Frontend specs (vitest)

- A spec is `<module>.test.ts[x]` beside its module.
- Environment `node` unless the spec needs a DOM: first line `// @vitest-environment happy-dom`, with `//:node_modules/happy-dom` in the config's `data`.
- `jsdom` only for a spec that fails under happy-dom: add it to `data`, explain the cause under the pragma, and add a `CLEANUP` tombstone whose gate ends the exception.
- `@vitest/eslint-plugin` recommended rules are errors (`.only`, `.skip`, no `expect`, unawaited async matchers fail the build); `valid-expect` allows the second message argument.

### Waiting

**Never sleep a fixed duration; wait for the condition** (Playwright `wait_for_selector`/`wait_for_function`/`wait_for_load_state("networkidle")`; `wait_for_stable` from <util/testing/page_capture.py> for finished rendering). If an app-internal condition has nothing to wait on, expose a flag/attribute/event. In tests, assert ordering, not elapsed time.

**Gotcha:** never await `document.getAnimations()` in visual tests — `DISABLE_ANIMATIONS_CSS` pauses animations, so `finished` never settles.

### Visual test fixtures

Name each rendered entity after the state it shows (`Suspended thread`; `unnamed-stopped-thread` where the id is the label), never a realistic placeholder. A scenario that changes an entity's state renames it everywhere it renders.

## Documentation

**Delete:** comments restating name/signature/next line; Args/Returns echoing types; trivial class docstrings; changelog or "used to" comments; prose justifying the current code (prior state, why the change was right, rejected alternatives — those go in the commit/PR or, if litigated, a design doc); section banners; counts of adjacent lists; process narration. A transition that needs action is a tombstone.

**Keep:** TODOs near context; non-obvious behavior, invariants, preconditions; why-comments; action at a distance; disambiguation ("container-side path"); test comments naming the edge case. A past state earns a comment only when a future editor must act on it (in-flight migration, binding compatibility or roll-safety constraint).

If deleting it loses no information, delete it. Judge the whole file too: if prose dominates, keep cutting. Then tighten what's left without dropping caveats.

- **Decision records:** a genuinely litigated rejection (looks right, was tried or costed, lost for a non-obvious reason) goes in `<dir>/docs/design.md` as the constraint that killed it. "X rather than Y" earns its clause only if a competent author would actually try Y.
- **Lifecycle:** live docs describe the present, active work, invariants and needed lessons; git holds the rest. Before deleting a resolved `debug/` note or finished plan, move any surviving workaround or fragility warning beside the code (with an upstream/commit link if useful). Obsolete workarounds leave at most a one-line warning where the area stays fragile.
- **Deviations:** when using a standard mechanism with a repo-specific twist, document only the difference, labelled **Deviation:**, with a pointer to the standard. **Gotcha** = surprising behavior that bites.
- Backtick every code-like token in prose.
- Fenced blocks always name a language (`text` for diagrams); separate blocks for separate content; no bare JSON in prose.
- Brace expansion for 2+ suffixed variants: `gitea-{namespace,secrets,db}`.
