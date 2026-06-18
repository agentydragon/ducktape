# Dogfood: the `debundle_stabilize` _skill_ on real tana/re (2026-06-18)

Friction log from running the `debundle_stabilize` skill end-to-end against
gaffer-private `tana/re/web/78d928dca7` as an **agent consumer** (not as the
minimizer's author). The complementary minimizer-internals/perf view is in
[the minimizer dogfood note](selector_minimizer_dogfood.md) and
[its perf companion](selector_minimizer_perf.md); this note is
the agent-experience layer — install path, doc gaps, CLI papercuts, and what the
skill's worklist does and doesn't surface. Where a finding maps to an
already-tracked minimizer gap, it cross-references rather than re-files.

Setup used: released `debundle` binary (pin `debundle-fa51a08d1a27`), the spec's
`modules/` tree, and the upstream chunk
`tana/upstream/web/snapshots/78d928dca7/static/index-DI2GynTv.js`. No pipeline
build was needed (see F8).

## Census the skill produced (grounds everything below)

`spec stats` + `spec selector-debt` (whole spec):

| metric                                          | value       |
| ----------------------------------------------- | ----------- |
| modules / bindings                              | 1751 / 5901 |
| bindings renamed / unrenamed                    | 5901 / 0    |
| `name_only_total` (fragile name pins)           | 2198        |
| of which scored fragile (`min-score`≥70-ish)    | 2197        |
| `source_match_total`                            | 5136        |
| source-aware **near-ambiguous** structural sels | **1867**    |
| `repeated_source_match` groups                  | 2           |
| binding-group collapse suggestions              | 2 (5 sels)  |

Name-pin fragility by score: 70→1024, 85→473, 90→694, 100→6. By layer:
`features` 1046, `domains` 511, `app` 452, `shared` 106, `integrations` 49,
`infra` 33. Worst single module: `app/bootstrap/initBundle` (172 pins, all score
100 — the fused at-init megamodule).

## Friction

### F1 — The skill was not installed; `/debundle_stabilize` was unavailable (HIGH)

At session start the skill was absent from the agent's skill registry (the other
7 `debundle_*` skills were present). Root cause: **pin drift on the persistent
web rootfs**, not a packaging bug. The skill _is_ registered in
`//skills:all_skills` and _is_ present in the current pinned artifact
(`skills-9fe10fd1a745`, the latest `skills-*` release). But the installed
`devtools` profile on disk dated to 2026-06-04, while the working-tree pin had
advanced to 2026-06-17 (which includes the skill, added in #2332). The deployed
skill set is gated on the CI release artifact + `nix/artifact-pins.json`, so a
session whose profile predates the pin bump silently lacks newer skills.

Fix that worked: `nix profile remove devtools && nix profile install
path:.#devtools` then re-symlink `…/share/claude-hooks/skills/*` into
`~/.claude/skills/` (exactly `web_setup.sh` steps 2+4). After that the skill
loaded cleanly, transclusions and all.

- Gotcha: the slash-command/skill registry is snapshotted at **session start**,
  so the reinstall makes the skill available to _new_ sessions; the in-flight
  session kept working from the `SKILL.md` directly.
- This is the documented "Pin drift on persistent rootfs" failure
  (<../../../claude/docs/web-setup-debug.md>); worth a one-line pointer from the
  skills README that "skill missing despite being in `all_skills`" ⇒ stale
  profile pin, reinstall.

### F2 — `SKILL.md` "Background" calls `--candidates N` "still to come" — it landed (LOW, stale doc)

The skill's Background says the "one planned affordance still to come is
`synthesize-selectors --candidates N`". It shipped in #2341 (wired in
`cli/mod.rs`, `selector_codemod.rs`). The commit message even notes "+ doc
drift". The skill body _does_ tell you to use `--candidates N` in step 2, so the
Background paragraph just contradicts the body. Delete the stale sentence.

### F3 — `cli.md` does not document `match-selector` (MEDIUM, doc gap)

