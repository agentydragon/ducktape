import EsmSplit.CoreJS.ExportRowsLink
import EsmSplit.CoreJS.IndexedUnitsAgree
import EsmSplit.CoreJS.ClassOrigin

/-! Live captured bindings for the actual indexed source/plan graph. Imports
are resolved by the existing linker, including when entry and reader import
one another. No module-order or value-stability assumption is used. -/

namespace CoreJS.IndexedAliasCapture

/-- A function whose free names occur in an actual source unit has the same
free-name capture and function construction in its authored module as in the
original. The imported frame comes from the real constructed program. -/
theorem module_capture {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (free : IndexedUnitsAgree.FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body) {md : EsmSplit.Module} (moduleIn : md ∈ P.mods)
    {id : Nat} {stmt : Stmt} (owned : id ∈ md.stmts) (original : body.lookup id = some stmt)
    {f : Func} (freeInStmt : ∀ name ∈ f.free, name ∈ stmt.free) :
    ∃ cells, (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P md).imports = .ok cells ∧
      let sourceEnv : Env := { vars := source.scope frames, this := .undef }
      let targetEnv : Env := { vars := (moduleOfIndexed body B P md).scope frames ++ cells, this := .undef }
      sourceEnv.capture f.free = targetEnv.capture f.free ∧
      (makeFunc sourceEnv f : M W Val) = makeFunc targetEnv f := by
  obtain ⟨cells, imported, agree⟩ := IndexedUnitsAgree.module_statement_agree valid footprints free complete
    frameNames sourceBody moduleIn owned original
  have functionAgree := agree.mono freeInStmt
  exact ⟨cells, imported, capture_agree functionAgree, makeFunc_agree functionAgree⟩

/-- A public alias and a function in any actual planned module retain the
same original cell. This includes an entry/reader cycle: the proof resolves
the constructed imports and never assumes an acyclic module graph. -/
theorem public_alias_captured {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (free : IndexedUnitsAgree.FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {source : Module} (sourceBody : source.body = body) {rows : List EsmSplit.LocalExportUnit}
    (sourceRows : ExportRows.SourceRows source rows) (sourceDeclared : ExportRows.LocalsDeclared source B)
    {entry : EsmSplit.Module} (entryIn : entry ∈ P.mods)
    {row : EsmSplit.LocalExportUnit} (rowIn : row ∈ rows) (ownedRow : row.ordinal ∈ entry.stmts)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {publicName localName : Name} (pair : (publicName, localName) ∈ row.names)
    {reader : EsmSplit.Module} (readerIn : reader ∈ P.mods)
    {id : Nat} {kind : DeclKind} {bindingName : Name} {f : Func}
    (ownedStmt : id ∈ reader.stmts) (original : body.lookup id = some (.decl kind bindingName (.func f)))
    (nonSelf : f.self = false) (captured : localName ∈ f.free) :
    ∃ entryCells readerCells declarationOwner declarationId address binding,
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P entry).imports = .ok entryCells ∧
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P reader).imports = .ok readerCells ∧
      S.claimants B row.ordinal = [entry.name] ∧ declarationOwner ∈ P.mods ∧
      declarationId ∈ declarationOwner.stmts ∧ S.claimants B declarationId = [declarationOwner.name] ∧
      localName ∈ (B.stmt declarationId).defs ∧
      (publicName, localName) ∈ (moduleOfIndexed body B P entry).exports ∧
      (source.scope frames).lookup localName = some address ∧
      ((moduleOfIndexed body B P entry).scope frames ++ entryCells).lookup localName = some address ∧
      (({ vars := (moduleOfIndexed body B P reader).scope frames ++ readerCells, this := .undef } : Env).capture f.closes).vars.lookup localName = some address ∧
      s'.cellAt address = some binding := by
  obtain ⟨entryCells, declarationOwner, declarationId, address, binding, entryImports, rowClaim,
    declarationIn, declarationOwned, declarationClaim, declared, publicAlias, sourceCell, entryCell, present⟩ :=
    ExportRows.row_alias_live_cell valid footprints complete allocated sourceBody sourceRows sourceDeclared
      entryIn rowIn ownedRow rowFootprint pair
  have freeInStmt : ∀ name ∈ f.free, name ∈ (Stmt.decl kind bindingName (.func f)).free := by
    intro name member
    simp [Stmt.free, Expr.free, member]
  obtain ⟨readerCells, readerImports, captures, _⟩ := module_capture (W := W) valid footprints free complete
    (declareTops_names body allocated) sourceBody readerIn ownedStmt original freeInStmt
  have readerCell : (({ vars := (moduleOfIndexed body B P reader).scope frames ++ readerCells, this := .undef } : Env).capture f.free).vars.lookup localName = some address := by
    rw [← captures]
    simpa [Env.capture, lookup_filterMap, List.mem_eraseDups, captured] using sourceCell
  have actualCapture : f.closes = f.free := by simp [Func.closes, nonSelf]
  rw [actualCapture]
  exact ⟨entryCells, readerCells, declarationOwner, declarationId, address, binding,
    entryImports, readerImports, rowClaim, declarationIn, declarationOwned, declarationClaim,
    declared, publicAlias, sourceCell, entryCell, readerCell, present⟩

/-- A successful write to a shared mutable binding is visible through both a
reader environment and the entry environment. These environments may be the
actual captures returned above; the written value is arbitrary. -/
theorem write_through_shared_cell {W : World} {writer reader entry : Env}
    {name : Name} {address : Addr}
    (writerCell : writer.vars.lookup name = some address)
    (readerCell : reader.vars.lookup name = some address)
    (entryCell : entry.vars.lookup name = some address)
    {s t : State W} {value : Val}
    (write : writeVar writer name value s = .ok () t) :
    readVar reader name t = .ok value t ∧
      readVar entry name t = .ok value t := by
  obtain ⟨actual, actualCell, updated, _⟩ := writeVar_origin write
  have same : actual = address := Option.some.inj (actualCell.symm.trans writerCell)
  subst actual
  constructor <;> simp [readVar, readerCell, entryCell, updated, M.bind_apply, getState, pure]

/-- The actual planned setter and reader capture the original public alias's
cell. Any successful later write through the setter capture is seen through
both the reader capture and the entry scope, for any value and later state. -/
theorem public_alias_write_through {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (free : IndexedUnitsAgree.FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {source : Module} (sourceBody : source.body = body) {rows : List EsmSplit.LocalExportUnit}
    (sourceRows : ExportRows.SourceRows source rows) (sourceDeclared : ExportRows.LocalsDeclared source B)
    {entry : EsmSplit.Module} (entryIn : entry ∈ P.mods)
    {row : EsmSplit.LocalExportUnit} (rowIn : row ∈ rows) (ownedRow : row.ordinal ∈ entry.stmts)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {publicName localName : Name} (pair : (publicName, localName) ∈ row.names)
    {reader : EsmSplit.Module} (readerIn : reader ∈ P.mods)
    {readerId : Nat} {kind : DeclKind} {readerName : Name} {readerFn : Func}
    (readerOwned : readerId ∈ reader.stmts)
    (readerSource : body.lookup readerId = some (.decl kind readerName (.func readerFn)))
    (readerNonSelf : readerFn.self = false) (readerCaptures : localName ∈ readerFn.free)
    {writer : EsmSplit.Module} (writerIn : writer ∈ P.mods)
    {writerId : Nat} {writerFn : Func} (writerOwned : writerId ∈ writer.stmts)
    (writerSource : body.lookup writerId = some (.funDecl writerFn))
    (writerNonSelf : writerFn.self = false) (writerCaptures : localName ∈ writerFn.free) :
    ∃ entryCells readerCells writerCells address,
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P entry).imports = .ok entryCells ∧
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P reader).imports = .ok readerCells ∧
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P writer).imports = .ok writerCells ∧
      (publicName, localName) ∈ (moduleOfIndexed body B P entry).exports ∧
      (({ vars := (moduleOfIndexed body B P writer).scope frames ++ writerCells, this := .undef } : Env).capture writerFn.closes).vars.lookup localName = some address ∧
      (({ vars := (moduleOfIndexed body B P reader).scope frames ++ readerCells, this := .undef } : Env).capture readerFn.closes).vars.lookup localName = some address ∧
      ((moduleOfIndexed body B P entry).scope frames ++ entryCells).lookup localName = some address ∧
      ∀ (before after : State W) (value : Val),
        writeVar (({ vars := (moduleOfIndexed body B P writer).scope frames ++ writerCells, this := .undef } : Env).capture writerFn.closes)
          localName value before = .ok () after →
        readVar (({ vars := (moduleOfIndexed body B P reader).scope frames ++ readerCells, this := .undef } : Env).capture readerFn.closes)
          localName after = .ok value after ∧
        readVar { vars := (moduleOfIndexed body B P entry).scope frames ++ entryCells, this := .undef }
          localName after = .ok value after := by
  obtain ⟨entryCells, readerCells, _, _, address, _, entryImports, readerImports, _, _, _, _, _,
    publicAlias, sourceCell, entryCell, readerCell, _⟩ :=
    public_alias_captured valid footprints free complete allocated sourceBody sourceRows sourceDeclared
      entryIn rowIn ownedRow rowFootprint pair readerIn readerOwned readerSource readerNonSelf readerCaptures
  have writerFree : ∀ name ∈ writerFn.free, name ∈ (Stmt.funDecl writerFn).free := by
    intro name member
    simp [Stmt.free, member]
  obtain ⟨writerCells, writerImports, writerCapture, _⟩ := module_capture (W := W) valid footprints free complete
    (declareTops_names body allocated) sourceBody writerIn writerOwned writerSource writerFree
  have writerCell : (({ vars := (moduleOfIndexed body B P writer).scope frames ++ writerCells, this := .undef } : Env).capture writerFn.closes).vars.lookup localName = some address := by
    rw [show writerFn.closes = writerFn.free by simp [Func.closes, writerNonSelf]]
    rw [← writerCapture]
    simpa [Env.capture, lookup_filterMap, List.mem_eraseDups, writerCaptures] using sourceCell
  refine ⟨entryCells, readerCells, writerCells, address, entryImports, readerImports, writerImports,
    publicAlias, writerCell, readerCell, entryCell, ?_⟩
  intro before after value write
  exact write_through_shared_cell writerCell readerCell entryCell write

#print axioms module_capture
#print axioms public_alias_captured
#print axioms write_through_shared_cell
#print axioms public_alias_write_through

end CoreJS.IndexedAliasCapture
