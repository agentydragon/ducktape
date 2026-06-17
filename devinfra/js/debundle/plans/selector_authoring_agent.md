# Plan: agent-authored forward-compatible selectors

Status: **in progress**. The plan and the `debundle_stabilize` skill (M2) landed in
#2332; **M1 (read-only primitives) is complete** — `match-selector` (query + over-pin
slack) in #2335, `synthesize-selectors --candidates N` (ranked menu) in #2339.
Remaining: grounding the skill's playbook with tested fixtures (M2) and port-based
evaluation (M3). Reframes the selector-choice half of
[automated spec workflows](automated_spec_workflows.md). That doc's mechanical
`selective × stable × cost` ranker
([read-off minimization](readoff_minimization.md)) stays — but demoted from
decision-maker to **suggester + uniqueness oracle**. The decision of **which**
anchor a selector pins moves to an agent (the `debundle_stabilize` skill). Validity
stays mechanically gated; forward-compatibility is an agent judgment, sanity-checked
after the fact against real bundle versions.

## Why a cost model can't author forward-compatible selectors

A `source_match` selector must satisfy two things
([automated spec workflows](automated_spec_workflows.md)):

1. resolve its target uniquely on the current chunk — **verifiable now**, and
2. keep resolving across future minified rebuilds of the same source — **not
   verifiable now**: there is no future chunk in hand.

The read-off minimizer optimizes `selective × stable × cost`. That model ranks
selectivity and crude stability (string-vs-number, volatile-hash regex), but it has
no notion of **what the binding is for**, so it cannot tell an incidental
discriminator from a purpose-bearing one. It keeps **a** unique token, not **the**
token a human recognizes as identifying the entity.

Concretely, the `single_target_class_whole_body` E2E fixture: the class's
descriptive anchor is the status string it reports (`"running"`), but the cheapest
unique anchor is the object key `name`, so the minimizer pins `{ name: ANYTHING }`
(value holed) — mechanically minimal and unique, semantically the wrong anchor, and
likely to break the moment an unrelated `{ name: … }` appears in a rebuild. The same
shape recurs across the dogfood spec: a positional structure or an incidental
literal is unique today but is not about the entity.

Forward-compatibility is thus not a cost term. Choosing it requires reading the
source and judging which tokens are (a) **causally tied to the entity's behavior**
(so a behavior-preserving refactor preserves them) and (b) **meaningful to a human**
(so they are unlikely to be renamed or churned). That is an intelligence task, not a
ranking one.

## Architecture: suggest + prove (mechanical) vs. choose (agent)

Split the responsibility the current design conflates:

- **Mechanical substrate (Rust).** Stops trying to choose well. It only:
  - **proves** validity and uniqueness — the one property that is checkable — as a
    hard gate every emitted selector passes; and
  - **suggests** — offers ranked candidates and cheap "what would this match?"
    answers.

    Neither path has to be "forward-compat smart"; the cost model is just a
    candidate ordering, not a verdict.

- **Agent (skill).** Owns anchor selection. Reads the binding's source and purpose,
  picks anchors that are behavior-causal and human-meaningful, groups cohesive
  neighbors deliberately, and confirms each choice through the substrate's prove gate
  before writing YAML.

The minimizer keeps earning its place: on the large mechanical tail (class/function
declarations, literal-initialized constants, multi-declarator groups) its choice is
usually fine and the agent can accept it wholesale. The agent spends its judgment on
the residue the cost model gets wrong — the over-narrow, incidental-anchor cases —
which, as the next section notes, the substrate cannot itself single out.

## The worklist is an intelligence task too

The same wall blocks the _other_ direction. Just as the cost model can't **choose** a
forward-compatible anchor, it can't reliably tell **which already-written selectors
need rework**. "Is this pin readable, stable, and unlikely to break on a rebuild?" is
the same judgment as authoring one — there is no mechanical predicate for it. So the
worklist of selectors to revisit is not something the substrate can hand the agent
pre-computed; the agent forms it by reading source and weighing purpose.

Two mechanical signals **bound** that search without defining it:

