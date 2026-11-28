import EsmSplit.CoreJS.InteriorPlanExports
import EsmSplit.CoreJS.SparseProjectedOrder

/-! The source-ID-aware CoreJS constructor satisfies the structural interface
used by the interior export and scope-address lemmas. No constructed program is
executed or compared with the source to establish this fact. -/

namespace CoreJS.IndexedConstructorCells

/-- The actual constructor emits modules and imports from the full authored
plan, while looking up executable statements by their original IDs. -/
theorem projection (source : List (Nat × Stmt)) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) :
    IndexedPlanCells.Projection source B P (splitProgramIndexed source B P)
      (moduleOfIndexed source B P)
    where
  find := SparseProjectedOrder.split_indexed_find source B P
  body := SparseProjectedOrder.moduleOfIndexed_body_lookup source B P
  exports := by
    intro md; rfl
  imports := by
    intro md; rfl

/-- The same constructor used for scope-address linkage has the original source
ID schedule whenever the source and authored plan satisfy `OrderKept`. -/
theorem projection_and_schedule {source : List (Nat × Stmt)} {B : EsmSplit.Bundle}
    {P : EsmSplit.Plan} (kept : SparseProjectedOrder.OrderKept source P) :
    IndexedPlanCells.Projection source B P (splitProgramIndexed source B P)
        (moduleOfIndexed source B P) ∧
      ((splitProgramIndexed source B P).loaded.flatMap (·.body)).map Prod.fst =
        source.map Prod.fst :=
  ⟨projection source B P, SparseProjectedOrder.loaded_ids_of_order_kept kept⟩

end CoreJS.IndexedConstructorCells
