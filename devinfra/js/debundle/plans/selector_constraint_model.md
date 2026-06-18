# Plan: selectors as one global constraint problem (relational spec model)

Status: **design note / proposal.** Extends
[the selector-authoring-agent plan](selector_authoring_agent.md) — which fixed
_who_ chooses anchors (an agent, not the cost model) — with a proposal for _what a
selector is_. Motivated by the tana/re stabilization pass
([stabilize dogfood note](../debug/2026_06_18_stabilize_skill_dogfood.md), findings
F11–F13): real bindings whose only stable identity lives in a **non-adjacent or
cross-module** node, which the current local-match model cannot express, and which
the minimizer therefore either skips or pins by borrowing an adjacent neighbor.

Notation: `@Name` means "the binding/node pinned elsewhere in the spec as `Name`"
— a cross-reference to another selector's target.

## Goal

Replace the hand-rolled JS↔JS template matcher with a Datalog-based selector resolver that
resolves every selector the `tana/re` spec depends on, over an explicit relational model of the
program (owner graph + AST facts) rather than ad-hoc AST walking. Parity must be **proven, not
asserted**: a corpus-wide differential against the current matcher reaches zero disagreements, and
the lowering is **fail-closed** — each construct compiles to atoms provably faithful to the
matcher's semantics, or it errors, never silently under-constraining. The design must stay
**principled** — one general, faithful encoding per selector kind and hole, with no per-selector
special cases, silent fallbacks, or hacks to force a hard construct through. If any construct, or
the model itself, turns out not to admit such a faithful, principled encoding, we **abort and
rethink the design** rather than push on with ugly hacks — an honest dead end beats a matcher we
cannot trust.

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
  (size / locality / `selective × stable × cost`, see
  [read-off minimization](readoff_minimization.md)); the real objective is
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

Most of this already exists as the owner/reference graph; the work is to _expose_ it as
queryable relations. `resolves_to` is the only genuinely new, genuinely semantic
relation — the one minification preserves while names churn.

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

**Engine.** In-process Rust Datalog — **Ascent** or **Crepe** (rules compile to Rust);
**Soufflé** for the fastest mature out-of-process engine; **Differential Dataflow /
DDlog** if we later want **incremental** re-evaluation (attractive for the interactive
`stabilize` loop: edit one selector, re-check instantly).

