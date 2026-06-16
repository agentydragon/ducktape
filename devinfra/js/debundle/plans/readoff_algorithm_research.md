# Research: the read-off minimization algorithm (de-risking spike)

Status: complete (2026-06-16). Inputs: five independent literature strands
(term indexing; tree automata & bottom-up tree pattern matching;
anti-unification / LGG; subtree mining & tree compression; minimal keys /
teaching-set / set-cover). The five full strand reports with citations live in
<readoff_research/>. This note is the synthesis + recommendation that gates
Wave 1 of <readoff_minimization.md>. It pressure-tests the "read off, not
search, in O(M+N)" thesis and tells us what is safe to build now vs. what must
stay behind a swappable interface.

## Problem, restated formally

Chunk parses to a JS AST. Items `I` = top-level statements / declared bindings.
A selector is a tree pattern in the `source_match` hole language `L` (typed
holes `EXPR`/`STMT`, variadic list holes `ARGS`/`STMT_LIST`/`OBJECT_PROPS`/
`DECLARATORS`/`CLASS_REST`, `ANYTHING`, and the `STR_LITERAL_MATCHING_RE`
predicate), matched by structural tree matching modulo holes and
alpha-equivalence (minified identifiers are wildcards; stable
literals/keys/exported names are concrete). For a target `t` we want the
**minimal, stability-biased selector matching exactly `{t}`**, plus a principled
**grouping** of targets whose selectors would largely overlap.

## What the literature settles

### 1. The data structure is solved and genuinely O(N): hash-consed Merkle DAG

Bottom-up **hash-consing** (`id(node) = H(kind ‖ child-ids)`) assigns every
distinct subtree shape a canonical id in one pass; equal shapes collapse and
share automatically; subtree equality becomes O(1) id comparison. Linear-time,
textbook: Downey–Sethi–Tarjan, "Variations on the Common Subexpression Problem,"
JACM 1980. Merkle/Git/Hashlife are the same construction in practice.

- **Alpha-equivalence falls out** by canonicalizing identifier leaves to a
  wildcard sentinel (or De Bruijn index for binding-aware) _before_ hashing, so
  renamed-isomorphic subtrees hash equal. Crucially this makes equality =
  identity, which keeps us in **regular tree language** territory and avoids the
  non-regular `f(t,t)` trap (see §2).
- **Free byproduct:** per-shape multiplicity (collision count) is exactly the
  **selectivity** signal, and feeds the **stability** signal — computed in O(N).
- **AVOID** frequent/discriminative subtree mining (FREQT/TreeMiner/gSpan+CORK):
  output-exponential, NP-hard, and solves a _global corpus_ feature-selection
  problem, not our _per-target_ one-witness query. Confirmed by the
  subtree-mining strand.

### 2. Matching/verification is O(N) given the index; the exponential is only in preprocessing and is avoidable

"All hole-patterns matching a fixed subtree `s`" is a **regular tree language**
`L_s`, linear in `|s|` (Comon et al., _Tree Automata Techniques and
Applications_). Bottom-up **match-set automata** (Hoffmann–O'Donnell, "Pattern
Matching in Trees," JACM 1982; the BURS/instruction-selection lineage,
Chase POPL'87, Cai–Paige–Tarjan TCS'92) match all indexed patterns in **O(N)**
at the subject pass via table lookups. The minimal distinguisher is, in theory,
the regular-language difference `L_t \ ⋃_{u≠t} L_u`. The exponential lives in
preprocessing / the product over many items — and is **avoided** by (i)
hash-consing + alpha-normalizing first (equality = identity ⇒ stay regular,
dodge `f(t,t)`), and (ii) using one shared automaton / posting-list index rather
than materializing pairwise differences. Term indexing (Sekar–Ramakrishnan–
Voronkov, _Handbook of Automated Reasoning_ ch. 26) frames this as an
**imperfect index**: cheap candidate retrieval + an exact verification step.
**Schulz fingerprint indexing (IJCAR 2012)** is the closest prior art to our
index — fixed-length feature vectors coded `{symbol, A=var, B=below-var,
N=absent}` with sound compatibility tables, stored as a constant-depth trie so
retrieval is **leaf-union, not per-coordinate intersection** (a concrete
improvement over a naive "intersect posting lists" design).