`match-selector` is the skill loop's **proof** step (step 4) and its only
interactive "what does this candidate bind?" probe, but it is absent from
`docs/cli.md`'s Spec-wide table (which lists `stats`, `selector-debt`,
`selector-codemod`, `synthesize-selectors`). It is fully implemented and has
`--help`. A reader working from `cli.md` would not know the command exists. Add
the row.

### F4 — `--candidates 3` returned a single candidate for a class pin (MEDIUM)

The skill leans on `--candidates N` to get "a ranked **menu** of alternative
anchor choices … then choose the most purpose-bearing one." On a real fragile
class pin (`domains/graph/metaNode:CardsViewAccessor`, minified `Uee`),
`synthesize-selectors --candidates 3` returned `candidate_count: 1` — no menu,
no alternatives. So for at least the bare-`class X extends Y {}` shape the menu
the skill's judgment hinges on is effectively empty; the agent gets the
minimizer's single pick or nothing. Either the menu doesn't enumerate
alternatives for this shape yet, or there genuinely is only one — but the output
doesn't say which, so the agent can't tell "menu of 1" from "no better option
exists." Worth surfacing the distinction in the JSON.

### F5 — Minimizer over-pinned a neighbor body instead of leaving honest debt (MEDIUM; corroborates a known gap)

`CardsViewAccessor` is `class Uee extends Ye {}` — an **empty class body**, so it
has no internal purpose-anchor. `synthesize-selectors` produced:

```
function M2e(n, e) { n.createMetaNodeIfMissing(); const t = n.metaNode.getOrCreateTupleByAttributeId(m.childTemplatesAttrId); t.tupleValue = e; }
class CardsViewAccessor extends ANYTHING { ANYTHING; }
```

i.e. it manufactured uniqueness by pinning an **unrelated preceding function's
exact body** (`M2e`). Confirmed via `match-selector` that the honest selector
`class CardsViewAccessor extends ANYTHING {}` is **not unique** (matches `Uee`
_and_ `pN`, another empty subclass) — which is _why_ the minimizer reached
outward. Per the skill this is a step-6 "leave honest debt" case (keep the name
pin + comment), and the synthesized selector should be rejected by the agent.

This is the consumer-side analogue of the already-tracked
`neighbor_context_whole_function_neighbor` over-pin (#2315 picks the right stable
neighbor but pins a _function declaration_ body whole; 46/62 hard over-pins in
[the minimizer dogfood note](selector_minimizer_dogfood.md)). New twist worth noting: here the target has
**no body of its own**, so no amount of interior-cover work helps — the only
correct outcomes are (a) emit no selector and report "no internal anchor", or (b)
anchor on a _stable readable_ superclass name if one exists. Quietly pinning a
neighbor is the worst option because it reads as a clean conversion. Suggest the
minimizer treat "uniqueness came entirely from a non-target neighbor" as a
skip-with-reason, not a `would_change`.

### F6 — `--identifiers` value spelling differs from the spec/docs (LOW, papercut)

Spec YAML and every `selectors.md`/`SKILL.md` example use `identifiers:
alpha_all` (underscore). The `match-selector` CLI flag rejects that:
`--identifiers alpha_all` errors; it wants `alpha-all` (hyphen). The CLI prints a
helpful "did you mean 'alpha-all'" hint, but an agent copying the doc spelling
hits the error first. Either accept both spellings on the flag, or note the
hyphen form in the skill where it first shows a `match-selector` invocation.

### F7 — The default worklist hides the larger fragility surface (MEDIUM, methodology)

The skill's worklist starts at `selector-debt --min-score 70` (name-only). That
surfaces 2197 pins. But the **source-aware** pass (`--source-file`) reports
**1867 near-ambiguous** `source_match` selectors — selectors that resolve
uniquely today but have high-scoring sibling statements, i.e. one upstream edit
from ambiguous. That population is ~85% the size of the name-pin backlog and is
the skill's own stated "blind spot," yet a reader following the worklist top-down
won't see it until they think to add `--source-file`. The skill should make the
source-aware `selector-debt` run a first-class worklist step, not a footnote —
the real backlog is "2197 name pins + ~1867 near-ambiguous structural," and the
second half is invisible by default. (Whole-spec source-aware run: **77s**, ~2.9
MB JSON — cheap enough to be routine.)

