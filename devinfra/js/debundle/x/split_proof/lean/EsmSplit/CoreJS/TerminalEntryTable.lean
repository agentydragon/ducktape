import EsmSplit.CoreJS.TerminalEntryNames
import EsmSplit.CoreJS.ExportRows

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

#print axioms exact_exports

end CoreJS.TerminalEntryTable