**Caveats.** Pure positive Datalog needs care for closed-world / counting anchors ("the
class with _exactly_ these members"; "the _only_ class with method m" as a writable
anchor) — stratified negation / aggregation; categoricity as a _check_ is plain
counting. Variable-length holes need recursion. Most cases — literal + `resolves_to`
anchors — are plain positive CQ. New code is the AST→atoms lowering (replacing the
matcher's _search_; we keep the lowering) plus the EDB extractor.

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

## Incremental path

1. **Named cross-reference anchors** (MVP): let an anchor be `@Name` joined through
   `resolves_to` (directly, or via the derived `calls` / `alias` patterns). A bounded
   extension of today's matcher — it already binds `target_binding` within a window;
   now an anchor can be a name resolved elsewhere. This alone turns metaNode's 18 debt
   into ~5 and removes the neighbor-borrow temptation.
2. **Component-decomposed global solve**: replace per-selector resolution with one CSP
   that decomposes to the current fast path for independent selectors and small joint
   solves for cross-ref clusters. `duplicate-claim` becomes an all-different
   constraint.
3. **Full relational templates**: arbitrary conjunctive queries over the invariant
   signature; whole-spec homomorphism as the limit.

## Bootstrapping prototype

Goal: a prototype that re-identifies the symbols the **current** Tana spec identifies,
end to end through the new engine — then ratchet selector quality up without touching
the engine.

1. **Name-pin bootstrap.** Add a `minified_name(binding, "EBt")` EDB relation and lower
   every current spec entry to the trivial rule
   `target_X(B) :- minified_name(B, "<current minified name>")`. This reproduces today's
   name-pin resolution exactly, through the Datalog path — exercising the EDB extractor,
   lowering, solve, and read-back against real Tana before any structural matching
   exists.
2. **Equivalence gate.** Assert the prototype's per-target bindings equal the current
   debundler's resolution for the whole chunk (a diff harness). That is the regression
   oracle for everything after.
3. **Swap name pins for structural / relational queries**, selector by selector, keeping
   the gate green: first local AST-shape (`str_lit` identity anchors), then `@Name` +
   `resolves_to` / `calls` / `alias` for the cross-ref cases (the metaNode debt).
   `minified_name` atoms stay as the honest-debt fallback for the genuinely anchorless.
4. **Drop `minified_name`** once no selector references it — at which point the spec is
   fully expressed in the T-invariant signature and the two-bundle-version
   forward-compat scorecard becomes meaningful.

First cut: a standalone PoC (its own crate under `devinfra/js/debundle/x/`) that reads
the chunk + the existing `owner_graph.json`, emits EDB facts, runs a handful of rules
(name-pin for most, the three metaNode cross-ref cases as structural), and prints
per-target bindings + categoricity — enough to validate the model on real Tana without
touching the production pipeline.

### Feasibility (probed 2026-06-18, chunk `78d928dca7`)

`owner_graph.json` _is_ the phase-1 EDB — no JS parser needed for name-pin + reference
cross-refs. Nodes are **owners** (top-level statements) carrying `declared_bindings`
(`{binding, export_name}` — minified + readable), `source_location`, `statement_kind`,
`purity`, and `destination` (module); edges are owner→owner carrying
`{binding, edge_kind, constrains_init_order}` with `edge_kind` ∈ `{lazy_use, eager_use,
sequenced, local_effect, lazy_rebind, deferred_rebind, eager_rebind}`. That is
`resolves_to` at owner/binding granularity, plus module membership and init-order.

A hand-rolled-Datalog probe over it confirmed the model on real data:

- **name-pin is categorical**: all **9069** chunk bindings resolve to exactly one
  declaring owner (0 ambiguous); all 23 metaNode pins unique. The bootstrap reproduces
  current resolution with no parser.
- **cross-refs resolve uniquely from the reference graph alone**: `UBt`
  (`isMeetingTranscriptionProvider`) is pinned by "the owner whose use-set is `{@EBt}`"
  (exactly one owner references `EBt`); `HI` by "the `var_decl` aliasing `@UJ`" — both
  unique, without the minified name.

**Phase-2 boundary**: AST shape and literals are _not_ in `owner_graph.json` (owners
expose only line spans + statement kind), so local-identity anchors (`str_lit` message
strings, method-name fingerprints) and call-arg/decorator precision need parsing the
chunk (SWC) and joining AST nodes to owners by `source_location` / `statement_ordinal`.
Until then the engine resolves at per-owner (statement) granularity, which already
covers name-pin + reference cross-refs.

An **in-process Datalog spike** (Ascent 0.8, ~60 lines) reran the same over the live
owner_graph and passed — `declares=9069 uses=25886`, name-pin categorical for all 9069
bindings, and the `ubt`/`hi` rules resolve to the right owners (8763 / 1225) without the
minified name — confirming the model through a real engine with the
`facts → rules → answers` boundary. The whole evaluator is:

```
name_owner(b, o) <-- declares(o, b).            // bootstrap
refs(o, b)       <-- uses(o, b, _).
ubt(o)           <-- refs(o, "EBt").             // calls @EBt
hi(o)            <-- stmt_kind(o, "var_decl"), uses(o, "UJ", "eager_use").  // alias @UJ
```

Productionizing it: add `ascent` to the root `Cargo.toml` + a crate_universe repin
(`CARGO_BAZEL_REPIN=1`), then a `rust_binary` under `x/` (the repo is one cargo package

- crate_universe — no standalone-nested-cargo precedent, so the spike itself lived
  outside the tree until then).

## Landing in the debundle binary

This is not a side tool — eventually it replaces the **resolution layer**: the code
that maps each spec selector to its node on the chunk. Today that is the per-selector
matcher in `source_match/` (`binding_resolution.rs`, `matcher.rs`, `target_matching.rs`)
seeded by the candidate/shape index (`selector_candidate_index.rs`, `shape_index.rs`).
The solver replaces the matcher behind every caller.

### The resolver seam (landed)

The swap point is named in code: `source_match::SelectorResolver`, the trait both matchers
implement — `(parsed chunk, JS-template selector) → unique {claimed binding | body-index
group}`, with no-match / ambiguous as the only failure modes. `AstWildcardResolver` is today's
hand-rolled matcher behind that trait (a thin delegation to the `binding_resolution` free
functions); the Datalog matcher becomes the second impl.

`DifferentialResolver { primary, shadow, sink }` is itself a `SelectorResolver`: it runs both,
returns the **primary's** result unchanged, and reports every divergence to a `DisagreementSink`.
The shadow never affects the answer, so it is safe to switch on in production — the parallel run
is observation, not behavior. Agreement is "both resolve to the same claim, or both reject"
(rejection text may differ; only the verdict counts) — the coarse output (the claim, never the
internal hole bindings) is what makes the comparison clean. This is the mechanism the **Shadow**
step below rides; **Flip** is swapping `primary`/`shadow`, and retiring the old matcher is
dropping the `AstWildcardResolver` arm. The phase-1 `selector_solve_shadow_test` already
demonstrates the agreement check at name-pin granularity, against `peel::resolve_binding_owners`.

### EDB from analysis the pipeline already does

The pipeline already parses the chunk (`js_ast`), computes binding resolution and the
owner/reference graph (`program_analysis`, `facts/`, `graph/`, emitted as
`owner_graph.json`), and builds the candidate index. Those _are_ the EDB: `resolves_to`

- module membership + ordinal/purity from the owner graph (proven), AST
  `child`/`kind`/`str_lit`/`prop_name` from the parsed tree (phase 2), label posting lists
  from the candidate index. EDB construction is a re-projection of existing analysis,
  slotted after analysis and before materialization.

### It replaces several workflow parts, not just the matcher

| Today                                                                       | Becomes                                                                                                                                  |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `lowering/materialize/plan_builder.rs` resolves each selector individually  | one `solve(spec, edb)`; the plan consumes per-target bindings                                                                            |
| `validate` no-match / ambiguous / duplicate-claim (per-selector + post-hoc) | solver categoricity (no-match / ambiguous) + `all_different` (duplicate-claim) in one keep-going pass                                    |
| `match_selector.rs` resolves one candidate                                  | single-query solve over the EDB; gains probing of **relational/cross-ref** candidates, not just local shapes                             |
| `selector_codemod.rs` prove-gate (candidate-index uniqueness)               | solver categoricity; the minimizer's search space grows to relational atoms — it can now _propose_ the cross-ref selectors that now skip |
| `selector_debt.rs` source-aware near-ambiguous / repeated-body scan         | solver reports per target **which atoms forced uniqueness, tagged invariant/variant**, and the categoricity margin                       |

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
atoms _provably equivalent_ to the matcher's semantics, or the lowering raises
`Unsupported(construct)` and the resolver **errors** — it never emits a query that quietly omits a
part it didn't model (which would under-constrain and silently match the wrong owner). The
existential "vanish" above is _not_ such an omission: the matcher's `ANYTHING`/run-holes mean
"don't care", and a CQ that doesn't mention those positions means exactly that — dropping a
don't-care is faithful; dropping a _constraining_ part is the forbidden thing that triggers the
error. The only sanctioned fallback is an **explicit, opt-in shape atom** that faithfully delegates
a whole sub-pattern to the existing matcher (`AstWildcardResolver`) — visible in the spec, never an
automatic swallow. So "unsupported" is always either a hard error or a conscious, recorded
delegation; the unsupported set is counted and shrinking toward empty (the precondition for
deleting the matcher). (Differential refinement for P2–P3: tag an `Unsupported` rejection
distinctly from a no-match, so a Datalog `Unsupported` is never masked when the matcher also
rejects — `DifferentialResolver`'s `(reject, reject) ⇒ agree` rule holds only when both are
genuine no-matches.)

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

**Finding (2026-06-18, probed against the matcher's `match_list_with_holes`/`place_segments` and
the gaffer `tana/re` corpus): the two are not independent, and the dividing line is the
_coupling_, not the hole count.** The matcher's run-hole routine does two things at once — (A)
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

### Extracting the facts faithfully (P1 substrate)

The facts above must be a faithful projection of the chunk, and fail-closed forbids silent gaps —
which rules out the two convenient substrates. A `swc_ecma_visit::Visit` has default no-op
methods, so a node type nobody overrode is **silently skipped** — the exact failure we reject. A
generic walk over a serialized AST is not faithful-by-construction either: it reflects struct
field layout, not source order, so it does not hand us the sibling ordering the run-hole/adjacency
encoding needs. So the extractor is a plain recursive walk whose only catch-all is a loud
`Unsupported { context }` error: a construct it has not modeled **crashes**, never projects to a
silently-incomplete fact set that would match wrongly. The matcher (`AstWildcardMatcher`,
`matcher.rs`) is the fidelity source of truth — it already encodes, per node type, which children
matter and in what order — so the extractor mirrors those structural decisions and the corpus
differential is what proves it did. Coverage grows construct-by-construct until the corpus
extracts with zero `Unsupported`; until then each gap is loud and counted, not approximated. The
top-level join to the owner graph is `js_ast::statement_ordinal_for_body_index` (owner = top-level
statement by ordinal).

### Landed (P1) and the P2/P3 matching design

**Landed (P1):** `chunk_facts` projects `node_kind` / `child(parent, ordinal, child)` / `str_lit` /
`num_lit` / `bool_lit` / `ident_name` / `prop_name` / `operator` / `regex` / `super_class` + the
`top_level` owner-ordinal join, and `chunk_facts_coverage` (the instrument that drove growth)
reports **100%** top-level extraction on every measured chunk (index, ReactGraph, Calendar,
VoiceChatModal). Fail-closed throughout: an unmodeled construct errors, never silently drops.

**P2/P3 — matching over the facts.** The selector needle is parsed and projected through the
**same `chunk_facts` extractor**, giving needle facts in the identical schema. A match is then a
**homomorphism** from needle nodes onto chunk nodes, anchored so the needle's top-level statement
maps to a chunk owner and the claimed distinguished variable's image is the resolved owner. The
homomorphism respects, per node: `node_kind` equality, label equality (`str_lit` / `prop_name` /
`operator` / `regex` / …), and the positional `child` structure — with hole rules layered on
exactly as the holes section prescribes (existential → the needle omits that child; alpha
identifier → a logic variable with **intra-pattern consistency**, the same needle identifier
mapping to one chunk identifier — a join, not a free wildcard; `STR_LITERAL_MATCHING_RE` → a
`str_matches` filter; ordered runs → child-index `<` constraints). This is the conjunctive query
the model describes, now concrete over `ChunkFacts`; whether evaluated as Ascent rules or a direct
homomorphism search is an implementation choice, but it operates over the EDB, not by re-walking
ASTs. **This per-`(selector, candidate)` homomorphism is the kernel match relation, not a rival
"N separate solves" architecture.** The one global evaluation is still the target (P4): for today's
cross-ref-free corpus the global solve **decomposes by connected components** into exactly these
independent matches (two selectors are connected only when they share a variable — an `@Name`
cross-reference — which the current YAML cannot even express), so per-selector and one-global give
identical answers, and the single fixpoint earns its keep only once selectors share variables
(`@Name`, `all_different`, counting). The order is forced by the parity gate: "zero disagreements
vs. the matcher" is inherently per-selector (the matcher is), so the kernel is proven per-pair
first, then composed into one fixpoint that adds the cross-selector joins. Fail-closed governs it:
a hole whose faithful rule is not yet implemented makes the lowering
**error**, never emit a weaker query — the alpha-consistency join in particular must not degrade
to a free wildcard (that would under-constrain and mis-match). `DatalogResolver` wraps this behind
`source_match::SelectorResolver`; `DifferentialResolver<AstWildcard, Datalog>` runs it beside the
matcher over the corpus selectors and **proves** parity (zero disagreements) rather than asserting
it.

**Landed (P2 rung 1):** `selector_match::matches` is the homomorphism over `ChunkFacts`, and
`selector_match_differential_test` proves it agrees with the production matcher
(`source_match::needle_matches`, exposing `PreparedNeedle::matches`) on the faithful subset —
exact-identifier structure with expression-position single-node holes — and is fail-closed on
run/list holes and the regex predicate (rejected up front, so an arity check cannot mask them as a
wrong `false`). The differential already earned its keep, catching that arity short-circuit in the
first cut.

**Design finding (the next rung's gate):** faithful **alpha-equivalence** needs _within-statement_
scope/binding information — which needle identifier occurrences are the same binding, with
shadowing — and the P1 EDB does not carry it (`resolves_to` is owner-level, across top-level
statements, not inside one). So rung 1 does exact-identifier matching and **fail-closes** on alpha
rather than approximating it with a free wildcard (which would under-constrain). The next rung adds
a per-occurrence **scope/alpha-canonical fact** to `chunk_facts` (a de Bruijn-style binding index
within the statement); `alpha_canonicalize.rs` seeds the canonicalization. This is an additive EDB
extension, not a drawing-board reset — but it is the fidelity-critical piece, so it is built
behind the differential, construct by construct, exactly as P1 coverage was.

**Landed (P2 rung 3 — run/list holes):** `selector_match::match_list_with_holes` / `place_segments`
match a run-hole-bearing list (`STMT_LIST` / `ARGS` / `OBJECT_PROPS` / `CLASS_REST` / `CASE_REST` /
`DECLARATORS`, plus `ANYTHING` in its run-hole positions) as an ordered subsequence with gaps,
mirroring the production matcher's same-named routines over facts (carrier detection per parent kind
via `is_run_hole_carrier`, projecting `source_match/holes.rs`; greedy-leftmost placement with
`Bindings` snapshot/restore). Fail-closed is now an **up-front needle scan**
(`unsupported_needle_construct`) that rejects the `STR_LITERAL_MATCHING_RE` predicate and any
run-hole keyword not consumed as a carrier (a misplaced hole) _before_ structural matching, so a
kind/arity short-circuit can never mask an unhandled construct (the regex predicate caught exactly
that — a `Call`-vs-`StrLit` mismatch returned a wrong `false`). `selector_match_differential_test`
proves agreement with the production matcher on 23 cases incl. anchored/interior segments, the
two-hole `{…, k, …}` corpus shape, and alpha+run-hole; fail-closed on the regex predicate and a
misplaced run hole. _Per the finding above, this kernel realizes the placement as a direct
greedy+backtracking search; the relational chain-join (cross-gap alpha-binding coupling fail-closed)
is the P3/P4 native-lowering form._

**Landed (P2 rung 4 — parse-position-polymorphic single-node holes):** `is_single_node_hole` now
fires by position — an expression `Ident` (`EXPR`/`ANYTHING`), a binding `BindingIdent` pattern
(`ANYTHING`, mirroring `is_anything_pat_hole`; its wildcard-idents registration gate is satisfied by
construction), or an expression statement `ExprStmt` (`STMT`/`ANYTHING`, matching **any** statement
kind, checked before the kind comparison). This fixes a real corpus disagreement:
`infra/android.serializeNodesForNativeBridge(ANYTHING, ANYTHING)` — two `ANYTHING` params that the
earlier expression-only rule would have forced into one alpha binding and wrongly rejected. The
differential now spans 28 cases. _Anonymous match-any only; cross-occurrence equality of **named**
single-node holes (`EXPR_x`) is the deferred equality-hole rung._

**Landed (the corpus-wide differential harness — the gate):** `corpus_match_differential` (a
`rust_binary`, run locally since ducktape CI cannot read the private gaffer corpus) loads every
`source_match` selector from a spec `modules/` tree (via `spec_modules`) and, for each top-level
statement of one or more real chunks, compares `selector_match::matches` against
`source_match::needle_matches`. First run over the `78d928dca7` spec (4327 selectors) × 120
ReactGraph statements = **505,080 pairs** surfaced exactly **one** disagreement — and it was a real
fidelity bug, not a matcher bug: `chunk_facts` dropped the `var`/`let`/`const` declaration keyword,
so a `let` selector matched a `const` of the same shape. Recording the keyword as an operator-class
label drove the differential to **0 disagreements** over those 505k pairs. **This is the gate
working as designed** — proven, not asserted, and it found a gap the 28 hand-picked cases did not.
Two rungs then drove the fail-closed set to **zero**: (1) extending run-hole carrier detection to
`SwitchCase` bodies (`STMT_LIST` inside a `case`/`default` clause — the case test falls out as an
anchored-left fixed segment under the same placement) closed the **14** "run-hole keyword outside a
list position" gaps; (2) lowering the **`STR_LITERAL_MATCHING_RE("re")` predicate** (a string-literal
subject whose value matches `re`, mirroring `holes.rs::string_literal_matches_regex`) cleared the
remaining **79**. Re-measured: **4302 of 4327 selectors compared, 0 fail-closed, 0 disagreements**
over 172,080 pairs — **every single-statement `source_match` selector in the corpus is matched, in
agreement with production**. Only 19 multi-statement and 6 unparseable-standalone selectors are
skipped; fail-closed now fires only on genuinely malformed input (a misplaced run hole, a malformed
predicate).

