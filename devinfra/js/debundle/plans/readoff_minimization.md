# Plan: read-off selector minimization

Status: active (agreed 2026-06-16). Supersedes the search-based cover as the
target architecture. Tracks the redesign of the debundle selector minimizer
around a single chunk-wide AST-shape index.

## Motivation

`debundle` reverse-engineers minified JS into named-module specs. Members pin a
runtime entity either by a fragile `binding.name` (a minified name that churns
every rebuild) or by a robust, alpha-equivalent `source_match` selector (holes +
kept anchors). The minimizer's job: mechanically turn name-pins into the
**sparsest `source_match` (or `binding_group`) that still uniquely and robustly
identifies the target** among its chunk siblings — discrimination _and_
meaningful value pinning, holes interleaved at every nesting level.

Today's minimizer **searches**: per member it generates candidate anchors,
renders selectors, and runs the matcher to test discrimination (now
index-prefiltered, #2254). That is per-member work with a cover search inside.
Whole-chunk (~7 MB, ~4k members) runs in ~113 s and three spec shapes still
over-pin (large objects keep all keys, large sibling classes dump full bodies,
some grouped objects keep every key).

## Thesis: read off, don't search

Build **one inverted feature index over AST shapes per chunk**, then read the
minimal selector off it instead of searching.

- **One pass, O(N):** walk every subtree once; at each node emit position-aware,
  **alpha-equivalent** shape features (minified identifiers holed; stable things
  kept): shallow literal values, object keys, member/method names, callee
  identities, bounded-depth shape skeletons.
- **Posting lists:** feature → set of items (bindings / top-level statements)
  exhibiting it. This is the "pseudo-trie generalized to AST trees": instead of
  indexing strings by character prefix, index subtrees by structural prefix
  (path-from-root + shape); the leaves are the items sharing that shape.
- **Selector = conjunction of features**, whose match set is the **intersection
  of posting lists**. A _minimal_ selector for a target is the smallest subset of
  the target's own features whose intersection is the singleton `{target}`.
- **Read-off:** scan the target's features (O(target size)), rank by **selective
  × stable**, take the most selective+stable; if its posting list is already
  `{target}`, done in one anchor. Most real code has such a feature, so the
  common case is a read-off, not a search.
- **Tail:** when no small stable combination is singleton, fall to a _bounded_
  intersection over the target's few features (greedy-near-optimal via
  selectivity ranking). Never an unbounded chunk-wide search.
- **Prove-gate stays:** the production matcher confirms the read-off selector
  resolves uniquely (gate 1). It is the correctness gate, never the search
  engine.

Net cost: `O(N + Σ target sizes) ≈ O(M+N)` — linear in chunk + spec size.

### Two things this makes principled, not bolted-on

- **Grouping overlapping captures.** Targets that would get near-identical
  selectors co-occur in the same posting lists — a lookup, not a heuristic.
  Group when targets **share an enclosing declaration OR their minimal selectors
  overlap beyond a threshold**, emitting a `binding_group` (with `DECLARATORS`
  holes) instead of N overlapping standalone selectors.
- **Forward-compatibility.** Feature ranking is two-key — **selective × stable**.
  Prefer semantic literals, exported names, structural shape; deprioritize
  minified names and volatile hashes (hole them, or regex-anchor a stable prefix
  per the `STR_LITERAL_MATCHING_RE` path). Minimal + stable = survives rebuilds,
  which is the higher goal: auto-minimized specs that are forward-compatible.

### Honest caveats

Read-off is linear in the _common_ case; the tail (no singleton feature) is
bounded set-cover, and the per-item prove-gate has real cost. Index build emits
several features per node and holds posting lists for a 7 MB chunk — watch
constant factors and memory. `≤10s` is plausible with this structure, not free:
a **size-sweep benchmark** measures the real slope (time vs members across
scopes; build ~constant per chunk, per-item ~constant) rather than asserting
linearity.

## Goal & acceptance criteria

**Read-off minimization.** Rebuild the minimizer around one chunk-wide AST-shape
index so minimal, forward-compatible selectors are read off rather than searched.

1. **Perf** — whole ~7 MB / ~4k-member spec minimizes in **≤10 s ideal, ≤30 s
   hard** (now 113 s); linearity shown by the size-sweep.
2. **Minimality** — emitted selectors are minimal-or-near (metric: retained
   AST-node count vs the read-off lower bound). **Hard rule: never dump an
   untrimmed AST.** `--full-ast-fallback` stays off by default; a run _reports_
   any member it could not minimize instead of dumping the full AST.
3. **No overlapping captures** — zero large-overlap standalone selector pairs on
   the dogfood spec; such cases become `binding_group`s.
4. **Coverage** — var (single + group), function, class, object, including the
   large-object / large-class cases that currently over-pin. The aspirational
   E2E cases (`object_keys_over_pinned`, `class_among_many_siblings`, grouped
   large object) get unignored as capabilities land.
5. **Forward-compat** — stable-anchor preference, validated by a
   rebuild-perturbation test (perturb volatile fragments → selector still
   resolves).
6. **Code health** — one unified minimization path, no double implementations;
   the old per-form cover search is deleted once everything routes through
   read-off. STYLE-clean.
7. **gaffer-private** — apply across the whole spec, replacing fragile
   `binding.name` pins and exact-minified-name pins; commit; quantify the
   fragile-pin reduction.

