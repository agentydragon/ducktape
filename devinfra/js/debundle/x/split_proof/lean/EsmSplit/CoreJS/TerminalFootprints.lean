import EsmSplit.CoreJS.MetadataCells
import EsmSplit.LocalExportUnit

/-! Decide the metadata-aware source footprint relation against the supplied
full original bundle. The checker does not construct or alter that bundle. -/

namespace CoreJS.MetadataCells

/-- A finite form suitable for checking the exact source statements and every
trailing source ordinal of the actual planner bundle. -/
theorem footprints_iff (ss : List Stmt) (B : EsmSplit.Bundle) : Footprints ss B ↔
    ss.length ≤ B.length ∧
    (∀ i (h : i < ss.length),
      (∀ x ∈ (B.stmt i).defs, x ∈ ss[i].bindings.map (·.1)) ∧
      (∀ x ∈ ss[i].bindings.map (·.1), x ∈ (B.stmt i).defs) ∧
      ∀ x ∈ ss[i].free, x ∈ (B.stmt i).defs ∨ x ∈ (B.stmt i).eager ++ (B.stmt i).lazy) ∧
    ∀ i (_h : i < B.length), ss.length ≤ i → (B.stmt i).defs = [] := by
  constructor
  · intro f
    refine ⟨f.withinBundle, ?_, ?_⟩
    · intro i hi
      have hf := f.statement i ss[i] (List.getElem?_eq_getElem hi)
      exact ⟨fun x hx => (hf.1 x).1 hx, fun x hx => (hf.1 x).2 hx, hf.2⟩
    · intro i hi htail
      exact f.metadata i htail hi
  · rintro ⟨bound, statements, metadata⟩
    refine ⟨bound, ?_, ?_⟩
    · intro i s hs
      have hi := (List.getElem?_eq_some_iff.1 hs).1
      have eq : ss[i] = s := (List.getElem?_eq_some_iff.1 hs).2
      have hf := statements i hi
      rw [eq] at hf
      exact ⟨fun x => ⟨hf.1 x, hf.2.1 x⟩, hf.2.2⟩
    · intro i htail hi
      exact metadata i hi htail

instance (ss : List Stmt) (B : EsmSplit.Bundle) : Decidable (Footprints ss B) :=
  decidable_of_iff _ (footprints_iff ss B).symm

/-- Check the exact ordered source export pairs and every local use on the
original metadata unit. A footprint may have deduplicated or extra uses. -/
theorem row_footprint_iff (row : EsmSplit.LocalExportUnit) (statement : EsmSplit.Stmt) :
    row.FootprintMatches statement ↔
      statement.exports = row.names.map (fun (publicName, localName) => (publicName, some localName)) ∧
      ∀ pair ∈ row.names, pair.2 ∈ statement.eager ++ statement.lazy := by
  constructor
  · intro h
    exact ⟨h.exports, h.locals⟩
  · rintro ⟨exports, locals⟩
    exact ⟨exports, locals⟩

instance (row : EsmSplit.LocalExportUnit) (statement : EsmSplit.Stmt) :
    Decidable (row.FootprintMatches statement) :=
  decidable_of_iff _ (row_footprint_iff row statement).symm

/-- Each exported local must be declared by an executable original unit.
This is checked on the same full bundle and does not search the output. -/
def RowLocalsDefined (ss : List Stmt) (B : EsmSplit.Bundle) (row : EsmSplit.LocalExportUnit) : Prop :=
  ∀ pair ∈ row.names, ∃ j, j < ss.length ∧ pair.2 ∈ (B.stmt j).defs

instance (ss : List Stmt) (B : EsmSplit.Bundle) (row : EsmSplit.LocalExportUnit) :
    Decidable (RowLocalsDefined ss B row) := by
  unfold RowLocalsDefined
  infer_instance

#print axioms footprints_iff
#print axioms row_footprint_iff

end CoreJS.MetadataCells