**Landed (resolver-level wiring — the end-to-end path):** `source_match::DatalogResolver` implements
the `SelectorResolver` trait using the fact matcher as the per-statement match oracle and reusing
the **same** production binding-extraction (`declared_bindings`, `selector_binding_location`) that
`AstWildcardResolver` uses — only the match decision is swapped, so resolver parity follows from the
proven matcher parity by construction. It resolves member selectors (returning the claimed binding
name) and anonymous-statement groups; `source_match_test` proves via
`DifferentialResolver<AstWildcard, Datalog>` that it returns the same claim as production (e.g.
`function f(){…}` → owner `alpha`) and is differential-silent. Fail-closed (an honest error, never a
wrong claim) on the paths it does not yet mirror: var-declarator / declarator-hole member targets
(production routes those through per-declarator alignment), binding groups, and multi-statement
needles.

**Landed (the resolver differential, run corpus-wide):** `corpus_match_differential` now also runs
the full `SelectorResolver` path both ways (`DatalogResolver` vs `AstWildcardResolver`) over the same
chunk and classifies each selector: resolved-parity (both claim the same owner), reject-parity (both
reject), fail-closed (datalog errors where production resolves — the worklist), over-resolved
(datalog resolves where production errors — must be 0), value-disagreement (both resolve, differ —
must be 0). Over the `78d928dca7` spec against the ReactGraph chunk: **0 genuine disagreements** (0
value, 0 over-resolved). All **619 anonymous-statement selectors resolve to the identical owner**
both ways (resolved-parity); the 3708 member selectors mostly reject against this non-target chunk
(reject-parity) with **2 var-declarator fail-closes** and 0 disagreements. The end-to-end resolver
path is therefore demonstrated parity-clean, with the fail-closed set being exactly the unimplemented
resolution paths.

