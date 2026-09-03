---
name: debundle_stabilize
description: >-
  Author forward-compatible `source_match` selectors for a debundle spec,
  replacing fragile minified-name pins with structural anchors tied to each
  entity's purpose. Not for renaming symbols (debundle_mint_names) or moving
  module boundaries (debundle_architect).
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

The minimizer (`synthesize-selectors`) is a first-class tool in your kit: it makes
selectors **compact and unique** for you — holing volatile subtrees, collapsing
declarator runs, and proving uniqueness (see The toolkit below). But it has **no
semantic intelligence** — it is a mechanical read-off of the AST optimizing a cost
model, so it finds _a_ unique anchor, not necessarily _the_ anchor that identifies
the entity, and when both a readable anchor and an accidental token are unique it
cannot tell which is which. It will
happily pin a bare `0`, or a generic `{ name: ANYTHING }` key with the value holed,
because those are cheap and unique _today_. Your job is to override those with an
anchor tied to what the code **does** — something a behavior-preserving refactor
would keep and a human wouldn't rename.

The selector **mechanics** — hole forms (`ANYTHING`, `STMT_LIST`, `CASE_REST`, …), `source_matches[]`, regex anchors, context windows — live in `selectors.md`, transcluded below. This skill does not restate them; it adds the judgment they can't encode: _which_ anchor to choose.

## The toolkit

Four debundler subcommands. `selector-debt` and `synthesize-selectors` read the
chunk + `modules/` tree directly; `match-selector` reads just the chunk and a
candidate selector. None of those three need a pipeline build or owner graph.
The fourth (`spec validate`) is the whole-spec gate and needs the full pipeline
(see Setup). Treat the **minimizer as a first-class instrument**, not a last
resort:

- **`spec selector-debt`** — the census. Ranks fragile name pins; add
  `--source-file` to also surface the near-ambiguous structural selectors (see the
  worklist).
- **`spec synthesize-selectors` — the selector minimizer.** Your workhorse for
  _compact_: it holes volatile subtrees (bodies, args, declarator runs,
  `ANYTHING;`/`CASE_REST`), collapses multi-declarator runs into grouped
  `source_matches[]` entries,
  and proves uniqueness — so much of the backlog converts to short, unique-today
  selectors with no hand-authoring. Run it dry to read its pick, `--candidates N` for
  a ranked menu, `--apply` to land a whole bucket. Use it two ways: as a **first-pass
  converter** for the easy majority, and as a **compaction pass** once you have
  hand-picked an anchor but want the surrounding shape holed down. But it has **no
  semantic intelligence**: whether the anchor it kept is _meaningful_ — and swapping
  in the readable one when it kept an accidental but-unique token — is judgment you
  supply on top of its output (next section); it cannot be read off the AST.
- **`spec match-selector`** — the prove/probe. Resolves your candidate and reports
  unique-or-not, the colliding matches, and over-pin slack.
- **`spec validate`** — the whole-spec keep-going sweep (`no-match` / `ambiguous` /
  `duplicate-claim`). Unlike the other three this runs the full pipeline (Bazel
  `:debundle`, package roots), not the standalone binary — see Setup.

Division of labor: the minimizer makes a selector **compact and unique today** by
mechanical read-off; judging whether its anchor is _meaningful_ (vs an accidental
token that happens to be unique) and so **forward-compatible** is intelligence you
supply — it cannot be read off the AST. Both halves of the backlog flow through
these tools — the name pins from `selector-debt`'s default census, and the
near-ambiguous structural selectors from its `--source-file` pass.

## Shared CLI workflows

@references/cli.md
@references/selectors.md

## Setup

