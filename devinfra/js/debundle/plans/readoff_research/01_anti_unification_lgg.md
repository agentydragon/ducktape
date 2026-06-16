# Anti-unification / LGG for group patterns and distinguishing structure

Research-spike strand (2026-06-16). Verbatim sub-agent report. Synthesis and
recommendation live in <../readoff_algorithm_research.md>.

## Scope and bottom line

Anti-unification (AU) — equivalently, **least general generalization (lgg)** /
most specific generalization (msg) — is exactly the right tool for problem
**(a)**: computing the common pattern of a group of sibling terms. It is a
mature, well-understood operation: for first-order syntactic terms it is
_unitary_ (a single canonical answer), unique up to renaming, and computable in
linear time. Problem **(b)** — producing a _minimal distinguishing_ pattern that
matches `t` but none of a set `S` of others — is **not** what classical AU
computes, and it is **not** a clean, single-answer problem: it is a
discrimination/concept-learning problem whose minimal-size variants are
set-cover/NP-hard-flavored.

## 1. Plotkin (1970) and Reynolds (1970): first-order lgg

In 1970, **Gordon Plotkin** and **John Reynolds** independently gave equivalent
procedures for generalizing first-order terms. AU is the **dual of Robinson's
unification** (1965), hence "anti-unification."

- Plotkin, G. D. (1970). _A Note on Inductive Generalization._ Machine
  Intelligence 5, pp. 153–163. (Follow-up: Machine Intelligence 6, 1971,
  pp. 101–124.)
- Reynolds, J. C. (1970). _Transformational systems and the algebraic structure
  of atomic formulas._ Machine Intelligence 5, pp. 135–151.

Key facts (established):

- For any two first-order terms, the lgg exists, is a single term, and is
  **unique up to variable renaming**. First-order syntactic AU is **unitary**.
- Algorithm: walk both terms in parallel; where heads agree, recurse; where they
  disagree, install a variable — _reusing the same variable for repeated
  identical disagreement pairs_ (`f(a,a)` vs `f(b,b)` → `f(X,X)`, not `f(X,Y)`).
- **Complexity:** linear-size output; modern formulations compute the lgg in
  **linear time**; naive non-sharing is at worst quadratic.

Sources: Wikipedia "Anti-unification (computer science)"; Cerna & Kutsia IJCAI
2023 survey (arXiv:2302.00277); Galitsky CEUR Vol-2529.

## 2. Anti-unification of a SET of n terms — the "common group pattern"

Pairwise lgg is the lattice meet under the subsumption order; the lgg of a set is
the order-independent fold `lgg(t1, lgg(t2, …))`. Result: concrete where **all**
n terms agree, fresh variable where they **disagree** (same variable reused for
recurring disagreement tuples) — "the common skeleton with holes punched where
the siblings differ." Complexity ≈ **O(n · |term|)** for the syntactic case.

**Caveat for ASTs:** list/sequence children of varying _length_ are not
first-order-syntactic (arity differs). Those need **unranked / hedge
anti-unification** (Kutsia, Levy, Villaret, _Anti-unification for Unranked Terms
and Hedges_, JAR 2014), which can be **finitary** (several incomparable
generalizations), so for variadic AST children you may get a small candidate set,
not one. Dedicated n-ary "anti-combination" (ILP 2020) beats naive iteration.

## 3. Higher-order / E-anti-unification and the Cerna–Kutsia survey

Cerna, D. M. & Kutsia, T. (2023), _Anti-unification and Generalization: A
Survey_, IJCAI 2023, arXiv:2302.00277. Categorizes AU by generalization type —
_unitary / finitary / infinitary / nullary_ — and by theory (syntactic vs
equational/E-generalization), order (first- vs higher-order), and added structure
(ordered, nominal, sorted, constraint-based).

- **Higher-order pattern AU is unitary and linear-time** (Baumgartner, Kutsia,
  Levy, Villaret, JAR 58(2):293–310, 2017). Unrestricted higher-order AU is
  unitary-or-nullary (arXiv:2207.08918).
- **E-anti-unification** ranges unitary→infinitary/nullary, theory-dependent.
- **Nominal AU** (binders / α-equivalence) is unitary–finitary (arXiv:2504.21097).

For plain AST selector grouping we stay in the **unitary, linear-time
first-order** world. Reach for E-/higher-order/unranked only for algebraic laws
or variadic/binding structure (accepting finitary answers + higher cost).

## 4. AU as the dual of unification; the lattice of generalizations

