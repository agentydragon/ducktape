# Term indexing for structural retrieval and minimal distinguishing patterns

Research-spike strand (2026-06-16). Verbatim sub-agent report. Synthesis in
<../readoff_algorithm_research.md>. Headline: **no term index solves the
minimal-distinguishing-pattern problem directly** — that is an
anti-unification / minimal-feature-cover problem; the indexes serve as the fast
candidate-pruning + verification oracle.

## 1. The canonical survey (Sekar, Ramakrishnan, Voronkov 2001)

R. Sekar, I. V. Ramakrishnan, A. Voronkov, "Term Indexing," ch. 26 in _Handbook
of Automated Reasoning_, 2001, pp. 1853–1964.

Model: maintain indexed term set `L`; answer queries about query term `t`. Four
retrieval operations by substitution relation:

- **Retrieve unifiable** — indexed `s` with `∃θ. sθ = tθ`.
- **Retrieve instances** — indexed `s` that are instances of the query
  (`∃θ. tθ = s`); query more general.
- **Retrieve generalizations** — indexed `s` that are generalizations of the
  query (`∃θ. sθ = t`); entries more general (forward subsumption/rewriting).
- **Retrieve variants** — `s` equal to `t` up to renaming.

"Find the smallest pattern matching exactly one stored term" is
generalization-flavored (a pattern `p` such that exactly one stored AST is an
instance of `p`) ⇒ **retrieve-instances of `p`** is the relevant primitive.

**Perfect vs. non-perfect (imperfect) indexing:** a perfect index returns
exactly the matching set; an imperfect index returns a **candidate superset**
filtered by an exact test — exactly the inverted-index situation (posting-list
intersection gives candidates; confirm with a real match). Fingerprint indexing
is explicitly non-perfect.

## 2. Discrimination trees & path indexing

McCune, "Experiments with discrimination-tree indexing and path indexing for term
retrieval," JAR 9(2):147–167, 1992.

**Discrimination tree** = trie over the **preorder (flattened) symbol string**;
all variables collapsed to one wildcard `*`; shared prefixes share trie paths.

- Build: O(|t|) per term, O(Σ|t|) total.
- Retrieve generalizations (ground query): descend the matching edge **and** the
  `*` edge — near O(|t|) in practice, the strong direction (E uses perfect
  discrimination trees for forward rewriting).
- Retrieve instances/unifiable (query has variables): a query wildcard matches
  any stored subtree → branching multiplies; the weak direction (Graf & Meyer on
  unifier retrieval).
- Worst case exponential in branching but proportional to consistent trie paths.

**Path indexing** (same paper): inverted index keyed by **root-to-position symbol
paths** (`f.1.g.1`) → posting list of terms containing that path; retrieval
intersects/unions posting lists. **The closest classical analog to our inverted
feature index.** Build O(Σ|t|); inherently non-perfect (verify candidates). The
trade-off (vs. fingerprinting) is the per-coordinate **intersection**.

## 3. Substitution trees (Graf, RTA 1995)

Nodes labeled with **substitutions**; the term at a leaf = composition of
substitutions along the path. Shares common _instantiation_ structure (not just
symbol prefixes) → **most compact** of the classical trio; supports all four
relations. Price: backtracking substitution state; insertion computes a
generalization (msg/anti-unifier) to decide splits — adjacent to, but not, a
distinguishing-pattern computation. Higher-order extension: Pientka, TOCL 2009.

## 4. Fingerprint indexing (Schulz, IJCAR 2012) — closest analog to our tool

S. Schulz, "Fingerprint Indexing for Paramodulation and Rewriting," IJCAR 2012,
LNAI 7364, pp. 477–483.

**General fingerprint feature function.** Fix sample positions. For term `t`,
position `p`, `gfpf(t,p) ∈ F ⊎ {A, B, N}`:

- `A` if `p ∈ pos(t)` and `t|_p` is a **variable**;
- `top(t|_p)` (the function symbol) if `p ∈ pos(t)` and `t|_p` is **not** a
  variable;
- `B` if `p = q.r` with `q ∈ pos(t)` and `t|_q` a variable (position lies
  **strictly below** a variable — could exist in an instance);
- `N` otherwise (position **cannot exist** in `t` or any instance).

A fingerprint is the length-`n` vector over the fixed positions — **constant
length, independent of term size.**

**Compatibility (the pruning core), verbatim from Fig. 1** (`f₁,f₂` distinct
symbols):

Unification compatibility (symmetric):

|        | f₁  | f₂  | A   | B   | N   |
| ------ | --- | --- | --- | --- | --- |
| **f₁** | Y   | N   | Y   | Y   | N   |
| **f₂** | N   | Y   | Y   | Y   | N   |
| **A**  | Y   | Y   | Y   | Y   | N   |
| **B**  | Y   | Y   | Y   | Y   | Y   |
| **N**  | N   | N   | N   | Y   | Y   |

Matching compatibility (match `s` onto `t`; asymmetric, rows = `s`, cols = `t`):

|        | f₁  | f₂  | A   | B   | N   |
| ------ | --- | --- | --- | --- | --- |
| **f₁** | Y   | N   | N   | N   | N   |
| **f₂** | N   | Y   | N   | N   | N   |
| **A**  | Y   | Y   | Y   | N   | N   |
| **B**  | Y   | Y   | Y   | Y   | Y   |
| **N**  | N   | N   | N   | N   | Y   |