The per-selector loop is **binary-only**: `selector-debt` and
`synthesize-selectors` read the chunk + the `modules/` tree directly, while
`match-selector` reads just the chunk and candidate selector. None of them need
a pipeline build, owner graph, or `DEBUNDLE_GRAPH`/`DEBUNDLE_OUT`. A built
`debundle` binary (or the pinned released one) plus the upstream snapshot is
their whole toolchain. Export `DEBUNDLE_MODULES` / `DEBUNDLE_SOURCE_ROOT` (see
`cli.md` above); `match-selector` can also be run with explicit
`--source-file` / `--source-root --chunk` flags. Use a per-agent Bazel output
base under `/tmp` to avoid lock contention, exactly as the other debundle
skills do. In a consuming repo the CLI label is `@ducktape//...`; inside the
debundler repo, drop the prefix.

The whole-spec gate is the exception. `spec validate` (and `debundle run`) is the
realizability/cycle pipeline in dry-run, so it needs the full `debundle` **pipeline
target** — the Bazel `:debundle` target with its package roots and a repo-root
source root — not the standalone binary against the snapshot dir. Run it through
Bazel. On NixOS, `--server_javabase=…` is a **startup** option: it must precede
`build`, not follow it.

## The worklist

The backlog has two _enumerable_ halves. Run both before picking work — the second
is invisible by default yet roughly as large as the first, so a top-down reader who
skips it works only half the surface.

**1. Name pins (default census).** Members still selected by their minified
`binding.name`, most rebuild-fragile first:

```bash
debundle spec selector-debt --modules "$DEBUNDLE_MODULES" --min-score 70 --format json
```

With `--against <prior-spec-modules>` it also flags members whose readable `name:`
held but whose minified `binding.name` **drifted** across a re-pin — those are proven
unstable, so they are the highest-value to re-express structurally. Group with
`--group-module-depth N` to take coherent module-family batches.

**2. Near-ambiguous structural selectors (source-aware pass).** Add a source flag to
the same command:

```bash
debundle spec selector-debt --modules "$DEBUNDLE_MODULES" \
  --source-file <chunk> --format json
```

This adds **source-aware near-ambiguous** rows — existing `source_match` selectors
that resolve uniquely today but have high-scoring sibling statements, i.e. one
upstream edit from ambiguous — plus source-aware repeated-exact bodies and
grouped `source_matches[]` suggestions. Treat this run as routine, not a footnote:
it is cheap (tens of seconds whole-spec) and surfaces a population about as large as
the name-pin backlog that is otherwise invisible.

**3. Re-check existing commented debt.** A name pin kept with a "blocked on X"
comment (step 6) may have been unblocked since: tooling that has landed (new hole
forms, declarator support, …) can make a previously-impossible selector convert
cleanly now. The census lists the name pin but not whether its recorded blocker is
still real, so periodically re-run the minimizer over commented debt and retire the
comment where it now converts.

A _third_ failure mode is **not** enumerable: a selector that is already
`source_match` yet pinned on an _incidental_ anchor (the `{ name: ANYTHING }` shape).
Judging whether a pin is forward-stable is the same intelligence as authoring it, so
you find these by reading source. `match-selector` (used in the loop below) reports
**slack** as a starting heuristic — kept things that could be holed further without
losing uniqueness — but a clean (zero-slack) selector can still be pinned on the
wrong anchor, so slack only prioritizes; it never decides.

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
   nothing about tomorrow. Add `--candidates N` to get a ranked **menu** of
   alternative anchor choices (in `alternatives`) instead of the single pick, then
   choose the most purpose-bearing one.

3. **Choose a purpose anchor** (rubric below) and write it into a
   `source_matches[]` entry — by hand, or by taking `synthesize-selectors --apply`
   output and tightening it onto the anchor you picked. After any `--apply`, run the repo
   formatter (`pre-commit` / prettier) **before** reading the diff: `--apply`
   re-emits the whole YAML in the debundler's canonical 0-space form, so a
   pre-prettier `git diff` is unreviewable noise; the formatter reconciles it back to
   exactly the semantic change.

