import EsmSplit.CoreJS.Syntax
import EsmSplit.ModulePath

namespace CoreJS

abbrev UnitPlacement :=
  Nat × String

def Module.unitPlacements (m : Module) : List UnitPlacement :=
  m.body.map fun (id, _) => (id, m.name)

/-- Model-only modules for host capabilities or template objects have reserved
names outside the space of JavaScript file paths. -/
def Program.fileUnitPlacements (p : Program) : List UnitPlacement :=
  (p.modules.filter fun m => EsmSplit.safeModulePath m.name).flatMap Module.unitPlacements

/-- Every executable source unit occurs once in the authored assignment, and
the decoded output has exactly the assigned unit IDs and file paths. Import
and export declarations are checked by the decoder and linker. -/
def Places (assignment : List UnitPlacement) (before after : Program) : Prop :=
  (assignment.map Prod.fst).Nodup ∧
    (before.fileUnitPlacements.map Prod.fst).Perm (assignment.map Prod.fst) ∧
    after.fileUnitPlacements.Perm assignment

private def checkIncreasing : (ids : List Nat) → Except String (PLift (ids.Pairwise (· < ·)))
  | [] => pure ⟨.nil⟩
  | [_] => pure ⟨.cons (by simp) .nil⟩
  | first :: second :: rest => do
    if order : first < second then
      let ⟨tail⟩ ← checkIncreasing (second :: rest)
      return ⟨List.pairwise_cons.mpr
            ⟨by
              intro id member
              rcases List.mem_cons.mp member with rfl | later
              · exact order
              · exact Nat.lt_trans order ((List.pairwise_cons.mp tail).1 id later), tail⟩⟩
    else
      throw s!"duplicate placement for unit {first}"

/-- Compare sorted lists by unit ID, retaining a permutation proof without a
quadratic pairwise scan. The comparison uses each module's decoded body. -/
def checkPlaces (assignment : List UnitPlacement) (before after : Program) :
    Except String (PLift (Places assignment before after)) := do
  let byOrdinal := fun (a b : Nat) => decide (a ≤ b)
  let byUnit := fun (a b : UnitPlacement) => byOrdinal a.1 b.1
  let planned := assignment.mergeSort byUnit
  let ids := planned.map Prod.fst
  let ⟨unique⟩ ← checkIncreasing ids
  let sourceIds := before.fileUnitPlacements.map Prod.fst
  let observed := after.fileUnitPlacements
  if sourceOk : sourceIds.mergeSort byOrdinal = ids then
    if outputOk : observed.mergeSort byUnit = planned then
      have planPerm : planned.Perm assignment := List.mergeSort_perm _ _
      have idPerm : ids.Perm (assignment.map Prod.fst) := planPerm.map Prod.fst
      return ⟨by
          refine ⟨idPerm.nodup_iff.mp (unique.imp (fun h => Nat.ne_of_lt h)), ?_, ?_⟩
          · refine (List.mergeSort_perm sourceIds byOrdinal).symm.trans ?_
            rw [sourceOk]
            exact idPerm
          · refine (List.mergeSort_perm observed byUnit).symm.trans ?_
            rw [outputOk]
            exact planPerm⟩
    else
      throw "decoded output units differ from the planned placement"
  else
    throw "placement must cover every executable source unit once"

end CoreJS
