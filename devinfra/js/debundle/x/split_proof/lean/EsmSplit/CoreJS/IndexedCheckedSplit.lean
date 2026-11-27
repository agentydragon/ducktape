import EsmSplit.CoreJS.IndexedConstLink
import EsmSplit.CoreJS.IndexedInputChecks

/-! A checked original-ID constructor. All checks inspect source and plan; the
returned CoreJS program is the indexed constructor, not a replayed pair. -/

namespace CoreJS.IndexedCheckedSplit

open LinkedBehavior

def assignments (body : List (Nat × Stmt)) (P : EsmSplit.Plan) : List UnitPlacement :=
  P.mods.flatMap fun m => (m.stmts.filterMap (indexedNumbered body)).map fun row => (row.1, m.name)

private def isConst : Stmt → Bool
  | .decl .const _ _ => true
  | _ => false

private theorem isConst_sound {stmt : Stmt} (accepted : isConst stmt = true) :
    ∃ name init, stmt = .decl .const name init := by
  cases stmt <;> simp [isConst] at accepted ⊢
  case decl kind name init =>
    cases kind <;> simp [isConst] at accepted ⊢

private theorem places {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (singleton : sourceProgram.modules = [source]) (sourceSafe : EsmSplit.safeModulePath source.name = true)
    (safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true)
    (unique : (body.map Prod.fst).Nodup)
    (coverage : ((assignments body P).map Prod.fst).Perm (body.map Prod.fst)) :
    Places (assignments body P) sourceProgram (splitProgramIndexed body B P) := by
  refine ⟨coverage.nodup_iff.mpr unique, ?_, ?_⟩
  · unfold Program.fileUnitPlacements Module.unitPlacements
    rw [singleton]
    simp only [sourceSafe, List.filter_cons, ↓reduceIte, List.filter_nil, List.flatMap_cons,
      List.flatMap_nil, List.append_nil, sourceBody, List.map_map]
    exact coverage.symm
  · rw [SparseProjectedOrder.split_indexed_fileUnitPlacements safe]
    exact List.Perm.refl _

/-- The computed candidate and its strict obligations use the actual source,
planner bundle, original rows, and supplied request. -/
structure Construction (source : Program) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (rows : List EsmSplit.LocalExportUnit) (requested : Guarantee) where
  body : List (Nat × Stmt)
  sourceCoverage : ((body.map Prod.fst) ++ rows.map (·.ordinal)).Perm (List.range B.length)
  plan : EsmSplit.Plan
  planEq : plan = EsmSplit.mkPlan B S
  publicEntry : S.privateEntryBody = false
  orderRequiresSequential : S.order ≠ [] → S.sequential = true
  schedule : S.sequential = true → plan.evalOrder = S.order ++ [S.entry]
  program : Program
  constructed : program = splitProgramIndexed body B plan
  assignment : List UnitPlacement
  assignmentEq : assignment = assignments body plan
  placement : Places assignment source program
  authored : ∀ id owner, (id, owner) ∈ assignment → S.claimants B id = [owner]
  rowOwner : ∀ row ∈ rows, S.claimants B row.ordinal = [S.entry]
  rowPlaced : ∀ row ∈ rows, ∃ entry, plan.find plan.entry = some entry ∧ row.ordinal ∈ entry.stmts ∧
    ∀ stmt, (row.ordinal, stmt) ∉ body
  sameExports : SameExports source program
  allOutcomes : ∀ w fuel, runProgram w fuel source = runProgram w fuel program
  behavior : ScheduleEquivalent requested (fun _ => True) source program

private theorem forRequest {source output : Program} (requested : Guarantee)
    (runs : ∀ w n, runProgram w n source = runProgram w n output) :
    ScheduleEquivalent requested (fun _ => True) source output := by
  cases requested with
  | allOutcomes => exact fun w _ n => runs w n
  | normalCompletion =>
    apply normalEquivalent_of_bounded
    intro w _ n final
    rw [runs w n]

/-- Check only source and plan conditions before returning the sole constructed graph. -/
def construct (original : Program) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (rows : List EsmSplit.LocalExportUnit) (requested : Guarantee) :
    Except String (Construction original B S rows requested) := do
  let .isTrue publicEntry := (inferInstance : Decidable (S.privateEntryBody = false)) |
    throw "checked split requires no private entry body"
  let .isTrue orderRequiresSequential := (inferInstance : Decidable (S.order ≠ [] → S.sequential = true)) |
    throw "an explicit module order requires a sequential request"
  match single : original.modules with
  | [source] =>
    if sourceEntry : original.entry = source.name then
      if sourceImports : source.imports = [] then
        if sourceSafe : EsmSplit.safeModulePath source.name = true then
          let body := source.body
          if checked : IndexedInputChecks.FootprintChecks body B then
            if defined : IndexedInputChecks.DefinitionChecks body B then
              if ordered : body.Pairwise (fun a b => a.1 ≤ b.1) then
                if consts : ∀ p ∈ body, isConst p.2 = true then
                  let P := EsmSplit.mkPlan B S
                  let .isTrue schedule := (inferInstance : Decidable
                      (S.sequential = true → P.evalOrder = S.order ++ [S.entry])) |
                    throw "constructed modules do not follow the requested order"
                  if valid : EsmSplit.Valid B S P then
                    if kept : SparseProjectedOrder.OrderKept body P then
                      if safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true then
                        let labels := assignments body P
                        if coverage : (labels.map Prod.fst).Perm (body.map Prod.fst) then
                          if sourceCoverage : ((body.map Prod.fst) ++ rows.map (·.ordinal)).Perm
                              (List.range B.length) then
                            if table : source.exports = rows.flatMap (·.names) then
                              if unique : (rows.map (·.ordinal)).Nodup then
                                if absent : ∀ row ∈ rows, row.ordinal ∉ body.map Prod.fst then
                                  if rowDefs : ∀ pair ∈ rows.flatMap (·.names), ∃ id ∈ List.range B.length,
                                      pair.2 ∈ (B.stmt id).defs then
                                    match planEntry : P.find P.entry with
                                    | none => throw "plan has no entry"
                                    | some owner =>
                                      if matched : ∀ id ∈ owner.stmts, ExportRows.UnitMatches B rows id then
                                        if rowOrder : owner.stmts.filterMap (ExportRows.rowAt rows) = rows then
                                          if glue : owner.exports = [] then
                                            have found : original.find original.entry = some source := by
                                              simp [Program.find, single, sourceEntry]
                                            have footprint := (IndexedInputChecks.checked_footprints checked).1
                                            have freeFootprint := (IndexedInputChecks.checked_footprints checked).2
                                            have complete := IndexedInputChecks.checked_definitions defined
                                            have sourceConsts : ∀ p ∈ body, ∃ name init,
                                                p.2 = .decl .const name init := fun p hp => isConst_sound (consts p hp)
                                            have sourceRows : ExportRows.SourceRows source rows := ⟨table, unique, absent⟩
                                            have defs : ∀ pair ∈ rows.flatMap (·.names), ∃ id, id < B.length ∧
                                                pair.2 ∈ (B.stmt id).defs := by
                                              intro pair hp
                                              obtain ⟨id, inRange, nameIn⟩ := rowDefs pair hp
                                              exact ⟨id, List.mem_range.1 inRange, nameIn⟩
                                            have ownerIn : owner ∈ P.mods := List.mem_of_find?_eq_some planEntry
                                            have entryName : owner.name = S.entry := by
                                              have eq := List.find?_some planEntry
                                              have nameEq : owner.name = P.entry := by
                                                simpa [EsmSplit.Plan.find] using eq
                                              exact nameEq.trans rfl
                                            have rowFacts : ∀ row ∈ rows, row.ordinal ∈ owner.stmts ∧
                                                S.claimants B row.ordinal = [owner.name] := by
                                              intro row member
                                              obtain ⟨owned, _⟩ := ExportRows.selected_row matched rowOrder member
                                              have within := valid.2.2.1.2.2.2.2.2.1 owner ownerIn row.ordinal owned
                                              exact ⟨owned, (valid.2.2.1.2.2.2.2.1 owner ownerIn row.ordinal
                                                (List.mem_range.2 within)).1 owned⟩
                                            have rowOwner : ∀ row ∈ rows, S.claimants B row.ordinal = [S.entry] := by
                                              intro row member
                                              simpa [entryName] using (rowFacts row member).2
                                            have rowPlaced : ∀ row ∈ rows, ∃ entry, P.find P.entry = some entry ∧
                                                row.ordinal ∈ entry.stmts ∧ ∀ stmt, (row.ordinal, stmt) ∉ body := by
                                              intro row member
                                              refine ⟨owner, planEntry, (rowFacts row member).1, ?_⟩
                                              intro stmt inBody
                                              exact absent row member (List.mem_map.2 ⟨(row.ordinal, stmt), inBody, rfl⟩)
                                            have runs := IndexedUnitsAgree.indexed_run_same_of_order_kept valid
                                              footprint freeFootprint complete rfl sourceImports found single ordered kept
                                            have exports := IndexedConstLink.rows_same_exports_of_order_kept valid footprint complete
                                              rfl sourceImports found single ordered kept sourceConsts sourceRows planEntry
                                              matched rowOrder glue defs
                                            have placed := places (B := B) (source := source) rfl single sourceSafe safe
                                              footprint.uniqueIds coverage
                                            have ownerProof : ∀ id ownerName, (id, ownerName) ∈ labels →
                                                S.claimants B id = [ownerName] := by
                                              intro id ownerName member
                                              apply SparseProjectedOrder.split_indexed_authored_owner valid.2.2.1 safe
                                              rw [SparseProjectedOrder.split_indexed_fileUnitPlacements safe]
                                              exact member
                                            return ⟨body, sourceCoverage, P, rfl, publicEntry, orderRequiresSequential, schedule,
                                              splitProgramIndexed body B P, rfl, labels, rfl,
                                              placed, ownerProof, rowOwner, rowPlaced, exports, runs, forRequest requested runs⟩
                                          else throw "entry needs unsupported glue exports"
                                        else throw "entry export rows differ from source order"
                                      else throw "entry units differ from source export metadata"
                                  else throw "export local lacks a declaration"
                                else throw "export ordinal is executable"
                              else throw "export ordinals are duplicated"
                            else throw "source public table differs from export rows"
                          else throw "source units do not cover original ordinals once"
                        else throw "plan does not cover original executable units once"
                      else throw "plan has an unsupported module path"
                    else throw "plan changes the original execution order"
                  else throw "plan failed source and linkage checks"
                else throw "only const source rows are supported"
              else throw "source rows are not sorted"
            else throw "a planner declaration has no source row"
          else throw "source and planner footprints differ"
        else throw "unsupported source module path"
      else throw "source imports are unsupported"
    else throw "source entry differs from its module"
  | _ => throw "expected one source module"

theorem construct_sound {source B S rows requested} (built : Construction source B S rows requested) :
    Places built.assignment source built.program ∧ SameExports source built.program ∧
      ScheduleEquivalent requested (fun _ => True) source built.program :=
  ⟨built.placement, built.sameExports, built.behavior⟩

#print axioms construct
#print axioms construct_sound

end CoreJS.IndexedCheckedSplit
