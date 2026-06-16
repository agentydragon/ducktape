# Tree automata & bottom-up tree pattern matching

Research-spike strand (2026-06-16). Verbatim sub-agent report. Synthesis in
<../readoff_algorithm_research.md>.

## 1. Hoffmann & O'Donnell (JACM 1982): bottom-up tree pattern matching

Christoph M. Hoffmann, Michael J. O'Donnell, "Pattern Matching in Trees," JACM
29(1):68–95, 1982. DOI 10.1145/322290.322295.

- Patterns are trees over a ranked alphabet with a wildcard `v`; "simple
  patterns" are **linear** (no repeated-variable equality constraint).
- Central object: the **match set** at a subject node = the set of pattern
  subtrees matching there. Computed bottom-up: at node `f` with children labeled
  match sets `m_1..m_k`, `table_f(m_1,…,m_k) → m`. A pattern matches at a node iff
  its root subpattern is in that node's match set.
- This is a **deterministic bottom-up tree automaton** whose states are match
  sets.
- **Matching:** O(n) table lookups for a subject of n nodes — **linear in the
  subject, independent of pattern-set size at match time.**
- **Preprocessing can be exponential** in the size/number of patterns (number of
  subsumption-closed match sets; per-operator table indexed by tuples of states).
- **Read-off:** at each node read its match-set label / a precomputed bitvector
  of which patterns fire — O(1) per (node, pattern), no re-traversal.

Follow-up taming the blow-up (not eliminating it):

- Chase, "An Improvement to Bottom-up Tree Pattern Matching," POPL 1987 —
  compresses transition tables; worst case stays exponential.
- Cai, Paige, Tarjan, "More Efficient Bottom-up Multi-pattern Matching in
  Trees," TCS 106(1):21–60, 1992 — asymptotic improvements, ~10× on hardest
  instances; no polynomial worst-case guarantee.

Industrial application — **instruction selection / BURS**: Pelegrí-Llopart &
Graham (POPL 1988); Proebsting, "BURS Automata Generation," TOPLAS 1995; survey
Hjort Blindell arXiv:1306.4898.

Mapping:

- _AST subtrees:_ direct; subject pass labels every node in O(N).
- _Alpha-equivalence:_ a single wildcard `v` for "any identifier leaf" is
  Hoffmann–O'Donnell's variable — native, linear. **Non-linear** matching (two
  holes must bind the _same_ identifier) is harder (Ramesh & Ramakrishnan,
  "Nonlinear Pattern Matching in Trees," JACM 39(2):295–316, 1992); fix by
  linear match + equality post-check, or canonicalize identifiers first (§4).
- _Variable-length list holes:_ the real impedance mismatch — Hoffmann–O'Donnell
  is over a **ranked** alphabet. Need **unranked/hedge automata** (§2) or
  cons-spine binarization.
- _Minimality / stable-anchor bias:_ not addressed — the automaton tells you
  _which_ patterns match, not _which is minimal_ (that is §3).

## 2. TATA — Tree Automata Techniques and Applications (Comon et al., 2008)

Free book; mirrors at eecs.harvard.edu and pages.di.unipi.it.

Chapter 1 essentials:

- **Recognizable = regular tree languages**, recognized by finite tree automata
  (bottom-up or top-down).
- **Determinization:** every NFTA has an equivalent DFTA via subset construction,
  **worst-case exponential** in #states. Deterministic top-down is strictly
  weaker — rely on bottom-up determinism.
- **Closure:** under union, intersection, complement, hence **difference**.
  Complement needs a complete deterministic automaton (⇒ determinization blow-up
  if input is nondeterministic).
- **Emptiness:** decidable by reachability fixpoint in **linear time O(|A|)**;
  PTIME for DFTAs. Pumping lemma holds.
- **Equivalence/inclusion:** decidable but **DEXPTIME-complete** in general
  (PSPACE-complete for finite languages).
- **Unranked extension (Chapter 8 / hedge automata):** the principled home for
  `ARGS`/`STMT_LIST` — child-state sequences described by a regular language, so
  a "list hole" becomes a regex over child states.

Corroborating text-extractable sources: Lammich TUM notes; "An Efficient Finite
Tree Automata Library" arXiv:1204.3240; congruence perspective arXiv:2104.11453.

## 3. Is "all hole-patterns matching subtree s" a regular tree language? Minimal distinguisher = language difference?

**Yes, `L_s` is regular — caveat on the hole language.** For a fixed subject `s`
and **linear patterns**, the set of patterns matching `s` is a regular tree
language `L_s` over `Σ ∪ {v}`: roughly `s` with any antichain of complete
subtrees replaced by `v`. Recognizable by a DFTA with O(|s|) states, built in
linear time. (The Hoffmann–O'Donnell match set at `s`'s root is a finite
description of `L_s ∩ indexed-patterns`.) Finitely many equivalence classes =
the Myhill–Nerode/congruence property.

**Caveat — nonlinearity.** If holes must bind equal subtrees (`f(x,x)`), the set
of matched _ground terms_ is **not regular** (pumping lemma). Fix: canonicalize
(hash-cons / alpha-normalize, §4) so equality = identity, staying regular.

**Minimal distinguisher as language difference:**

```
Distinguishers(t) = L_t \ ⋃_{u ≠ t, u indexed} L_u
```

