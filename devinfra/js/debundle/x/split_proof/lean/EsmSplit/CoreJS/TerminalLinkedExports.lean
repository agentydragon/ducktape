import EsmSplit.CoreJS.LinkedEntryFind
import EsmSplit.CoreJS.TerminalEntryCells
import EsmSplit.CoreJS.TerminalEntryTable

/-! The original row and actual linked entry have the same complete public
address table once actual candidate scopes use the source allocation frames. -/

namespace CoreJS.TerminalLinkedExports

/-- No candidate link is assumed by the source-plan conditions. The `scopes`
equation is the actual linker intermediate, supplied by the full-plan link
proof using the source allocation frames. -/
theorem linked_table {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : MetadataCells.Footprints ss B)
    {frames : List (Nat × List (Name × Addr))} (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {entry : EsmSplit.Module} (entryFound : P.find P.entry = some entry) (entryLoaded : P.entry ∈ P.evalOrder)
    {row : EsmSplit.LocalExportUnit} {prior : List Nat}
    (units : entry.stmts = prior ++ [row.ordinal])
    (unpublished : ∀ i ∈ prior, (B.stmt i).exports = []) (glue : entry.exports = [])
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    (sourceDefs : TerminalEntryCells.DeclaredRows ss B P row)
    {linked : List (Module × Env)}
    (scopes : (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok linked)
    (sourceName : String) :
    let source : Module := ⟨sourceName, [], (List.range ss.length).zip ss, row.names⟩
    ∃ table, EntryExports.resolve sourceName [(source, bundleEnv ss frames)] = .ok table ∧
      EntryExports.resolve P.entry linked = .ok table := by
  have entryIn : entry ∈ P.mods := List.mem_of_find?_eq_some entryFound
  have owned : row.ordinal ∈ entry.stmts := by rw [units]; simp
  obtain ⟨cells, imports, bindings⟩ :=
    TerminalEntryCells.entry_cells valid footprints hF entryIn owned rowFootprint sourceDefs
  have loaded := LinkedEntryFind.loaded_find (ss := ss) (B := B) entryFound entryLoaded
  have linkedFind := LinkedEntryFind.scopes_find (splitProgram ss B P) frames P.entry
    (moduleOf ss B P entry) cells imports _ _ loaded scopes
  let source : Module := ⟨sourceName, [], (List.range ss.length).zip ss, row.names⟩
  let candidateEnv : Env := { vars := (moduleOf ss B P entry).scope frames ++ cells, this := .undef }
  have sourceFind : [(source, bundleEnv ss frames)].find? (fun (m, _) => m.name == sourceName) =
      some (source, bundleEnv ss frames) := by simp [source]
  have rightFind : linked.find? (fun (m, _) => m.name == P.entry) =
      some (moduleOf ss B P entry, candidateEnv) := linkedFind
  have exports : (moduleOf ss B P entry).exports = row.names :=
    TerminalEntryTable.exact_exports units unpublished glue rowFootprint
  apply TerminalEntryTable.resolve_table sourceFind rightFind rfl exports
  intro pair member
  exact bindings pair member

#print axioms linked_table

end CoreJS.TerminalLinkedExports
