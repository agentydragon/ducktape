# Recursive selector minimizer: discrimination-driven anchor selection

Status: in progress (started 2026-06-15). The expectation suite
`//devinfra/js/debundle/e2e:selector_minimizer_expectation_test` is fully
un-ignored and green (function bodies, object literals, class bodies, var
declarator groups, group-vs-standalone partitioning); a swc-native property
test (`//devinfra/js/debundle:selector_codemod_test`) guards gate 1.

## Goal

`debundle spec synthesize-selectors` should, for a target binding living among
near-duplicate siblings in a chunk, emit the **sparsest robust** `source_match`
selector — keeping the meaningful, stable anchors that identify the target and
holing the incidental/volatile rest (`ANYTHING`, `STMT_LIST`, `OBJECT_PROPS`,
`CLASS_REST`, `DECLARATORS_*`), interleaving holes and partial statements at
every nesting level.

Perf budget: ideally <10s per invocation, 60s hard.

## Use cases and the batch application

There are two entry shapes, and the second is primary:

1. **Pull out one item** — minimize a single named binding's selector.
2. **Minify a module YAML** — take an existing extracted module's members
   (today mostly `binding.name` minified-name pins) and rewrite them into
   minified, forward-compatible `source_match` selectors and `binding_groups`.
   This path **decides grouping**: members sharing an enclosing declaration
   become one `binding_group`; one YAML may yield several groups plus
   standalones. Use case 1 is the **N=1 special case** of this path, not a
   separate code path.

**Application:** run this mechanically over the `tana/re` web spec (~6k
name-pins) to convert it to minified selectors/groups, then minimally tweak and
review diffs. The algorithm therefore has to be a useful _first pass_, not
perfect — it may occasionally over-pin (emit a wasteful fuller AST) as long as
it finds the common, useful minimizations.

### Scale (must work at Tana scale)

- **Parse/index once per chunk, not per member.** A YAML's members all resolve
  against the same chunk; share one parsed module + one `SelectorCandidateIndex`
  (PR 2251) across every member's minimization.
- **Prefilter competitors with the index before any matcher call.** The cover
  is currently matcher-driven (~O(anchors × competitors) real matches per
  binding), which is fine for test chunks but too slow for Tana-size chunks. Use
  the index posting lists to narrow competitors first; fall back to the matcher
  only to prove the chosen candidate.
- Pragmatic over-pinning is acceptable if it keeps the algorithm fast and
  simple; correctness (gate 1) is never traded away — every emitted selector is
  proven by the real matcher.

### Known mechanical limit (the policy tension)

"Keep meaningful landmarks" and "exact-minimum cover" genuinely conflict on
near-identical shapes, and no purely-AST rule resolves it (it needs the
intelligence a human spec author has):

- single `object_property_literals` _needs_ object-value literals
  (`kind:"primary"`); group `binding_group_partition` _must skip_ the incidental
  shared object-value `enabled: true`;
- group `binding_group_declarators` _keeps_ a non-discriminating `15`; CLI
  `stableKey` _drops_ a non-discriminating `count: 3`.

Current resolution: groups keep each slot's **direct** shallow literals
(declarator inits, call args — tracked via an `in_object` flag that skips
object-property values); single-target uses exact-minimum cover. This is a
deliberate per-path policy split, not a bug. Unifying to one policy means
choosing one side and re-baselining a couple of CLI assertions.

### Motivation (why minimal selectors matter to a debundle user)

The spec is re-applied on every `debundle run` against a blob whose minified
identifiers churn between rebuilds and version ports. A `selector.binding.name`
pin is therefore rebuild-fragile (the name re-mints and the RE work silently
detaches — this is the "selector debt" `selector-debt` measures). A
`source_match` selector anchors on what the code _is_ (shape, literals, calls,
config keys), which survives re-minification. The minimizer turns a brittle
exact body into the concise anchor that re-identifies the entity across future
builds while staying readable in the spec.

### Two gates (from `skills/shared/workflow.md`)

1. **Uniqueness/correctness** — must match and claim the intended current entity.
2. **Conciseness/robustness** — must not overpin incidental bodies, arguments,
   generated values, or unrelated siblings.

Exact long bodies satisfy gate 1 only; they are drafts to minimize.

### Objective is dual, not pure discrimination

Keep anchors that are _meaningful, stable landmarks_ (distinctive string
literals, API method calls, config `key: value`s) — these both discriminate
from siblings AND durably re-identify the entity in future builds, so retaining
one is worthwhile even when it is not the minimum needed to beat today's
siblings. Hole what is incidental/volatile (transient locals, generated values,
ordering noise, unrelated siblings). The cost model should _prefer retaining
concrete meaningful content over incidental structure_, not merely minimize
token count.

### Selector groups are a first-class minimization target

