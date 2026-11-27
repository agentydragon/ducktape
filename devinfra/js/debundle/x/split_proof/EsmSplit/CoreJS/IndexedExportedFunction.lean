import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.ExportRowsOrder
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.IndexedSortedScopes
import EsmSplit.CoreJS.LinkWorld

/-! Publish one hoisted predicate through an original standalone export row.
The bounded expression gate needs no host behavior even for object arguments:
strict comparison and truthiness do not invoke coercion hooks. -/

namespace CoreJS.IndexedExportedFunction

open AuthenticatedSource (IndexedSource)
open LinkedBehavior

private def literal : Expr → Bool
  | .null | .bool _ | .num _ | .str _ => true
  | _ => false

private def predicate (names : List Name) : Expr → Bool
  | .var name => names.contains name
  | .binary .strictEq left right | .binary .strictNe left right
  | .logical .or left right | .logical .and left right =>
    predicate names left && predicate names right
  | e => literal e

private def seeds (names : List Name) : List (Nat × Stmt) → Option (List Name)
  | [] => some names
  | (_, .decl .const name value) :: rest =>
    if names.contains name || !literal value then none else seeds (name :: names) rest
  | _ => none

private def checkFamily (source : Module) : Except String Unit := do
  let (_, .funDecl (.mk name .normal false [param] [.ret value])) :: revSeeds :=
      source.body.reverse | throw "expected an ordinary one-parameter return predicate"
  let some captures := seeds [] revSeeds.reverse | throw "expected only literal const captures"
  unless !captures.contains name && !captures.contains param && param != name &&
      predicate (param :: captures) value do
    throw "expected strict comparisons and logical expressions over the parameter and literal captures"
  let [(_, localName)] := source.exports | throw "expected one public function"
  unless name == localName do throw "the source export must name the original predicate"

/-- Call a public cell after a fresh module evaluation. Failure, fuel and
the final call state are retained. The receiver of the imported call is undefined. -/
def runExportCall (w : World) (initFuel callFuel : Nat) (program : Program)
    (publicName : Name) (arguments : List Val) : Res w Val :=
  match runProgram w initFuel program with
  | .ok _ state =>
    (do
      let some addr := (LinkedBehavior.exports w program).bind (·.lookup publicName) |
        throwErr .reference "missing public function"
      callFn callFuel (← readCell addr) .undef arguments) state
  | .throw value state => .throw value state
  | .unknown why state => .unknown why state
  | .fuel => .fuel

structure Construction (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) where
  schedule : IndexedSourceSchedule.Evidence decoded B request
  program : Program
  constructed : program = splitProgramIndexed decoded.source.body B schedule.plan
  places : Places (IndexedCheckedSplit.assignments decoded.source.body schedule.plan) decoded.program program
  metadataOnce : IndexedCheckedSplit.MetadataSelectorsOnce B request decoded.localExports
  metadataOwner : ∀ row ∈ decoded.localExports, request.claimants B row.ordinal = [request.entry]
  exactExports : ∀ w, ∃ cells, LinkedBehavior.exports w decoded.program = some cells ∧
    LinkedBehavior.exports w program = some cells
  allOutcomes : ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel program

