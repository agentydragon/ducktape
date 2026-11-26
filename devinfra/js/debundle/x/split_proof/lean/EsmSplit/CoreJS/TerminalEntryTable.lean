import EsmSplit.CoreJS.TerminalEntryNames
import EsmSplit.CoreJS.LinkedBehavior

/-! Ordered entry export tables for a terminal metadata row. Cell
existence and equality must be proved from the source and actual split scopes;
this module does not perform an output comparison or assume linking succeeded. -/

namespace CoreJS.TerminalEntryTable

/-- A terminal row and earlier unpublished units expose exactly the original
public names and aliases. Generated glue is independently required to be empty. -/
theorem exact_exports {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {md : EsmSplit.Module} {row : EsmSplit.LocalExportUnit} {prior : List Nat}
    (units : md.stmts = prior ++ [row.ordinal])
    (unpublished : ∀ i ∈ prior, (B.stmt i).exports = []) (glue : md.exports = [])
    (footprint : row.FootprintMatches (B.stmt row.ordinal)) :
    (moduleOf ss B P md).exports = row.names := by
  have empty : ∀ ids : List Nat, (∀ i ∈ ids, (B.stmt i).exports = []) →
      ids.flatMap (fun i => (B.stmt i).exports) = [] := by
    intro ids
    induction ids with
    | nil => simp
    | cons i rest ih =>
      intro h
      simp only [List.flatMap_cons]
      rw [h i (by simp)]
      simp only [List.nil_append]
      apply ih
      intro j hj
      exact h j (by simp [hj])
  change md.namedExports B = row.names
  simp only [EsmSplit.Module.namedExports, EsmSplit.Module.namedExportsFrom, glue, List.nil_append,
    EsmSplit.Module.publishedFrom, units, List.flatMap_append, empty prior unpublished,
    List.nil_append, List.flatMap_cons, List.flatMap_nil, List.append_nil,
    EsmSplit.Bundle.toArray_getD, footprint.exports, EsmSplit.localExports, List.filterMap_map]
  generalize row.names = names
  induction names with
  | nil => rfl
  | cons pair rest ih => cases pair <;> simp [Function.comp_def]

/-- If two actual linked entry environments resolve each local to the same
present address, the complete ordered public table resolves to exactly the
same addresses on both sides. Aliases are retained by the list traversal. -/
theorem resolve_table {leftEntry rightEntry : String} {left right : List (Module × Env)}
    {lm rm : Module} {le re : Env} {pairs : List (Name × Name)}
    (lfind : left.find? (fun (m, _) => m.name == leftEntry) = some (lm, le))
    (rfind : right.find? (fun (m, _) => m.name == rightEntry) = some (rm, re))
    (lrows : lm.exports = pairs) (rrows : rm.exports = pairs)
    (cells : ∀ pair ∈ pairs, ∃ a, le.vars.lookup pair.2 = some a ∧ re.vars.lookup pair.2 = some a) :
    ∃ table, EntryExports.resolve leftEntry left = .ok table ∧ EntryExports.resolve rightEntry right = .ok table := by
  have loop : ∀ (xs : List (Name × Name)),
      (∀ pair ∈ xs, ∃ a, le.vars.lookup pair.2 = some a ∧ re.vars.lookup pair.2 = some a) →
      ∃ table,
        xs.mapM (fun (publicName, localName) => (do
          let some cell := le.vars.lookup localName | throw s!"unbound entry export {publicName}"
          return (publicName, cell) : Except String (Name × Addr))) = .ok table ∧
        xs.mapM (fun (publicName, localName) => (do
          let some cell := re.vars.lookup localName | throw s!"unbound entry export {publicName}"
          return (publicName, cell) : Except String (Name × Addr))) = .ok table := by
    intro xs
    induction xs with
    | nil => intro _; exact ⟨[], rfl, rfl⟩
    | cons pair rest ih =>
      intro each
      obtain ⟨a, la, ra⟩ := each pair (by simp)
      obtain ⟨table, lt, rt⟩ := ih (fun p hp => each p (by simp [hp]))
      refine ⟨(pair.1, a) :: table, ?_, ?_⟩ <;>
        cases pair <;> simp_all <;> rfl
  obtain ⟨table, lt, rt⟩ := loop pairs cells
  refine ⟨table, ?_, ?_⟩
  · unfold EntryExports.resolve
    rw [lfind]
    simp only [lrows]
    exact lt
  · unfold EntryExports.resolve
    rw [rfind]
    simp only [rrows]
    exact rt

#print axioms exact_exports
#print axioms resolve_table

end CoreJS.TerminalEntryTable
