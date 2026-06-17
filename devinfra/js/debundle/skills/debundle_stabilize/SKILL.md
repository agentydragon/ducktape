---
name: debundle_stabilize
description: >
  Author forward-compatible `source_match` selectors for a debundle spec:
  convert fragile minified-name (`binding.name`) pins into stable structural
  selectors by choosing anchors tied to each entity's purpose, not the cheapest
  incidental unique token. The debundler proves validity/uniqueness;
  forward-compatibility is your judgment. Use when stabilizing a debundle spec's
  selectors, replacing name pins, or making a spec survive future bundle
  rebuilds. Does NOT rename symbols (that is debundle_mint_names) or redraw
  module boundaries (that is debundle_architect).
---

# Debundle Stabilize

Turn a debundle spec's fragile selectors into ones likely to keep working across
future minified rebuilds of the same app. You do exactly one thing: **choose and
write `source_match` selectors**. You do not rename symbols, move modules, or
redraw boundaries.

## The one judgment this skill exists for

A `source_match` selector must satisfy two things, and only one is checkable now:

1. **Validity** — it resolves to its target uniquely on the current chunk. The
   debundler **proves** this (`debundle spec validate`); it is a hard gate.
2. **Forward-compatibility** — it keeps resolving after the app is rebuilt and
   re-minified. **Nothing can verify this at authoring time** — there is no future
   bundle in hand. It is an _educated guess_, and making that guess well is the
   whole job.

The minimizer (`synthesize-selectors`) optimizes a mechanical cost model. It finds
_a_ unique anchor, not necessarily _the_ anchor that identifies the entity. It will
happily pin a bare `0`, or a generic `{ name: ANYTHING }` key with the value holed,
because those are cheap and unique _today_. Your job is to override those with an
anchor tied to what the code **does** — something a behavior-preserving refactor
would keep and a human wouldn't rename.

The selector **mechanics** — hole forms (`ANYTHING`, `STMT_LIST`, `CLASS_REST`, …), `binding_groups`, regex anchors, context windows — live in `selectors.md`, transcluded below. This skill does not restate them; it adds the judgment they can't encode: _which_ anchor to choose.

## Shared CLI workflows

@references/cli_basics.md
@references/selectors.md

## Setup

Build the debundler and export the standard env vars (see `cli_basics.md` above for
`DEBUNDLE_MODULES` / `DEBUNDLE_SOURCE_ROOT` / `DEBUNDLE_OUT`). Use a per-agent
Bazel output base under `/tmp` to avoid lock contention, exactly as the other
debundle skills do. In a consuming repo the CLI label is `@ducktape//...`; inside
the debundler repo, drop the prefix.

## The worklist

Rank fragile pins, most rebuild-fragile first:

```bash
debundle spec selector-debt --modules "$DEBUNDLE_MODULES" --min-score 70 --format json
```

The name-only section lists members still selected by their minified
`binding.name`. With `--against <prior-spec-modules>` it also flags members whose
readable `name:` held but whose minified `binding.name` **drifted** across a
re-pin — those are proven unstable, so they are the highest-value to re-express
structurally. Group with `--group-module-depth N` to take coherent module-family
batches.

This census is complete for name pins but blind to the other failure mode: a
selector that is _already_ `source_match` yet pinned on an incidental anchor (the
`{ name: ANYTHING }` shape). No command enumerates those — judging whether a pin is
forward-stable is the same intelligence as authoring it, so you find them by reading
source. `match-selector` (used in the loop below) reports **slack** as a starting
heuristic — kept things that could be holed further without losing uniqueness — but a
clean (zero-slack) selector can still be pinned on the wrong anchor, so slack only
prioritizes; it never decides.

## The loop (per member, or per cohesive cluster)

1. **Read what it is.** `debundle show-source` for the binding (and the emitted JS
   for surrounding context). Decide the entity's role: a route table, an error
   class, a reducer, a style map, an event emitter, an API client, a config
   constant. The role tells you where its identifying anchor lives.

2. **Get the minimizer's suggestion (optional, as input).** Dry-run
   `debundle spec synthesize-selectors --item <module>:<export> --format json`.
   Read it as a _suggestion_. If it anchored on something incidental (a bare
   number, a generic key with the value holed, pure positional/structural shape),
   discard the anchor and keep looking — the proof that it is unique today says
   nothing about tomorrow.