4. **Prove it.** Test the candidate with
   `debundle spec match-selector --source-file <chunk> --match '<selector>'
--target-binding <name>`: it reports whether the selector resolves **uniquely** to
   the binding you mean, and its **slack** — the kept things you could still hole
   without losing uniqueness (i.e. whether you over-pinned). It uses the public
   alpha-equivalent source-match identifier policy. For a whole-spec sweep,
   `debundle spec validate` (keep-going) resolves every selector
   and reports `no-match` / `ambiguous` / `duplicate-claim`.

5. **Group** adjacent or cohesive bindings that share a declaration context into
   one `source_matches[]` entry rather than emitting N overlapping selectors.

6. **Leave honest debt.** If the entity has no purpose-bearing anchor stable enough
   to trust, keep the name pin and add a YAML comment saying why. A truthful name
   pin beats an incidental `source_match` that looks stable and isn't.

## What makes a good anchor

A selector is a **claim about what makes the entity recognizable**. The durable
claims describe the entity's **identity / contract** — what it _is_, in words a
human would use to name it and a minifier cannot erase. The fragile claims describe
its **implementation** — what its code currently _looks like_, which is exactly what
a behavior-preserving refactor or the next re-minification churns. Anchor on the
identity side of that line.

**The read-it-back test.** Say the `match` aloud as a sentence. "The class whose
`getName` returns `'DocumentAccessorFactory'`" names an identity — durable. "The
class with a method holding a `for`-loop over `.children` that calls `eN(…)`" can
only be read as a description of today's code — a photograph; replace it.
Unreadability is a _symptom_: a selector reads like an AST dump precisely when it
anchors on tokens that carry no identity, and those are the volatile ones. If you
can't read it back as "the X that _<declares/does the identifying thing>_," keep
looking.

**Literals the code emits about itself are the strongest anchor.** A minifier
renames `getOwner` → `gO` freely but cannot rewrite the string
`"DocumentAccessorFactory"` — strings are observable behavior. So an identity carried
by a _literal_ is doubly stable: identity-bearing _and_ minification-immune. The
ladder:

Prefer (identity / contract — behavior-causal, human-meaningful):

- a literal the entity emits **about itself** — a `getName`/`get type` returning its
  name, `static displayName`, an error `name`/`message`, an action `type`, an
  event / route / MIME / i18n / registration key;
- public member or method names that name behavior _and_ survive minification
  (`fetchAcl`, `dispatch`) — under `alpha_all` property names are exact anchors, so
  they only help when the build keeps them (in this app, member names generally
  survive);
- API / operation identities (GraphQL op names, action types);
- a **stable prefix** of an otherwise volatile string, via a regex anchor.

Disprefer (implementation / incidental — churned by refactors and rebuilds):

- **control-flow and body internals** — `for`/`while`/`switch` shape, statement
  sequences, nested expression trees: the mechanism, never the identity;
- positional / structural shape with no kept value (arity, declaration order);
- uniqueness borrowed from an unrelated **neighbor** declaration;
- bare numbers (`0`, `1`), booleans, ubiquitous literals; a generic object key with
  its value holed (`{ name: ANYTHING }`); minified identifiers (already wildcarded);
- **content hashes and generated ids** — hashed CSS-module class names
  (`Button-module_root__a1b2c3`), hashed asset URLs (`/static/app.7f3e9c.js`),
  build-id query params, cache-busting suffixes: the _most_ volatile thing in the
  bundle. Pin the stable prefix and hole / regex-anchor the volatile tail; **never
  pin the hash.**

### Good / okay / bad: one entity, three selectors

The source the chunk happens to ship today (minified top-level names elided):

```js
class DocumentAccessorFactory extends NodeAccessor {
  getName() {
    return "DocumentAccessorFactory";
  }
  getOwner(node) {
    for (const c of node.children) if (c.isOwner) return c;
    return null;
  }
}
```

All three resolve uniquely _today_; they differ only in what they claim makes this
the class. The claim wrapper is the same each time — only the `match:` body
changes.

**Good** — anchors on the self-naming literal; holes the mechanism:

