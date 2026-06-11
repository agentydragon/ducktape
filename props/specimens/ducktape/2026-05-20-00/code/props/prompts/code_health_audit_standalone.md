# Repository Code Health Audit — Standalone Prompt (Generic)

Goal

- Audit a codebase for code‑health issues and design/architecture smells.
- Apply the embedded, repo‑agnostic property definitions below.
- Produce two outputs: (1) Findings with precise anchors, (2) A prioritized Plan.

How To Work

- Read‑only analysis; do not modify files. You may run read‑only tools (linters, type‑checkers, complexity/duplication scanners) if available, but your judgment must follow the written rules below.
- Apply properties strictly by their wording. Do not stretch or infer beyond what a definition actually states.
- Cite precise anchors for evidence (file:line or file:start‑end, 1‑based). For many similar cases, use one short rationale plus a compact list of anchors.

Broader Smells To Look For

- Duplication/drift: repeated logic across modules; slight divergences that risk inconsistency.
- Excess complexity: deep nesting, high cyclomatic complexity, long parameter lists, magic positional tuples.
- Least‑power violations: reflection/dynamic import/low‑level syscalls when simpler, safer constructs suffice.
- Dynamic attribute footguns: probing with getattr/hasattr/setattr (or equivalents) and catching attribute errors to mask type issues.
- Exception handling smells: swallowing errors; broad/bare catch; defaulting to empty on error.
- Architecture/layering: wrong ownership or cross‑layer imports, singletons/globals as hidden state, tight coupling.
- Inconsistency/asymmetry: mixed patterns in read/write or client/server surfaces; barrel imports hiding true dependencies.
- Typing/contracts: untyped maps for domain data; overuse of Any/unknown; re‑parsing dumped models instead of using typed attributes; suspicious Optional/nullability.
- Logging/diagnostics: missing module‑level loggers; logging inside functions instead of using a declared logger; missing context.
- Testing gaps: missing tests for changed behavior; avoiding fixtures for shared setup; slow integration where unit suffices.

Structural Anti‑Patterns (explicitly flag)

- Cross‑layer coupling: lower layers importing higher‑level application internals; middleware depending on handler types. Prefer neutral shared interfaces and keep dependency direction clean.
- Wrong‑direction dependency: domain/library code depending on runtime/server layers. Extract shared primitives to a neutral module and invert the dependency.
- Private reach‑through: accessing underscore/private members or non‑API internals of another module. Require a public method on the owning component.
- Brittle exception detection: branching on exception message strings (locale/version fragile). Prefer typed exceptions or normalize at the boundary.
- Always‑on heavy deps: instantiating Docker/DB/clients on hot paths even for modes that don’t use them. Defer/inject lazily or feature‑gate.
- Protocol leakage: business/handler decisions leaking into transport/middleware layers. Define a minimal shared protocol and adapt at edges.
- Duplicate mechanisms: multiple implementations for the same mechanism (e.g., patch engines) that may drift. Establish one source of truth.
- Barrel imports: convenience re‑exports that obscure true module boundaries; reserve for real public API surfaces only.

Deliverables (print exactly these sections)

1. Findings
   - Group by: (a) Property violations (by property name), (b) Structural/architecture smells, (c) Other smells.
   - For each finding: one‑line rationale + precise anchors. Group similar anchors compactly.
   - Do not print “No violations” lines.
2. Plan
   - Prioritized, actionable phases: quick wins (lint/config), targeted refactors (local), architectural adjustments (broader), tests, verification.
   - Map steps to properties/rules/smells. Note risky/destructive steps for confirmation.

Core Code Quality Properties (Embedded, repo‑agnostic)

1. No dead code

- Remove unused symbols and unreachable branches; if a “can’t happen” guard is desired, keep at most an assertion or hard error at the boundary.
  Example:

```
if not condition_already_guaranteed:  # unreachable — delete
    return default
```

2. Imports and dependency hygiene

- Keep imports at module/file top. Localize imports only to break a proven cycle and include a brief comment. Maintain layering direction (lower layers must not import higher layers).
  Example:

```
# ✅ top‑level import
from feature import run
def handler():
    return run()
```

3. Error handling discipline

- No blanket/bare catches; catch specific errors narrowly; surface or re‑raise at boundaries; never silently swallow.
  Example:

```
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    logger.error("invalid json", extra={"preview": text[:100]})
    raise
```

4. Structured data over opaque maps

- Prefer concrete, typed models for domain data (Pydantic/dataclasses, interfaces/types, structs/records). Use enums/literal unions for closed sets. Validate at boundaries.
  Examples (conceptual):

```
// TS
type Role = "admin" | "user"
interface User { id: string; email: string; role: Role }

# Python
class User(BaseModel):
    id: str; email: str; role: Literal["admin","user"]
```

5. Principle of least power

- Choose the simplest safe construct that achieves the goal; avoid reflection/dynamic attribute probing/low‑level syscalls unless necessary and documented.

6. Minimize unnecessary nesting

- Flatten trivial guard nests; use guard clauses; bind intermediate values inline when helpful.

7. Self‑describing names and consistency

- Keep naming schemes and file/test layouts consistent within a project/module; avoid mixed synonyms for the same concept.

8. Type correctness and specificity

- Prefer precise types (discriminated unions/ADTs/enums). Avoid overuse of Any/unknown; document when unavoidable.

9. Truthful code and comments

- Structure and comments must accurately reflect behavior and intent; do not justify exceptions with misleading rationales.

10. Paths and URLs

- Use standard libraries for path/URL handling; avoid manual string concatenation for URLs/paths; encode/parse via library helpers.

11. Testing practices

- Prefer targeted unit tests; avoid over‑mocking plain data; use fixtures/shared helpers; ensure changed behavior is covered.

Language‑Specific Notes (apply if relevant)

- Python: avoid getattr/hasattr/setattr probing; use pathlib.Path for filesystem; catch specific exceptions; prefer Pydantic/dataclasses for schemas; StrEnum for string enums; use walrus (:=) judiciously to reduce nesting.
- TypeScript/JS: prefer interfaces/types with runtime validation at boundaries; literal unions/enums for closed sets; avoid Record<string, unknown> for domain shapes.
- Go: use structs and named types over map[string]any; const‑backed typed enums; time.Time and time.Duration for time; path/filepath utilities for paths.
- Java/Kotlin: records/POJOs/data classes with serde annotations; enums for closed sets; avoid raw maps for domain payloads.
