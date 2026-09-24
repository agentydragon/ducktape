# Selector resolution

How a spec's selectors become a claim map: which minified binding each readable
entity names. This is the engine contract behind `run`, `spec validate`,
`spec match-selector`, the editing gates, and the codemods.

## The pipeline

Every selector kind compiles into **one selector IR program** per chunk
(`selector_ir_lowering`), and one solve assigns every target jointly
(`selector_runtime::solve_global_selector_program`). Joint assignment is the
point: `all_different` across claimed targets is selector semantics, so a
selector that is ambiguous alone can be forced by what the rest of the spec
already claimed.

Facts reach that program two ways, by selector kind:

| Selector kind                                                                                               | How it reaches the IR                                                                                                      |
| ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `binding` (name pin)                                                                                        | lowered natively — a name lookup over `chunk_facts`                                                                        |
| `cross_ref`, `reads_member`, `member_of_module`, `passed_to_call`, `makes_decorate_call`, `intrinsic_alias` | lowered natively — relation atoms over `chunk_facts`                                                                       |
| `source_match` (JS template with holes)                                                                     | **`ChunkResolver` enumerates candidates**, which are projected into the IR as a per-target `ProjectedAllowedTuples` domain |

The backend is an OR-Tools CP-SAT sidecar
(`selector_constraint_model_builder` compiles the IR to a finite-domain problem;
`solver_backends/ortools_cpsat` solves it). It is a required tool of the
`debundle_pipeline` rule, not an optional accelerator.

## Why `source_match` goes through a matcher

Tree-shape matching and target assignment are different problems, and the split
follows what each engine is good at.

Matching a JS-template-with-holes against a chunk is a tree homomorphism. It is
local, and an inverted token index over identifiers, literals and property names
prunes candidates to the structurally compatible few
(`selector_match::Index`, `subject_tokens`). Assignment is the opposite: a small
combinatorial problem over candidate-sized domains, where `all_different` and
shared `@Name` variables genuinely need a solver.

So `ChunkResolver` acts as a **specialized propagator** for the shape subproblem
and hands the solver a domain of a few rows; the solver does the joint
assignment. A selector the matcher places nowhere never reaches the solver: it
is reported unmatched without a solve.

A target whose projected candidate rows exceed `MAX_CANDIDATES_PER_SELECTOR`
(100, `lowering/materialize/plan_builder.rs`) is likewise rejected before the
solve, as `too_broad_selector`: a selector that loose names no declaration, and
its rows would only swell the request. This holds for member selectors,
`source_matches[]` groups and anonymous statements alike.

## Resolved by elimination

`all_different` can make a selector unique that is ambiguous on its own: its
other candidates are claimed by other selectors. After a successful solve, each
unique `source_match` target's own candidate rows are filtered by dropping every
row whose owner or binding another `all_different` target's solved value holds.
When it had several rows and one survives, it resolved by elimination and gets a
`resolved_by_elimination` diagnostic naming the claimers. That entry has
severity `warning`: the run still succeeds, and the entry appears in
`selector_diagnostics.json` and `spec validate`. Such a selector silently moves
when a claimer is edited, so it should be anchored on its own.

### Rejected: let the solver consume AST facts natively instead of candidate rows

Encoding tree-shape matching as finite-domain constraints over AST nodes —
dropping `ChunkResolver` so every `source_match` lowers natively — was the
planned direction through 2026-06. It was measured and abandoned. The numbers,
all on the largest known downstream chunk (6.81 MiB, 204,235 lines) with
`-c opt` binaries:

| Path                                              | Scope                          |         Wall |
| ------------------------------------------------- | ------------------------------ | -----------: |
| matcher (`spec validate --modules --source-file`) | 6,179 `source_match` selectors | 11.1s warmed |
| native lowering (then `spec match-selector`)      | **one** selector               |  7.1s warmed |

The native path costs more for one selector than the matcher costs for the whole
spec, and its ~7s is near-constant across selectors of very different
complexity — it is per-chunk fact and domain construction, not matching work.

The model is why. One selector lowered to a 5.19 MB backend request carrying
2,383,797 domain values, including two variables over the full 1,190,984-node
AST domain; the CP-SAT solve of that request took **0.02s** against 1.91s of
model construction. A general finite-domain solver has no index over AST shape,
so the encoding spent its time rebuilding what `selector_match::Index` already
provides. Scaled to a whole spec the compile did not finish: a production-sized
run timed out at 120s in fact-domain construction without reaching the solver.
Measurements: <../debug/perf/2026_09_17_matcher_vs_native_lowering.md>.