Takeaway: our production matcher is the **verification oracle / prove-gate**;
the index narrows candidates. No index choice can yield a wrong selector.

### 3. The minimization is NOT a free read-off — it is Minimum Set Cover (and that is fine)

This is the load-bearing finding. "Smallest feature subset whose posting-list
intersection is `{t}`" reduces **exactly** to Minimum Set Cover over the
non-target items (feature `f` "covers" the non-targets it excludes). Equivalent,
under the same reduction, to **minimum key / unique-column-combination**
(Lucchesi–Osborn, JCSS 1978: NP-complete), **minimum teaching set / specifying
set / witness set** (Goldman–Kearns 1995), and the one-target **Minimum Test
Cover** (Garey–Johnson [SP6]).

- NP-hard; **W[2]-complete** in solution size; not approximable below
  `(1−ε)ln n` unless P=NP (Feige JACM'98; Dinur–Steurer STOC'14).
- **Greedy "most-selective-first"** (repeatedly take the feature excluding the
  most still-included non-targets) gives `H(d) ≤ 1 + ln d` (`d` = max
  non-targets one feature excludes), tight worst case `ln n − ln ln n + Θ(1)`
  (Slavík'97), and is **essentially the best any poly-time algorithm achieves**.
- **The common case is a true read-off.** Real selectivity is Zipfian, so a
  single highly selective+stable feature usually has posting list `{t}` already
  ⇒ `OPT=1` ⇒ greedy returns it in step 1 in **O(target)**. The search domain is
  intrinsically bounded by the target's own `≤d` features (`2^d`), **never the
  corpus** — so "bounded, not unbounded search" is rigorous.
- **Exact tail, if ever wanted:** `O*(2^d)` subset-DP, or UCC/minimal-hitting-set
  enumerators (Ducc, HyUCC, HPIValid; Fredman–Khachiyan quasi-poly) — keep in
  reserve; greedy is the default.

So the user's "mostly minimal / gets close, never dump the full AST" is **not a
compromise — it is the theoretically correct target.** Pure read-off holds for
the Zipfian majority (`OPT=1`); the tail is bounded greedy set-cover.

### 4. Grouping is solved and linear: anti-unification / LGG

The common pattern of several sibling targets is the **least general
generalization** (Plotkin 1970, Reynolds 1970): first-order syntactic AU is
_unitary_ (one canonical answer up to renaming) and **linear-time**; the n-ary
version is the order-independent lattice meet — concrete where all siblings
agree, holes where they diverge. Co-occurrence in posting lists detects the
overlap cheaply (no search). Caveat: **variadic** AST children need
**unranked/hedge anti-unification** (Kutsia–Levy–Villaret), which is _finitary_
(a small candidate set, not one). The "distinguish one from the rest" half is
NOT AU — that is the §3 set-cover problem (version-space G-boundary descent).

## The one real lock-in fork: variable-length list holes

Every strand flags the same break point. Hash-consing, match-set automata,
fingerprint/term indexing, and first-order AU **all assume fixed arity**. A hole
absorbing a 0..k-child run (`ARGS`, `STMT_LIST`, `OBJECT_PROPS`, `DECLARATORS`,
`CLASS_REST`) has no native fixed-arity encoding. Options:

- **(a) Cons-spine binarization + bounded-depth skeletons.** Encode child lists
  as right-leaning cons spines so a list hole is a wildcard over the spine tail;
  hash only the top `d` levels (variadic frontier = one wildcard child). Keeps
  everything in the ranked-tree / hash-cons / greedy world. Lowest lock-in,
  slight expressivity loss on deep list-body matching.
- **(b) Unranked / hedge automata** (TATA ch. 8) — native variadic holes
  (regular language over child-state sequences), principled but heavier and a
  bigger architectural commitment.
- **(c) Schulz `B`-code sampling** — "this position may exist in an instance";
  cheap but models one variable absorbing a fixed subtree, not a variadic run.

**Recommendation: (a), behind a `shape-feature-extraction` + `matcher-verify`
interface.** It preserves O(N) build + greedy read-off, matches how the existing
matcher already handles list holes, and the interface lets us swap to (b) hedge
automata later _iff_ deep list-body matching proves load-bearing. This is the
lock-in mitigation: **no arity assumption is baked into the index/greedy core**;
it lives only in feature extraction + verification, both swappable.

## Recommended architecture (three layers, each grounded in solved prior art)

1. **Canonicalization + shape index (O(N), low regret — build now).**
   Hash-consed Merkle DAG with alpha-leaf canonicalization; multi-granularity
   shape features (shallow literals, object keys, member/callee names,
   bounded-depth skeletons); inverted posting lists; per-shape selectivity +
   stability scores. Supersets the existing `SelectorCandidateIndex` (#2251),
   which is already a partial path-index — extend it, don't fork it.
2. **Read-off minimization (greedy set-cover, two-key ranking).** For a target,
   scan its `≤d` features ranked by **selective × stable**; if the top feature's
   posting list is `{t}`, emit it (the `OPT=1` read-off); else greedy set-cover
   over its features until the intersection is `{t}`. The production matcher
   proves the result (imperfect-index + exact-filter). Never an unbounded scan;
   never a full-AST dump.
3. **Grouping (n-ary anti-unification).** When targets share an enclosing
   declaration OR their minimal selectors overlap beyond a threshold (the agreed
   trigger), emit the LGG as a `binding_group`; hedge-AU for variadic children.

## Lock-in analysis (what's safe now vs. swappable)

- **Safe to build now (low regret):** hash-cons Merkle index + alpha-leaf
  canonicalization + inverted posting lists + greedy set-cover read-off + LGG
  grouping. All textbook, O(N) / O(target), independently validated; greedy is
  provably near-optimal so we are not betting on a heuristic that a better
  poly-time algorithm could embarrass.
- **Behind a swappable interface:** (i) the feature taxonomy (which
  shapes/granularities), (ii) the **list-hole encoding** (cons-spine now; hedge
  automata later), (iii) the exact-vs-greedy **tail solver** (greedy now;
  UCC/hitting-set enumerator later if provable minimality is ever required).
- **Invariant regardless of choices:** the production matcher is the correctness
  gate, so no design decision can emit a selector that doesn't resolve uniquely.

## Perf expectation & the validating experiment

Build O(N) (one hash-cons pass); per-target O(d) in the `OPT=1` majority,
O(d · posting) greedy tail; total ≈ O(N + M) for the common case. The ≤10s
aspiration is consistent; the costs to watch are the per-emitted-selector
prove-gate and the greedy tail. **Validating experiment for Wave 1** (before
committing to the full migration): build the hash-cons index over the real
~7MB chunk, measure (i) build time + memory, (ii) the distribution of `OPT` /
read-off depth `d` across items (validates the Zipfian `OPT=1`-majority
assumption), and (iii) per-target read-off time — a size-sweep confirming the
near-linear slope. If `OPT=1` is _not_ the majority, the tail greedy cost
dominates and we revisit before building the full minimizer.

## Key sources

- Hash-consing / min-DAG: Downey, Sethi, Tarjan, JACM 27(4), 1980.
- Tree pattern matching / automata: Hoffmann & O'Donnell, JACM 29(1), 1982;
  Chase, POPL 1987; Cai–Paige–Tarjan, TCS 106, 1992; Comon et al., TATA (2008).
- Term indexing: Sekar, Ramakrishnan & Voronkov, Handbook of Automated Reasoning
  ch. 26, 2001; McCune, JAR 9(2), 1992 (discrimination/path); Graf, RTA 1995
  (substitution trees); Schulz, IJCAR 2012 (fingerprint indexing).
- Anti-unification: Plotkin (1970), Reynolds (1970); Cerna & Kutsia survey,
  IJCAI 2023; Kutsia–Levy–Villaret (unranked/hedge AU), JAR 2014.
- Minimality core: Karp (1972); Lucchesi & Osborn, JCSS 1978 (keys NP-complete);
  Feige, JACM 1998 & Dinur–Steurer, STOC 2014 (`ln n` inapproximability);
  Chvátal, MOR 1979 & Slavík, 1997 (greedy bounds); Goldman & Kearns, JCSS 1995
  (teaching dimension); Birnick et al., PVLDB 2020 (UCC ≡ hitting-set).

## Decision needed before Wave 1

Confirm the **list-hole encoding** (recommend (a) cons-spine + bounded-depth
skeletons behind a swappable interface) and the green light to dispatch Wave 1
(build layer 1 + run the validating experiment) on this design.
