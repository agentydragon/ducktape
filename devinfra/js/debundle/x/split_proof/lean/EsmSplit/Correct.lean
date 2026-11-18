import EsmSplit.Plan
import EsmSplit.Semantics

namespace EsmSplit

/-- What a valid split guarantees.
1. Every bundle statement runs exactly once.
2. Every name in every statement means, in its new module, the same declaration as in the bundle.
3. For every way of giving meaning to statements (any value type `V`, any world type `W`,
   any `Meaning`, any starting state), the split program ends with the same value for every name
   and the same world (heap, console, …) as the bundle. -/
def Correct (B : Bundle) (P : Plan) : Prop :=
  (P.execOrder.Nodup ∧ ∀ x, x ∈ P.execOrder ↔ x < B.length)
  ∧ Hygienic B P
  ∧ ∀ (V W : Type) (M : Meaning V W) (s0 : St V W),
      B.exec M s0 P.execOrder = B.exec M s0 (List.range B.length)

/-- **Main theorem.** The decidable checks in `Valid` are enough for `Correct`. -/
theorem split_correct {B : Bundle} {S : Spec} {P : Plan} (h : Valid B S P) : Correct B P := by
  obtain ⟨_, _, _, _, hyg, ⟨hnd, hlt, hall⟩, hord⟩ := h
  have hmem : ∀ x, x ∈ P.execOrder ↔ x < B.length :=
    fun x => ⟨hlt x, fun hx => hall x (List.mem_range.2 hx)⟩
  exact ⟨⟨hnd, hmem⟩, hyg, fun _ _ M s0 => B.exec_reorder M s0 B.length P.execOrder hnd hmem hord⟩

end EsmSplit
