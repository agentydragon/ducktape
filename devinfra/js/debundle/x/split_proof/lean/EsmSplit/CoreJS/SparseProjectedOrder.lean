import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! An order projection for source units whose original ordinals have holes.
The plan continues to enumerate every source unit, including standalone
metadata; CoreJS construction resolves each planned ID against the original
executable rows and therefore omits metadata without shifting later IDs. -/

namespace CoreJS.SparseProjectedOrder

/-- Original executable IDs in the order the full plan evaluates them. A plan
entry absent from `source` is metadata and is omitted only after module order
has been computed. -/
def executableIds (source : List (Nat × Stmt)) (P : EsmSplit.Plan) : List Nat :=
  P.execOrder.filter fun id => (indexedNumbered source id).isSome

/-- The executable part of the plan retains the source's original row order. -/
def OrderKept (source : List (Nat × Stmt)) (P : EsmSplit.Plan) : Prop :=
  (source.map Prod.fst).Nodup ∧ executableIds source P = source.map Prod.fst

instance (source : List (Nat × Stmt)) (P : EsmSplit.Plan) : Decidable (OrderKept source P) := by
  unfold OrderKept executableIds
  infer_instance

private theorem indexedNumbered_key (source : List (Nat × Stmt)) (id : Nat) {row : Nat × Stmt}
    (found : indexedNumbered source id = some row) : row.1 = id := by
  unfold indexedNumbered at found
  rcases Option.map_eq_some_iff.mp found with ⟨selected, _, rowEq⟩
  have keyEq := congrArg Prod.fst rowEq
  simpa using keyEq.symm

private theorem indexedNumbered_ids (source : List (Nat × Stmt)) : ∀ ids : List Nat,
    (ids.filterMap (indexedNumbered source)).map Prod.fst =
      ids.filter fun id => (indexedNumbered source id).isSome
  | [] => rfl
  | id :: ids => by
      simp only [List.filterMap_cons]
      cases h : indexedNumbered source id with
      | none => simp [h, indexedNumbered_ids source ids]
      | some row =>
          cases row with
          | mk key stmt =>
              have keyEq : key = id := indexedNumbered_key source id h
              subst key
              simp [h, indexedNumbered_ids source ids]

theorem split_indexed_find (source : List (Nat × Stmt)) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (m : String) : (splitProgramIndexed source B P).find m = (P.find m).map (moduleOfIndexed source B P) := by
  simp only [Program.find, splitProgramIndexed, EsmSplit.Plan.find, List.find?_map]
  rfl

/-- Membership in a projected module body means that the full plan assigned
the original ID to this module and the original source lookup supplies the
same statement. This is the construction premise used by sparse cell proofs. -/
theorem moduleOfIndexed_body_lookup (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) (m : EsmSplit.Module) (id : Nat) (stmt : Stmt) :
    (id, stmt) ∈ (moduleOfIndexed source B P m).body ↔
      id ∈ m.stmts ∧ source.lookup id = some stmt := by
  change (id, stmt) ∈ m.stmts.filterMap (indexedNumbered source) ↔
    id ∈ m.stmts ∧ source.lookup id = some stmt
  constructor
  · intro member
    rcases List.mem_filterMap.mp member with ⟨planId, inPlan, row⟩
    have planIdEq : planId = id := (indexedNumbered_key source planId row).symm
    subst planId
    exact ⟨inPlan, by simpa [indexedNumbered] using row⟩
  · rintro ⟨inPlan, found⟩
    refine List.mem_filterMap.mpr ⟨id, inPlan, ?_⟩
    simpa [indexedNumbered] using found

private theorem split_indexed_visit (source : List (Nat × Stmt)) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) :
    ∀ (fuel : Nat) (m : String) (st : List String × List String),
      (splitProgramIndexed source B P).visit fuel m st = P.visit fuel m st := by
  intro fuel m st
  unfold Program.visit EsmSplit.Plan.visit
  congr 1
  funext name
  rw [split_indexed_find]
  cases P.find name <;> simp [moduleOfIndexed, Function.comp_def]

