import EsmSplit.CoreJS.ExportRowsLink
import EsmSplit.Split

/-! Source-ordered metadata is selected from an independently sorted list of
actual plan unit IDs. Executable IDs may occur anywhere between the rows. -/

namespace CoreJS.ExportRows

/-- A source row with a unique original ID is found at that ID. -/
theorem rowAt_found {rows : List EsmSplit.LocalExportUnit}
    (unique : (rows.map (·.ordinal)).Nodup) {row : EsmSplit.LocalExportUnit} (member : row ∈ rows) :
    rowAt rows row.ordinal = some row := by
  have inMap : (row.ordinal, row) ∈ rows.map (fun r => (r.ordinal, r)) :=
    List.mem_map.mpr ⟨row, member, rfl⟩
  have uniqueKeys : ((rows.map (fun r => (r.ordinal, r))).map Prod.fst).Nodup := by
    simpa [List.map_map, Function.comp_def] using unique
  unfold rowAt
  apply lookup_of_mem inMap
  intro other otherIn
  exact nodup_keys uniqueKeys otherIn inMap

/-- Ordered, unique source rows selected from ordered plan IDs occur exactly in
source order whenever each source row is present. No assertion is made about
where executable IDs occur relative to metadata in module evaluation order. -/
theorem select_ordered_rows {ids : List Nat} {rows : List EsmSplit.LocalExportUnit}
    (idsOrdered : ids.Pairwise (· < ·))
    (rowsOrdered : rows.Pairwise (fun a b => a.ordinal < b.ordinal))
    (owned : ∀ row ∈ rows, row.ordinal ∈ ids) : ids.filterMap (rowAt rows) = rows := by
  have keysOrdered : (rows.map (·.ordinal)).Pairwise (· < ·) := List.pairwise_map.mpr rowsOrdered
  have uniqueKeys : (rows.map (·.ordinal)).Nodup := keysOrdered.imp (by
    intro a b less same
    subst b
    exact (Nat.lt_irrefl a) less)
  have selectedOrdered : (ids.filterMap (rowAt rows)).Pairwise
      (fun a b => a.ordinal < b.ordinal) := List.pairwise_filterMap.mpr (idsOrdered.imp (by
    intro i j less a foundA b foundB
    rw [(rowAt_some foundA).2, (rowAt_some foundB).2]
    exact less))
  have selectedUnique : (ids.filterMap (rowAt rows)).Nodup := selectedOrdered.imp (by
    intro a b less same
    subst b
    exact (Nat.lt_irrefl a.ordinal) less)
  have rowsUnique : rows.Nodup := rowsOrdered.imp (by
    intro a b less same
    subst b
    exact (Nat.lt_irrefl a.ordinal) less)
  have sameMembers : ∀ row, row ∈ ids.filterMap (rowAt rows) ↔ row ∈ rows := by
    intro row
    constructor
    · intro inSelected
      obtain ⟨id, _, found⟩ := List.mem_filterMap.mp inSelected
      exact (rowAt_some found).1
    · intro inRows
      exact List.mem_filterMap.mpr ⟨row.ordinal, owned row inRows, rowAt_found uniqueKeys inRows⟩
  have perm : (ids.filterMap (rowAt rows)).Perm rows :=
    (List.perm_ext_iff_of_nodup selectedUnique rowsUnique).2 sameMembers
  exact perm.eq_of_pairwise (le := fun a b => a.ordinal < b.ordinal)
    (fun a b _ _ ab ba => False.elim (Nat.lt_asymm ab ba)) selectedOrdered rowsOrdered

#print axioms rowAt_found
#print axioms select_ordered_rows

end CoreJS.ExportRows

namespace EsmSplit

private theorem picked_ordered (S : Spec) (B : Bundle) {entry : ModName × List Nat}
    (member : entry ∈ S.picked B) : entry.2.Pairwise (· < ·) := by
  simp only [Spec.picked, List.mem_map] at member
  obtain ⟨⟨name, selectors⟩, _, rfl⟩ := member
  let picked : Std.TreeSet Nat :=
    ((selectors.flatMap (·.picks B.declIdx B.length)).foldl (·.insert ·) ∅)
  have ordered := Std.TreeSet.ordered_toList (t := picked)
  simpa only [Nat.compare_eq_lt] using ordered

private theorem picked_map_ordered (entries : List (ModName × List Nat))
    (ordered : ∀ entry ∈ entries, entry.2.Pairwise (· < ·)) (name : ModName) :
    ((entries.foldr (fun e acc => acc.insert e.1 e.2) (∅ : Std.TreeMap ModName (List Nat))).getD name []).Pairwise (· < ·) := by
  induction entries with
  | nil => simp
  | cons first rest ih =>
    simp only [List.foldr_cons, Std.TreeMap.getD_insert]
    split
    · exact ordered first (by simp)
    · exact ih (fun entry member => ordered entry (by simp [member]))

private theorem stmts_after_import_update (m : Module) (condition : Prop) [Decidable condition]
    (imports : List (ModName × List Name)) :
    (if condition then { m with imports := imports } else m).stmts = m.stmts := by
  split <;> rfl

/-- Every actual planner module keeps its selected source IDs in increasing
order, independently of the order in which modules are evaluated. -/
theorem mkPlan_module_ordered (B : Bundle) (S : Spec) {md : Module}
    (member : md ∈ (mkPlan B S).mods) : md.stmts.Pairwise (· < ·) := by
  simp only [mkPlan, List.mem_map] at member
  obtain ⟨mid, ⟨base, ⟨spec, specIn, rfl⟩, rfl⟩, eq⟩ := member
  have same := congrArg Module.stmts eq
  rw [stmts_after_import_update] at same
  rw [← same]
  exact picked_map_ordered (S.picked B) (fun entry inPicked => picked_ordered S B inPicked) _

#print axioms mkPlan_module_ordered

end EsmSplit

namespace CoreJS.ExportRows

/-- Actual planner selection preserves source metadata order when every
source row is owned by the entry. No condition relates executable and
metadata positions in the plan's module evaluation order. -/
theorem mkPlan_select_source_rows {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)) {owner : EsmSplit.Module}
    (found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some owner)
    {rows : List EsmSplit.LocalExportUnit}
    (rowsOrdered : rows.Pairwise (fun a b => a.ordinal < b.ordinal))
    (within : ∀ row ∈ rows, row.ordinal < B.length)
    (claims : ∀ row ∈ rows, S.claimants B row.ordinal = [S.entry]) :
    owner.stmts.filterMap (rowAt rows) = rows := by
  have ownerIn : owner ∈ (EsmSplit.mkPlan B S).mods := List.mem_of_find?_eq_some found
  have entryName : owner.name = S.entry := by
    have nameEq := List.find?_some found
    have actual : owner.name = (EsmSplit.mkPlan B S).entry := by
      simpa [EsmSplit.Plan.find] using nameEq
    exact actual.trans rfl
  apply select_ordered_rows (EsmSplit.mkPlan_module_ordered B S ownerIn) rowsOrdered
  intro row inRows
  exact (valid.2.2.1.2.2.2.2.1 owner ownerIn row.ordinal
    (List.mem_range.2 (within row inRows))).2 (by simpa [entryName] using claims row inRows)

#print axioms mkPlan_select_source_rows

end CoreJS.ExportRows