3. **Choose a purpose anchor** (rubric below) and write the `source_match` into the
   member YAML — by hand, or by taking `synthesize-selectors --apply` output and
   tightening it onto the anchor you picked.

4. **Prove it.** Test the candidate with
   `debundle spec match-selector --source-file <chunk> --match '<selector>'
--target-binding <name>`: it reports whether the selector resolves **uniquely** to
   the binding you mean, and its **slack** — the kept things you could still hole
   without losing uniqueness (i.e. whether you over-pinned). For a whole-spec sweep,
   `debundle spec validate` (keep-going) resolves every selector and reports
   `no-match` / `ambiguous` / `duplicate-claim`.

5. **Group** adjacent or cohesive bindings that share a declaration context into a
   `binding_group` rather than emitting N overlapping member selectors.

6. **Leave honest debt.** If the entity has no purpose-bearing anchor stable enough
   to trust, keep the name pin and add a YAML comment saying why. A truthful name
   pin beats an incidental `source_match` that looks stable and isn't.

## What makes a good anchor

Prefer anchors that are **behavior-causal** (the code would have to change behavior
to lose them) and **human-meaningful** (named for what they do):

- descriptive string/number literals — route paths, error codes, event names, i18n
  keys, MIME types, status strings;
- stable member/method/property names that name behavior (`fetchAcl`, `dispatch`);
- API/operation/selector identities (`.then`, GraphQL op names, action types);
- a **stable prefix** of an otherwise volatile string, via a regex anchor.

Avoid — incidental or actively unstable, churned by unrelated rebuilds:

- bare numbers (`0`, `1`), booleans, and other ubiquitous literals;
- a generic object key with its value holed (`{ name: ANYTHING }`);
- positional / structural shape with no kept value (arity, declaration order);
- minified identifiers (the matcher already wildcards them) and their neighbors;
- **content hashes and generated ids** — hashed CSS-module class names
  (`Button-module_root__a1b2c3`), hashed asset URLs (`/static/app.7f3e9c.js`), build-id
  query params, cache-busting suffixes. The hashed segment is the _most_ volatile thing
  in the bundle. Pin the stable prefix and hole / regex-anchor the volatile tail;
  **never pin the hash.**

## Playbook (common cases)

- **literal / enum tables** (i18n, routes, MIME, error codes): anchor on the
  distinctive key/value pair; group siblings into one selector.
- **CSS Modules / style maps**: the dogfood app is built with CSS Modules, so class
  literals are shaped `<Component>-module_<local>__<hash>`. Never pin the `<hash>` (it
  is regenerated every build) — anchor each className constant with
  `STR_LITERAL_MATCHING_RE("^<Component>-module_<local>__[A-Za-z0-9_-]+$")` and collect
  the component's `*Styles` object constants into one `binding_group` (the
  `Widget-module_*` declaration-range example in `selectors.md` is exactly this shape).
  Tailwind `tw-`-prefixed utility classes are shared across every component and so do
  not discriminate — never anchor on them.
- **error classes**: the `name` / message string the class sets — not its field
  shape.
- **event emitters / reducers**: the event or action name strings.
- **API clients**: the endpoint path or operation name and the stable method name;
  hole host / version / cache-busting parts of any URL.

## Don't hand-transcribe long bodies

If a selector needs roughly a whole function body, object literal, or class body to
be unique, that is **minimizer backlog, not stabilization**. A `match` block that
is `>40` lines with `≤2` holes is an over-pin: it is an exact snapshot of today's
code. Revert it to the name pin with a comment, and report the gap to the debundler
(a missing hole / anchor capability) rather than scaling a fragile pattern across
many modules.

## Boundaries with sibling skills

- Naming minified symbols → `debundle_mint_names`.
- Module boundaries / taxonomy → `debundle_architect`.
- Read-only planning and worklists → `debundle_plan_work`.

This skill only chooses selectors. The minimizer is a tool you call, not the
authority.

## Background

The design rationale (why anchor choice is an agent judgment rather than a cost
term) and the verifiability asymmetry live in the `selector_authoring_agent` plan
under `devinfra/js/debundle/plans/`. `match-selector` (which probes "what does this
candidate match?" and reports over-pin slack in one shot) has landed; the one planned
affordance still to come is `synthesize-selectors --candidates N` (a menu of ranked
candidates rather than the minimizer's single pick). The two-bundle-version dogfood
pair is the eventual scorecard for whether these instructions actually produce
durable selectors.