A **binding group** is one `source_match` + `exports:` map that resolves
several members at once (spec `binding_groups:`; matcher support already exists
via `resolve_member_binding_group_match`). Preferring a group over N individual
selectors **avoids multiplication**: the shared enclosing structure is described
once instead of N times, and the cluster re-identifies as a unit (fewer,
sturdier anchors to maintain across rebuilds).

Minimizing a group is the same anchor-cover search, but the uniqueness target is
the _tuple_ of target slots resolving to the right exports. It must (1) hole all
non-target slots/structure (`DECLARATORS_BETWEEN/_AFTER`, `OBJECT_PROPS`, ...),
(2) keep enough shared anchors to claim the enclosing declaration uniquely among
chunk siblings, and (3) keep enough per-member anchors to bind each export to
the correct slot when members are otherwise alike (a literal like `"primary"`
vs `"secondary"` can both claim the group and disambiguate members).
`minimize_var_group_selector` renders the shared declaration with
`DECLARATORS_*` gaps for non-target runs and proves the tuple via the
binding-group matcher.

The **grouping decision** (`binding_group_partition`) partitions requested
targets into groups-vs-standalone — group those sharing a declaration, split off
distant ones — so the minimizer emits one group + one standalone rather than
three individual selectors. This is part of the objective, not a post-hoc step.

## Background: why the PR 2250 implementation produced over-/mis-pinned selectors

(Historical — the diagnosis that motivated the current design; all of these are
now fixed.) Running the suite with `--ignored`: 1/7 passed
(`binding_group_declarators`, the var-group path). The other 6 failed with two
root causes:

1. **Objective is structural, not discriminative.** Candidates are ranked by
   `(cost, source.len())` where a _kept but fully holed_ statement
   (`const X = ANYTHING`, cost 1) is cheaper than dropping it into `STMT_LIST`.
   So the search finds a selector that is unique only by _accidental position_
   (e.g. "target is the only function starting with two `const` decls") instead
   of keeping the literal/call that genuinely differs from siblings.
   Example — `sparse_function_body`:
   - got: `function F(A,A,A){ const transient=ANYTHING; const marker=ANYTHING; STMT_LIST }`
   - want: `function F(A,A,A){ STMT_LIST; const marker=123; STMT_LIST; A.foo(A,123); STMT_LIST }`

2. **Renderers cannot express multiple anchors within one node.**
   `render_object_expr_selector_variants` only ever retains ONE object key
   (`{kind:"primary", OBJECT_PROPS}`); `object_property_literals` needs two
   (`kind` + `mode`). The class path anchors on member _names_ only and renders
   member bodies as bare `STMT_LIST`; `class_body` needs a member body anchor
   (`return ANYTHING.format("stable", ANYTHING)`).

## Architecture (current)

- **Holing is an AST→AST prune, not a string render.** `hole_expr` / `hole_stmts`
  / `hole_object` / `hole_class_members` clone the target's `swc` subtree and
  replace dropped positions with ordinary marker nodes (`ANYTHING` ident,
  `STMT_LIST;` expr-statement, `OBJECT_PROPS` shorthand prop, `CLASS_REST;` class
  field). The holed declaration is serialized by **swc codegen**
  (`js_ast::emit_module_source`) — the one AST→string step, which the matcher's
  parse inverts. Selector and code are the same AST type; there is no second
  serializer.
- **Anchor selection** is a tiered minimum set cover (`cover_competitors` +
  `min_set_cover` B&B). Tiers: shallow literals (≤`SHALLOW_LITERAL_DEPTH` calls
  deep) → structural key/member presence → deeper literals. Within a tier, an
  exact minimum-cardinality cover avoids greedy over-pinning. Each anchor's
  exclusion set comes from the production matcher, so discrimination is exact;
  the chosen union is proven once.
- **Expectation tests compare through swc**, not text: both produced and expected
  selectors are parsed and re-emitted by codegen (`normalize_selector`), so
  formatting is irrelevant and fixtures stay prettier-managed.

## Status

Done (all via the AST-prune path, validated through swc, green):

1. Statement-list / function bodies (`sparse_function_body`,
   `call_argument_literal`, `nested_async_try`) — `minimize_function_selector`.
2. Object literals (`object_property_literals`) via the var path
   (`minimize_var_selector`): `const X = <holed init>`, multi-key retention.
3. Class bodies (`class_body`) — `minimize_class_selector`: member-body descent,
   `CLASS_REST` for dropped member runs.
4. Multi-target var binding groups + group-vs-standalone partitioning
   (`binding_group_declarators`, `binding_group_partition`) —
   `minimize_var_group_selector`: `DECLARATORS_*` gaps, shallow per-slot literal
   keep (`in_object`-aware), binding-group matcher used as a resolves-uniquely
   oracle.

The shallow-literal/structural tier split also keeps the
`selector_codemod_cli_test` anchor-quality guarantees (stable key over volatile
nested-call value; global-minimum cover over greedy prefix).

## Roadmap (toward the tana/re batch run)