def construct (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) :
    Except String (Construction decoded B request) := do
  let [helper] := request.order | throw "expected one authored predicate module"
  let [_] := decoded.localExports | throw "expected one original standalone export row"
  let source := decoded.source
  let original := decoded.program
  checkFamily source
  unless request.sequential && !request.privateEntryBody && helper != request.entry &&
      request.modules.all (fun module => module.1.endsWith ".mjs") && !decoded.hasTemplates do
    throw "expected two sequential .mjs modules with a public entry and no templates"
  let .isTrue metadataOnce := (inferInstance : Decidable
      (IndexedCheckedSplit.MetadataSelectorsOnce B request decoded.localExports)) |
    throw "each original metadata row must be selected exactly once"
  let .isTrue metadataOwner := (inferInstance : Decidable
      (∀ row ∈ decoded.localExports, request.claimants B row.ordinal = [request.entry])) |
    throw "standalone export row must be authored in the entry"
  unless source.body.all (fun (id, _) => request.claimants B id == [helper]) do
    throw "predicate and captures must be authored in the helper"
  unless decide (IndexedInlineConstructor.SelectorsOnce source.body B request) do
    throw "each original executable unit must be selected exactly once"
  let schedule ← IndexedSourceSchedule.check decoded B request
  let P := schedule.plan
  let program := splitProgramIndexed source.body B P
  unless program.evalOrder == request.order ++ [request.entry] do throw "plan must follow authored module order"
  let .isTrue imports := (inferInstance : Decidable (source.imports = [])) | throw "expected no source imports"
  let .isTrue sourceEntry := (inferInstance : Decidable (original.entry = source.name)) | throw "wrong source entry"
  have found : original.find original.entry = some source := by
    change decoded.program.find decoded.program.entry = some decoded.source
    change decoded.program.entry = decoded.source.name at sourceEntry
    simp [Program.find, decoded.singleton, sourceEntry]
  let .isTrue sorted := (inferInstance : Decidable (source.body.Pairwise fun a b => a.1 ≤ b.1)) |
    throw "unexpected source ordinal order"
  have kept : SparseProjectedOrder.OrderKept source.body P := by
    change SparseProjectedOrder.OrderKept decoded.source.body schedule.plan
    rw [← schedule.sequence.sourceEq]
    exact schedule.executableOrder
  let .isTrue fp := (inferInstance : Decidable (IndexedInputChecks.FootprintChecks source.body B)) |
    throw "source and planner footprints differ"
  let .isTrue defs := (inferInstance : Decidable (IndexedInputChecks.DefinitionChecks source.body B)) |
    throw "planner definitions lack source rows"
  -- Use the decoder's classification for the body and every metadata ordinal.
  let .isTrue table := (inferInstance : Decidable (source.exports = decoded.localExports.flatMap (·.names))) |
    throw "only authored standalone publication is supported"
  let .isTrue rowOrder := (inferInstance : Decidable
      (decoded.localExports.Pairwise (fun a b => a.ordinal < b.ordinal))) |
    throw "standalone export rows are not in source order"
  have rows : ExportRows.SourceRows source decoded.localExports :=
    ⟨table, schedule.sequence.metadataIdsUnique, by
      simpa only [source, schedule.sequence.sourceEq] using schedule.sequence.disjoint⟩
  let .isTrue declared := (inferInstance : Decidable (ExportRows.LocalsDeclared source B)) |
    throw "export local lacks an original declaration"
  let ⟨owner, ⟨planEntry⟩⟩ ← (show Except String (Σ m, PLift (P.find P.entry = some m)) from
    match P.find P.entry with
    | none => .error "missing plan entry"
    | some m => .ok ⟨m, ⟨rfl⟩⟩)
  let .isTrue matched := (inferInstance : Decidable
      (∀ id ∈ owner.stmts, ExportRows.UnitMatches B decoded.localExports id)) |
    throw "entry units differ from the original metadata"
  let .isTrue noGlue := (inferInstance : Decidable (owner.exports = [])) |
    throw "entry needs unsupported glue exports"
  have within : ∀ row ∈ decoded.localExports, row.ordinal < B.length := by
    intro row member
    exact List.mem_range.1 (schedule.sequence.sourceCoverage.mem_iff.mp
      (List.mem_append_right _ (List.mem_map.mpr ⟨row, member, rfl⟩)))
  have selected : owner.stmts.filterMap (ExportRows.rowAt decoded.localExports) = decoded.localExports := by
    have valid := schedule.valid
    change schedule.plan.find schedule.plan.entry = some owner at planEntry
    rw [schedule.planEq] at valid planEntry
    exact ExportRows.mkPlan_select_source_rows valid planEntry rowOrder within metadataOwner
  let labels := IndexedCheckedSplit.assignments source.body P
  let ⟨places⟩ ← checkPlaces labels original program
  let before ← checkLink original
  have footprint := IndexedInputChecks.checked_footprints fp
  have complete := IndexedInputChecks.checked_definitions defs
  have ⟨tops, regions⟩ := IndexedSortedScopes.indexed_tops_regions schedule.valid footprint.1
    rfl decoded.singleton imports found sorted
  have scopes := IndexedUnitsAgree.indexed_scopes_agree schedule.valid footprint.1 footprint.2 complete
    rfl imports found sorted (SparseProjectedOrder.split_indexed_loaded_eq_source kept)
  have linked (w : World) : ∃ after : CheckedLink program w,
      (before.forWorld w).ready = after.ready ∧
      SameUnits (unitsOf (before.forWorld w).linked) (unitsOf after.linked) ∧
      ∃ cells, EntryExports.resolve original.entry (before.forWorld w).linked = .ok cells ∧
        EntryExports.resolve program.entry after.linked = .ok cells := by
    obtain ⟨_, frames, _, after, allocated, beforeScopes, afterScopes, ready, same, _⟩ :=
      OrderedRegion.matching_link_with_frames regions tops scopes (before.forWorld w)
    rw [IndexedEntryLink.import_free_source_tops found imports sorted] at allocated
    obtain ⟨actual, cells, actualScopes, _, sourceTable, targetTable, _⟩ :=
      ExportRows.rows_resolved_from_plan schedule.valid footprint.1 complete allocated rfl imports found
        rows planEntry matched selected noGlue (fun _ => ExportRows.row_locals_declared rows declared)
    have sourceLinked := Except.ok.inj
      (beforeScopes.symm.trans (IndexedEntryLink.import_free_entry_scopes found imports frames))
    have targetLinked := Except.ok.inj (afterScopes.symm.trans actualScopes)
    exact ⟨after, ready, same, cells, sourceLinked.symm ▸ sourceTable, targetLinked.symm ▸ targetTable⟩
  have exactExports : ∀ w, ∃ cells, LinkedBehavior.exports w original = some cells ∧
      LinkedBehavior.exports w program = some cells := by
    intro w
    obtain ⟨after, _, _, cells, left, right⟩ := linked w
    refine ⟨cells, ?_, ?_⟩
    · rw [LinkedBehavior.exports, (before.forWorld w).equation]
      change (EntryExports.resolve original.entry _).toOption = _
      rw [left]
      rfl
    · rw [LinkedBehavior.exports, after.equation]
      change (EntryExports.resolve program.entry _).toOption = _
      rw [right]
      rfl
  have outcomes : ∀ w fuel, runProgram w fuel original = runProgram w fuel program := by
    intro w fuel
    obtain ⟨after, ready, same, _⟩ := linked w
    rw [run_after_link fuel (before.forWorld w), run_after_link fuel after, ready, runUnits_same fuel same]
  return ⟨schedule, program, rfl, places, metadataOnce, metadataOwner, exactExports, outcomes⟩

/-- Transfer the authored placement and full outcome contract to independently
decoded files, including calls on the identical live export cell. -/
theorem Construction.readbackGuarantees {decoded B request} (built : Construction decoded B request)
    {observed : Program} (same : observed = built.program) :
    Places (IndexedCheckedSplit.assignments decoded.source.body built.schedule.plan) decoded.program observed ∧
    (∀ w, ∃ cells, LinkedBehavior.exports w decoded.program = some cells ∧
      LinkedBehavior.exports w observed = some cells) ∧
    (∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel observed) ∧
    ∀ w initFuel callFuel name args,
      runExportCall w initFuel callFuel decoded.program name args =
        runExportCall w initFuel callFuel observed name args := by
  subst observed
  refine ⟨built.places, built.exactExports, built.allOutcomes, ?_⟩
  intro w initFuel callFuel name args
  obtain ⟨_, before, after⟩ := built.exactExports w
  unfold runExportCall
  rw [built.allOutcomes, before, after]

end CoreJS.IndexedExportedFunction
