import EsmSplit.Instance

/-!
# Strategies, tried in order

Nothing here searches for a proof term. The validity of a split is decided by computation (`Valid` is decidable),
and a *strategy* says how to run that computation. `first | a | b | c` tries `a`, then `b` only if `a` failed, and so on.

| Strategy | Runs the decision procedure in | Trusts |
| --- | --- | --- |
| `decide` | the elaborator | the kernel |
| `decide +kernel` | the kernel | the kernel |
| `native_decide` | compiled code | the kernel, Lean's compiler and runtime |

`experiments/proof_search/prove.mjs` does the same from outside, for a generated file, with a time limit per strategy.
-/

open EsmSplit Example

macro "decide_split" : tactic => `(tactic| first | decide | decide +kernel | native_decide)

theorem ladder_valid : Valid bundle spec plan := by decide_split

/-- Refutations go through the same ladder. `OrderOk` fails for this control. -/
theorem ladder_rejected : ¬ Valid bundle control_effect_order (mkPlan bundle control_effect_order) := by decide_split

#print axioms ladder_valid
