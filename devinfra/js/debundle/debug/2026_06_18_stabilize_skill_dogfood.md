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
build was needed (the per-selector loop is binary-only; only the whole-spec gate
needs the Bazel pipeline).

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

_Doc/skill-fixable findings from this log (F1, F3, F6–F8, F10–F11, F13–F14) were
actioned into `docs/cli.md` and `skills/debundle_stabilize/SKILL.md` on 2026-06-21
and removed here; the entries that remain need minimizer **code** changes (or, for
F9, fuller output-schema docs)._

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

### F9 — Output JSON shapes are undocumented; field names had to be probed (LOW)

`selector-debt` / `synthesize-selectors` / `match-selector` JSON keys aren't in
`cli.md`, so each query needed a `jq 'keys'` probe first (e.g. group rows use
`name_only_count`, not `count`; `match-selector` uses `unique` + `matches[]` with
`{body_index, binding_name}`; synthesize nests under `candidates[].match_source`).
A short "output schema" stub per command — or just one example object each — would
remove the guesswork. (Update 2026-06-21: `match-selector`'s output fields now live
in its `cli.md` row; `selector-debt` / `synthesize-selectors` keys are still
undocumented.)

## What worked well

- Released-binary path: fetch pin, run `spec *` against the spec tree — zero build.
- `match-selector` is exactly the right probe: `unique` + the colliding
  `matches[]` made the `CardsViewAccessor` ambiguity self-evident in one call.
- Source-aware `selector-debt` at 77s/whole-spec is fast enough for routine use.
- The skill's central thesis held up live: the minimizer _did_ pick an incidental
  anchor, and the skill's "discard incidental anchors / leave honest debt"
  guidance was exactly what the situation needed.

## Suggested fixes, prioritized

Remaining open items — all minimizer **code** changes plus output-schema docs; the
doc/skill-fixable findings were actioned 2026-06-21 (see the note under Friction):

1. (F5/F12) Minimizer: when uniqueness derives only from a non-target neighbor,
   skip with reason instead of `would_change` — keyed on the _semantic_ property
   (uniqueness contributed only by a non-target node), not the `>40-line` size
   heuristic, which flagged none of the 5 neighbor-borrows.
2. (F4) `--candidates` "menu of 1 vs none" signal: distinguish "only one anchor
   exists" from "menu not enumerated for this shape" in the JSON.
3. (F9) Add per-command output-schema examples for `selector-debt` /
   `synthesize-selectors` (match-selector's landed in `cli.md`).

## Further friction (surfaced during a real conversion pass)

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