```yaml
match: |
  class DocumentAccessorFactory extends ANYTHING {
    ANYTHING;
    getName() {
      STMT_LIST;
      return "DocumentAccessorFactory";
    }
    ANYTHING;
  }
```

Reads: "the class whose `getName` returns `'DocumentAccessorFactory'`." The kept
anchors are one method name plus the literal it returns — the literal is the
load-bearing, minifier-immune part. Superclass and every body except the
identifying `return` are holed, so any identity-preserving refactor still matches.

**Okay** — anchors on a stable member name, no self-identity literal:

```yaml
match: |
  class DocumentAccessorFactory extends ANYTHING {
    ANYTHING;
    getOwner(ANYTHING) {
      STMT_LIST;
    }
    ANYTHING;
  }
```

Reads: "the class with a `getOwner` method." Bodies holed (good), but it leans on
the method _name_ surviving minification and only describes a _capability_, not an
identity — another class could grow a `getOwner`. Fine when no self-naming literal
exists; strictly weaker than Good.

**Bad** — anchors on the implementation of `getOwner`:

```yaml
match: |
  class DocumentAccessorFactory extends ANYTHING {
    ANYTHING;
    getOwner(node) {
      for (const c of node.children) if (c.isOwner) return c;
      return null;
    }
    ANYTHING;
  }
```

Reads: "the class whose `getOwner` loops over `node.children` looking for
`isOwner`." It pins the _mechanism_: rewrite the loop to
`node.children.find((c) => c.isOwner)` (behavior-preserving) and it breaks though
the class is unchanged. A photograph of today's body — and the longer and more
literal the body it pins, the more fragile, not less.

**No good anchor at all → leave honest debt.** If the entity is just
`class DocumentAccessorFactory extends NodeAccessor {}` — empty body, no self-name,
no distinctive surviving member — then every unique selector is either
neighbor-borrowed or shape-only. Keep the name pin with a comment (step 6). An
honest pin beats a photograph that _looks_ structural and durable but isn't.

## Playbook (common cases)

- **literal / enum tables** (i18n, routes, MIME, error codes): anchor on the
  distinctive key/value pair; group siblings into one selector.
- **CSS Modules / style maps**: the dogfood app is built with CSS Modules, so class
  literals are shaped `<Component>-module_<local>__<hash>`. Never pin the `<hash>` (it
  is regenerated every build) — anchor each className constant with
  `STR_LITERAL_MATCHING_RE("^<Component>-module_<local>__[A-Za-z0-9_-]+$")` and collect
  the component's `*Styles` object constants into one grouped `source_matches[]`
  entry (the `Widget-module_*` declaration-range example in `selectors.md` is
  exactly this shape).
  Tailwind `tw-`-prefixed utility classes are shared across every component and so do
  not discriminate — never anchor on them.
- **error classes**: the `name` / message string the class sets — not its field
  shape.
- **event emitters / reducers**: the event or action name strings.
- **API clients**: the endpoint path or operation name and the stable method name;
  hole host / version / cache-busting parts of any URL.
- **duplicated TS codegen helpers** (`__decorate` / `__defineProperty` /
  `__getOwnPropertyDescriptor` aliases): the bundler emits a byte-identical copy per
  module, so nothing but the minified name distinguishes the copies — there is no
  sparse selector to author. **Not your job:** leave them as honest name-pin debt.
  Recognizing and collapsing these is a debundler tooling concern (the
  `effect: typescript_decorate_helper` annotation is the partial-awareness hook), not
  selector authoring — don't burn effort hunting an anchor that cannot exist.

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
term) and the verifiability asymmetry are summarized in
`devinfra/js/debundle/docs/selectors.md`. Both `match-selector` (probes "what does
this candidate match?" and reports over-pin slack in one shot) and
`synthesize-selectors --candidates N` (a menu of ranked candidates rather than the
minimizer's single pick) have landed. The two-bundle-version dogfood pair is the
eventual scorecard for whether these instructions actually produce durable
selectors.