- **Unify single into N=1 group.** Make the module-YAML path the one entry that
  takes a set of target bindings, partitions them into groups (sharing a
  declaration) plus standalones, and minimizes each — with the single-item path
  as the degenerate N=1 group. Pick one anchor policy (favor keep-shallow,
  accept occasional over-pin per the pragmatic stance) and re-baseline the 1–2
  strict-min CLI assertions this changes.
- **Retire the legacy `render_*_selector_variants` / `VarSlotConstraintSearch`
  zoo** once every path routes through AST-prune (they remain only as fallbacks
  today).
- **Index-prefilter for scale** — share one parse + `SelectorCandidateIndex` per
  chunk across a YAML's members; narrow competitors via posting lists before
  matcher calls.
- **Regex-literal variable minimizer** — a dedicated heuristic for `binding`
  selectors whose minified _name_ matches a stable regex, emitting
  `STR_LITERAL_MATCHING_RE("^…$")` pins (the AST-anchor cover does not target
  these).
- **Dogfood on `tana/re`** — run the module-YAML path over real chunks, measure
  conversion rate + speed, let results drive priorities.
- Extend the proptest generator beyond functions to var/object/class/group.

Each step is validated against the expectation suite (compared through swc). If
the minimizer finds an equivalently-minimal-or-better shape than a fixture, the
fixture is updated to the produced `f(input)=output` (per the suite's own
preamble) rather than forcing the old bytes.

## Regex-over-string-literal anchors (`STR_LITERAL_MATCHING_RE`)

Status: landed for the var-binding minimizer path (2026-06-15).

A var-binding selector that pins an exact string literal
(`const x = load("chunk-a1b2c3")`) breaks the moment a rebuild perturbs a
volatile fragment of that literal (content hashes, build counters). When the
_stable_ part of the literal already discriminates the target from its
siblings, the minimizer now offers a regex anchor that pins the stable
structure and wildcards the volatile tail, so the selector survives the rebuild.

### Where it plugs in

`minimize_var_group_selector` (single-target and group var declarations; the
single is the N=1 group). It runs as an **upgrade pass after the normal
keep-shallow cover already resolves uniquely**: regex never participates in
finding the initial discriminating cover, so it cannot make the minimizer pick a
worse cover. It only rewrites literals the cover already kept.

### Derivation rule (`regex_anchor_pattern`)

Conservative, trailing-volatility-only:

- The literal must end in a _volatile tail_ — a trailing run of hex/digit
  characters at least `MIN_VOLATILE_TAIL_LEN` (= 4) chars long. Shorter numeric
  suffixes (`v2`, `s3`) are left alone: they are more often meaningful than
  generated.
- The pattern is `^<escaped stable prefix><tail class>$`. The prefix is run
  through `regex::escape`, so every metacharacter in the literal is matched
  literally; the only wildcard introduced is the tail character class
  (`[0-9]+` when the tail is pure decimal, else `[0-9A-Fa-f]+`). Anchors `^`/`$`
  are required because `string_literal_matches_regex` uses `Regex::is_match`,
  which is otherwise a substring test.
- A separator immediately before the tail (`chunk-`, `main.`) stays _inside_ the
  pinned prefix, which is the conservative choice (one fewer wildcarded char).
- If the whole literal would be the volatile tail (empty or separator-only
  prefix), we return `None`: `^[0-9]+$` pins nothing meaningful and rarely
  discriminates. A pattern that fails to compile as `regex::Regex` is likewise
  never produced.

### How the cover chooses it (gate 1 preserved)

For each kept string literal with a derivable pattern, the minimizer tentatively
substitutes the `STR_LITERAL_MATCHING_RE` predicate (via a span-keyed
`VisitMut` post-pass on the holed init) and **re-runs `prove_synthesized_selector`**.
The upgrade is accepted only if the selector still resolves uniquely to the
intended binding; otherwise the exact literal is restored. Upgrades are applied
one literal at a time, so an over-broad pattern on one literal cannot block a
sound upgrade on another. Because the proof re-parses the rendered string and
runs the production matcher (which already interprets the predicate), an
over-broad regex that would match a sibling is rejected, never emitted.

### Deliberate limits / judgment calls to revisit

- **Trailing hex/digit only.** Embedded volatility (`main.4f3a.chunk.js`),
  GUID/UUID shapes, and base64 content hashes are not modeled. They are the
  obvious next extensions if dogfooding shows them common.
- **`MIN_VOLATILE_TAIL_LEN = 4`** is a guess at the meaningful-vs-generated
  boundary; a real bundler corpus may want it tuned (e.g. 6–8 for hashes).
- **Hex-vs-decimal class choice** treats an all-decimal tail as `[0-9]+`. A tail
  like `1234` is ambiguous (could be a short hex hash) but the tighter decimal
  class is the more honest wildcard for the value we actually saw.
- **Function/class body literals are not yet upgraded** — only the var-binding
  path, per the immediate need ("variables binding style literals"). The same
  `RegexAnchorSubstitution` post-pass could be wired into
  `minimize_via_retention` if wanted.
