import EsmSplit.CoreJS.TerminalPlanScope
import EsmSplit.CoreJS.TerminalLinkedExports

/-! Compose the source/actual-plan link with the complete original entry
export table. The footprint and entry-shape premises are input checks, not
claims about arbitrary parsed JavaScript or output bytes. -/

namespace CoreJS.TerminalPlanFamily

open LinkedBehavior

/-- The depth-first traversal starts with an unvisited entry and appends it
after visiting its imports, independently of whether the plan is valid. -/
theorem entry_in_order (P : EsmSplit.Plan) : P.entry ∈ P.evalOrder := by
  simp [EsmSplit.Plan.evalOrder, EsmSplit.Plan.visit, EsmSplit.visitImports]

/-- For each world, the actual source and authored split have the same
complete ordered public cell table and every modeled execution result. -/
theorem world_result {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (consts : ∀ s ∈ ss, ∃ x value, s = .decl .const x value)
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)) (footprints : MetadataCells.Footprints ss B)
    (ordered : ProjectedOrder.Kept ss (EsmSplit.mkPlan B S)) (name : Name)
    (row : EsmSplit.LocalExportUnit) (entry : EsmSplit.Module)
    (found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some entry)
    (units : entry.stmts = [row.ordinal]) (glue : entry.exports = [])
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    (sourceDefs : ∀ pair ∈ row.names, ∃ j, j < ss.length ∧ pair.2 ∈ (B.stmt j).defs)
    (w : World) :
    ∃ table,
      LinkedBehavior.exports w (TerminalPlanLink.source name row.names ss) = some table ∧
      LinkedBehavior.exports w (splitProgram ss B (EsmSplit.mkPlan B S)) = some table ∧
      ∀ fuel, runProgram w fuel (TerminalPlanLink.source name row.names ss) =
        runProgram w fuel (splitProgram ss B (EsmSplit.mkPlan B S)) := by
  obtain ⟨frames, before, after, _, names, sourceLinked, scopes, _, _, _, outcomes⟩ :=
    TerminalPlanScope.const_planned_links consts valid footprints ordered name row.names w
  have declared := TerminalEntryCells.declared_rows_of_source valid footprints sourceDefs
  obtain ⟨table, sourceTable, candidateTable⟩ := TerminalLinkedExports.linked_table
    valid footprints names found (entry_in_order (EsmSplit.mkPlan B S)) units glue rowFootprint declared scopes name
  refine ⟨table, ?_, ?_, outcomes⟩
  · unfold LinkedBehavior.exports
    rw [before.equation]
    change (EntryExports.resolve name before.linked).toOption = some table
    rw [sourceLinked, sourceTable]
    rfl
  · unfold LinkedBehavior.exports
    rw [after.equation]
    change (EntryExports.resolve (EsmSplit.mkPlan B S).entry after.linked).toOption = some table
    rw [candidateTable]
    rfl

/-- Modeled execution equivalence and public live-cell equivalence for the
actual constructed plan, uniformly over all worlds and fuel. -/
theorem family {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (consts : ∀ s ∈ ss, ∃ x value, s = .decl .const x value)
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)) (footprints : MetadataCells.Footprints ss B)
    (ordered : ProjectedOrder.Kept ss (EsmSplit.mkPlan B S)) (name : Name)
    (row : EsmSplit.LocalExportUnit) (entry : EsmSplit.Module)
    (found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some entry)
    (units : entry.stmts = [row.ordinal]) (glue : entry.exports = [])
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    (sourceDefs : ∀ pair ∈ row.names, ∃ j, j < ss.length ∧ pair.2 ∈ (B.stmt j).defs) :
    LinkedBehavior.SameExports (TerminalPlanLink.source name row.names ss) (splitProgram ss B (EsmSplit.mkPlan B S)) ∧
      ∀ w fuel, runProgram w fuel (TerminalPlanLink.source name row.names ss) =
        runProgram w fuel (splitProgram ss B (EsmSplit.mkPlan B S)) := by
  constructor
  · intro w
    obtain ⟨table, before, after, _⟩ := world_result consts valid footprints ordered name row entry found units glue
      rowFootprint sourceDefs w
    exact ⟨table, table, before, after, List.Perm.refl _⟩
  · intro w fuel
    exact (world_result consts valid footprints ordered name row entry found units glue rowFootprint sourceDefs w).choose_spec.2.2 fuel

#print axioms entry_in_order
#print axioms world_result
#print axioms family

end CoreJS.TerminalPlanFamily