- **Name-pins are unambiguously fragile.** `selector-debt` enumerates the
  `binding.name` pins (a minified name churns every rebuild). That bucket is mechanical
  and complete — every name-pin is debt.
- **Holing slack is a heuristic for over-pin.** A `source_match` that pins more than it
  needs — an anchor that could be holed further and still resolve uniquely — is a
  _candidate_ for rework (`match-selector` reports this slack; see below). But slack is
  only a smell:
  a zero-slack selector can still be pinned on an incidental anchor (the `name`-key case
  has no slack and is still wrong), and a selector with slack can be perfectly readable.
  The agent uses it to **prioritize**, never to decide.

Everything between those bounds — a robust-looking `source_match` nonetheless pinned on
the wrong thing — is invisible to the substrate and surfaces only when the agent reads
the source, or when a future bundle breaks it.

## Forward-compatibility is an educated guess, not a guarantee

Criterion 2 is unverifiable at authoring time. The agent **cannot know** a spec is
forward-compatible; realistically it won't be completely. The bar is a **reasonable
educated guess**: anchor on the things most likely to survive a rebuild, and never on
the things most likely to churn. That makes the heuristics below the actual product,
and means:

- **Validity is a hard prove-gate; stability is a heuristic.** Being wrong about
  stability is expected and only discovered when a new bundle ships.
- **The version-port flow (Flow 3 in
  [automated spec workflows](automated_spec_workflows.md)) is the only ground
  truth.** When v2 chunks arrive, "which agent-authored selectors still resolve?" is
  the real quality signal — and a regression corpus for tuning the heuristics. The
  authoring skill and the port flow should be one feedback loop: every port run emits
  a survived/broke verdict per selector, attributed to the anchor kind it used, so the
  playbook improves from evidence instead of taste.

### Evaluation substrate (later)

We already hold a real spec captured against **two bundle versions** of the same app
(the dogfood target). That is a ready-made held-out evaluation of the skill itself,
not just of one spec: re-author selectors against v1 with the skill's instructions,
resolve them against v2, and the survival rate measures **whether the instructions we
gave the agent were good**. Use it later to tune the playbook and the "good anchor"
rubric. Keep the data in its own repo — do not copy real bundle source into Ducktape
fixtures; the eval runs against the private spec, only anonymized reductions become
ducktape tests.

There is **no inline self-check** for forward-compatibility — no perturbation pass
that mutates the current chunk and re-resolves. A single-bundle perturbation only
re-confirms today's match (which the prove-gate already guarantees) and would lend
false confidence to a guess that only a real second bundle can test. The version-port
flow is the sole stability signal; until v2 ships, stability is the agent's reasoned
bet and nothing more.

## Agent-facing affordances

The agent works by composing read-only queries, forming an anchor hypothesis,
testing it, and emitting. Handles (item ids / binding ids / spans) returned by one
query feed the next.

### Already exposed

- `debundle show-source` — source of a body item / binding.
- `debundle bindings …` / `debundle modules …` — binding and module inventory.
- `debundle spec selector-debt` — the mechanical name-pin census (every `binding.name`
  pin). Complete for that bucket; **not** the over-pin worklist, which is not
  mechanically identifiable (see above).
- `debundle spec synthesize-selectors` — the minimizer's single chosen selector.
- keep-going spec validation — all selector failures in one pass.

### Landed: `match-selector` (hypothesis-test probe + over-pin slack) — #2335

Give it a candidate `source_match` and a chunk; get back **what it resolves to** — the
matching item/binding ids, the uniqueness verdict, and the binding it would bind — and,
when the selector pins a unique target, its **slack**. The interactive counterpart to
the batch minimizer: the agent asks "if I anchor on X, is the match set the singleton I
mean, the right one — and did I over-pin it?" and chains the returned ids into
`show-source`. The matcher already exists internally (the prove-gate); this exposes it
as a probe that returns handles instead of a boolean.