Terms are quasi-ordered by subsumption (`t ≤ u` iff `tσ = u`). Unification = least
upper bound (mgu, specialize); anti-unification = greatest lower bound (lgg,
generalize). First-order terms form a lattice; lgg/mgu are meet/join. This is why
the n-ary lgg is well-defined and order-independent. Generalization _type_ = the
cardinality of the minimal complete set of generalizations.

## 5. KEY QUESTION: most-specific generalization of {t} that excludes a set S

**Honest answer: a different, non-clean problem; minimal-size versions are
NP-hard, set-cover-flavored.** AU only generalizes; it has no "must not match
these." This is a _discrimination_ problem:

- **(a) Version spaces / candidate elimination (Mitchell 1977/1982).** Maintains
  the S-boundary (maximally specific) and G-boundary (maximally general). Your
  "most specific generalization of `{t}` not generalizing any `s∈S`" lives near
  the **G-boundary**, which is generally a _set_ (can be exponentially many).
- **(b) ILP / Golem / inverse resolution.** RLGG with negatives as consistency
  constraints; **plain RLGG clause length grows O(mⁿ)**; Golem only polynomial
  under **ij-determinacy**. Direct evidence "generalize-while-excluding" is hard
  in general.
- **(c) Contrast-set / minimal-distinguishing pattern mining** (most on-point):
  Ting & Bailey, _Mining Minimal Contrast Subgraph Patterns_, SDM 2006 (minimal
  hypergraph transversal core); minimal distinguishing subgraphs/subsequences
  (Ramamohanarao/Bailey); survey arXiv:2209.13556.

**Complexity verdict:** minimal distinguishing pattern is **NP-hard /
set-cover-flavored**. Two sources: (i) matching/subgraph-iso (polynomial for
_trees_, which helps us) and (ii) **minimizing** = minimal hypergraph transversal
/ minimum set cover (NP-hard, log-approximable). No clean unitary
"most-specific distinguishing generalization" exists.

**Practical mapping (`t` = target sibling, `S` = others):**

1. Specialize from the top (G-boundary descent): start most-general, add concrete
   constraints from `t` until it stops matching every `s∈S`.
2. Each concrete feature "kills" a subset of negatives; choosing the **fewest** =
   **minimum set cover** over `S` ⇒ greedy set-cover (O(log|S|)-approx) is the
   standard, defensible engineering answer.
3. Enumerating _all_ maximally-general distinguishers = G-boundary / minimal
   transversal; bound it (top-k, size cap).

## Synthesis

For **grouping** use first-order syntactic **lgg / anti-unification** (Plotkin
1970, Reynolds 1970): unitary, linear-time, order-independent lattice meet —
"shared structure concrete, divergence as variables." Variadic children →
unranked/hedge AU (finitary). For **distinguishing** one declaration from the
rest, AU is insufficient and there is no canonical operator: it is a
discrimination problem (version-space G-boundary; ILP/Golem RLGG O(mⁿ); contrast
mining → minimum set cover, NP-hard). Recommended: greedy top-down specialization
with a set-cover heuristic, not an exact lgg-style operator.

### Caveats on sourcing

Plotkin 1970 / Reynolds 1970 are pre-digital Machine Intelligence volumes;
titles/venues verified via the survey and secondary sources. Linear-time
first-order lgg firmly established for higher-order _pattern_ generalization
(Baumgartner et al. 2017, verified); naive non-sharing is quadratic. NP-hardness
of _minimal_ distinguishing patterns well-supported for graphs (subgraph-iso +
minimal-transversal/set-cover); for **trees/ASTs**, matching is polynomial, so
the residual hardness is the minimization (set cover).

### Primary URLs

- Cerna & Kutsia survey: https://arxiv.org/abs/2302.00277 ·
  https://www.ijcai.org/proceedings/2023/0736.pdf
- Higher-order pattern AU linear time:
  https://link.springer.com/article/10.1007/s10817-016-9383-3 ·
  https://pmc.ncbi.nlm.nih.gov/articles/PMC6109779/
- Wikipedia (CS): https://en.wikipedia.org/wiki/Anti-unification_(computer_science)
- Golem / ILP: https://en.wikipedia.org/wiki/Golem_(ILP) ·
  https://arxiv.org/pdf/2008.07912
- Version spaces (Mitchell): https://en.wikipedia.org/wiki/Version_space_learning
- Minimal contrast subgraphs (Ting & Bailey 2006):
  https://people.eng.unimelb.edu.au/baileyj/papers/siampaper.pdf
- Contrast pattern mining survey: https://arxiv.org/pdf/2209.13556
- Unranked/hedge AU: https://link.springer.com/article/10.1007/s10817-013-9285-6
