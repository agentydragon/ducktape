# Subtree mining vs. canonical subtree fingerprinting

Research-spike strand (2026-06-16). Verbatim sub-agent report. Synthesis in
<../readoff_algorithm_research.md>.

**Bottom line.** For "read off one minimal distinguishing pattern per target in
O(target size), with compact posting lists and cheap dedup of identical subtree
shapes":

- **AVOID** frequent/discriminative subtree (and subgraph) mining — a global,
  output-exponential, NP-hard-flavored enumeration/selection problem,
  mis-aligned with the local per-target task.
- **USE** bottom-up Merkle-style hash-consing / minimal-DAG compression — a
  textbook O(N) construction assigning every distinct subtree shape a canonical
  id (equal shapes share a posting list automatically), composes with
  alpha-equivalence canonicalization, exactly right for "bounded-depth shape
  skeleton hashed to a feature."

## PART A — Frequent and discriminative subtree mining (TOO HEAVY)

### A1. Frequent subtree mining

**FREQT** (Asai et al., 2002/2004) mines frequent labeled ordered tree patterns
via levelwise rightmost-extension. **TreeMiner** (Zaki, KDD 2002; TKDE 2005)
mines frequent embedded ordered subtrees with scope-lists.

Why heavy:

- **Support test NP-complete in general** (subgraph-isomorphism instance);
  polynomial only for ordered trees.
- **Combinatorial explosion**: exponentially more frequent subtrees than maximal
  ones.
- **No output-polynomial enumeration in general** (unless P=NP); none for
  maximal frequent tree mining even for rooted unordered bounded-height trees.

### A2. Discriminative / contrast subtree (subgraph) mining — wrong frame

**gSpan** (Yan & Han, ICDM 2002) min-DFS-code canonical labeling. **CORK**
(Thoma et al., SDM 2009/2010) submodular discriminativeness + greedy (1−1/e),
folded into gSpan. Applications to ASTs/CFGs (arXiv:2308.11161).

Why wrong for us:

1. Solves a **global** problem (jointly discriminate across the whole labeled
   corpus), not per-target one-witness.
2. Pays the full frequent-mining pass first (inherits A1 cost).
3. "Minimal distinguishing pattern" ≠ "discriminative feature set."
4. The (1−1/e) guarantee approximates an NP-hard global optimum — irrelevant
   overhead for a single existence witness.

## PART B — Tree compression and canonical subtree fingerprints (USE)

### B3. DAG compression / hash-consing / CSE — the right primitive

- **Downey, Sethi, Tarjan, "Variations on the Common Subexpression Problem,"
  JACM 27(4):758–771, 1980** — congruence-closure / CSE, basis of hash-consing.
- Mechanism: build nodes bottom-up; a node equals an existing one iff label +
  child-_ids_ match → return that id, else mint fresh. Identical shapes collapse.
- **Linear time:** minimal DAG of a tree computable in O(n); one bottom-up pass.
- **Merkle phrasing:** `id = hash(label, child-ids)`; equal subtrees hash equal
  (Git object model / Hashlife).
- Scope note: minimal DAG shares only **complete rooted-subtree** repeats;
  top-trees/grammars (B4) also exploit internal/partial repeats.

Answers to the three key questions:

- **O(N) canonical ids so equal shapes share a posting list?** Yes — hash-cons /
  minimal-DAG, one bottom-up pass.
- **Cheap alpha-equivalence (minified ids as wildcards)?** Yes — canonicalize
  leaves before hashing (replace identifiers with a wildcard/De-Bruijn token);
  renamed-isomorphic subtrees hash equal. A single `IDENT` sentinel suffices if
  binders don't matter (O(N)).
- **Variable-length list holes?** Partially — the weak spot. A Merkle id hashes a
  _fixed_ child sequence, so `[a,b,c]` and `[a,b,c,d]` don't collapse.
  Mitigations: (a) **bounded-depth shape skeletons** (hash top d levels, variadic
  frontier = one wildcard child) — the "shape feature" we want; (b) tree-grammar
  / top-tree representations for genuine variadic list-body matching.

### B4. Top-tree compression and tree grammars — more than needed

- **Top-tree compression** (Bille, Gørtz, Landau, Weimann, Inf. Comput. 2015 /
  ICALP 2013): exploits internal repeats, can be exponentially smaller than DAG,
  O(log n) navigation.
- **SLCF tree grammars / TreeRePair** (Lohrey, Maneth, Mennicke): minimal SLCF
  grammars smaller than minimal DAGs but **computing the minimal grammar is
  NP-hard**; TreeRePair is a linear-time heuristic.