regular (closed under ∩, complement). "Minimal" = the minimum-cost member under
a preference order (fewest concrete nodes / most wildcards, stability-biased) —
a weighted-emptiness / best-tree DP over the automaton (polynomial in its size).
"Is `{t}` distinguished only by the fully-concrete pattern?" is an
emptiness/inclusion check.

**Where the blow-up lives:** each `L_s` is linear in `|s|`; the **product /
intersection over many indexed `u`** is O(∏|L_u|) — exponential in the number of
indexed items if done monolithically (same explosion as the match-set universe).
Equivalence/inclusion underlying "unique distinguisher" is DEXPTIME-complete in
the worst case (polynomial for the deterministic linear-hole automata in
practice). **Mitigation:** don't materialize the global product — reuse one
shared match-set automaton, turning "distinguish t from everyone" into membership
queries against a single automaton, not k pairwise differences.

## 4. Bottom-up subtree hashing / Merkle identity / hash-consing / DAG compression

Hash-consing: bottom-up `id(node)=H(op ‖ child-ids)`; structurally identical
subtrees collapse; subtree equality = O(1) id comparison; whole tree becomes a
maximally-shared DAG. Merkle phrasing identical. Linear-time foundation: Downey,
Sethi, Tarjan, JACM 1980 (O(n log n) general congruence closure; linear special
cases incl. bottom-up subtree identity).

Beyond rooted-subtree sharing: top-tree compression (Bille et al., Inf. Comput.
2015; arXiv:1304.5702) exploits internal repeats, can beat DAG exponentially,
O(log n) navigation.

Mapping:

- AST subtrees / equal shapes share one id: exactly hash-consing, O(N).
- Alpha-equivalence: fold into the digest (normalize minified ids to a canonical
  token / De Bruijn) ⇒ alpha-equivalent subtrees hash-cons to the same id in
  O(N), and **restores regularity** for §3 (equality-of-holes = identity-of-ids).
- List holes: hash-cons over the same binarized cons-spine the matcher uses.
- Minimality / stable anchors: hashing gives **per-shape frequency** for free
  (count collisions per id) — the stable-anchor / selectivity signal; weight the
  §3 cost function by these counts.

## 5. XML/XPath twig-query indexing (brief analog)

TwigStack (Bruno, Koudas, Srivastava, SIGMOD 2002): stack-chains encoding
partial path matches; I/O- and CPU-optimal for ancestor-descendant twigs; weak on
parent-child (spurious intermediate results). Index-accelerated variants
(TwigX-Guide, C-Tree). **Borrow:** holistic stack-based linear composition for
list holes; region/Dewey encoding for O(1) ancestor/depth tests. **Don't import**
the parent-child intermediate-result pathology (axis semantics we don't have).

## Bottom line on the O(N) aspiration

- **Per-subject matching is genuinely O(N)** (match-set lookups; hash-cons
  identity), given the tables/automata.
- **The exponential lives entirely in preprocessing** (match-set universe;
  equivalently the intersection/complement product over items; DEXPTIME
  equivalence worst case).
- **It need not kill O(N) if** you (i) hash-cons/alpha-normalize first (equality
  = identity ⇒ regular), (ii) use one shared deterministic match-set automaton
  instead of k pairwise differences, (iii) use hedge automata or principled
  binarization for list holes. The exponential is then bounded by pattern/index
  structure, paid once, far below worst case in practice; the subject pass and
  per-target read-off stay linear.

## Sources

- Hoffmann & O'Donnell JACM 1982: https://doi.org/10.1145/322290.322295
- Chase POPL 1987: https://dl.acm.org/doi/10.1145/41625.41640
- Cai, Paige, Tarjan TCS 1992: https://scholarsmine.mst.edu/math_stat_facwork/106/
- Ramesh & Ramakrishnan, Nonlinear Pattern Matching, JACM 1992:
  https://doi.org/10.1145/128749.128752
- BURS: Proebsting TOPLAS 1995 https://dl.acm.org/doi/10.1145/203095.203098 ;
  survey https://arxiv.org/pdf/1306.4898
- TATA: http://tata.gforge.inria.fr/ ;
  https://www.eecs.harvard.edu/~shieber/Projects/Transducers/Papers/comon-tata.pdf
- Tree-automata facts: https://www21.in.tum.de/~lammich/2015_SS_Automata2/slides/handout.pdf ;
  https://arxiv.org/pdf/1204.3240 ; https://arxiv.org/pdf/2104.11453
- Downey, Sethi, Tarjan JACM 1980: https://dl.acm.org/doi/10.1145/322217.322228
- Hash-consing: https://en.wikipedia.org/wiki/Hash_consing ; https://arxiv.org/pdf/2509.20534
- Top-tree compression: https://arxiv.org/abs/1304.5702 ;
  https://www.sciencedirect.com/science/article/pii/S0890540114001643
- XML twig: https://www.ijert.org/xml-twig-pattern-matching-algorithms-and-query-processing

**Verification caveats:** TATA PDF mirrors are image-encoded (structural/
complexity claims corroborated via Lammich notes, the FTA-library paper, the
congruence-perspective paper, not direct quotation). The Hoffmann–O'Donnell
exponential-preprocessing characterization is established (motivation for Chase
1987 / Cai–Paige–Tarjan 1992). The "`L_s` regular / minimal distinguisher =
language difference" framing in §3 is sound synthesis built on the closure-and-
emptiness machinery + match-set-as-DFTA equivalence, not a single cited theorem.