Two distinct concrete symbols at one position (`f₁` vs `f₂`) are never
compatible — the prune. **Soundness (Thm 1):** incompatible fingerprints ⇒ not
unifiable / does not match. Compatibility is a **necessary condition** (never
drops a true match; over-approximates).

**Organization & cost:** fingerprints partition terms into disjoint classes; the
index is a **constant-depth trie of depth `n`**; query descends every
compatible edge and **unions** reached leaves. Build O(n·|L|); query O(n) +
candidates. **Key advantage vs. posting-list intersection:** each term sits at
exactly one leaf, so retrieval **unions compatible leaves** — _no per-coordinate
intersection_ (Schulz §3 contrasts coordinate/path indexing explicitly).

**Mapping:** our "inverted feature index, intersect posting lists" is the
coordinate/path formulation; Schulz's fixed-length sampled vector + trie replaces
intersection with one descent + union, with sound `A/B/N` compatibility tables we
can adopt wholesale. Break points: alpha-equivalence (handled — see §"breaks"),
and variadic list holes (the `B` code only models one variable absorbing a fixed
subtree).

## 5. Code trees and context/abstraction trees

**Code trees** (Voronkov, JAR 1995; Riazanov & Voronkov, JELIA 2000): a
discrimination tree compiled into matching-VM instructions; faster execution
model, same asymptotic retrieval semantics. **Context trees** (Ganzinger,
Nieuwenhuis, Nivela, JAR 2004) generalize substitution trees with **context
variables (holes)** — the closest classical idea to "list holes" — but still
index for the four standard relations, not minimality.

## 6. Do these indexes read off the minimal distinguishing pattern? NO.

Every structure answers _membership/relation_ queries. With retrieve-instances
you can **verify** a candidate `p` ("does exactly one stored term match `p`?")
efficiently — an excellent **oracle**. But none **searches** the pattern space or
**reads off** the minimal distinguisher in linear preprocessing.

Why it's separate: "most general pattern matching exactly one stored term" is an
**anti-unification-lattice + minimum-feature-cover** problem — among
generalizations of `x`, find a maximal one whose instance-set ∩ stored-set =
`{x}`. Generalizing increases coverage monotonically ⇒ a **minimum-cover /
hitting-set** optimization. Studied as **minimal distinguishing patterns** and
**teaching / distinguishing sets** (Wang & Bailey KAIS; "Distinguishing Pattern
Languages with Membership Examples," 2017; teaching dimension, Goldman & Kearns
1995). Minimization is **NP-hard** (set cover); greedy ln(n)-approximation
(Chvátal 1979) in practice — the index makes each greedy step cheap but does not
remove the minimization.

**Precise relationship:** (1) build the inverted feature index (path/coordinate
or Schulz fingerprint trie) — **linear preprocessing**; (2) it gives posting
lists + a fast retrieve-instances oracle (literature fully supports this); (3)
"smallest feature subset whose intersection is `{x}`" is **minimum set cover** —
NP-hard — use greedy over posting lists. The index is the pruning/verification
oracle; the minimality is layered on top and is set-cover-hard.

## Where the analogy breaks (explicit)

- **Variable-length list holes.** All five structures assume **fixed-arity**
  symbols. A variadic hole has no native encoding; closer to tree/word automata
  with gap symbols / unranked tree indexing (hedge automata). Schulz's `B` code
  partially models "this fixed position may exist in an instance," not a variadic
  run.
- **Alpha-equivalence.** Indexes collapse variables to one wildcard (`*`) or
  `A`/`B` codes — already alpha-invariant at the leaf level. The break appears
  only if you must track _binding/sharing_ (same variable in two positions);
  substitution/context trees recover some of this at extra cost.
- **The minimality objective.** Not an indexing property at all.

## Key URLs

- Handbook ch. 26: https://dl.acm.org/doi/10.5555/778522.778535
- McCune JAR 1992: https://link.springer.com/article/10.1007/BF00245458
- Graf RTA 1995: https://link.springer.com/chapter/10.1007/3-540-59200-8_52
- Schulz fingerprint PDF:
  http://wwwlehre.dhbw-stuttgart.de/~sschulz/PAPERS/schulz_fp-index.pdf
- Riazanov–Voronkov code trees:
  https://link.springer.com/chapter/10.1007/3-540-40006-0_15
- Ganzinger–Nieuwenhuis–Nivela context trees:
  https://link.springer.com/article/10.1023/B:JARS.0000029963.64213.ac
- Mining minimal distinguishing patterns:
  http://people.eng.unimelb.edu.au/baileyj/papers/KIS.pdf
- Teaching dimension: https://en.wikipedia.org/wiki/Teaching_dimension
- Anti-unification linear-time: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6109779/

**Confidence:** the Schulz `gfpf` definition, both compatibility tables, and the
"union not intersection" advantage are quoted from the primary PDF (high
confidence). The Handbook taxonomy and Graf/McCune/Voronkov/context-tree
attributions are confirmed via publisher pages. The part-6 NP-hardness/set-cover
framing is reasoning from the problem structure + distinguishing-pattern/teaching-
dimension literature, not a verbatim theorem from a single indexing paper.
