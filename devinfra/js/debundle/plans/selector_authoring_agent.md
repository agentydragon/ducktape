# Plan: agent-authored forward-compatible selectors

Status: **design**. Reframes the selector-choice half of
[automated spec workflows](automated_spec_workflows.md). That doc's mechanical
`selective × stable × cost` ranker
([read-off minimization](readoff_minimization.md)) stays — but demoted from
decision-maker to **suggester + uniqueness oracle**. The decision of **which**
anchor a selector pins moves to an agent (a new `debundle_*` skill). Validity stays
mechanically gated; forward-compatibility is an agent judgment, sanity-checked after
the fact against real bundle versions.

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
the residue the cost model gets wrong — the over-narrow, incidental-anchor cases.

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

Until that runs, a cheap inline proxy is **perturbation testing** (a W5 acceptance
idea in [read-off minimization](readoff_minimization.md)): mutate volatile fragments
of the current chunk and confirm the selector still resolves. It catches "pinned an
incidental detail" without a second bundle.

## Agent-facing affordances

The agent works by composing read-only queries, forming an anchor hypothesis,
testing it, and emitting. Handles (item ids / binding ids / spans) returned by one
query feed the next.

### Already exposed

- `debundle show-source` — source of a body item / binding.
- `debundle bindings …` / `debundle modules …` — binding and module inventory.
- `debundle spec selector-debt` — fragile-pin worklist.
- `debundle spec synthesize-selectors` — the minimizer's single chosen selector.
- keep-going spec validation — all selector failures in one pass.

### New: `match-selector` (the hypothesis-test primitive)

Give it a candidate `source_match` (inline or a spec member) and a chunk; get back
**what it resolves to**: the set of matching item/binding ids, the count, the
uniqueness verdict, and the binding it would bind. This is the interactive
counterpart to the batch minimizer — it lets the agent ask "if I anchor on X, is the
match set the singleton I mean, and is it the right singleton?" and chain the
returned ids into `show-source`. The matcher already exists internally (the
prove-gate); this exposes it as a standalone probe that returns handles instead of a
boolean.

### New: `synthesize-selectors --candidates N`

Emit the top-N ranked candidates per item (each with its uniqueness proof, cost, and
the concrete anchors it pins), not just the one minimal pick. The agent reads them as
a menu — accept one, or use them to locate a better semantic anchor the ranker
undervalued.

Both new commands follow the
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
- **content hashes and generated ids** — hashed CSS class/module names
  (`Button_a1b2c3`), hashed asset URLs (`/static/app.7f3e9c.js`), build-id query
  params, cache-busting suffixes. Pin the stable prefix and hole/regex the volatile
  tail; never pin the hash.

## Common-case playbook

Domain patterns the skill recognizes and has a canned approach for:

- **literal/enum tables** (i18n, routes, MIME, error codes): anchor on the
  distinctive key/value pair; group siblings.
- **CSS / style maps**: regex-prefix anchors for generated class names; hole the hash
  suffix. The hashed segment is the most volatile thing in the bundle — never pin it.
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
  debt even when they match" goal stays — now the agent is what acts on that debt.
- [read-off minimization](readoff_minimization.md) stays as the suggester/prover
  implementation. Its remaining over-pin backlog (e.g. value-over-key preference, the
  `single_target_class_whole_body` outcome) is no longer a blocker for spec quality —
  the agent overrides incidental picks — but still useful as better default
  suggestions.

## Open questions / milestones

- **M1 — `match-selector` + `--candidates N`.** The two read-only primitives; the
  loop is not useful without them.
- **M2 — the skill.** Loop + playbook + anonymized fixtures, grounded/tested.
- **M3 — port-based evaluation.** Run the two-version dogfood pair as a held-out eval
  of the skill's instructions; version-port emits a per-selector survived/broke
  verdict attributed to anchor kind; feed a stability scorecard back into the
  playbook.
- **Open:** how much the agent should batch-accept minimizer output vs. review
  per-binding (cost/latency vs. quality); whether perturbation testing is cheap enough
  to run inline as a stability pre-check; how the skill records its anchor rationale so
  the port feedback can attribute survival to a choice.
