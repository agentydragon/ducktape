# Plan: selectors as one global constraint problem (relational spec model)

**Current state (2026-06-22, after #2439).** Ascent remains the selected
production solver. PR #2439 landed on `devel` as `db25ef639` and closed the
first global-solver bridge: `source_match` selectors now enter the global
selector IR/solver instead of bypassing it. That bridge still feeds the solver
`SourceMatchCandidate` rows enumerated by the fact-based `ChunkResolver`;
`selector_ir_lowering.rs` lowers `source_match` to that candidate atom, and
`plan_builder.rs` populates the rows by calling `ChunkResolver` candidate APIs.

Full AST facts already exist in `chunk_facts.rs` and are appended by
`SelectorFactStore::extend_chunk_facts`, but they are not yet the native solve
path for `source_match`. The remaining P0 is to make Ascent consume those AST
facts directly, lower `source_match` onto them with zero-differential parity,
and then delete the candidate oracle.

**Reframing.** The next architectural goal is not to grow more bridge
vocabulary. It is to make the global constraint solve native over AST +
owner/reference facts: compile every selector form into one selector IR, solve
shared `@Name` variables and `all_different` together, and hand materialization
a resolved claim map. Real spec conversions are dogfood and evidence for
missing language/fact coverage; they should not add more long-lived
anchor-first bridge architecture. The remaining-work index is
<../debug/2026_06_19_p4_debt_worklist.md>, and the Gaffer-derived language
evidence is summarized below.

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

Replace the remaining `SourceMatchCandidate` oracle with a Datalog/Ascent
selector resolver that resolves every selector the `tana/re` spec depends on
over an explicit relational model of the program (owner graph + AST facts).
Parity must be **proven, not asserted**: a corpus-wide differential against the
current `ChunkResolver` reaches zero disagreements, and the lowering is
**fail-closed** — each construct compiles to atoms provably faithful to current
`source_match` semantics, or it errors, never silently under-constraining. The
design must stay **principled** — one general, faithful encoding per selector
kind and hole, with no per-selector special cases, silent fallbacks, or hacks to
force a hard construct through. If any construct, or the model itself, turns out
not to admit such a faithful, principled encoding, we **abort and rethink the
design** rather than push on with ugly hacks — an honest dead end beats a matcher
we cannot trust.

Operationally, this means the Ducktape-side work leads with the engine contract:

1. one engine-facing selector IR shared by `run`, `validate`,
   `match-selector`, `selector-debt`, synthesis, and repair;
2. one fact store per chunk/component containing AST-shape facts,
   binding-resolution facts, owner/reference facts, and derived-predicate inputs,
   with the solver consuming the AST facts natively rather than candidate rows;
3. one solve per connected selector component, with `@Name` as a shared logic
   variable rather than a lookup into already-resolved members;
4. one diagnostic/result projection for no-match, ambiguity, duplicate claims,
   unsupported constructs, and "which facts forced uniqueness";
5. one resolved claim map consumed by the existing atomic-DAG / realizability /
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
  solve** — a single CSP instance, not selector-by-selector resolution. (Evaluating a
  conjunctive query _is_ finding homomorphisms into the structure; a CSP solver and a
  pattern-matcher are the same thing here.)
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
CSP/homomorphism problem, but its natural off-the-shelf engine is **Datalog**, not a
numeric finite-domain solver: the structure is a large fixed EDB (~1–2M AST facts)
joined by selective equality atoms (database territory), and a Datalog engine takes
exactly `(facts, rules, query) → answers`. Using one avoids reimplementing a solver and
**forces a clean serialization boundary** (program → facts, spec → rules).

**EDB — facts extracted once per chunk.** The AST plus the one semantic edge:

```
child(parent, idx, node).   kind(node, Kind).   prop_name(node, "foo").
str_lit(node, "…").   num_lit(node, 5).   operator(node, "+").
resolves_to(use, def).   member_of_module(binding, module).
```

The AST projection already exists in `chunk_facts.rs` and
`SelectorFactStore::extend_chunk_facts`; the owner/reference facts already feed
the selector solver. The open work is to make the Ascent rule library consume
the AST rows directly instead of only consuming `SourceMatchCandidate` rows
pre-enumerated by `ChunkResolver`. `resolves_to` remains the genuinely semantic
edge — the one minification preserves while names churn.

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
_mentions_: `CLASS_REST`, `ANYTHING`, `STMT_LIST` are just unmentioned structure.
(Ordered/positional holes — "`open` before `close`", "a run of statements" — need
recursion or sibling-order atoms, and are T-variant anyway.)

**Variable scoping — the resolved form of "don't apply/rename independently":**

- _Global symbols_ = the named spec entities (each target + shared anchor). A symbol
  used in two selectors is the **same** logic variable ⇒ same source binding. That is
  the cross-reference; `@Name` is just reusing a name — one global renaming for named
  things, no post-hoc node-identity join.
- _Clause-local holes_ = pattern-internal throwaways (a param, an `EXPR`), standardized
  apart per selector so unrelated functions' `n`s do not collide.

**Solve / read-back.** Evaluate once. Per target symbol: exactly one binding → pinned;
zero → `no-match`; ≥2 → `ambiguous` (categoricity is just _counting_ answer bindings, no
full-solution enumeration). `all_different(targets)` is a rule
`dup(A, B) :- claims(A, N), claims(B, N), A < B.` → `duplicate-claim`. Translate-out =
answer tuple per distinguished variable → claim node → module.

**Solver choice.** Use **Ascent** as the first production solver. It is already
in the Rust dependency set, `selector_solve.rs` uses it for the owner-graph
relational kernel, and the synthetic query examples already prove the needed
feature shape: cross-ref joins, disequality / `all_different`, stratified
negation, recursion, count aggregation, and mixed AST-label + graph-edge joins.
The selector IR should be represented as EDB facts consumed by a fixed Ascent
rule library, not by generating Rust or raw Datalog per selector. That keeps the
Bazel/Rust integration in-process and makes the current `selector_solve` tests a
real migration bridge.

Do **not** start with Soufflé, Crepe, or DDlog. Soufflé is the fallback if
Ascent cannot hit the latency/memory target after profiling, but it would add an
out-of-process toolchain and serialization boundary now. Crepe does not buy us
anything over the already-used Ascent path. Differential Dataflow / DDlog is a
future incremental-evaluation option for interactive `stabilize` loops, not the
initial batch resolver.

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

The problem **decomposes by connected components**: a selector that shares no symbol
with any other is an independent subquery whose rare-literal anchors prune it to
near-singleton — so the common single-match case is as cheap as today's matcher, and
only the small cross-reference clusters need a genuine joint solve. With the Datalog
back-end this falls out of indexing + selectivity-ordered semi-naive evaluation rather
than hand-rolled propagation.

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
each spec selector to its node on the chunk. After #2439, the global selector
solver is on that path, but `source_match` still reaches it through
`ChunkResolver`-enumerated candidate rows. Native AST EDB lowering makes the
solver itself responsible for the shape match.

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
- label posting lists still used by `ChunkResolver` as the current
  `SourceMatchCandidate` oracle.

The cutover is a consumer change: add native AST relations and shape rules to
the Ascent solver, then delete the candidate-oracle projection once the
differential is zero.

### It replaces several workflow parts, not just the matcher

| Today                                                                                             | Becomes                                                                                                                                  |
| ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `lowering/materialize/plan_builder.rs` still asks `ChunkResolver` for `SourceMatchCandidate` rows | one native `solve(spec, edb)` over AST + owner/reference facts; the plan consumes per-target bindings                                    |
| `validate` no-match / ambiguous / duplicate-claim (per-selector + post-hoc)                       | solver categoricity (no-match / ambiguous) + `all_different` (duplicate-claim) in one keep-going pass                                    |
| `match_selector.rs` resolves one candidate                                                        | single-query solve over the EDB; gains probing of **relational/cross-ref** candidates, not just local shapes                             |
| `selector_codemod.rs` prove-gate (candidate-index uniqueness)                                     | solver categoricity; the minimizer's search space grows to relational atoms — it can now _propose_ the cross-ref selectors that now skip |
| `selector_debt.rs` source-aware near-ambiguous / repeated-body scan                               | solver reports per target **which atoms forced uniqueness, tagged invariant/variant**, and the categoricity margin                       |

The cycle/atomic gate (`gate.rs`, `realizability/`, `atomic_units.rs`) and emit run
**downstream of resolution on the resolved ownership** — unchanged, as are the module
taxonomy and `describe`/`show-source`/`cluster` (they read the graph, not selectors).

### Spec-format evolution (what the current YAML can't express)

Two layers, kept distinct:

- **IR (engine-facing) — Datalog-native.** The compiled form the solver consumes is a
  normalized atom/rule set: per-target distinguished variables, the derived-predicate
  library (`calls`/`alias`/… as rules), `@Name` cross-refs as shared variables, and any
  negation. Non-negotiable — it is what the engine evaluates.
- **Authoring (human/agent-facing) — a structured 1:1 skin over that IR**, not a raw
  Datalog file. A _pure_ `.dl` spec isn't actually on the table: the dominant **shape**
  atom must stay JS-with-holes (hand-written AST atoms are the unreadable dump the rubric
  exists to avoid), so the format is always Datalog-for-relations + a JS-shape escape.

So the authoring vocabulary — today `selector.binding.name`, one contiguous
`source_match`, `binding_groups`, `anonymous_statements`, none of which can express a
cross-reference, disjoint relational constraints, a shared global symbol, or
negation/counting — grows from "a name pin or one match-string" to **a query = a
conjunction of atoms**:

- **shape atom** — today's JS-with-holes `source_match`, lowered to
  `child`/`kind`/`str_lit`/`prop_name` atoms. Holes mostly disappear: a query constrains
  only what it mentions, so `CLASS_REST`/`STMT_LIST`/`ANYTHING` go away (only
  ordered/positional holes remain, and those are T-variant).
- **relational atoms** — `calls`, `alias`, `reads_member`, or the raw `resolves_to`,
  tying the target to other nodes.
- **cross-reference** — `@Name` is the _same logic variable_ as that entity's target,
  shared across the whole spec (subsumes per-match `target_binding`); `$x` is a
  clause-local hole.
- **escape hatch** — a `where:` block of raw IR atoms sharing `$` variables, for the rare
  multi-atom join or negation the structured keys express clumsily. This is where the
  format goes rule-native exactly when YAML would fight the semantics, without paying that
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
  `STMT_LIST` / `ARGS` / `OBJECT_PROPS` / a single `CLASS_REST` / `CASE_REST` /
  `DECLARATORS` ("ignore the rest") — **vanish**: a CQ asserts only what it requires, so
  you simply do not mention those children/args/members.
- **equality** — a named `EXPR_x` repeated (same subtree in two places) → a **shared
  variable** used twice (a join). (List-hole name suffixes like `STMT_LIST_x` are labels,
  not equality, so they stay existential.)
- **predicate** — `STR_LITERAL_MATCHING_RE("re")` → a **filter atom**
  `str_matches(node, "re")` (a builtin), not existence.
- **ordered / positional** — multiple anchored runs implying a subsequence
  (`CLASS_REST; a(){} CLASS_REST; b(){}` ⇒ `a` before `b`), `DECLARATORS_BEFORE/AFTER`,
  the `target_statement` index → **sibling-order atoms**, which are **T-variant**.
  Permitted only via the `where:` escape hatch and flagged by `selector-debt` — a
  stabilization target, not a default.

So in the clean native form the surviving holes are exactly the existence qualifiers;
equality and regex become first-class CQ constructs, and order/position is
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

| construct                                                                                    | faithful encoding                                     | EDB fact needed                                        |
| -------------------------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------ |
| `binding: { name }`                                                                          | `name_owner` lookup                                   | owner graph (have it)                                  |
| structural shape (`class` / `function` / call …)                                             | positive `kind` / `child` / `prop_name` join          | `kind(node,k)`, `child(parent,idx,child)`, `prop_name` |
| anonymous `EXPR` / `STMT` / `ANYTHING`                                                       | unmentioned position (faithful don't-care)            | —                                                      |
| run-holes `STMT_LIST` / `ARGS` / `OBJECT_PROPS` / `CLASS_REST` / `CASE_REST` / `DECLARATORS` | absorb = don't-constrain; anchors keep relative order | `child` index column                                   |
| named `EXPR_x` (cross-occurrence equality)                                                   | one shared variable used twice (a join)               | per-node structural-canonical id                       |
| `STR_LITERAL_MATCHING_RE("re")`                                                              | filter atom `str_matches(node,"re")`                  | `str_lit(node,value)`                                  |
| `identifiers: exact`                                                                         | name filter                                           | `name(node,spelling)`                                  |
| `identifiers: alpha_all`                                                                     | identifier variable + scope                           | `resolves_to` (have it) + alpha-canonical ids          |
| `target_binding` / `target_statement(s)`                                                     | choice of distinguished variable                      | —                                                      |
| `binding_groups` (adopt_names / exports)                                                     | mechanical per-target expansion (already done)        | —                                                      |
| ordered / positional anchors                                                                 | child-index compare (`i < j`; adjacency `j == i + 1`) | `child` index column                                   |

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

**The two are not independent, and the dividing line is the _coupling_, not the hole count.** The
run-hole routine does two things at once — (A)
variable-length ordered-subsequence **placement** of the fixed segments around the holes, and (B)
a **global bijective alpha-binding** constraint threaded through that placement (which is _why_ it
backtracks — `place_segments` snapshots/restores `MatcherState{replacements, alpha_scopes}`). They
encode very differently:

- **(A) placement is cleanly Datalog-expressible — backtracking and all.** A list-with-holes is
  `[hole?, seg₁, hole, …, segₖ, hole?]`; "`segᵢ` matches starting at ordinal `p`" is the
  node-homomorphism over its elements vs `subject[p..]`, and a full match is a **chain**
  `seg_at(1,p₁), seg_at(2,p₂), …` with `end(i) < start(i+1)` per gap-allowing hole plus the
  anchoring equalities. Datalog evaluates the whole feasible-placement set at once, so the
  imperative greedy+snapshot/restore search **disappears** — it was one operational strategy for a
  relation Datalog computes set-at-a-time. **≥2 holes per list (interior segments with a free
  start) are fine, and the corpus uses them** — `infra/sentryInit.initSentry` is
  `[STMT_LIST, const s = …, STMT_LIST]`, `infra/android.serializeNodeForNativeBridge` is
  `{OBJECT_PROPS, isTag: ANYTHING, OBJECT_PROPS}`. So the earlier "≤1 hole per list" intuition is
  **empirically false**; the chain-join handles interior links regardless.
- **(B) the alpha bijection coupled with variable placement does _not_ map onto pure set-at-a-time
  Datalog.** Per _fixed_ placement the bijection is clean (stratified negation: reject
  `pair(n,s₁),pair(n,s₂),s₁≠s₂` or `pair(n₁,s),pair(n₂,s),n₁≠n₂`). But which pairs a placement
  induces depends on _where_ segments land, so a global per-placement check must carry the
  accumulated binding map along the chain — an unbounded value: either materialize combinatorially
  many placement-tagged binding sets, or model a "binding-map lattice" whose merge can _fail_
  (a failing merge is not a lattice join). Both leave the fragment Ascent evaluates efficiently.
  This is the sharpened form of the alpha-scoping risk above.

**So we narrow on the coupling axis, not the hole-count axis.** In every corpus run-hole pattern
the fixed landmarks between holes are structurally distinctive (`const s = ANYTHING && !0, …`, the
prop name `isTag`, `return …`) and the alpha identifiers they carry are **fresh local
declarations** or **anonymous `ANYTHING`** that never bind — no identifier's binding is decided by
a variable-gap placement, so the bijection decomposes per-segment and stays the rung-2 conjunction.
The principled encoding therefore lowers **placement** fully and keeps "an alpha-bound identifier
whose binding would be decided by a variable-gap placement" **fail-closed** (loud `Unsupported`).
The corpus-wide differential is what _proves_ the corpus never needs the forbidden coupling; if it
ever flags a real selector that does, that is the go-back-to-the-drawing-board signal — pre-resolve
the binding with scope facts independent of placement, or narrow the _selector_ language upstream so
the pattern cannot be authored.

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
`selector_query_examples.rs` (`selector_query_examples_test`): cross-ref joins, a disequality
`all_different`, stratified negation (`!rel`), recursive transitive closure, `count` aggregation
with a uniqueness guard, and an AST-literal-∧-import-edge join all resolve to the expected owners.
So the vocabulary is proven expressible in the engine we picked — the gap to
production is native consumption of the AST-facts EDB plus template→atoms
lowering, not the solver's capability.

## Closed by #2439

- `source_match` selectors now enter the global selector IR/solver.
- The materializer has a single Ascent-backed selector solve path for binding
  pins, staged relational selectors, and `source_match` claims.
- Ascent remains the production solver choice; do not restart solver selection
  unless profiling proves the in-process path cannot meet the latency/memory
  target.

## Remaining work (native AST EDB cutover)

This is the detailed P0 queue; <../TODO.md> should only summarize it.

- **G1 — native AST EDB ingestion.** Add Ascent relations and indexes for the
  AST rows already emitted by `chunk_facts.rs` and imported by
  `SelectorFactStore::extend_chunk_facts`: node kind, parent/child ordinal,
  literals, identifier/property names, operators, regex, superclass, top-level
  statement ordinal, and source-span/diagnostic payloads. Preserve the
  fail-closed rule: unsupported constructs report `unsupported`, not a partial
  fact set.
- **G2 — `source_match` lowering parity.** Lower today's JS-with-holes selectors
  and binding groups into native AST/owner IR atoms. Keep `ChunkResolver` as the
  reference until the corpus differential is zero, including alpha matching,
  run-hole placement, regex literal predicates, anonymous statements, exact
  identifiers, target bindings, and binding groups.
- **G3 — candidate-oracle deletion.** Remove `SelectorAtom::SourceMatchCandidate`
  / `SelectorFact::SourceMatchCandidate` from the production path after G2
  parity. No-match, ambiguous, unsupported, and duplicate-claim diagnostics
  should be projections of the native solver result, not `ChunkResolver`
  side tables.
- **G4 — relation/language fold-in.** Re-express `cross_ref`, `reads_member`,
  `member_of_module`, `passed_to_call`, `makes_decorate_call`,
  `intrinsic_alias`, and the Gaffer feature-request vocabulary as IR atoms or
  derived predicates over owner/reference + AST facts. The real-spec conversion
  worklist in <../debug/2026_06_19_p4_debt_worklist.md> is evidence and dogfood
  for this, not a reason to add more permanent bridge passes.

## Execution contract

- **Build/test gate**: build the changed debundler library and its consumers;
  run the changed area's tests with `--cache_test_results=no` and lint on for a
  final step.
- **Resolver parity gate**: while both resolver paths exist, the global solve
  and the current fact-based/staged resolver agree on every covered resolved
  target, no-match, ambiguous match, duplicate claim, and unsupported construct.
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
