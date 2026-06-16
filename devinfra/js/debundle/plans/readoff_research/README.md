# Read-off minimization: research spike (2026-06-16)

De-risking literature survey for the read-off selector-minimizer redesign,
requested to avoid committing to an algorithm that can't reach the efficient one.
Five independent strands, each a verbatim sub-agent report with verified
citations. The synthesis + recommendation + lock-in analysis is one level up in
<../readoff_algorithm_research.md>; the redesign plan is <../readoff_minimization.md>.

| Strand                             | File                                         | Verdict                                                                                    |
| ---------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Anti-unification / LGG             | <01_anti_unification_lgg.md>                 | Grouping = first-order LGG (unitary, linear). Distinguishing ≠ AU → set-cover.             |
| Subtree mining & tree compression  | <02_subtree_mining_and_tree_compression.md>  | USE hash-cons/Merkle min-DAG (O(N)); AVOID frequent/discriminative mining.                 |
| Tree automata & pattern matching   | <03_tree_automata_and_pattern_matching.md>   | Match-set automata: O(N) match, exponential only in preprocessing (avoidable).             |
| Term indexing                      | <04_term_indexing.md>                        | Schulz fingerprint trie (union, not intersection). No index reads off minimality.          |
| Minimal keys / distinguishing sets | <05_minimal_keys_and_distinguishing_sets.md> | Minimality ≡ Minimum Set Cover / teaching set: NP-hard; greedy ln n optimal; OPT=1 common. |

Convergent conclusion: **hash-consed Merkle shape index (O(N)) + greedy
set-cover read-off (true read-off when OPT=1, the Zipfian majority) + LGG
grouping**, with the production matcher as verification oracle. The single
lock-in fork is the **variable-length list-hole encoding** (recommend cons-spine
plus bounded-depth skeletons behind a swappable interface).