Do their canonical-form ideas help fingerprints? Marginally and not for free: the
internal DAG still rests on hash-consing; the extra power is partial/spine
repeats (the list-hole problem). But optimal grammars are NP-hard, heuristics are
**not canonical** (no stable equality token), and top-trees give canonical
_navigation_, not a canonical _equality token_. Reach for them only if list-hole
matching is load-bearing.

### B5. Merkle / structural hashing in practice

Git (Merkle DAG of content-addressed objects); compiler CSE/value-numbering
(Downey–Sethi–Tarjan congruence closure); Hashlife (canonical ids for repeated
quadtree subpatterns). Grounds "bounded-depth shape skeleton → feature": hash the
top d levels (labels + child ids, identifiers wildcarded), emit the digest as the
posting-list key. O(N), content-addressed, free dedup.

## Final verdict

| Technique                                                                    | Use?                                                 | Why                                                                                                                                               |
| ---------------------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hash-consing / minimal-DAG / Merkle hashing (Downey–Sethi–Tarjan 1980)       | **USE — core primitive**                             | O(N) canonical id per shape; equal shapes share posting list; alpha-equivalence via leaf-wildcarding; bounded-depth skeleton = the shape feature. |
| Alpha-equivalence canonicalization (leaf-wildcarding; De-Bruijn for binders) | **USE — leaf step on top of hash-consing**           | Linear-ish; collapses minified/renamed identifiers.                                                                                               |
| Top-tree / SLCF grammars (TreeRePair)                                        | **MAYBE — only if variadic list holes load-bearing** | Exploit internal/spine repeats DAG misses, but optimal NP-hard, heuristics non-canonical, give navigation not a stable equality token.            |
| Frequent subtree mining (FREQT, TreeMiner)                                   | **AVOID**                                            | Output-exponential; NP-complete support test off ordered-tree case.                                                                               |
| Discriminative/contrast subtree mining (gSpan+CORK)                          | **AVOID**                                            | Global NP-hard feature-selection; requires full frequent-mining pass; mis-framed for per-target one-witness.                                      |

**One-line recommendation:** build the shape index with bottom-up Merkle
hash-consing (O(N)), canonicalize identifier leaves to wildcards for
alpha-equivalence, answer "minimal distinguishing pattern per target" as a query
against per-shape posting lists. Keep tree-grammar/top-tree compression in
reserve solely for variable-length list-hole matching; stay away from
frequent/discriminative mining.

## Sources

- Frequent subtree mining: https://en.wikipedia.org/wiki/Frequent_subtree_mining ;
  complexity https://arxiv.org/html/2602.03436 ;
  https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4085724/
- TreeMiner (Zaki) TKDE 2005: http://www.cs.rpi.edu/~zaki/PaperDir/TKDE05-treeminer.pdf ;
  https://dl.acm.org/doi/10.1109/TKDE.2005.125
- FREQT: https://globals.ieice.org/en_transactions/information/10.1587/e87-d_12_2754/_p ;
  https://link.springer.com/chapter/10.1007/3-540-45681-3_1
- gSpan: http://protocols.netlab.uky.edu/~liuj/teaching/CS685_s19/ref-gSpan.pdf
- CORK / discriminative subgraph mining: https://www.dbs.ifi.lmu.de/Publikationen/Papers/SAM2010.pdf ;
  https://sites.cs.ucsb.edu/~xyan/papers/sdm09_submodular.pdf ;
  survey https://link.springer.com/chapter/10.1007/978-3-642-40837-3_4
- Downey, Sethi, Tarjan JACM 1980: https://dl.acm.org/doi/10.1145/322217.322228
- Hash-consing / O(n) tree hashing: https://arxiv.org/pdf/1109.0784 ;
  https://www.baeldung.com/cs/hashing-tree ; https://arxiv.org/pdf/2105.01344
- Alpha-equivalent hash-consing (thinnings / De Bruijn):
  https://www.philipzucker.com/thin_hash_cons_codebruijn/
- Top-tree compression: https://www2.compute.dtu.dk/~phbi/files/publications/2013tcwttC.pdf ;
  https://www.sciencedirect.com/science/article/pii/S0890540114001643 ;
  https://arxiv.org/pdf/1506.04499
- Grammar-based tree compression / TreeRePair:
  https://link.springer.com/chapter/10.1007/978-3-319-21500-6_3 ;
  https://arxiv.org/pdf/1007.5406
- Merkle/structural hashing in practice: https://en.wikipedia.org/wiki/Merkle_tree ;
  https://en.wikipedia.org/wiki/Hashlife
