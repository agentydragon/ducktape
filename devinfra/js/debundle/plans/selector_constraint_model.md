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
- **relations** — AST `child` / `sibling` _and_ semantic edges:
  `calls(caller, callee, argpos)`, `alias(x, y)`, `decorates(helper, class, "method")`,
  `def_use(def, use)`, `member_access(node, "prop")`, `imports` / `exports`,
  `member_of_module`;
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

| Class           | Relations / labels                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------------- |
| **T-invariant** | literals, property/method names, `calls`, `alias`, `decorates`, `def_use`, `imports`, module membership |
| **T-variant**   | identifier spellings, source position/order, **adjacency**, control-flow shape, arity                   |

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
- **Relational atoms** — `calls` / `alias` / `decorates` / `def_use` /
  `member_access`, addressing other targets by `@Name` (including across modules).
  Not naturally AST-shaped; written as explicit edge constraints.

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

## Scale and performance

One _conceptual_ CSP, but it **decomposes by connected components**: a selector with no
cross-references is a size-1 component, solved by the current direct
match/lookup; only cross-referencing clusters need a joint solve, and those are small.
Rare-literal anchors prune variable domains to near-singletons (the same pruning that
makes today's matcher fast), so subgraph-match NP-hardness is defanged in practice.

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
    templates): "the helper `h` in `h(_, @CalendarViewAccessor.prototype,
"clearDayStartEndTimeConfig")`" — a `decorates` edge to an already-pinned class
    plus a method literal. The use-site disambiguates the copy.
  - `let HI = UJ`: `alias(HI, @NavigationStackItemAccessor)`.
- **Residual true debt**: only when no invariant label is reachable from the target
  along any invariant edge.

So the honest-debt set shrinks from "no _adjacent_ anchor" to the much smaller "no
invariant label in the target's reachable neighborhood" — metaNode's 18 debt items
become roughly 5.

## Incremental path

1. **Named cross-reference anchors** (MVP): let an anchor be `@Name` plus one of a
   small edge set (`calls`, `alias`, `decorates`, `member_access`). A bounded
   extension of today's matcher — it already binds `target_binding` within a window;
   now an anchor can be a name resolved elsewhere. This alone turns metaNode's 18 debt
   into ~5 and removes the neighbor-borrow temptation.
2. **Component-decomposed global solve**: replace per-selector resolution with one CSP
   that decomposes to the current fast path for independent selectors and small joint
   solves for cross-ref clusters. `duplicate-claim` becomes an all-different
   constraint.
3. **Full relational templates**: arbitrary conjunctive queries over the invariant
   signature; whole-spec homomorphism as the limit.

## Open questions

- **Relation set to expose first** — `calls` / `alias` / `decorates` / `member_access`
  cover the pass's cases; `def_use` and `imports/exports` are the obvious next.
- **Alpha-consistency across cross-refs** — within one pattern, co-referring
  identifier holes must back the same source-identifier-class; across a cross-ref the
  join should be by **resolved node identity** (`@Name`'s target), not by name, so two
  patterns alpha-rename independently.
- **Cross-module references** — a target in module A anchored on a binding owned by
  module B is fine: it is the same `G`. Worth confirming the owner-graph/module
  assignment stays consistent when a selector reaches across a module boundary.
- **Keep whole-window uniqueness as an option?** Target-categoricity is strictly more
  expressive; whole-window-unique is the special case of over-constraining. Probably
  relax everywhere, but some existing selectors may rely on the stricter reading.
