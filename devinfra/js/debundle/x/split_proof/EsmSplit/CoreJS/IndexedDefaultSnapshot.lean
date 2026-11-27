import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedReorder
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.PrimitiveRhs
import EsmSplit.CoreJS.SyntaxMatch

/-! Move one bound captured by an ordinary function and retain the executable
default expression in the entry. The public cell is the expression's own
declaration, whose initializer reads the function at its original ordinal. -/

namespace CoreJS.IndexedDefaultSnapshot

open AuthenticatedSource (IndexedSource)
open LinkedBehavior

/-- At sufficient fuel, the executable default row reads a value once and
initializes its own cell with that value. It does not publish the read cell. -/
theorem snapshotStep {W : World} (n : Nat) (env : Env) (name : Name) :
    (execStmt (n + 2) env (.decl .const "default" (.var name)) : M W Flow) =
      (do
        let value ← readVar env name
        initVar env "default" value
        pure .normal) := by
  simp only [execStmt.eq_def, evalExpr.eq_def]

private def checkFamily (source : Module) : Except String (Nat × Nat × Nat × Name) := do
  let (boundId, .decl .var bound (.num _)) ::
      (functionId, .funDecl (.mk name .normal false [arg] [.ret value])) ::
      (snapshotId, .decl .const "default" (.var read)) :: rest := source.body |
    throw "expected a numeric var bound, a unary return function and an executable default read"
  unless name == read && bound != name && bound != arg && name != arg &&
      name != "default" && bound != "default" && value.free.contains bound &&
      (PrimitiveRhs.check (fun x => x == arg || x == bound) value).isSome do
    throw "expected a default snapshot of the function capturing the bound"
  unless source.exports == [("default", "default")] do throw "expected only the default snapshot to be public"
  for (_, stmt) in rest do
    let .expr (.assign (.var target) (.func (.mk _ .normal _ params [.ret replacement]))) := stmt |
      throw "expected only a subsequent local function reassignment"
    unless target == name && (PrimitiveRhs.check params.contains replacement).isSome do
      throw "expected a local reassignment to the snapshotted binding"
  return (boundId, functionId, snapshotId, name)

structure Construction (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) where
  schedule : IndexedSourceSchedule.Evidence decoded B request
  program : Program
  constructed : program = splitProgramIndexed decoded.source.body B schedule.plan
  places : Places (PlanPlacement.assignments schedule.plan) decoded.program program
  snapshotOf : Name
  snapshotId : Nat
  snapshotInSource : decoded.source.body.lookup snapshotId = some (.decl .const "default" (.var snapshotOf))
  snapshotInEntry : ∃ entry, program.find program.entry = some entry ∧
    entry.body.lookup snapshotId = some (.decl .const "default" (.var snapshotOf))
  snapshotOwner : request.claimants B snapshotId = [request.entry]
  exactExports : ∀ w, ∃ cells, LinkedBehavior.exports w decoded.program = some cells ∧
    LinkedBehavior.exports w program = some cells
  linkedCaptures : ∀ w, ∃ before : CheckedLink decoded.program w, ∃ after : CheckedLink program w,
    before.ready = after.ready ∧ captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked)
  allOutcomes : ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel program

def construct (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) :
    Except String (Construction decoded B request) := do
  let [helper] := request.order | throw "expected one authored bound module"
  let source := decoded.source
  let original := decoded.program
  let (boundId, functionId, snapshotId, name) ← checkFamily source
  unless request.sequential && !request.privateEntryBody && helper != request.entry &&
      request.modules.all (fun row => row.1.endsWith ".mjs" || row.1.endsWith ".js") do
    throw "expected two sequential JavaScript modules with a public entry"
  unless decoded.localExports.isEmpty && !decoded.hasTemplates do throw "expected only the executable default export"
  let .isTrue snapshotOwner := (inferInstance : Decidable (request.claimants B snapshotId = [request.entry])) |
    throw "the executable default export must stay in the entry"
  unless request.claimants B boundId == [helper] && request.claimants B functionId == [request.entry] &&
      source.body.all (fun (id, _) => id == boundId || request.claimants B id == [request.entry]) do
    throw "only the original bound may be extracted"
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
  let ⟨entry, ⟨planEntry⟩⟩ ← (show Except String (Σ m, PLift (P.find P.entry = some m)) from
    match P.find P.entry with
    | none => .error "missing plan entry"
    | some m => .ok ⟨m, ⟨rfl⟩⟩)
  let .isTrue owned := (inferInstance : Decidable (IndexedReorder.InlineExportsOwned source B entry)) |
    throw "the default snapshot is not published by its executable entry row"
  let .isTrue table := (inferInstance : Decidable ((moduleOfIndexed source.body B P entry).exports = source.exports)) |
    throw "entry export table differs from the source"
  let snapshot := Stmt.decl .const "default" (.var name)
  let some ⟨sourceRow⟩ := SyntaxMatch.optionEq SyntaxMatch.stmtEq (source.body.lookup snapshotId) (some snapshot) |
    throw "original default row differs from the snapshot"
  let some ⟨entryRow⟩ := SyntaxMatch.optionEq SyntaxMatch.stmtEq
      ((moduleOfIndexed source.body B P entry).body.lookup snapshotId) (some snapshot) |
    throw "entry default row differs from the snapshot"
  have targetEntry : program.find program.entry = some (moduleOfIndexed source.body B P entry) := by
    change (splitProgramIndexed source.body B P).find P.entry = _
    rw [SparseProjectedOrder.split_indexed_find, planEntry]
    rfl
  let ⟨places⟩ ← checkPlaces (PlanPlacement.assignments P) original program
  let before ← checkLink original
  have footprint := IndexedInputChecks.checked_footprints fp
  have linked (w : World) := IndexedReorder.inline_links_of_order_kept schedule.valid decoded.singleton
    imports found footprint.1 footprint.2 (IndexedInputChecks.checked_definitions defs)
    sorted kept planEntry owned table (before.forWorld w)
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
  have captures : ∀ w, ∃ a : CheckedLink original w, ∃ b : CheckedLink program w,
      a.ready = b.ready ∧ captureUnits (unitsOf a.linked) = captureUnits (unitsOf b.linked) := by
    intro w
    obtain ⟨after, ready, same, _⟩ := linked w
    exact ⟨before.forWorld w, after, ready, OrderedRegion.sameUnits_capture same⟩
  have outcomes : ∀ w fuel, runProgram w fuel original = runProgram w fuel program := by
    intro w fuel
    obtain ⟨after, ready, same, _⟩ := linked w
    rw [run_after_link fuel (before.forWorld w), run_after_link fuel after, ready, runUnits_same fuel same]
  return ⟨schedule, program, rfl, places, name, snapshotId, sourceRow, ⟨_, targetEntry, entryRow⟩,
    snapshotOwner, exactExports, captures, outcomes⟩

theorem Construction.readbackGuarantees {decoded B request} (built : Construction decoded B request)
    {observed : Program} (same : observed = built.program) :
    Places (PlanPlacement.assignments built.schedule.plan) decoded.program observed ∧
    (∀ w, ∃ cells, LinkedBehavior.exports w decoded.program = some cells ∧
      LinkedBehavior.exports w observed = some cells) ∧
    (∃ entry, observed.find observed.entry = some entry ∧
      entry.body.lookup built.snapshotId = some (.decl .const "default" (.var built.snapshotOf))) ∧
    ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel observed := by
  subst observed
  exact ⟨built.places, built.exactExports, built.snapshotInEntry, built.allOutcomes⟩

end CoreJS.IndexedDefaultSnapshot