**Slack** is the mechanical half of "report over-narrow selectors as debt even when
they match" (the [automated spec workflows](automated_spec_workflows.md) goal). It
walks the selector AST and, for each pinned place, tries replacing it with the matching
wildcard / run-hole — value-holing (`ANYTHING`) plus structural drops of an object
property, class member, block statement, or call/`new` argument — keeping the
relaxation iff the same unique target still resolves. Matching and slack share the
parse + baseline resolve, so they are answered together (`--no-slack` skips slack for a
fast match-only check). A non-empty slack list is a **where to look next** heuristic,
never a verdict: high slack flags a likely over-pin, but the agent still judges whether
the surviving anchors are right (a zero-slack selector can still be pinned on an
incidental key). It reports only; relaxing a pin is an authoring decision, run back
through `match-selector`. _(This subsumes the separately-planned `selector-slack`
command — query and slack are the same authoring question.)_ Not yet covered:
top-level context-statement drops and destructure-pattern property drops.

### Landed: `synthesize-selectors --candidates N` — #2339

Emit the top-N ranked candidates per item, not just the one minimal pick — the agent
reads them as a menu to override an incidental anchor with a purpose-bearing one. The
read-off walk now collects the top-N proving anchor sets (`read_off_candidates`, with
`limit == 1` reproducing the single pick); the extras beyond the primary surface as
`alternatives` on each report candidate, and the primary's own `match_source` is now in
the report too. Covers the function/class and single-target var/object read-off forms;
the multi-declarator binding-group menu is still single-pick (tracked in `TODO.md`).

Both commands follow the
[automated spec workflows](automated_spec_workflows.md) contract: `--format
json|ndjson`, the shared `--module` / `--source-root` filters, stable output, and a
reason for every rejected candidate.

## The authoring loop (skill)

For one binding or cohesive cluster:

1. **Read** the source (`show-source`) and decide what the entity is (a route table,
   an error class, a reducer, a style map, an event emitter).
2. **Hypothesize** an anchor from purpose — a descriptive string/key, a stable
   member/method name, an API/operation name, a CSS-class prefix — not the cheapest
   unique token.
3. **Test** with `match-selector`: unique? and is the match set semantically the
   intended one? If not, refine; consult `--candidates N` for options the ranker
   surfaced.
4. **Group** when adjacent bindings share a declaration context or purpose — emit a
   `binding_group` rather than N overlapping member selectors.
5. **Emit** and **validate** (keep-going). Every emitted selector passes the prove
   gate; nothing hand-written ships unproven.
6. Record a YAML comment for anything that has no stable purpose-bearing anchor
   (genuine debt) instead of forcing an incidental one.

## What makes a good anchor (heuristics the agent applies)

Prefer anchors that are **behavior-causal** (the code would have to change behavior
to lose them) and **human-meaningful** (named for what they do):

- descriptive string/number literals (route paths, error codes, event names, i18n
  keys, MIME types, status strings);
- stable member/method/property names that name behavior (`fetchAcl`, `dispatch`);
- API/operation/selector identities (`.then`, GraphQL op names, action types);
- a **stable prefix** of an otherwise volatile string, via a regex anchor.

Avoid — these are incidental or actively unstable and churn on unrelated rebuilds:

- bare numbers (`0`, `1`), booleans, and other ubiquitous literals;
- a generic object key with its value holed (`{ name: ANYTHING }`);
- positional/structural shape with no kept value (arity, declaration order);
- minified identifiers (already wildcarded) and their close neighbors;
- **content hashes and generated ids** — hashed CSS-module class names
  (`Button-module_root__a1b2c3`), hashed asset URLs (`/static/app.7f3e9c.js`), build-id
  query params, cache-busting suffixes. Pin the stable prefix and hole/regex the
  volatile tail; never pin the hash.

## Common-case playbook

Domain patterns the skill recognizes and has a canned approach for:

- **literal/enum tables** (i18n, routes, MIME, error codes): anchor on the
  distinctive key/value pair; group siblings.
