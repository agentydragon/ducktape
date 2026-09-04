# Plan: selectors as one global constraint problem (relational spec model)

**Current state.** `source_match` selectors, exact single-statement shapes,
superclass constraints, and `match-selector` baseline probes all lower through
the global selector IR/solver on native AST facts; materialization still
constructs `ChunkResolver` for the legacy projection path, and deleting that
path is remaining work. Profiling the production-sized path showed the current
Ascent exact-assignment encoding is not the final backend: it pushes
`AssignmentRow = Vec<(var, value)>` payloads through `partial_assignment` /
`stepped_assignment`, and `all_different` becomes pairwise row filtering
instead of native finite-domain propagation. Ascent remains useful for
relation derivation and as a migration oracle, but it is no longer the planned
production owner of target assignment.

The bridge that remains is narrower: production materialization still has a
legacy `source_match` projection path that asks `ChunkResolver` for candidate
rows before trying native fallback, source-only validation still calls
`ChunkResolver` as a fast preflight, and some retained selector shapes still
need faithful native lowering before they can be accepted in production. The P0
pivot is to keep one whole-spec constraint model, but split backend ownership
explicitly: Ascent/Rust may derive facts and allowed tuples; exact assignment
belongs to a finite-domain CP/SAT backend with semantic `all_different`, not to
`AssignmentRow` enumeration.

**Reframing.** The next architectural goal is not to grow more bridge
vocabulary. It is to make the global constraint solve native over AST +
owner/reference facts: compile every selector form into one selector IR, solve
shared `@Name` variables and `all_different` together, and hand materialization
a resolved claim map. Real spec conversions are dogfood and evidence for
missing language/fact coverage; they should not add more long-lived
anchor-first bridge architecture. The remaining-work index is
<../debug/2026_06_19_p4_debt_worklist.md>, and the Gaffer-derived language
evidence is summarized below.

Put another way: debundle's selector YAML is only an authoring frontend. The
engine contract should be "compile the spec into one query-shaped constraint
model, run that model over the JS-derived fact store, and read back the relation
`readable entity -> minified entity`." Datalog/CSP terminology is about using an
efficient off-the-shelf solver for that model, not about adding a second
resolver architecture.

Extends the selector-authoring guidance in <../docs/selectors.md> and the
`debundle_stabilize` skill — which fix _who_ chooses anchors (an agent, not the
cost model) — with a proposal for _what a selector is_. Motivated by the tana/re
stabilization pass
([stabilize dogfood note](../debug/2026_06_18_stabilize_skill_dogfood.md), findings
F11–F13): real bindings whose only stable identity lives in a **non-adjacent or
cross-module** node, which the current local-match model cannot express, and which
the minimizer therefore either skips or pins by borrowing an adjacent neighbor.

Notation: `@Name` means "the binding/node pinned elsewhere in the spec as `Name`"
— a cross-reference to another selector's target.

## Goal

Complete the global selector resolver so it resolves every retained selector
form over an explicit relational model of the program (owner graph + AST
facts), without production calls to the procedural `ChunkResolver`. Parity must
be **proven, not asserted**: each construct compiles to constraints provably
faithful to current `source_match` semantics, or it remains an explicit
unsupported fallback while that construct is being implemented. The design must stay
**principled** — one general, faithful encoding per selector kind and hole, with
no per-selector special cases, silent fallbacks, or hacks to force a hard
construct through. If any retained construct, or the model itself, turns out not
to admit such a faithful, principled encoding, we **abort and rethink the
design** rather than push on with ugly hacks — an honest dead end beats a
matcher we cannot trust.

Operationally, this means the Ducktape-side work leads with the engine contract:

1. one engine-facing selector IR shared by `run`, `validate`,
   `selector-debt`, synthesis, repair, and authoring probes such as the already
   migrated `match-selector`;
2. one fact store per chunk/component containing AST-shape facts,
   binding-resolution facts, owner/reference facts, and derived-predicate inputs,
   with the solver consuming the AST facts natively rather than candidate rows;
3. one logical whole-spec solve, with `@Name` as a shared logic variable rather
   than a lookup into already-resolved members; the engine may decompose
   independent connected components internally without changing the model;
4. one explicit exact-assignment backend boundary with typed finite domains,
   allowed tuples, equality/disequality, ordering, target projection, and a
   native/global `all_different`;
5. one diagnostic/result projection for no-match, ambiguity, duplicate claims,
   unsupported constructs, and "which facts forced uniqueness";
6. one resolved claim map consumed by the existing atomic-DAG / realizability /
   emission pipeline.

## Problem: the only join we have is adjacency

Today a selector is a single **contiguous AST window** matched as a tree-with-holes,
modulo alpha-renaming. `binding_groups` does not merge two patterns — it claims
several targets that already sit _inside one window_. So the only way target `X` can
be constrained by another node is if that node is **adjacent** (in the same window as
context). The one join the language has is **positional adjacency**.

When `X`'s identifying evidence lives in a non-adjacent node, there is nothing to
ride on (examples from the pass):

- `isMeetingTranscriptionProvider` is `return EBt(n)` — its identity is in `EBt` and
  in the caller that reads `meetingRecordingTranscriptionProviderParamId`;
- the 12 `__decorate` / `__defineProperty` helper copies are byte-identical templates
  — only their **use sites** distinguish them;
- `let HI = UJ` is a re-export alias — its identity is the class declared elsewhere.

Cross-module is just the extreme of "non-adjacent"; the same wall hits _inside_ one
module. **Neighbor-borrow is the misuse of the one join we have**: lacking a better
edge, the minimizer grabs the adjacency edge to an unrelated neighbor — and adjacency
is exactly the relation a rebuild destroys.

## Model: program graph + conjunctive-query selectors + one global solve

Model the source not as text but as a **labeled relational structure** `G`:

- **nodes** — AST nodes plus binding entities;
- **relations** — AST `child` / `sibling` plus the one semantic edge alpha-renaming
  forces on us (and that survives it): `resolves_to(use, def)` — which identifier
  occurrence binds to which declaration — and `member_of_module(binding, module)`.
  Higher-level edges (`calls`, `alias`, a decorator application, …) are **derived**
  from AST shape + `resolves_to`, not primitives (see Implementation);
- **labels** — the tokens minification cannot rewrite: string/number literals,
  property & method names, operators, keywords. Identifiers carry _no_ label (that is
  the renaming).

Then:

- **A selector is a conjunctive query (a pattern)** over `G`: distinguished variables
  (the targets it claims) plus anchor variables, joined by relation-atoms and
  constrained by labels.
- **The whole spec is the conjunction of every selector's pattern**, sharing a
  variable wherever one selector references another (`@Name`). That conjunction is
  one big pattern over `G`.
- **`run` = find the homomorphisms of the whole-spec pattern into `G`, as one
  solve** — a single logical CSP/query instance, not selector-by-selector
  resolution. (Evaluating a conjunctive query _is_ finding homomorphisms into
  the structure; a CSP solver and a pattern-matcher are the same thing here.)
- **Validity = target-categoricity**: projected onto the distinguished variables, the
  solution set is a single tuple. Anchors may match in many places; only the
  **claimed** image must be forced. Per target: 0 solutions → `no-match`; ≥2 distinct
  → `ambiguous`; exactly 1 → pinned. Plus a global **all-different on targets** (two
  distinct claims may not land on the same node — today's `duplicate-claim`, now a
  first-class constraint instead of a post-hoc error).

This **subsumes** the current language: `source_match` is a _tree-shaped_ query over
`(child, sibling, label)`; `binding_groups` is the same with several distinguished
variables. We are lifting "tree pattern over adjacency" to "graph pattern over all
relations." Nothing is discarded.

### Why solve globally, not per-selector

Solving the whole spec at once is the key move, not an optimization:

- A selector that references another (`@Name`) is just a **shared variable** plus a
  relation-atom. There is no resolution order and no stratification — both endpoints
  propagate simultaneously.
- **Reference cycles are a non-issue** (`X` anchored on `@Y`, `Y` anchored on `@X`):
  CSP constraints are non-directional, so a mutually-constraining cluster is solved
  jointly with no "which first."
- Cross-selector consistency (all-different, shared anchors) is enforced _during_ the
  solve. Two `__decorate` selectors that each say "the helper that decorates
  `@ClassA.m1`" / "`@ClassB.m2`" get assigned to their respective copies at the same
  time, and all-different keeps them apart.

So selectors do not "depend on each other"; they are **matched against `G` at the
same time**.

This also makes the current anchor-first bridge a migration artifact. Today
relational selectors often resolve `@Name` through members that were already
claimed by `source_match` or binding-name resolution. In the endpoint, that
ordering disappears: both the anchor and the target are distinguished variables
in the same query, so the solver can handle cycles and mutually-constraining
clusters without stratifying selector resolution.

## The invariant signature (what makes a selector stable)

Partition the signature by behavior under the minification transform `T`:

| Class           | Relations / labels                                                                                                  |
| --------------- | ------------------------------------------------------------------------------------------------------------------- |
| **T-invariant** | literals, property/method names, `resolves_to` (and edges derived from it — `calls`, `alias`, …), module membership |
| **T-variant**   | identifier spellings, source position/order, **adjacency**, control-flow shape, arity                               |

A selector should be a query over the **invariant** sub-signature only. The whole
"identity vs implementation" rubric of the `debundle_stabilize` skill collapses to one
rule — _use invariant relations, not variant ones_:

- **identity anchor** = the target carries an invariant **label** locally (its own
  string);
- **reach out** = traverse invariant **edges** to a node that does;
- **neighbor-borrow** = a query that looks structural but bought uniqueness with the
  **adjacency** edge (variant);
- **honest debt** = no invariant label is reachable from the target along any
  invariant edge.

`run`/categoricity is gated mechanically; **`T`-invariance cannot be** (there is no
future bundle in hand), so it stays the agent's judgment — the model just supplies
invariant **edges** as new building blocks and removes the incentive to neighbor-borrow.

## Two front-ends, one constraint store

A JS-AST-with-holes pattern already _is_ a conjunctive query over `(child, sibling,
label)`: the holes are existential variables, the kept tokens are label-atoms. So the
two surface syntaxes are front-ends to the same back-end:

