import EsmSplit.CoreJS.TerminalEntryNames
import EsmSplit.CoreJS.ExportRows
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
  by_cases absent : row.ordinal ∉ prior
  · have onlyRow : ∀ id ∈ md.stmts, id ≠ row.ordinal → (B.stmt id).exports = [] := by
      intro id member different
      rw [units, List.mem_append] at member
      rcases member with inPrior | inLast
      · exact unpublished id inPrior
      · simp only [List.mem_singleton] at inLast
        exact (different inLast).elim
    have matched := ExportRows.singleton_matches footprint onlyRow
    have selected : md.stmts.filterMap (ExportRows.rowAt [row]) = [row] := by
      rw [units, List.filterMap_append, ExportRows.singleton_absent row prior absent]
      simp [ExportRows.rowAt_singleton]
    change md.namedExports B = row.names
    simpa using ExportRows.exact_entry_exports matched selected glue
  · have inPrior : row.ordinal ∈ prior := Classical.not_not.mp absent
    have noExports := unpublished row.ordinal inPrior
    rw [footprint.exports] at noExports
    have emptyNames : row.names = [] := List.map_eq_nil_iff.mp noExports
    have unitEmpty : ∀ id ∈ md.stmts, (B.stmt id).exports = [] := by
      intro id member
      rw [units, List.mem_append] at member
      rcases member with inPrior | inLast
      · exact unpublished id inPrior
      · simp only [List.mem_singleton] at inLast
        subst id
        exact unpublished row.ordinal inPrior
    have matched : ∀ id ∈ md.stmts, ExportRows.UnitMatches B [] id := by
      intro id member
      simpa [ExportRows.UnitMatches, ExportRows.rowAt] using unitEmpty id member
    have selected : md.stmts.filterMap (ExportRows.rowAt []) = [] := by
      simp [ExportRows.rowAt]
    change md.namedExports B = row.names
    simpa [emptyNames] using ExportRows.exact_entry_exports matched selected glue

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
