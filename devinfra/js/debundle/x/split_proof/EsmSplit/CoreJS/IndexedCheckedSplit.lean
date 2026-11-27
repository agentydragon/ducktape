import EsmSplit.CoreJS.IndexedConstLink
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.ExportRowsOrder

/-! A checked original-ID constructor. All checks inspect source and plan; the
returned CoreJS program is the indexed constructor, not a replayed pair. -/

namespace CoreJS.IndexedCheckedSplit

open LinkedBehavior

def assignments (body : List (Nat × Stmt)) (P : EsmSplit.Plan) : List UnitPlacement :=
  P.mods.flatMap fun m => (m.stmts.filterMap (indexedNumbered body)).map fun row => (row.1, m.name)

/-- Count the authored selectors themselves, before the planner deduplicates
the units they pick. Each metadata ordinal must be selected exactly once. -/
def MetadataSelectorsOnce (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (rows : List EsmSplit.LocalExportUnit) : Prop :=
  ∀ row ∈ rows, (S.modules.flatMap fun m => m.2.filter fun selector =>
    selector.claims B row.ordinal).length = 1

instance (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (rows : List EsmSplit.LocalExportUnit) :
    Decidable (MetadataSelectorsOnce B S rows) := by
  unfold MetadataSelectorsOnce
  infer_instance

private def isConst : Stmt → Bool
  | .decl .const _ _ => true
  | _ => false

private theorem isConst_sound {stmt : Stmt} (accepted : isConst stmt = true) :
    ∃ name init, stmt = .decl .const name init := by
  cases stmt <;> simp [isConst] at accepted ⊢
  case decl kind name init =>
    cases kind <;> simp [isConst] at accepted ⊢

theorem places {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
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

/-- Source-only facts used by the constructor. Production obtains these from
 the decoder's item sequence; independent CoreJS callers can check them. -/
structure SourceFacts (original : Program) (B : EsmSplit.Bundle)
    (rows : List EsmSplit.LocalExportUnit) where
  source : Module
  singleton : original.modules = [source]
  coverage : ((source.body.map Prod.fst) ++ rows.map (·.ordinal)).Perm (List.range B.length)
  unique : (rows.map (·.ordinal)).Nodup
  ordered : rows.Pairwise (fun a b => a.ordinal < b.ordinal)
  absent : ∀ row ∈ rows, row.ordinal ∉ source.body.map Prod.fst

/-- Check source facts for a standalone CoreJS caller. -/
def SourceFacts.check (original : Program) (B : EsmSplit.Bundle) (rows : List EsmSplit.LocalExportUnit) :
    Except String (SourceFacts original B rows) := do
  match single : original.modules with
  | [source] =>
    let .isTrue coverage := (inferInstance : Decidable
      (((source.body.map Prod.fst) ++ rows.map (·.ordinal)).Perm (List.range B.length))) |
      throw "source units do not cover original ordinals once"
    let .isTrue unique := (inferInstance : Decidable ((rows.map (·.ordinal)).Nodup)) |
      throw "export ordinals are duplicated"
    let .isTrue ordered := (inferInstance : Decidable (rows.Pairwise (fun a b => a.ordinal < b.ordinal))) |
      throw "export ordinals are not in source order"
    let .isTrue absent := (inferInstance : Decidable (∀ row ∈ rows, row.ordinal ∉ source.body.map Prod.fst)) |
      throw "export ordinal is executable"
    return ⟨source, single, coverage, unique, ordered, absent⟩
  | _ => throw "expected one source module"

/-- Private-mode priority is supported without a facade when the actual
priority plan keeps source execution order and needs no extra entry exports.
Before planning, require the full authored priority and one ordinal selector
per original slot. The planner's filtering and deduplication cannot certify
an altered request. -/
def PrivatePriority (B : EsmSplit.Bundle) (S : EsmSplit.Spec) : Prop :=
  S.privateEntryBody = true ∧ S.sequential = false ∧ S.order ≠ [] ∧
  S.order.Perm ((S.modules.map Prod.fst).filter (· != S.entry)) ∧
  ((S.modules.flatMap Prod.snd).map (fun
    | .stmt id => some id
    | .name _ => none)).Perm ((List.range B.length).map some)

instance (B : EsmSplit.Bundle) (S : EsmSplit.Spec) : Decidable (PrivatePriority B S) := by
  unfold PrivatePriority
  infer_instance

def SupportedRequest (B : EsmSplit.Bundle) (S : EsmSplit.Spec) : Prop :=
  (S.privateEntryBody = false ∧ (S.order ≠ [] → S.sequential = true)) ∨ PrivatePriority B S

instance (B : EsmSplit.Bundle) (S : EsmSplit.Spec) : Decidable (SupportedRequest B S) := by
  unfold SupportedRequest
  infer_instance

/-- The computed candidate and its strict obligations use the actual source,
planner bundle, original rows, and supplied request. -/
structure Construction (source : Program) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (rows : List EsmSplit.LocalExportUnit) (requested : Guarantee) where
  body : List (Nat × Stmt)
  sourceCoverage : ((body.map Prod.fst) ++ rows.map (·.ordinal)).Perm (List.range B.length)
  plan : EsmSplit.Plan
  planEq : plan = EsmSplit.mkPlan B S
  requestSupported : SupportedRequest B S
  schedule : (S.sequential = true ∨ S.privateEntryBody = true) → plan.evalOrder = S.order ++ [S.entry]
  entryWithoutGlue : ∃ entry, plan.find plan.entry = some entry ∧ entry.exports = []
  program : Program
  constructed : program = splitProgramIndexed body B plan
  assignment : List UnitPlacement
  assignmentEq : assignment = assignments body plan
  placement : Places assignment source program
  authored : ∀ id owner, (id, owner) ∈ assignment → S.claimants B id = [owner]
  metadataSelectorsOnce : MetadataSelectorsOnce B S rows
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

/-- Construct from source facts and the actual authored plan. -/
def construct (original : Program) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (rows : List EsmSplit.LocalExportUnit) (requested : Guarantee)
    (facts : SourceFacts original B rows) : Except String (Construction original B S rows requested) := do
  let .isTrue requestSupported := (inferInstance : Decidable (SupportedRequest B S)) |
    throw "expected a public ordered request or a private priority with complete original ordinals"
  let .isTrue metadataSelectorsOnce := (inferInstance : Decidable (MetadataSelectorsOnce B S rows)) |
    throw "each metadata row must be selected exactly once"
  let source := facts.source
  have single := facts.singleton
  if sourceEntry : original.entry = source.name then
    if sourceImports : source.imports = [] then
      if sourceSafe : EsmSplit.safeModulePath source.name = true then
        let body := source.body
        if checked : IndexedInputChecks.FootprintChecks body B then
          if defined : IndexedInputChecks.DefinitionChecks body B then
            if ordered : body.Pairwise (fun a b => a.1 ≤ b.1) then
              if consts : ∀ p ∈ body, isConst p.2 = true then
                let planned := EsmSplit.mkPlanWithImports B S
                let P := planned.1
                let .isTrue schedule := (inferInstance : Decidable
                  ((S.sequential = true ∨ S.privateEntryBody = true) → P.evalOrder = S.order ++ [S.entry])) |
                  throw "constructed modules do not follow the requested order"
                if valid : EsmSplit.Valid B S P then
                  if kept : SparseProjectedOrder.OrderKept body P then
                    if safe : ∀ m ∈ P.mods, EsmSplit.safeModulePath m.name = true then
                      let labels := assignments body P
                      if coverage : (labels.map Prod.fst).Perm (body.map Prod.fst) then
                        if table : source.exports = rows.flatMap (·.names) then
                          if declared : ExportRows.LocalsDeclared source B then
                            match planEntry : P.find P.entry with
                            | none => throw "plan has no entry"
                            | some owner =>
                              if matched : ∀ id ∈ owner.stmts, ExportRows.UnitMatches B rows id then
                                if authoredRows : ∀ row ∈ rows, S.claimants B row.ordinal = [S.entry] then
                                  if coveredNames : EsmSplit.PlanGlue.Covered (owner.published B)
                                      (EsmSplit.dedup ((planned.2.getD owner.name []).reverse)) then
                                    have found : original.find original.entry = some source := by
                                      simp [Program.find, single, sourceEntry, source]
                                    have footprint := (IndexedInputChecks.checked_footprints checked).1
                                    have freeFootprint := (IndexedInputChecks.checked_footprints checked).2
                                    have complete := IndexedInputChecks.checked_definitions defined
                                    have sourceConsts : ∀ p ∈ body, ∃ name init,
                                        p.2 = .decl .const name init := fun p hp => isConst_sound (consts p hp)
                                    have sourceRows : ExportRows.SourceRows source rows := ⟨table, facts.unique, facts.absent⟩
                                    have defs : ∀ pair ∈ rows.flatMap (·.names), ∃ id, id < B.length ∧
                                        pair.2 ∈ (B.stmt id).defs := by
                                      intro pair hp
                                      exact ExportRows.row_locals_declared sourceRows declared hp
                                    have within : ∀ row ∈ rows, row.ordinal < B.length := by
                                      intro row member
                                      apply List.mem_range.mp
                                      apply facts.coverage.mem_iff.mp
                                      exact List.mem_append_right _ (List.mem_map.mpr ⟨row, member, rfl⟩)
                                    have rowOrder : owner.stmts.filterMap (ExportRows.rowAt rows) = rows :=
                                      ExportRows.mkPlan_select_source_rows valid planEntry facts.ordered within authoredRows
                                    have covered : EsmSplit.PlanGlue.EntryCovered B S owner := by
                                      simpa [EsmSplit.PlanGlue.EntryCovered, planned] using coveredNames
                                    have glue : owner.exports = [] :=
                                      EsmSplit.PlanGlue.mkPlan_no_extra_glue planEntry covered
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
                                      exact facts.absent row member (List.mem_map.2 ⟨(row.ordinal, stmt), inBody, rfl⟩)
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
                                    return ⟨body, facts.coverage, P, rfl, requestSupported, schedule, ⟨owner, planEntry, glue⟩,
                                      splitProgramIndexed body B P, rfl, labels, rfl,
                                      placed, ownerProof, metadataSelectorsOnce, rowOwner, rowPlaced,
                                      exports, runs, forRequest requested runs⟩

                                  else throw "entry needs unsupported glue exports"
                                else throw "export row is not authored in entry"
                              else throw "entry units differ from source export metadata"
                          else throw "export local lacks a declaration"
                        else throw "source public table differs from export rows"
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

theorem construct_sound {source B S rows requested} (built : Construction source B S rows requested) :
    Places built.assignment source built.program ∧ SameExports source built.program ∧
      ScheduleEquivalent requested (fun _ => True) source built.program :=
  ⟨built.placement, built.sameExports, built.behavior⟩


end CoreJS.IndexedCheckedSplit