- **AST-shape atoms** — JS-with-holes (today's `source_match`). Natural for local
  structure ("a class with these methods", "a function returning this literal").
  Keep it.
- **Relational atoms** — references to other targets by `@Name`, joined through
  `resolves_to` (and the derived predicates built on it — `calls`, `alias`, …),
  including across modules. Not naturally AST-shaped; written as explicit edge
  constraints.

Both compile to constraint-atoms in the one CSP. Use JS-AST where it reads naturally;
use relational atoms for the cross-node edges. They are not two systems — they are two
parsers feeding one constraint store.

## `run` and `stabilize` as operations over this model

- **`run`** = build the CSP from the whole spec, solve, project onto targets, emit the
  claimed nodes into their modules. Decidable and checkable: categoricity gives
  `no-match` / `ambiguous`, the all-different gives `duplicate-claim`.
- **`stabilize`** = search for an alternative constraint-set for the targets that (a)
  keeps the global CSP target-categorical and (b) maximizes expected `T`-invariance of
  the atoms used. Heuristic, because `T` is unknown — the minimizer optimizes a proxy
  (size / locality / `selective x stable x cost`); the real objective is
  `T`-invariance, and the agent supplies that prior.

## Implementation: the solving core

Conjunctive-query evaluation over a fixed relational structure _is_ the
CSP/homomorphism problem. There are two distinct engine jobs here, and the
profiling result says not to collapse them:

- **relation derivation** over a large fixed EDB (~1-2M AST facts), joined by
  selective equality atoms. This is database/Datalog territory, and Ascent or
  direct Rust indexes can produce compact domains and allowed tuples.
- **exact assignment** of selector variables to candidate values, including
  target categoricity and `all_different`. This is finite-domain CP/SAT
  territory, and should use the backend named below.

The serialization boundary is therefore `facts + lowered selectors ->
CompiledSelectorProblem -> SelectorCpSatRequest`, not "generate a second
procedural matcher" and not "push whole assignment rows through Datalog until
one survives." The compiled problem owns shared interned value domains,
narrowed variable supports, allowed tables, binary constraints, target
projections, and semantic `all_different` in one representation.

**EDB — facts extracted once per chunk.** The AST plus the one semantic edge:

```
child(parent, idx, node).   kind(node, Kind).   prop_name(node, "foo").
str_lit(node, "…").   num_lit(node, 5).   operator(node, "+").
resolves_to(use, def).   member_of_module(binding, module).
```

The AST projection already exists in `chunk_facts.rs` and
`SelectorFactStore::extend_chunk_facts`; the owner/reference facts already feed
the selector solver. The open work is to make every retained selector shape
consume those rows directly, with no production pre-enumeration by
`ChunkResolver`. `resolves_to` remains the genuinely semantic edge — the one
minification preserves while names churn.

**Derived predicates — a rule library, not engine primitives.** `calls`, `alias`, a
decorator application, etc. are rules over AST shape + `resolves_to`:

```
calls(Caller, Callee) :- kind(S, CallExpr), ancestor(Caller, S),
                         callee(S, Use), resolves_to(Use, Callee).
alias(X, Target)      :- kind(D, VarDecl), declarator(D, X, Init),
                         resolves_to(Init, Target).
```

**Selectors lower to query bodies.** An AST-shape `source_match` compiles to a
conjunction of `child` / `kind` / `prop_name` / `str_lit` / `resolves_to` atoms — and
the **holes mostly vanish**, because a conjunctive query constrains only what it
_mentions_: `ANYTHING`, `STMT_LIST`, `DECLARATORS` are just unmentioned structure.
(Ordered/positional holes — "`open` before `close`", "a run of statements" — need
recursion or sibling-order atoms, and are T-variant anyway.)

**Variable scoping — the resolved form of "don't apply/rename independently":**

- _Global symbols_ = the named spec entities (each target + shared anchor). A symbol
  used in two selectors is the **same** logic variable ⇒ same source binding. That is
  the cross-reference; `@Name` is just reusing a name — one global renaming for named
  things, no post-hoc node-identity join.
- _Clause-local holes_ = pattern-internal throwaways (a param, an `EXPR`), standardized
  apart per selector so unrelated functions' `n`s do not collide.

**Solve / read-back.** Evaluate once. Per target symbol: exactly one binding →
pinned; zero → `no-match`; ≥2 → `ambiguous`. `all_different(targets)` is a
semantic constraint over the distinguished target variables, not an optional
post-pass: two distinct claims may not land on the same source owner, and the
constraint should propagate during assignment. Translate-out = answer tuple per
distinguished variable → claim node → module.

**Backend ownership.** Keep the model as one global constraint problem, but do
not make Ascent own the exact assignment search. The owned layers are:

1. **Lowering:** selector YAML / `source_match` / relational atoms →
   `SelectorProgram`.
2. **Fact and tuple derivation:** bundle AST + owner/reference facts →
   finite domains and allowed tuples. This may use Ascent or direct Rust joins
   where Datalog-style relation derivation is a good fit.
3. **Compiled constraint problem:** `CompiledSelectorProblem` with shared
   interned finite domains, narrowed variable supports, allowed tuples,
   equality/disequality, ordinal/order constraints, `all_different`, and target
   projections.
4. **Exact solve:** a CP/SAT backend searches the model and returns enough
   solutions/projections to decide target categoricity.
5. **Projection/diagnostics:** map solver assignments to claims and explain
   no-match, ambiguity, duplicate claim, unsupported constructs, and forcing
   facts.

**Solver choice.** Target
**[OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver)**
for the exact-assignment backend. It matches this problem directly: finite
integer domains, allowed-assignment table constraints, solution/status
reporting, and a native
[`AllDifferent`](https://developers.google.com/optimization/reference/python/sat/python/cp_model#AddAllDifferent)
global constraint. The first production implementation is the thin Bazel/C++
sidecar with protobuf transport. That keeps OR-Tools ownership on the C++ side
and keeps the Rust boundary as `CompiledSelectorProblem -> SelectorCpSatRequest`.
The next risk is not integration feasibility; it is whether the retained Tana
selector language lowers into this model with enough coverage and acceptable
runtime.

The fallback is **[RustSAT](https://github.com/chrjabs/rustsat) +
CaDiCaL/Kissat** if OR-Tools integration is too expensive. In that shape the
same compiled finite-domain problem is encoded to SAT: boolean variables for
`(selector variable, candidate value)`, exactly-one domain constraints,
at-most-one per candidate value for `all_different`, and clauses/auxiliary
variables for allowed tuples. This is an implementation fallback for the same
CP model, not a license to hand-roll CSP heuristics in debundle.

Do **not** keep optimizing the current `AssignmentRow` scheduler as the
destination. It is useful as a migration oracle and as evidence for which facts
and tuples are needed, but it is exactly the wrong shape for target injection:
it enumerates partial/full assignment rows and checks pairwise disequality after
rows are materialized. Also do **not** restart this as Soufflé, Crepe, DDlog, or
a bespoke backtracking solver unless a measured backend spike proves both
OR-Tools and the SAT fallback are unsuitable.

**Caveats.** Pure positive Datalog needs care for closed-world / counting anchors ("the
class with _exactly_ these members"; "the _only_ class with method m" as a writable
anchor) — stratified negation / aggregation; categoricity as a _check_ is plain
counting. Variable-length holes need recursion. Most cases — literal + `resolves_to`
anchors — are plain positive CQ. New code is AST→atoms lowering plus native
solver ingestion of the existing EDB extractor. That extractor is a fail-closed
recursive walk (not a `swc_ecma_visit::Visit`, whose no-op defaults would
silently skip an un-overridden node type): an unmodeled construct raises a loud
`Unsupported`, never projecting a silently-incomplete fact set that would match
wrongly.

## Scale and performance

The model may decompose by connected components as a backend optimization, but
that must not weaken the semantics. A selector that shares no symbol with any
other is an independent subproblem; a selector cluster connected by shared
targets, alpha variables, relation atoms, or `all_different` is one exact
assignment problem. Diagnostics should not force a Rust-level decomposition that
changes the model or sacrifices propagation.

Tooling payoff: have the solver **report, per target, which relations forced
uniqueness, tagged invariant/variant**. Then `selector-debt` can auto-flag "unique only
via a `T`-variant edge" — the principled form of the neighbor-borrow detector (dogfood
F12) and of the skip-with-reason ask (F5).

## Worked examples (from the metaNode pass)

- **Converted with local invariant labels** (already done): `addMeetingBotCommand` /
  `startAiChatCommandHandler` (own message strings), `sortNodeByViewSortSpec`
  (`.sortBySortSpecification`), `canRunMeetingClassificationCommands`
  (`eventClassificationConfig`), `recordHomeNodeAttributeUsage` (own
  `throw new Error("Could not find workspace attribute definition")`).
- **Newly pinnable via invariant edges**:
  - `isMeetingTranscriptionProvider`: `body = return @EBt(_)` — a `calls` edge to a
    separately-pinned `EBt` (or one edge further, to the reader of the
    `meetingRecordingTranscriptionProviderParamId` member).
  - the 12 `__decorate` copies (**provably un-pinnable locally** — identical
    templates): the helper `h` in the call `h(_, @CalendarViewAccessor.prototype,
"clearDayStartEndTimeConfig")` — generic call/arg/member atoms plus
    `resolves_to` of the 2nd argument's base to `@CalendarViewAccessor` and a method
    literal. The use-site disambiguates the copy.
  - `let HI = UJ`: `alias(HI, @NavigationStackItemAccessor)`.
- **Residual true debt**: only when no invariant label is reachable from the target
  along any invariant edge.

So the honest-debt set shrinks from "no _adjacent_ anchor" to the much smaller "no
invariant label in the target's reachable neighborhood" — metaNode's 18 debt items
become roughly 5.

## Landing in the debundle binary

This is not a side tool — it is the **resolution layer**: the code that maps
each readable spec entity to its minified node on the chunk. After #2439, the
global selector solver is on that path. After #2443/#2446/#2447, exact
single-statement and superclass `source_match` forms lower to native AST
constraints without needing candidate rows. `match-selector`'s baseline probe
also uses this solver path. Current work is to try native materialization first,
delete the remaining member/group candidate-projection path, and remove the
production anonymous fallback. Remaining production work is native coverage for
retained selector shapes that should fail closed as unsupported once projection
is gone.

The reference oracle is named in code: `source_match::SelectorResolver`, the
trait `ChunkResolver` implements — `(parsed chunk, JS-template selector) →
unique {claimed binding | body-index group}`, with no-match / ambiguous as the
only failure modes.

### EDB from analysis the pipeline already does

The pipeline already parses the chunk (`js_ast`), computes binding resolution
and the owner/reference graph (`program_analysis`, `facts/`, `graph/`, emitted
as `owner_graph.json`), and extracts `chunk_facts.rs` AST rows. Those are the
EDB:

- module membership + ordinal/purity from the owner graph;
- owner/reference rows already consumed by `selector_ir_solver.rs`;
- AST `kind` / `child` / literal / identifier / property / operator /
  top-level rows already stored by `SelectorFactStore::extend_chunk_facts`;
- label posting lists still used by `ChunkResolver` in source-only validation
  and the production `source_match` projection fallback.

The cutover is a consumer change: add native AST relations and shape rules to
the fact/tuple derivation layer, build the shared `CompiledSelectorProblem`,
delete each production fallback shape after focused equivalence tests and
corpus checks pass, then keep tooling/authoring uses behind the same
solver-backed semantics.

### It replaces several workflow parts, not just the matcher

| Today                                                                                                     | Becomes                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| production `source_match` projection still asks `ChunkResolver` for candidate rows before native fallback | native-first solve over AST + owner/reference facts; legacy projection kept only as a migration census until unsupported shapes are covered |
| source-only `validate --modules --source-file` still calls `ChunkResolver` directly                       | same solver-backed result model as materialization, with the legacy resolver retained only as a parity oracle                               |
| `validate` no-match / ambiguous / duplicate-claim (per-selector + post-hoc)                               | solver categoricity (no-match / ambiguous) + `all_different` (duplicate-claim) in one keep-going pass                                       |
| `match-selector` baseline resolution is native, but slack/provenance remain command-local                 | solver-native explanations/provenance so the probe and production diagnostics share one result vocabulary                                   |
| `selector_codemod.rs` prove-gate (candidate-index uniqueness)                                             | solver categoricity; the minimizer's search space grows to relational atoms — it can now _propose_ the cross-ref selectors that now skip    |
| `selector_debt.rs` source-aware near-ambiguous / repeated-body scan                                       | solver reports per target **which atoms forced uniqueness, tagged invariant/variant**, and the categoricity margin                          |

The cycle/atomic gate (`gate.rs`, `realizability/`, `atomic_units.rs`) and emit run
**downstream of resolution on the resolved ownership** — unchanged, as are the module
taxonomy and `describe`/`show-source`/`cluster` (they read the graph, not selectors).

### Spec-format evolution (what the current YAML can't express)

Two layers, kept distinct:

- **IR (engine-facing) — constraint-native.** The compiled form the solver
  consumes is a normalized constraint model: per-target distinguished variables,
  finite domains, allowed tuples derived from the predicate library
  (`calls`/`alias`/…), `@Name` cross-refs as shared variables, and any
  equality/disequality/negation-derived constraints. Non-negotiable — it is what
  the engine evaluates.
- **Authoring (human/agent-facing) — a structured 1:1 skin over that IR**, not a raw
  Datalog or SAT file. The dominant **shape** atom must stay JS-with-holes
  (hand-written AST atoms are the unreadable dump the rubric exists to avoid),
  so the format is always relational atoms + a JS-shape escape, compiled to the
  constraint model.

So the authoring vocabulary — today `selector.binding.name`, one contiguous
`source_match`, `binding_groups`, `anonymous_statements`, none of which can express a
cross-reference, disjoint relational constraints, a shared global symbol, or
negation/counting — grows from "a name pin or one match-string" to **a query = a
conjunction of atoms**:

- **shape atom** — today's JS-with-holes `source_match`, lowered to
  `child`/`kind`/`str_lit`/`prop_name` atoms. Holes mostly disappear: a query constrains
  only what it mentions, so `STMT_LIST`/`ANYTHING`/`DECLARATORS` go away (only
  ordered/positional holes remain, and those are T-variant).
- **relational atoms** — `calls`, `alias`, `reads_member`, or the raw `resolves_to`,
  tying the target to other nodes.
- **cross-reference** — `@Name` is the _same logic variable_ as that entity's target,
  shared across the whole spec (subsumes per-match `target_binding`); `$x` is a
  clause-local hole.
- **escape hatch** — a `where:` block of raw IR atoms sharing `$` variables, for the rare
  multi-atom join or negation the structured keys express clumsily. This is where the
  format goes relation-native exactly when YAML would fight the semantics, without paying that
  cost for the simple 95%.
- **negation / uniqueness** (later) — "the _only_ X with method m", "whose sole use is
  @Y" — stratified-negation atoms (most naturally written in the `where:` escape hatch);
  a few anchors need them, optional at first.
- **`minified_name`** — the bootstrap atom (temporary; dropped once nothing uses it).

Sketch:

```
- name: isMeetingTranscriptionProvider
  query: [{ calls: "@resolveMeetingTranscriptionProvider" }]
- name: HI
  query: [{ alias_of: "@NavigationStackItemAccessor" }]
- name: DocumentAccessorFactory
  query:
    - shape: |
        class $self extends ANYTHING {
          getName() { return "DocumentAccessorFactory"; }
        }
- name: soleConsumerOfFoo # escape hatch: multi-atom join + negation
  where:
    - "calls($self, @foo)"
    - "not (calls(other, @foo), other != $self)"
```

**Backward-compatible migration**: `binding.name` and `source_match` lower to query atoms
(a name pin = `{ minified_name: "…" }`; a `source_match` = a shape atom), so every
existing selector keeps resolving while the new atoms are added incrementally; the
equivalence gate guards the swap. Keeping the skin **1:1 with the IR** also keeps a full
rule-DSL switch cheap — a mechanical YAML→DSL codemod — deferred until the atom vocabulary
stabilizes and only if relational/negation usage grows enough to justify it.

### Holes under the query model

The current `source_match` hole vocabulary maps four ways; only the first is a true
"hole" in the CQ:

- **existential** (the majority) — `ANYTHING`, anonymous `EXPR`/`STMT`, and the run-holes
  `STMT_LIST` / `ARGS` / a single `CASE_REST` / `DECLARATORS` ("ignore the rest") — **vanish**: a CQ asserts only what it requires, so
  you simply do not mention those children/args/members.
- **readability labels** — `EXPR_*`, `STMT_*`, `ANYTHING_*`, and list-hole
  suffixes like `STMT_LIST_x` are comments, not equality constraints, so they
  lower exactly like their bare hole keyword and stay existential.
- **predicate** — `STR_LITERAL_MATCHING_RE("re")` → a **filter atom**
  `str_matches(node, "re")` (a builtin), not existence.
- **ordered / positional** — multiple anchored runs implying a subsequence
  (`ANYTHING; a(){} ANYTHING; b(){}` ⇒ `a` before `b`), `DECLARATORS_BEFORE/AFTER`,
  and any explicit source-order choice → **sibling-order atoms**, which are
  **T-variant**. Permitted only via the `where:` escape hatch and flagged by
  `selector-debt` — a stabilization target, not a default.

Legacy `target_statement`, `target_statements`, and authored
`wildcard_string_literals` are not current Tana/Gaffer requirements. Their
public authoring surfaces have been removed, so do not re-nativeize them by
default; keep only a documented migration path if another consumer proves it
needs them. For anonymous side effects, prefer one distinguished target per
selector/query instead of an index into nearby context.

So in the clean native form the surviving holes are exactly the existence qualifiers;
regex becomes a first-class CQ construct, and order/position is
expressible-but-discouraged.

**Fail-closed, never silently weaker.** The lowering is total-or-loud: each construct compiles to
provably-faithful atoms, or the lowering raises `Unsupported(construct)` and the resolver
**errors** — it never emits a query that quietly omits a part it didn't model (which would
under-constrain and silently match the wrong owner). The existential "vanish" above is _not_ such an
omission: `ANYTHING`/run-holes mean "don't care", and a CQ that doesn't mention those positions
means exactly that — dropping a don't-care is faithful; dropping a _constraining_ part is the
forbidden thing that triggers the error.

### Can the query model match every selector kind faithfully?

Yes — every selector kind and hole the specs depend on has a faithful encoding over an AST-facts
EDB; nothing is fundamentally inexpressible.

| construct                                                                       | faithful encoding                                     | EDB fact needed                                        |
| ------------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------ |
| `binding: { name }`                                                             | `name_owner` lookup                                   | owner graph (have it)                                  |
| structural shape (`class` / `function` / call …)                                | positive `kind` / `child` / `prop_name` join          | `kind(node,k)`, `child(parent,idx,child)`, `prop_name` |
| `EXPR[_label]` / `STMT[_label]` / `ANYTHING[_label]`                            | unmentioned position (faithful don't-care)            | —                                                      |
| run-holes `STMT_LIST` / `ARGS` / `ARRAY_ELEMENTS` / `CASE_REST` / `DECLARATORS` | absorb = don't-constrain; anchors keep relative order | `child` index column                                   |
| `STR_LITERAL_MATCHING_RE("re")`                                                 | filter atom `str_matches(node,"re")`                  | `str_lit(node,value)`                                  |
| internal exact identifier constraint                                            | name filter                                           | `name(node,spelling)`                                  |
| public `source_match` identifiers                                               | identifier variable + scope                           | `resolves_to` (have it) + alpha-canonical ids          |
| `target_binding`                                                                | choice of distinguished variable                      | —                                                      |
| `binding_groups` (adopt_names / exports)                                        | mechanical per-target expansion (already done)        | —                                                      |
| ordered / positional anchors                                                    | child-index compare (`i < j`; adjacency `j == i + 1`) | `child` index column                                   |

So beyond the phase-1 owner graph the lowering needs: `kind`, `child(parent, idx, child)`,
`prop_name`, `name`, `str_lit`, and a per-node structural-canonical id (plus the existing
`resolves_to`). Two encodings must be proven **bit-for-bit faithful**, not merely plausible — this
is exactly where fail-closed lowering earns its keep:

- **run-hole adjacency / subsequence** — the matcher's precise rule for when siblings must be
  contiguous vs may have gaps (run-hole present ⇒ gaps allowed; absent ⇒ exact adjacency). The
  index-comparison encoding is simple; reproducing the matcher's exact rule is the work.
- **alpha-equivalence scoping** — shadowing and which binding a wildcard reference resolves to.
  `resolves_to` carries scope, but matching the matcher's exact alpha rules is the subtlest point.

Both are guarded twice: the lowering **errors** on any pattern whose faithful encoding it has not
implemented (no silent approximation), and the corpus differential flags any lowered query that
disagrees with the matcher.

**The two are coupled, and the native encoding must keep that coupling
declarative.** The run-hole routine does two things at once — (A)
variable-length ordered-subsequence **placement** of the fixed segments around
the holes, and (B) a **global bijective alpha-binding** constraint threaded
through that placement (which is _why_ it backtracks —
`place_segments` snapshots/restores `MatcherState{replacements, alpha_scopes}`).
In the endpoint, neither part is a procedural side table: placement choices and
the identifier equalities/disequalities they induce are facts in the same
solver model.

- **(A) placement is cleanly relation-expressible — backtracking and all.** A list-with-holes is
  `[hole?, seg₁, hole, …, segₖ, hole?]`; "`segᵢ` matches starting at ordinal `p`" is the
  node-homomorphism over its elements vs `subject[p..]`, and a full match is a **chain**
  `seg_at(1,p₁), seg_at(2,p₂), …` with `end(i) < start(i+1)` per gap-allowing hole plus the
  anchoring equalities. The feasible-placement set should be derived as allowed
  tuples, then handed to the exact backend; the imperative
  greedy+snapshot/restore search **disappears** because placement is data, not a
  side resolver. **≥2 holes per list (interior segments with a free start) are fine, and the corpus uses them** — `infra/sentryInit.initSentry` is
  `[STMT_LIST, const s = …, STMT_LIST]`, `infra/android.serializeNodeForNativeBridge` is
  `{ANYTHING, isTag: ANYTHING, ANYTHING}`. So the earlier "≤1 hole per list" intuition is
  **empirically false**; the chain-join handles interior links regardless.
- **(B) alpha-all is the same query, not a matcher clone.** A placement induces
  identifier-pair facts tagged with the placement id. Alpha bijection is then a
  declarative rejection condition: reject any placement where one selector
  identifier maps to two source identifiers, where two selector identifiers map
  to the same source identifier, or where required `resolves_to` / scope facts
  disagree. In the backend model this is equality/disequality plus
  `all_different` over placement-tagged variables. If the tuple set gets too
  large, that is a profiling and encoding problem to solve with better indexes
  or the SAT fallback, not a reason to reintroduce a procedural `MatcherState`
  side resolver.

So we narrow on the coupling axis, not the hole-count axis. The faithful
encoding lowers **placement** and **alpha constraints** together. The only
fail-closed cases should be forms whose required facts are not modeled yet, not
forms that would be easier to handle by calling the old matcher.

## What the matcher cannot do that the query model can

Today's matcher is single-pattern, positive, intra-statement, and per-selector independent: it
matches one statement's JS shape in isolation, with no view of the reference graph. Six things
out of reach today (or surviving only as brittle minified-name pins) become ordinary conjunctive
queries once every selector shares one solve. (Predicate names below are illustrative of the
derived-atom library — `calls`/`alias` exist; `imports`/`exports`/`extends` are the obvious next,
per the open questions.)

**1 — Anchor on a relationship, not a shape.** A bare delegator `function UBt(x){ return EBt(x) }`
has no distinctive shape; the only handle today is the minified callee `EBt`, which dies on
re-minification.

```
- name: isMeetingTranscriptionProvider
  query: [{ calls: "@meetingTranscriptionRegistry" }] # the owner whose call target is that entity
```

**2 — Tie several targets together** (shared variable + automatic `all_different`). No selector
can reference another's match today, and duplicate-claim is caught only post-hoc. `@registry` is
the _same_ owner in every query; the consumers are pinned relative to it and forced distinct.

```
- name: registry
  query: [{ shape: "const $self = new Map();" }]
- name: addProvider
  query: [{ calls: "@registry" }, { reads_member: { of: "@registry", name: "set" } }]
- name: getProvider
  query: [{ calls: "@registry" }, { reads_member: { of: "@registry", name: "get" } }]
  # the solve keeps addProvider != getProvider without either naming the other
```

**3 — Negation / absence.** Positive-only matching cannot say "and lacks m" or "nothing
references it".

```
- name: DisposableAccessor # has getName, but no dispose
  where: ['has_method($self, "getName")', 'not has_method($self, "dispose")']
- name: chunkEntryRoot # referenced by nothing else in the chunk
  where: ["not resolves_to(_, $self)"]
```

**4 — Transitive closure / recursion.** The matcher has no reachability; "everything reachable"
is inexpressible per-statement.

```
- name: meetingSubsystem # the registry plus its whole eager-use cone
  query: [{ reachable_from: { root: "@meetingRegistry", via: eager_use } }]
```

**5 — Uniqueness / counting.** "Exactly one" today is per-selector existence over the chunk body,
not a count predicate over the graph.

```
- name: theConfigSingleton # the ONLY exported class extending @BaseConfig
  where: ["extends($self, @BaseConfig)", "exported($self)", "unique $self"]
```

**6 — Join identity (an AST literal) with relation (a graph edge) in one anchor.** A selector is
shape-only or name-only today; it cannot combine "emits this literal" with "is imported by that
module".

```
- name: DocumentAccessorFactory
  query:
    - shape: |
        class $self extends ANYTHING {
          getName() { return "DocumentAccessorFactory"; }
        }
    - { imported_by: "@settingsModule" } # str_lit identity AND a cross-module edge, one solve
```

Cases 1, 2, and 6 are the metaNode pins that are honest debt today — bare delegators, empty-ish
classes, consumer clusters the matcher can reach only by minified name or a fragile body shape
(exactly what the stabilization rubric flags). The relational model turns each into a
re-minification-proof identity anchor.

**All six are implemented and pass** as Ascent rules over a synthetic EDB in
`selector_query_examples.rs` (`selector_query_examples_test`): cross-ref joins,
disequality, stratified negation (`!rel`), recursive transitive closure, `count`
aggregation with a uniqueness guard, and an AST-literal-∧-import-edge join all
resolve to the expected owners. That proves the fact vocabulary and rule shape
are useful. It does **not** prove Ascent is the right exact-assignment backend
for production-sized selector programs, because the current solver reaches
target assignment by enumerating `AssignmentRow` tuples and filtering them. The
backend pivot preserves the vocabulary proof while moving exact target
assignment to a finite-domain solver.

## Remaining work (single-model cutover)

This is the detailed P0 queue; <../TODO.md> should only summarize it.

- **G1 — alpha-all as query constraints.** Lower `identifiers: alpha_all` to
  identifier occurrence variables, `resolves_to`/scope facts, placement-tagged
  equality/disequality constraints, and native `all_different` where the
  selector semantics require injectivity. Do not clone `MatcherState` or add a
  procedural alpha resolver.
- **G2 — exact-assignment backend cutover.** The compact
  `CompiledSelectorProblem`, backend solver adapter, generated proto bindings,
  CP-SAT sidecar wire format, and anonymized broad-vs-specific fixture are
  landed for the supported subset. The next risk is production-sized runtime
  and language coverage, not sidecar feasibility. If the OR-Tools native path
  becomes too expensive after profiling, encode the same compiled finite-domain
  problem through RustSAT + CaDiCaL/Kissat. The acceptance bar is measured
  runtime and matching semantics, not a nicer hand-written scheduler.
- **G3 — retained hole and predicate lowering.** Lower today's retained
  JS-with-holes selectors and binding groups into native AST/owner IR atoms:
  simple existential holes, regex string predicates, run-hole placement
  (`STMT_LIST`, `ARGS`, `DECLARATORS`, `CASE_REST`, and the `ANYTHING`
  object-property / class-member runs), anonymous statements as distinguished targets, exact
  identifiers, target bindings, and binding groups. Prioritize shapes that
  still force the production projection fallback. Each construct either
  compiles faithfully or reports `unsupported`.
- **G4 — source-match surface pruning.** Keep unused authoring/tooling options
  out of the native query model. Current Gaffer/Tana census showed no use of
  `target_statement`, `target_statements`, authored `wildcard_string_literals`,
  the single-choice `match-selector --identifiers` CLI flag, or selector-codemod
  exact-body fallbacks; those surfaces have been removed. Slate any other
  vestigial debundle features that gaffer-private does not use for removal
  rather than carrying them into the query model. Defer the alpha-all identifier
  mode, exact anonymous `match`, readability labels, and top-level `STMT_LIST`
  cleanup until either gaffer-private is migrated or the single-resolution
  backend owns the retained forms.
- **G5 — tooling semantics cutover.** `match-selector` baseline resolution
  already uses `selector_runtime`; move source-only validation, selector
  codemods, synthesis, repair/prove gates, and slack/provenance diagnostics onto
  solver-backed selector semantics so authoring tools and production
  materialization do not drift. No-match, ambiguous, unsupported, and
  duplicate-claim diagnostics should be projections of the native solver result,
  not `ChunkResolver` side tables.
- **G6 — relation/language fold-in.** Re-express `cross_ref`, `reads_member`,
  `member_of_module`, `passed_to_call`, `makes_decorate_call`,
  `intrinsic_alias`, and the Gaffer feature-request vocabulary as IR atoms or
  derived predicates over owner/reference + AST facts. The real-spec conversion
  worklist in <../debug/2026_06_19_p4_debt_worklist.md> is evidence and dogfood
  for this, not a reason to add more permanent bridge passes.

## Execution contract

- **Build/test gate**: build the changed debundler library and its consumers;
  run the changed area's tests with `--cache_test_results=no` and lint on for a
  final step.
- **Injective-disambiguation gate**: a broad selector and a stricter selector
  must resolve jointly through target injectivity. Example: if `X` is
  constrained by `const x = f(ANYTHING)` and `Y` by `const y = f(123)`, while
  the source has `xx = f(134)` and `yy = f(123)`, the global solve must force
  `Y = yy` and then `X = xx`. Disabling `all_different` may be used only as a
  temporary profiling switch, never as final semantics.
- **Resolver parity gate**: while fallback shapes still exist, the native solve
  and the current fallback resolver agree on every covered resolved target,
  no-match, ambiguous match, duplicate claim, and unsupported construct.
  Compatibility checks live in focused tests/corpus gates; production should not
  grow a permanent dual-resolver diagnostics stream.
- **Real-spec conversion gate**: after converting a downstream selector from a
  name pin to a structural or relational selector, generated output stays
  byte-identical and the converted selector resolves to the same binding the
  name pin did. Cross-ref-only selectors prove through solver categoricity and
  byte-identical output.
- **Abort bar**: if a selector kind or relation will not admit one general
  faithful encoding, stop and write the dead-end analysis. Do not add a
  special-case resolver, silent fallback, or under-constrained lowering.

## Gaffer evidence queue

The 2026-06-22 `tana/re/web` stabilization dispatch on Gaffer
`78d928dca7` reduced selector debt from `name_only_total` 940 to 905
(`name_only_fragile` 939 to 904). The largest remaining buckets were
`app/bootstrap` 73, `domains/graph` 39, `features/nodes` 32, and
`domains/ai` 17. Workers stopped where the current language required
neighbor-borrowed context, long exact bodies, positional multi-declarator pins,
or relation shapes visible in facts but awkward in the selector surface.

The 2026-06-23 current-spec census found 5,583 `source_match` blocks across
1,751 YAML files in the available `tana/re/web` spec, all using
`identifiers: alpha_all`. The largest native-lowering blockers are hole forms:
`ANYTHING`, `STMT_LIST`, `DECLARATORS`, `OBJECT_PROPS`, regex literal
predicates, class-rest, and args/array-element runs. It found no uses of
`target_statement`, `target_statements`, or authored
`wildcard_string_literals`, so those are not P0 solver work.

Use these as language/synthesis requirements for G5, not as independent
resolver architecture:

- **Inverse use-site selectors**: target-as-call-argument, setter/callback
  assignment, owner that reads a stable member/key, and owner that calls or
  registers `@Anchor` with a stable property/member. Needed for use-site-only
  constants and registry/setter slots.
- **State slot / setter / getter families**: claim a state cell by its relation
  to its setter/getter/cache/singleton family without pinning neighboring
  declarations positionally.
- **Multi-declarator slot selectors**: identify one target inside mixed
  `let` / `const` runs by stable family evidence plus a named slot relation, not
  "the second variable in this declaration."
- **Registry and roster selectors**: identify values by stable membership in an
  object/array table, call, key, or adapter roster.

## Cleanup Dispatch Queue

Do not preserve old matcher conveniences merely because they exist today. The
remaining safe early cleanup queue starts with tooling-only conveniences:

- narrow source-aware `selector-debt` near-match options;
- replace generic `NoMatch` fallback text, empty `nearest_candidates`, fact
  near-miss scoring, `match-selector` slack relaxation, and selector-IR
  row/stat stderr diagnostics with solver-native explanations.

Defer removal of `identifiers: alpha_all`, exact anonymous `match`, readability
labels / typed holes, and top-level anonymous `STMT_LIST` until a gaffer-private
migration or the single-resolution backend covers the retained form.

Further synthesis/planning work:

- **Large-prefix synthesis performance**: make prefix-wide inventory and
  candidate discovery practical for `app/bootstrap` scale, ideally under 10s on
  warmed inputs or with an explicit resumable offline mode.
- **Neighbor-borrow diagnostics**: tag candidate provenance as own
  literal/member, relation anchor, sibling family anchor, or neighbor borrow;
  make broad apply refuse neighbor-borrow candidates unless forced.
- **Dispatchable worklists**: emit lane-ready groups with item names, likely
  selector family, candidate command, and blocker class (`landable`,
  `needs-relation-feature`, `honest-debt`, `too-expensive`).

## Open questions

- **Derived predicates to ship first** — `calls` and `alias` (both over `resolves_to`)
  cover the pass's cases; a decorator-application pattern and `imports`/`exports` are
  the obvious next. `resolves_to` itself is the only genuinely new EDB relation.
- **Scoping** (resolved — see Implementation): named spec symbols are global logic
  variables (shared by name ⇒ same source binding, which _is_ the cross-reference);
  pattern-internal holes are clause-local, standardized apart. Open sub-question: how
  the surface syntax distinguishes "this `@Name` is the global entity" from a
  clause-local hole that happens to share a spelling.
- **Cross-module references** — a target in module A anchored on a binding owned by
  module B is fine: it is the same `G`. Worth confirming the owner-graph/module
  assignment stays consistent when a selector reaches across a module boundary.
- **Keep whole-window uniqueness as an option?** Target-categoricity is strictly more
  expressive; whole-window-unique is the special case of over-constraining. Probably
  relax everywhere, but some existing selectors may rely on the stricter reading.