**Non-goals / accepted imperfections:** perfect cardinality-minimality on
adversarial tail cases (near-minimal is fine); preserving YAML comments (out via
serde); embedded/non-trailing volatility in regex anchors (future).

## Decisions (agreed 2026-06-16)

- **Migration: strangler-fig.** Build the index + read-off path alongside the
  current minimizer; migrate one spec form at a time keeping all tests green;
  delete the old search only once everything routes through read-off. Every PR
  is independently landable.
- **Grouping trigger:** shared enclosing declaration OR minimal-selector overlap
  beyond a threshold.
- **Runtime:** ≤10 s ideal, ≤30 s hard commit; optimize toward 10 s and report
  the real number.
- **Old pins:** replace fragile gaffer-private pins in the same wave as each
  scope re-minimizes cleanly.

## Validated architecture & locked decisions (W0 research spike — done 2026-06-16)

The literature spike (<readoff_algorithm_research.md> + five strand reports in
<readoff_research/>) is complete and confirms the **three-layer architecture**:

1. **Canonicalization + shape index (O(N), build now).** Hash-consed Merkle DAG
   (Downey-Sethi-Tarjan) with alpha-leaf canonicalization; multi-granularity
   shape features; inverted posting lists; selectivity (free byproduct) +
   stability scores. Supersets `SelectorCandidateIndex`.
2. **Read-off minimization (greedy set-cover, two-key `selective × stable`).**
   `OPT=1` read-off in the Zipfian common case; bounded greedy tail otherwise.
   Minimization reduces _exactly_ to Minimum Set Cover (NP-hard, W[2]-complete);
   greedy is provably near-optimal, so "mostly minimal" is the correct target,
   not a compromise. The production matcher is the prove-gate, never the engine.
3. **Grouping (n-ary anti-unification / LGG).** Linear; co-occurrence in posting
   lists detects overlap. (Wave 2.)

**Locked decisions (confirmed before W1):**

1. **List-hole encoding = cons-spine binarization + bounded-depth shape
   skeletons, behind a swappable feature-extraction interface.** No arity
   assumption is baked into the index or greedy core; all variadic handling
   lives behind the `ShapeFeatureExtractor` trait (default `ConsSpineExtractor`)
   and the matcher-verify boundary, so it can later be swapped for hedge
   automata (TATA ch. 8) _iff_ deep list-body matching proves load-bearing.
2. **Build the full Layer-1 index now** (not just a measurement spike), and emit
   the OPT / read-off-depth distribution as part of the benchmark.

## Work waves

- **W1 — Index foundation (DONE 2026-06-16).** Generalized AST-shape index as a
  superset of `SelectorCandidateIndex`: multi-granularity, alpha-equivalent,
  stability-ranked features with posting lists; `minimal_anchor_set(item)`
  read-off API; size-sweep + OPT-distribution benchmark. Shipped as
  `shape_index.rs` (lib), `shape_index_soundness_test.rs` (matcher-backed
  soundness gate), `shape_index_bench.rs` (synthetic sweep). Additive
  (strangler-fig): the existing minimizer's decision path is unchanged and all
  prior tests stay green. **Benchmark result (synthetic, N=200/1000/4000):**
  build is linear (~0.034-0.051 ms/item; ~1.27 distinct shapes/item); among
  resolvable items the OPT=1 share is **100%** — strongly validating the
  Zipfian `OPT=1`-majority assumption that underwrites the near-linear /
  `≤10s`-ideal target. Caveat surfaced for W2: items that are genuinely
  alpha-duplicates return `None` ("unminimizable", honest) and currently pay an
  O(N) greedy-universe allocation each — cheap to bound, but flag it.
- **W2 — Read-off minimizer + grouping.** Route forms through read-off behind the
  existing tests (strangler-fig); principled grouping from feature co-occurrence;
  begin deleting the bespoke cover search.
- **W3 — Tail + over-pin elimination.** Bounded cover for non-singleton cases;
  large-object → `OBJECT_PROPS` + discriminator, large-class → `CLASS_REST` +
  member. Unignore the E2E cases. Finish deleting the old search.
- **W4 — Whole-spec apply + validation.** Apply across gaffer-private, replace
  fragile pins, validate ≤10/30 s + linearity, update the dogfood note.

## Execution model (PM)

Foundational worker (W1) runs first; later waves stack and parallelize where
independent, each worker in its own worktree + output base (disk-aware, one heavy
build at a time). Coordinator reviews every diff, re-validates on RBE, opens and
**subscribes to each PR immediately**, lands as mergeable, rebases the stack on
each merge, and unignores E2E cases progressively. Build/test recipe: `bazelisk`

- session bazelrc + system Java + RBE (not `bbr`, whose git-mirroring is broken
  in this environment).

## Progress log

- 2026-06-16: #2253 (unify + serde apply), #2254 (index-prefilter + regex-literal
  anchors, folding #2255) landed on `devel`. Whole-chunk run 113 s; apply
  transactional; regex anchors firing. Plan agreed; W1 dispatched.
- 2026-06-16: W0 research spike landed (#2257). W1 built on
  `claude/debundle-shape-index`: Layer-1 shape index + read-off API + soundness
  test + benchmark, additive/behavior-preserving. OPT=1 share 100% on synthetic
  resolvable items; build linear. Ready for W2 (route forms through read-off).
