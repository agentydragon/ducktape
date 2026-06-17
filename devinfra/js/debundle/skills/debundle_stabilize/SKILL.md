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

## Shared CLI workflows

@references/guide.md

## Setup

Build the debundler and export the standard env vars (see the shared guide for
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

4. **Prove it.** Run `debundle spec validate` (keep-going): it resolves every
   selector and reports `no-match`, `ambiguous`, and `duplicate-claim`. Your edit
   must resolve uniquely. This is today's way to answer "does my anchor actually
   single this out?"; the planned `match-selector` probe (see Background) will make
   it a faster inline check.

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
- **content hashes and generated ids** — hashed CSS class/module names
  (`Button_a1b2c3`), hashed asset URLs (`/static/app.7f3e9c.js`), build-id query
  params, cache-busting suffixes. The hashed segment is the _most_ volatile thing
  in the bundle. Pin the stable prefix and hole / regex-anchor the volatile tail;
  **never pin the hash.**

## Playbook (common cases)

- **literal / enum tables** (i18n, routes, MIME, error codes): anchor on the
  distinctive key/value pair; group siblings into one selector.
- **CSS / style maps**: regex-prefix anchor for generated class names; hole the
  hash suffix.
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
term), the verifiability asymmetry, and the planned affordances that will speed
this loop up — `match-selector` (probe "what does this candidate selector match?")
and `synthesize-selectors --candidates N` (a menu of ranked candidates rather than
one) — live in the `selector_authoring_agent` plan under
`devinfra/js/debundle/plans/`. The two-bundle-version dogfood pair is the eventual
scorecard for whether these instructions actually produce durable selectors.
