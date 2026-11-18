# TODO

- **`OrderOk` in near-linear time.** Check conflicting pairs, not all pairs: effect statements form a chain, each read points at its declaration, shared declarations conflict.
  Needs a position map and a proof that the edge check implies the `Pairwise` condition of `Semantics.exec_reorder`. 28 of 42 s at 42,880 statements ([experiments/README.md](experiments/README.md) § 5).
- **`Faithful`: index selectors by name.** It still compares every selector with every statement. 5 s at 42,880 statements.
- **Move the two front-end rules into `Valid`.** `Reassign` and `ExportOutsideEntry` live in `lean/Experiments/LowerSplit.lean`, so the kernel certificate does not cover them.
  A field per statement (`assigns`, `entryOnly`), one conjunct each, one equivalence proof each.
- **Kernel certificates at scale.** `decide +kernel` needs about 60 MB per statement; 288 statements hit 12 GB. Carry the derived tables (read sets, plan) as literals and let the kernel check them
  (closed; least by derivation order), as CompCert's validators do. Compare with `native_decide`.
- **A typed ESTree for Lean.** Generate it from `eslint-visitor-keys`. Then `Lower.lean` is structural recursion instead of `partial` over `Json`, the analysis can be proved sound, and the certificate covers parse output → footprints → plan.
- **test262 module tests** as an oracle for `Plan.visit`. Top-level `await` and async module graphs are not modeled.
- **A better effect analysis.** Calls to pure local helpers (`__commonJS`, factories) count as effects, so 37% of statements are effects ([experiments/README.md](experiments/README.md) § 6).
- **The frame assumption for real JavaScript.** Only the tiny `MiniJS` derives it.