Remaining: (a) the **var-declarator / binding-group / multi-statement** resolution paths the resolver
fail-closes (member resolved-parity is best measured by resolving each selector against its _target_
chunk, which needs the spec→chunk map); (b) broaden subjects/chunks (the harness caps subjects
because the production matcher re-parses per call). Still-latent and differential-clean (not yet
exercised by the corpus): alpha shadowing (whole-pattern bijection has held over 250k+ pairs) and
named single-node-hole **equality** (`EXPR_x` ⇒ same subtree, currently anonymous match-any). Each
remains differential-gated.

### What the matcher cannot do that the query model can

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
So the vocabulary is proven expressible in the engine we picked — the gap to production is the
phase-2 AST-facts EDB and the template→atoms lowering, not the solver's capability.

### Rollout: to a parity Datalog resolver

The spine is `source_match::DifferentialResolver` (landed): it runs both matchers, returns the
primary, and records divergence — so **parity is a continuously-checked invariant** ("0
differential disagreements across the corpus"), not a big-bang cutover. The enabling trick is the
**shape-atom escape hatch**: the Datalog resolver delegates any un-lowered construct to the
existing matcher, so parity holds from the first day the resolver exists and is _preserved_ as
constructs are lowered one at a time — the delegated set only ever shrinks.

**P0 — landed.** `selector_solve` (phase-1 Ascent EDB over the owner graph: `name_owner` +
derived `aliases`); `selector_solve_bin --check` and `e2e/selector_solve_shadow_test` (the
bootstrap equivalence gate — the solver's name-pin resolution agrees owner-for-owner with
`peel::resolve_binding_owners` on real emitted output, plus the categoricity precondition); the
`SelectorResolver` / `AstWildcardResolver` / `DifferentialResolver` seam; and
`selector_query_examples` (all six relational capabilities proven runnable in Ascent).
_Deferred:_ embedding the shadow check **inside** the `validate` verb needs a core-pipeline knob
to force `Full` report emission under `--dry-run` (`validate`'s dry-run emits `owner_graph.json`
only on realizability rejection, `ReportEmission::OnRejection`); done as the e2e gate on real
`Full` output instead, with no core-pipeline change.

**P1 — AST-facts EDB. ✅ Landed.** `chunk_facts` projects a parsed chunk into `node_kind` /
`child(parent, ordinal, child)` / `str_lit` / `num_lit` / `bool_lit` / `ident_name` / `prop_name`
/ `operator` / `regex` / `super_class` + the `top_level` owner-ordinal join, fail-closed (loud
`Unsupported` on any unmodeled construct). `chunk_facts_coverage` drove growth by real-corpus
blocker frequency to **100%** top-level extraction on every measured chunk (index, ReactGraph,
Calendar, VoiceChatModal). Alpha-canonicalization (for the alpha-identifier hole rule) is deferred
to P3's lowering, where it is the fidelity-critical piece.

**P2 — `DatalogResolver`, explicit total delegation.** Second `SelectorResolver` impl that lowers
every `source_match` to one **explicit, faithful** shape atom — a call into `AstWildcardResolver`
(total delegation that runs the real matcher, _not_ a silent skip); wire
`DifferentialResolver<AstWildcard, Datalog>` into the resolution path behind a flag. _Exit:_
differential green by construction across the gaffer `tana/re` corpus — proves the
EDB/plumbing/round-trip at zero behavior risk.

**P3 — native lowering, construct by construct, fail-closed + differential-gated.** Replace shape
atoms with real atoms in the hole order from "Holes under the query model": existential → omit,
equality → shared variable, `STR_LITERAL_MATCHING_RE` → `str_matches` filter, identifiers → alpha
facts, then the structural body (`child` / `kind` / `prop_name`). The lowering is **fail-closed**:
a construct compiles to provably-faithful atoms or it hard-errors — never a silently-weaker query.
_Exit per construct:_ 0 differential disagreements corpus-wide and no fail-closed errors over the
set declared native. This is the bulk of the work, but each step is small and reversible.

**P4 — one global solve + `@Name` cross-refs.** Shift from per-selector solves to one CSP over the
whole spec: shared logic variables for `@Name`, `all_different` for duplicate-claim, per-target
categoricity. **"Fully capable" lands here** — the six proven capabilities become usable in real
specs. _Exit:_ the solver reproduces the existing ambiguity / duplicate-claim diagnostics, and the
metaNode debt pins (bare delegators, empty-ish classes) rewrite as cross-ref queries and resolve.

**P5 — flip + delete.** Swap `primary` / `shadow` in production, soak one cycle with the
differential still recording, then drop the `AstWildcardResolver` arm and retire the dead matcher
modules. _Exit:_ the solver is the sole resolver, the matcher is removed, CI green.

**Risks** (none is the engine — proven in P0): fact extraction (P1) is the only true greenfield
and the biggest chunk; the two encodings that must be proven bit-for-bit faithful are **run-hole
adjacency/subsequence** and **alpha-equivalence scoping** (both expressible — see the feasibility
table — but fidelity-sensitive; fail-closed lowering refuses anything unproven, and the
differential catches drift). Positional anchors _are_ expressible (child-index compare); they stay
discouraged for stability (T-variant), not because the engine cannot match them.

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
