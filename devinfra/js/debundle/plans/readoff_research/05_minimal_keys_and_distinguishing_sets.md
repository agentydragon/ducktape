# The combinatorial core: "smallest feature set whose posting-list intersection is {target}"

Research-spike strand (2026-06-16). Verbatim sub-agent report. Synthesis in
<../readoff_algorithm_research.md>.

## 0. The reduction that ties everything together

Fix target `t` and a set of features (each a posting list = items having it).
Keep only features whose posting list **contains** `t`. The intersection of
chosen lists is `{t}` iff, for **every non-target** `x`, at least one chosen
feature **excludes** `x`. This is exactly **Minimum Set Cover**, dualized:

- Universe `U` = non-targets (size `n`).
- Feature `f` ↦ `cover(f)` = the non-targets it excludes.
- A subcollection covers `U` ⟺ its posting-list intersection is `{t}`.

Parameters: `n` = #non-targets; `d` = max non-targets one feature excludes
(largest set); `f` = max #features excluding any one non-target (frequency).

## 1. Minimal unique column combinations / minimal keys (UCC discovery)

- **Minimum-cardinality key is NP-complete** (decision: key of size ≤ k?):
  Lucchesi & Osborn, "Candidate keys for relations," JCSS 17(2):270–279, 1978 —
  also "is an attribute prime?" NP-complete; gives an **output-polynomial**
  all-keys enumeration.
- **#minimal keys can be exponential** (counting #P-hard): GORDIAN, Sismanis et
  al., VLDB 2006.
- **= minimal transversals (hitting sets) of the difference-set hypergraph**
  (Mannila & Räihä, JCSS 1986; DAM 1992; DKE 1994).
- **Transversal-hypergraph complexity:** Eiter & Gottlob, SIAM J. Comput.
  24(6):1278–1304, 1995; enumeration in **quasi-polynomial total time
  N^{o(log N)}** (Fredman & Khachiyan, J. Algorithms 21(3):618–628, 1996);
  output-polynomial is the famous open problem.
- **Modern equivalence:** Birnick et al., "Hitting Set Enumeration with Partial
  Information for UCC Discovery," PVLDB 13(11):2270–2283, 2020 (HPIValid):
  minimal-UCC discovery ≡ minimal-hitting-set enumeration of the difference-set
  hypergraph under parsimonious reductions.
- **Practical algorithms:** Ducc (PVLDB 2013), DFD (CIKM 2014), HyUCC (BTW 2017,
  hybrid sampling + lattice validation — scales far beyond Ducc/DFD/GORDIAN).
- **Parameterized:** bounded-size UCC detection is **W[2]-complete** in solution
  size (Bläsius, Friedrich, Schirneck, arXiv:2103.13331).

## 2. Minimum Set Cover and Minimum Test Cover

- **Set Cover NP-hard** — Karp's 21 (1972).
- **Greedy H-approximation:** Johnson (JCSS 1974), Lovász (Discrete Math. 1975),
  Chvátal (Math. OR 4(3):233–235, 1979, weighted). Guarantee cost ≤
  `H(s)·OPT ≤ (1 + ln s)·OPT`, `s` = largest set size.
- **`(1−o(1)) ln n` inapproximable:** Feige, JACM 45(4):634–652, 1998.
  Tightened to **`(1−ε) ln n` NP-hard ∀ε>0 under P≠NP:** Dinur & Steurer, STOC 2014. So greedy's `ln n` is essentially optimal.
- **Minimum Test Cover / Test Collection** — Garey & Johnson [SP6], NP-complete;
  set cover over the `~m(m−1)/2` pairs to separate. De Bontridder et al., Math.
  Prog. B 98:477–491, 2003: greedy `O(log m)`, `O(log k)` for tests of size ≤ k,
  APX-hard. **Our problem is the one-target special case** — only every `{t,x}`
  pair, i.e. cover every non-target `x` — collapsing to plain set cover (with the
  `OPT=1` shortcut).
- **Frequency-`f` bound:** every element in ≤ `f` sets ⇒ LP-rounding / primal-dual
  gives an **`f`-approximation** (generalizes vertex cover, `f=2`): Hochbaum,
  SIAM J. Comput. 11(3):555–556, 1982; Vazirani, _Approximation Algorithms_,
  ch. 14–15.

## 3. Teaching dimension / minimal distinguishing sets

- **Teaching dimension:** Goldman & Kearns, "On the Complexity of Teaching,"
  COLT 1991 / JCSS 50(1):20–31, 1995. A **teaching set** = sample on which the
  target is the _unique_ consistent concept — "smallest feature set making the
  target unique." Co-originated: Shinohara & Miyano, New Generation Computing 8(4), 1991.