### F8 — "Setup: build the debundler" overstates what the loop needs (LOW)

The skill's Setup says "Build the debundler and export … `DEBUNDLE_GRAPH` …". For
the stabilize loop none of `selector-debt` / `synthesize-selectors` /
`match-selector` / `validate` need the owner graph or a pipeline build — they
read the **chunk** directly (`--source-file`/`--source-root`+`--chunk`) plus the
`modules/` tree. A pinned released binary + the in-repo upstream snapshot is the
whole toolchain. Only the _planning_ commands (`describe`, `show-source`,
`cluster`, `modules propose`) need `DEBUNDLE_GRAPH`. Calling this out would save
a consumer an unnecessary (heavy, RBE) pipeline build.

### F9 — Output JSON shapes are undocumented; field names had to be probed (LOW)

`selector-debt` / `synthesize-selectors` / `match-selector` JSON keys aren't in
`cli.md`, so each query needed a `jq 'keys'` probe first (e.g. group rows use
`name_only_count`, not `count`; `match-selector` uses `unique` + `matches[]` with
`{body_index, binding_name}`; synthesize nests under `candidates[].match_source`).
A short "output schema" stub per command — or just one example object each — would
remove the guesswork.

## What worked well

- Released-binary path: fetch pin, run `spec *` against the spec tree — zero build.
- `match-selector` is exactly the right probe: `unique` + the colliding
  `matches[]` made the `CardsViewAccessor` ambiguity self-evident in one call.
- Source-aware `selector-debt` at 77s/whole-spec is fast enough for routine use.
- The skill's central thesis held up live: the minimizer _did_ pick an incidental
  anchor, and the skill's "discard incidental anchors / leave honest debt"
  guidance was exactly what the situation needed.

## Suggested fixes, prioritized

1. (F1) Skills README: note "skill in `all_skills` but missing at runtime ⇒ stale
   `devtools`/`skills` pin on persistent rootfs; reinstall."
2. (F3) Add `match-selector` to `cli.md`.
3. (F7) Promote source-aware `selector-debt` to a first-class worklist step in
   `SKILL.md`; state both halves of the backlog.
4. (F5) Minimizer: when uniqueness derives only from a non-target neighbor, skip
   with reason instead of `would_change`.
5. (F2) Delete the stale `--candidates` "still to come" sentence in the Background.
6. (F4/F6/F9) `--candidates` "menu of 1 vs none" signal; accept `alpha_all` on the
   flag (or doc the hyphen); add per-command output-schema examples.

## Applied this session

- **F2 fixed**: deleted the stale "`--candidates N` still to come" sentence in the
  `SKILL.md` Background.
