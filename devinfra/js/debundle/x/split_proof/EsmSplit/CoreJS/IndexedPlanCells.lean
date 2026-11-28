import EsmSplit.CoreJS.IndexedExportCells

/-! Linkage from the original indexed source and authored full plan. The
projection contract describes the actual generated CoreJS modules; the sparse
constructor must prove it without comparing executions or output values. -/

namespace CoreJS.IndexedPlanCells

/-- The structural interface of an original-ID module projection. These
fields specify construction, not equality of observed behavior or cells. -/
structure Projection (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (program : Program) (emit : EsmSplit.Module → Module) : Prop where
  find : ∀ name, program.find name = (P.find name).map emit
  body : ∀ md id stmt, (id, stmt) ∈ (emit md).body ↔ id ∈ md.stmts ∧ body.lookup id = some stmt
  exports : ∀ md, (emit md).exports = md.namedExports B
  imports :
    ∀ md,
      (emit md).imports =
        md.imports.map
          (fun (source, names) =>
            { source,
              names := names.map fun u => (EsmSplit.exportNameFor (P.namedExports B source) u, u) })

/-- Every original declaration in the full bundle is an executable source
unit. In particular an export-only row has no declaration of its own. -/
def DefinitionsPresent (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Prop :=
  ∀ id, id < B.length → ∀ name ∈ (B.stmt id).defs, ∃ stmt, body.lookup id = some stmt

/-- Each generated import resolves to the source scope address and the
original declaration owner, retaining its original source ID. -/
theorem import_cell {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} {program : Program} {emit : EsmSplit.Module → Module}
    (valid : EsmSplit.Valid B S P) (projection : Projection body B P program emit)
    (footprints : IndexedExportCells.Footprints body B) (complete : DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames) {source : Module}
    (sourceBody : source.body = body) {md : EsmSplit.Module} (moduleIn : md ∈ P.mods)
    {edge : String × List Name} (edgeIn : edge ∈ md.imports) {name : Name}
    (nameIn : name ∈ edge.2) :
    ∃ owner id a,
      P.find edge.1 = some owner ∧
        id ∈ owner.stmts ∧
        S.claimants B id = [owner.name] ∧
        name ∈ (B.stmt id).defs ∧
        program.importCell frames edge.1 (EsmSplit.exportNameFor (P.namedExports B edge.1) name) =
          .ok a ∧
        (source.scope frames).lookup name = some a := by
  obtain ⟨unique, -, faithful, linkable, exportsUnique, -, -, -, -⟩ := valid
  obtain ⟨declares, exports⟩ := ((linkable md moduleIn).1 edge edgeIn).2.2 name nameIn
  unfold EsmSplit.Plan.declaresIn at declares
  unfold EsmSplit.Plan.exportsName at exports
  cases found : P.find edge.1 with
  | none =>
    rw [found] at declares; cases declares
  | some owner =>
    rw [found] at declares exports
    have ownerIn : owner ∈ P.mods := List.mem_of_find?_eq_some found
    obtain ⟨id, owned, hasName⟩ :=
      List.any_eq_true.1 (by simpa using declares : owner.declares B name = true)
    have hasName := List.contains_iff_mem.1 hasName
    have inBundle := faithful.2.2.2.2.2.1 owner ownerIn id owned
    obtain ⟨stmt, inSource⟩ := complete id inBundle name hasName
    have inProvider : (id, stmt) ∈ (emit owner).body :=
      (projection.body owner id stmt).2 ⟨owned, inSource⟩
    have findProvider : program.find edge.1 = some (emit owner) := by
      rw [projection.find, found]; rfl
    have publicLookup :
      (emit owner).exports.lookup (EsmSplit.exportNameFor (P.namedExports B edge.1) name) =
        some name := by
      rw [projection.exports, EsmSplit.Plan.namedExports, found]
      simp only [Option.map_some, Option.getD_some]
      exact
        moduleOf_exports (ss := []) (P := P) owner (by simpa using exports)
          (exportsUnique owner ownerIn)
    obtain ⟨a, sourceCell, importCell⟩ :=
      IndexedExportCells.source_and_import_cell footprints frameNames unique sourceBody inSource
        inProvider hasName findProvider publicLookup
    exact
      ⟨owner, id, a, rfl, owned,
        (faithful.2.2.2.2.1 owner ownerIn id (List.mem_range.2 inBundle)).1 owned, hasName,
        importCell, sourceCell⟩

/-- The actual imports of any projected plan module link successfully and
all its declared or imported names have the source address, including names
used only by an export list. -/
theorem module_scope_cells {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} {program : Program} {emit : EsmSplit.Module → Module}
    (valid : EsmSplit.Valid B S P) (projection : Projection body B P program emit)
    (footprints : IndexedExportCells.Footprints body B) (complete : DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames) {source : Module}
    (sourceBody : source.body = body) {md : EsmSplit.Module} (moduleIn : md ∈ P.mods) :
    ∃ cells,
      program.imported frames (emit md).imports = .ok cells ∧
        ∀ name,
          (∃ id ∈ md.stmts, name ∈ (B.stmt id).defs) ∨ name ∈ md.imports.flatMap (·.2) →
            ((emit md).scope frames ++ cells).lookup name = (source.scope frames).lookup name := by
  have hv := valid
  obtain ⟨unique, -, faithful, linkable, -, -, -, -, -⟩ := valid
  have imported :
    ∃ cells,
      program.imported frames (emit md).imports = .ok cells ∧
        ∀ name,
          cells.lookup name =
            if name ∈ md.imports.flatMap (·.2) then (source.scope frames).lookup name
            else none := by
    rw [projection.imports]
    apply
      imported_ok program frames ((source.scope frames).lookup ·)
        (fun src u => EsmSplit.exportNameFor (P.namedExports B src) u) md.imports
    intro edge edgeIn
    refine ⟨?_, ?_⟩
    · rw [projection.find, Option.isSome_map]
      exact ((linkable md moduleIn).1 edge edgeIn).2.1
    · intro name nameIn
      obtain ⟨_, _, a, _, _, _, _, cell, original⟩ :=
        import_cell hv projection footprints complete frameNames sourceBody moduleIn edgeIn nameIn
      exact ⟨a, cell, original⟩
  obtain ⟨cells, linked, lookup⟩ := imported
  refine ⟨cells, linked, ?_⟩
  intro name admitted
  rw [List.lookup_append]
  by_cases declared : ∃ id ∈ md.stmts, name ∈ (B.stmt id).defs
  · obtain ⟨id, owned, hasName⟩ := declared
    have inBundle := faithful.2.2.2.2.2.1 md moduleIn id owned
    obtain ⟨stmt, inSource⟩ := complete id inBundle name hasName
    have inModule : (id, stmt) ∈ (emit md).body := (projection.body md id stmt).2 ⟨owned, inSource⟩
    obtain ⟨a, original, localCell⟩ :=
      IndexedExportCells.source_and_module_cell footprints frameNames unique sourceBody inSource
        inModule hasName
    rw [localCell, Option.some_or, original]
  · have noScope : ((emit md).scope frames).lookup name = none := by
      apply IndexedExportCells.scope_lookup_none footprints frameNames
      intro p hp hasName
      have owned := ((projection.body md p.1 p.2).1 hp).1
      exact declared ⟨p.1, owned, hasName⟩
    have isImport : name ∈ md.imports.flatMap (·.2) := admitted.resolve_left declared
    rw [noScope, Option.none_or, lookup]
    simp [isImport]

end CoreJS.IndexedPlanCells