- **Vocabulary equivalence:** minimal teaching set = specifying set = witness set
  = key = discriminant (Kushilevitz, Linial, Rabinovich, Saks, JCTA 73(2), 1996).
- **Minimum teaching set is NP-hard** by reduction from Set Cover
  (Shinohara–Miyano; Goldman–Kummer; recursive TD NP-hard, arXiv:2307.09792).
- **Combinatorial ancestor — separating systems:** Katona, J. Comb. Theory
  1(2):174–194, 1966 (origin Rényi 1961). Modern identifying/discriminating codes
  descend from this.

## 4. Greedy behavior in practice

- **Right guarantee is `H_d`, not `H_n`** (Chvátal/Lovász): `H(d) = ln d + O(1)`,
  `d` = max non-targets one feature excludes. If no feature is very selective `d`
  is small ⇒ small constant (`H(7) ≈ 2.59`).
- **Exact worst case `ln n − ln ln n + Θ(1)`** (Slavík, STOC 1996 / J. Algorithms
  25(2):237–254, 1997); greedy essentially optimal given §2 inapproximability.
- **`OPT=1` trivial and common:** a single feature excluding all non-targets ⇒
  greedy returns it in step 1, optimal.
- **Why typical instances are easy:** Zipf/Heaps term-frequency power laws
  (Manning, Raghavan, Schütze, _IR_, 2008) ⇒ usually some highly selective
  feature ⇒ small `OPT`. The IR/DB **"most-selective-first"** heuristic _is_ the
  greedy max-coverage rule under the §0 duality (System R, Selinger et al. SIGMOD
  1979).
- **`f`-bound lever:** few features per non-target ⇒ small-constant `f`-approx;
  operative bound `min(H_d, f)`.
- **Bounded VC-dimension lever:** Brönnimann & Goodrich, DCG 14(4):463–479, 1995
  give `O(d·log(d·c))` (so `O(log c)` for constant VC-dim, beating `ln n` when
  OPT `c ≪ n`); Clarkson–Varadarajan / shallow-cell-complexity line. Caveat:
  whether _our_ exclusion system has bounded VC-dim is data-dependent.

## 5. Bounded vs unbounded search; FPT and output-sensitivity

- **W[2]-complete in solution size `k`** (Downey & Fellows 1999) — not FPT in `k`
  unless the W-hierarchy collapses.
- **FPT in universe size `n`:** exact in **`O*(2^n)`** subset-DP
  (`OPT(T)=1+min_i OPT(T\S_i)`; Fomin & Kratsch 2010; inclusion–exclusion
  Björklund–Husfeldt–Koivisto 2006). When #non-targets-to-exclude is small, FPT in
  _that_ parameter.
- **Search bounded by the target's own `d` features:** any usable feature must
  contain `t`, so a minimal distinguishing set ⊆ those `d` features — domain
  `2^d`, **never the whole corpus** (attribute-lattice bound for keys). Choosing
  the minimum subset is still set cover (`O*(2^d)` exact / `H_d`-approx).
- **Output-sensitive enumeration of all minimal distinguishing sets** =
  transversal-hypergraph enumeration, quasi-poly (Fredman–Khachiyan 1996);
  UCC ≡ hitting-set (JCSS 2021).

## Bottom line

- **Minimal-distinguishing-feature-set NP-hard?** **Yes** — exactly Minimum Set
  Cover over non-targets (= one-target Test Cover = minimum teaching set =
  minimum key). NP-hard (Karp 1972 / Lucchesi–Osborn 1978); **W[2]-complete** in
  solution size; not approximable below `(1−ε) ln n` unless P=NP (Dinur–Steurer
  2014).
- **Best practical guarantee:** greedy `ln n` (rank by selectivity, take the
  feature excluding the most still-included non-targets) ⇒ `H(d) ≤ 1 + ln d`,
  tight `ln n − ln ln n + Θ(1)` (Slavík 1997) — essentially optimal for any
  poly-time algorithm. Instance levers: `f`-approx; `O(log OPT)` under bounded
  VC-dim.
- **Why typical instances are easy:** Zipfian selectivity ⇒ a single selective
  feature drives `OPT=1` (greedy finds it in step 1); search bounded by the
  target's own `d` features, never an unbounded scan; exact optimum reachable in
  `O*(2^{#non-targets})` or `O*(2^d)` when small.

**Caveats:** the Garey–Johnson [SP6] pointer and the exact test-cover greedy
constant are secondary-sourced; the set-cover-over-rival-pairs reduction, the
IR-duality framing, and "bounded by `d` posting lists" are sound synthesis. All
complexity constants (Lucchesi–Osborn, Feige, Dinur–Steurer, Slavík,
Chvátal/Lovász, Fredman–Khachiyan, W[2]-completeness) independently re-verified
against primary indexes (DBLP, ScienceDirect, JACM/STOC records).