The capability argument that motivated the native direction — cross-selector
references, negation, counting, reachability — does not depend on it. Those are
relation atoms over `chunk_facts`, and the relational selector kinds in the
table above already lower natively and solve jointly today.

## The shape matcher

`source_match/chunk_resolver.rs` builds one per-chunk model and resolves many
selectors against it, so a chunk with thousands of selectors pays setup once.
`selector_match` is the homomorphism itself: hole-skipping, alpha-equivalence
bijections, and run-hole subsequence alignment, with
<../selector_match_differential_test.rs> pinning the exact semantics.

Two properties make it near-linear rather than quadratic in selector count:
needle-only validation is hoisted out of the candidate loop, and exact-mode
identifier spellings are indexed so an identifier-only needle prunes through
postings instead of scanning every top-level statement. Scaling on a synthetic
corpus of the same shape class measures an exponent of ≈1.30
(<../debug/perf/2026_06_21_fact_resolver.md>).

## Fail-closed

A selector the matcher cannot resolve becomes an unmatched or resolution
diagnostic — never a guess.
`chunk_facts` extraction is likewise fail-closed: a construct it cannot project
faithfully is `Unsupported` rather than approximated. Rejecting input debundle
cannot handle is correct behavior; silently resolving it to the wrong binding is
not.

## Unsatisfiable programs

One contradiction must not hide every other result in the chunk, so an
unsatisfiable program is localized to the targets that cause it
(`selector_backend_solver::solve_localizing_conflicts`).

The first compile presolves across targets
(`PresolveScope::AcrossTargets`): `all_different` propagates fixed values and a
relation table narrows both of its variables. That is what keeps requests small,
but a contradiction then surfaces as an empty domain wherever propagation met
it, not where it started. When that compile or its solve is unsatisfiable, the
program is recompiled within targets (`PresolveScope::WithinTargets`) and solved
again with every constraint attributed:

- **Ownership.** A target owns its owner variable and its binding variable. A
  constraint is attributed to every target owning one of its variables: a
  candidate table to its own target, a relation table to its owner and anchor,
  a `source_matches[]` group table to every target in the group, and each
  `all_different` entry to the target(s) owning that variable.
- **Hard constraints.** A constraint over variables no target owns carries no
  attribution and stays hard.
- **Presolve within targets** narrows a variable only from constraints attributed
  solely to targets owning it, and `all_different` propagates nothing. A target
  whose own constraints empty its domain gets an empty table instead of an empty
  domain.

The sidecar gives each target an assumption literal and enforces a constraint
only while every target it is attributed to is enabled; a disabled
`all_different` entry takes a value no other entry can. It takes CP-SAT's
sufficient assumptions for infeasibility as one conflict set, disables those
targets, and repeats until the rest is feasible, then solves the rest with the
conflicting targets disabled. CP-SAT's cores are not necessarily minimal, so a
conflict set may name a target that is not strictly needed for the
contradiction.

Each target in a conflict set of two or more comes out `Conflict { with }`,
naming the others (`conflicting_selector` in keep-going diagnostics); a set of
one is that target's own constraints failing and comes out `NoMatch`. Every
other target resolves as usual. A target that depends on a conflicting one, such
as a relation anchored on it, loses that relation with it and may come out
ambiguous. Only when the hard constraints alone are unsatisfiable does every
target come out `NoMatch`, with a global diagnostic.

## `selector-solve`

`selector_solve.rs` is an Ascent (Datalog) prototype over the owner graph,
exposed as `debundle selector-solve`. It is **not** on the `run` path. It is the
working vehicle for relational selector semantics: its `references` /
`aliases` rules are proven against real pipeline output in
<../e2e/selector_solve_cross_reference_test.rs>, and
<../e2e/selector_solve_shadow_test.rs> gates its EDB against the production
binding-name resolver. Relational selector work lands here first, then in
`selector_ir_lowering` once the rule shape is settled.

## Interactive budget

Root `AGENTS.md` § Profiling applies. Interactive commands target under 10s on
warmed inputs for the largest known downstream specs; sustained runs over 60s
are priority bugs unless the command is an explicit offline/profile mode.

Always record the compilation mode with a selector timing. A `fastbuild` binary
measured 5.1× slower than `-c opt` on the same `match-selector` probe, and 35×
slower on a historical proposer fixture — `fastbuild` numbers are not
comparable to anything.
