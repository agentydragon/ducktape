# Reuse the parsed source in commutation proposals

PR #13639's hosted `propose_commutations_budget_test` failed twice with the
unchanged 30-second command budget: 63.142852994 seconds in BuildBuddy
`6461aa1a-40a7-5abd-a9aa-39a2fdcef842` and 51.204735239 seconds in
`05355d06-b729-58b5-9763-a91ede9b02c8`. Each run failed only this target
among 228 tests. The test fixture has 4096 step owners and 96 readers with
393216 unrelated lazy reads. These logs do not identify the slow phase or
establish that this source change caused the failures.

`propose-commutations` already parses the verified source into `PreparedSource`
for statement ordinals and excerpts. It used to clone the same source text and
parse it again solely to build `ChunkSelectorIndex` before selector synthesis.
The command now passes `PreparedSource::module()` to that index builder. The
selector index, synthesis requests and shared uniqueness proof are unchanged.
This removes one whole-source clone, one duplicate AST parse and its AST
allocation from the command, including a second parse of the readers' large
bodies. It makes no claim about the elapsed-time effect; hosted CI must check
the budget separately.