private theorem split_indexed_evalOrder (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) : (splitProgramIndexed source B P).evalOrder = P.evalOrder := by
  unfold Program.evalOrder EsmSplit.Plan.evalOrder
  have moduleCount : (splitProgramIndexed source B P).modules.length = P.mods.length := by
    simp [splitProgramIndexed]
  rw [moduleCount]
  exact congrArg Prod.snd (split_indexed_visit source B P _ _ _)

private theorem split_indexed_loaded_bodies (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) : ∀ order : List String,
      ((order.filterMap (splitProgramIndexed source B P).find).flatMap (·.body)) =
        (order.flatMap fun m => ((P.find m).map (·.stmts)).getD []).filterMap (indexedNumbered source)
  | [] => rfl
  | m :: order => by
      rw [List.filterMap_cons, List.flatMap_cons, List.filterMap_append,
        ← split_indexed_loaded_bodies source B P order, split_indexed_find]
      cases P.find m <;> rfl

/-- The actual indexed CoreJS constructor enumerates precisely the executable
rows reached by the complete plan. Its statement values come from lookup by
original ID; plan-only metadata has no row and is omitted. -/
theorem split_indexed_loaded_flat (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) :
    (splitProgramIndexed source B P).loaded.flatMap (·.body) =
      P.execOrder.filterMap (indexedNumbered source) := by
  rw [Program.loaded, split_indexed_evalOrder, split_indexed_loaded_bodies]
  rfl

/-- The loaded original identities are the projection of plan evaluation onto
the IDs for which the original source has an executable row. -/
theorem split_indexed_loaded_ids (source : List (Nat × Stmt)) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) :
    ((splitProgramIndexed source B P).loaded.flatMap (·.body)).map Prod.fst = executableIds source P := by
  rw [split_indexed_loaded_flat]
  exact indexedNumbered_ids source P.execOrder

/-- For any source rows and plan, keeping the projected original IDs proves
that linking's loaded units carry the same ID schedule. This theorem is about
the real plan-derived CoreJS program, not only a list-level projection. -/
theorem loaded_ids_of_order_kept {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (kept : OrderKept source P) :
    ((splitProgramIndexed source B P).loaded.flatMap (·.body)).map Prod.fst = source.map Prod.fst := by
  rw [split_indexed_loaded_ids]
  exact kept.2

/-- A statement in the constructed program is the source row selected by its
original identity. This is the value-level part of the projection contract. -/
theorem loaded_row_lookup {source : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {id : Nat} {stmt : Stmt}
    (member : (id, stmt) ∈ (splitProgramIndexed source B P).loaded.flatMap (·.body)) :
    indexedNumbered source id = some (id, stmt) := by
  rw [split_indexed_loaded_flat] at member
  rcases List.mem_filterMap.mp member with ⟨planId, _, lookup⟩
  unfold indexedNumbered at lookup
  simp only [Option.map_eq_some_iff, Prod.mk.injEq] at lookup
  rcases lookup with ⟨row, found, idEq, stmtEq⟩
  subst idEq
  subst stmtEq
  simp [indexedNumbered, found]

/-- A faithful `mkPlan` retains the spec's unique owner for every planned
unit, including a non-executable metadata ordinal. -/
theorem authored_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (faithful : EsmSplit.Faithful B S P) {id : Nat} {owner : String}
    (member : (id, owner) ∈ CoreJS.PlanPlacement.assignments P) : S.claimants B id = [owner] :=
  CoreJS.PlanPlacement.authored_owner faithful member

#print axioms split_indexed_loaded_flat
#print axioms split_indexed_find
#print axioms moduleOfIndexed_body_lookup
#print axioms split_indexed_loaded_ids
#print axioms loaded_ids_of_order_kept
#print axioms loaded_row_lookup
#print axioms authored_owner

end CoreJS.SparseProjectedOrder