- **CSS Modules / style maps** (the dogfood target's styling): the bundle is built
  with CSS Modules, so every class-name string literal is shaped
  `<Component>-module_<local>__<hash>` (e.g. `Button-module_root__a1b2c3`). The trailing
  `<hash>` is regenerated every build — the single most volatile token in the bundle —
  so **never pin it**: anchor each className constant with a
  `STR_LITERAL_MATCHING_RE("^<Component>-module_<local>__[A-Za-z0-9_-]+$")` prefix regex
  and let the tail float. Components collect these constants into `*Styles` **objects**
  (`{ root: rootClassName, … }`, composed at use sites with `clsx`); there are many such
  objects across the spec. Pin the object as a `binding_group` over its
  className-constant declarators — each constant carrying its own prefix-regex anchor —
  and hole the object body with `OBJECT_PROPS` down to the discriminating keys, rather
  than emitting N overlapping member selectors. Tailwind utility classes (the
  `tw-`-prefixed atoms appearing in the same `clsx` calls) are shared across every
  component, so they **do not discriminate** — treat them as noise, never as anchors.
  Ships as an anonymized `css_module_styles` fixture, not real bundle source.
- **error classes**: the `name` / message string the class sets, not its field shape.
- **event emitters / reducers**: the event/action name strings.
- **API clients**: the endpoint path or operation name, the stable method name; hole
  host/version/cache-busting parts of any URL.

Each playbook entry is a tested recipe in the skill (per the ground-skill
convention), with an anonymized fixture — **never** private downstream bundle source.

## Priorities

1. **Validity** — the spec resolves and passes the prove-gate. Non-negotiable, and
   the only mechanically enforced one.
2. **Forward-compatible, natural, minimal** — purpose-bearing anchors, holes over
   incidental detail, deliberate grouping of adjacent/cohesive bindings, the loosest
   readable selector that still proves.

## Packaging

A new skill in the `debundle_*` family — `debundle_stabilize` (or
`debundle_selector_author`) — driving the loop above. It sits beside
`debundle_mint_names` (chooses names) and `debundle_architect` (chooses module
boundaries); this one chooses **selectors**. The minimizer is a tool it calls, not
the authority. The `debundle_orchestrator` can dispatch stabilization lanes the same
way it dispatches naming/extraction lanes.

## Relationship to existing plans

- [automated spec workflows](automated_spec_workflows.md) keeps its
  inventory/plan/apply/validate CLI model, patch-plan artifacts, perf budgets, and the
  three product flows. This plan amends only its selector-choice assumption: the cost
  model ranks and proves; it does not decide. The "report over-narrow selectors as
  debt even when they match" goal stays, but is reframed: mechanically it is only the
  slack heuristic `match-selector` reports (which selectors _could_ be holed further),
  and judging which flagged selectors are genuinely debt is the agent's call, not a
  worklist the substrate hands over.
- [read-off minimization](readoff_minimization.md) stays as the suggester/prover
  implementation. Its remaining over-pin backlog (e.g. value-over-key preference, the
  `single_target_class_whole_body` outcome) is no longer a blocker for spec quality —
  the agent overrides incidental picks — but still useful as better default
  suggestions.

## Open questions / milestones

- **M1 — read-only primitives — complete.** `match-selector` (hypothesis-test probe +
  over-pin slack, value + structural) **landed** (#2335); `synthesize-selectors
--candidates N` (the ranked-candidate menu) **landed** (#2339). Residue tracked in
  `TODO.md`: the candidates menu for multi-declarator var groups, and slack for
  top-level context statements / destructure-pattern properties.
- **M2 — the skill.** Loop + playbook + anonymized fixtures. The `debundle_stabilize`
  skill **landed** (#2332); grounding its playbook entries with tested fixtures is
  still open.
- **M3 — port-based evaluation.** Run the two-version dogfood pair as a held-out eval
  of the skill's instructions; version-port emits a per-selector survived/broke
  verdict attributed to anchor kind; feed a stability scorecard back into the
  playbook.
- **Open:** how much the agent should batch-accept minimizer output vs. review
  per-binding (cost/latency vs. quality); how the skill records its anchor rationale so
  the port feedback can attribute survival to a choice.