- **Minimizer added to the toolkit** (per user request): new `## The toolkit`
  section in `SKILL.md` positions `synthesize-selectors` as a first-class
  instrument — first-pass converter for the easy majority and compaction pass over a
  hand-picked anchor — while spelling out that it has **no semantic intelligence**
  (it cannot tell a readable anchor from an accidental but-unique token; that
  judgment is the agent's and is not readable off the AST). This also folds in F3
  (names `match-selector`), F7 (source-aware census as first-class), and F8 (no
  pipeline build needed for the loop).
- **Still open** (not yet done, need owner sign-off / separate PRs): F1 skills-README
  pin-drift note, F3 `cli.md` `match-selector` row, F4/F5 minimizer behavior
  (skip-with-reason on neighbor-only uniqueness; menu-of-1 signal), F6 `alpha_all`
  flag spelling, F9 output-schema docs.

## Pass 1 — running a real pass on `domains/graph/metaNode`

Ran one full pass (23 name pins). Outcome: **5 converted** to forward-compatible
`source_match` (4 accepted from the minimizer on own-identity anchors —
`addMeetingBotCommand`/`startAiChatCommandHandler` own message strings,
`sortNodeByViewSortSpec` `.sortBySortSpecification`, `canRunMeetingClassificationCommands`
`eventClassificationConfig`; 1 hand-authored — `recordHomeNodeAttributeUsage` onto its
own `throw new Error("Could not find workspace attribute definition")`, proven unique).
**18 left as honest debt**: 12 TS codegen helpers, 3 anchorless re-export aliases, 2 bare
delegators, 1 empty class. ~22% convertible is the _correct_ answer here — this module is
anchor-poor (mostly mobx-decorated accessors + their helpers/aliases), not a pass failure.

### F10 — `synthesize-selectors --apply` canonicalizes the whole YAML; review only after prettier (MEDIUM)

`--apply` of 4 selectors produced a **2051-line diff** (whole file rewritten in the
debundler's canonical 0-space form). Running gaffer's prettier reconciled it to **46/11
— exactly the 4 semantic changes**. So the gaffer-AGENTS "prettier reconciles" claim
holds _fully_, but a `git diff` taken before prettier is unreviewable noise. The skill /
gaffer workflow should state: **run the repo formatter immediately after `--apply`,
before reading the diff.**

### F11 — the pipeline gate needs Bazel; only the per-selector loop runs binary-only (MEDIUM; refines F8)

`spec validate` / `debundle run` (the realizability + cycle gate) can't run standalone
from the released binary here: (a) `--tree-source-root` must be the **repo root**, not the
snapshot dir — `spec_config` paths are repo-root-relative, and passing the snapshot dir
doubles the path; (b) it then needs Bazel-provided **vendor package trees** (`mermaid`, …)
via runfiles (`Could not locate Bazel-provided package tree; pass packagesRoot`). So:
selector-debt/synthesize/match-selector = binary-only (great); the gate = the Bazel
`:debundle` target. (Also: `--server_javabase` is a **startup** option — must precede
`build`, not follow it; the gate recipe should show placement.)

### F12 — 5 of 9 minimizer conversions were neighbor-borrows, and line-count didn't catch them (MEDIUM; reinforces F5)

Of the 9 `would_change` candidates, **5 manufactured uniqueness from an adjacent unrelated
declaration**, with the target's own body fully holed: `CardsViewAccessor`←`M2e`,
`recordHomeNodeAttributeUsage`←a 30-line neighbor, `isMeetingTranscriptionProvider`←the
`SystemCommandFailed` error class, `calendarViewAccessor`/`NavigationStackAccessor_export`←
adjacent `decorate(…, "<method>")` statements. The `>40-line` over-pin heuristic flagged
**none** of them (they were 33/8/2/2 lines). Neighbor-borrow is a _semantic_ property
(uniqueness contributed only by a non-target node), not a size one — the detector in F5
must key on that, not length. For 1 of the 5 (`recordHomeNodeAttributeUsage`) the target
_did_ have its own anchor the minimizer ignored (the error string); the other 4 had none.

### F13 — duplicated TS codegen helpers are structurally un-pinnable; carve them out (MEDIUM)

**12 of 23** metaNode pins are `__decorate` / `__defineProperty` /
`__getOwnPropertyDescriptor` helper aliases. All 12 skip with "no sparse selector" —
correctly: the bundler emits an identical copy per module, so nothing but the minified
name distinguishes them. Selector authoring _cannot_ fix these; they need debundler
helper-recognition (the existing `effect: typescript_decorate_helper` annotation shows
partial awareness). The skill should explicitly scope these out ("not your job — leave as
name-pin debt; tracked as a helper-recognition tooling item") so an agent doesn't burn
effort hunting anchors that can't exist.

### F14 — honest-debt comments go stale when tooling catches up (LOW; worklist gap)

`startAiChatCommandHandler` carried a comment: blocked on "matching one string-literal
declarator inside a mixed multi-declarator declaration." That DECLARATORS support has since
landed, and the minimizer now converts it cleanly (the comment was obsolete). A pass must
**re-evaluate existing commented debt against current tooling**, not only the bare name
pins — the worklist's `selector-debt` census lists the name pin but not "is its blocker
still real." Add a re-check step.

### Skill-update candidates from this pass

- worklist: add "run the repo formatter right after `--apply`, before reviewing" (F10);
- playbook: add a "duplicated codegen helpers → honest debt, not your job" case (F13);
- worklist: add "re-check commented debt — tooling may have caught up" (F14).
