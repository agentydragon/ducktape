import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.HoistedAdjacentSwap
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedExportedFunction
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.StateMatch

/-! An authored two-file cycle may advance just its second function declaration.
The full ordinal list, including exports, must exhibit the same transposition.
The actual links check closure instantiation independently of evaluation. -/

namespace CoreJS.IndexedHoistedSwap

open LinkedBehavior EsmSplit

/-- The linked source has two declaration steps first. Every corresponding
step agrees in syntax, identity, receiver and captured cells after the swap. -/
def LinkedSwap (before after : List LinkedUnit) : Prop :=
  ∃ (a b : Nat) (f g : Func) (envF envG : Env) (tail : List LinkedUnit),
    before = (a, .funDecl f, envF) :: (b, .funDecl g, envG) :: tail ∧
    SameUnits ((b, .funDecl g, envG) :: (a, .funDecl f, envF) :: tail) after

structure Behavior (source candidate : Program) : Prop where
  linked : ∀ w, ∃ (before : CheckedLink source w) (after : CheckedLink candidate w),
    before.ready = after.ready ∧ LinkedSwap (unitsOf before.linked) (unitsOf after.linked)
  publicCells : SameExports source candidate
  allOutcomes : ∀ w fuel, runProgram w fuel source = runProgram w fuel candidate

/-- Check both complete linker results; the probe world suffices because
linking does not call the host. No evaluation outcome is tested here. -/
def checkBehavior (source candidate : Program) : Except String (PLift (Behavior source candidate)) := do
  let pair ← checkProbePair source candidate
  match original : unitsOf pair.before.linked with
  | (a, .funDecl f, envF) :: (b, .funDecl g, envG) :: tail =>
    let some ⟨swapped⟩ := sameUnits? ((b, .funDecl g, envG) :: (a, .funDecl f, envF) :: tail)
        (unitsOf pair.after.linked) |
      throw "candidate is not the adjacent transposition with identical statement captures"
    let some ⟨ready⟩ := StateMatch.probe pair.before.ready pair.after.ready |
      throw "linked ready states differ"
    if exports : exportsMatchFrom pair.before pair.after = true then
      have readyFor (w : World) : (pair.before.forWorld w).ready = (pair.after.forWorld w).ready :=
        congrArg (linkState w) ready
      return ⟨⟨fun w => ⟨pair.before.forWorld w, pair.after.forWorld w, readyFor w,
        a, b, f, g, envF, envG, tail, original, swapped⟩, pair.sameExports exports,
        fun w fuel => HoistedAdjacentSwap.runProgram_eq fuel (pair.before.forWorld w) (pair.after.forWorld w)
          (readyFor w) [] tail a b f g envF envG original swapped⟩⟩
    else throw "entry exports do not resolve to the same live cells"
  | _ => throw "source does not begin with two function declarations"

/-- Interest in this rule is strictly an inversion of the first two source
ordinals, with the second declaration selected first in the earlier helper. Neither
ordinary ordered construction nor a general reordering route uses this check. -/
def recognises (decoded : AuthenticatedSource.IndexedSource) (B : Bundle) (request : Spec) : Bool :=
  match request.order, decoded.source.body with
  | [helper], (a, .funDecl _) :: (b, .funDecl _) :: _ =>
    request.sequential && !request.privateEntryBody && a == 0 && b == 1 &&
      (((request.picked B).lookup helper).bind List.head? == some b)
  | _, _ => false

structure Construction (decoded : AuthenticatedSource.IndexedSource) (B : Bundle) (request : Spec) where
  plan : Plan
  planEq : plan = mkPlan B request
  valid : Valid B request plan
  fullOrder : plan.execOrder = 1 :: 0 :: (List.range B.length).drop 2
  program : Program
  constructed : program = splitProgramIndexed decoded.source.body B plan
  authoredOrder : program.evalOrder = request.order ++ [request.entry]
  authored : ∀ id owner, (id, owner) ∈ PlanPlacement.assignments plan → request.claimants B id = [owner]
  places : Places (IndexedCheckedSplit.assignments decoded.source.body plan) decoded.program program
  sourceRows : ExportRows.SourceRows decoded.source decoded.localExports
  metadataOwner : ∀ row ∈ decoded.localExports, request.claimants B row.ordinal = [request.entry]
  metadataPlaced : ∃ entry, plan.find plan.entry = some entry ∧
    entry.stmts.filterMap (ExportRows.rowAt decoded.localExports) = decoded.localExports ∧
    entry.namedExports B = decoded.source.exports
  behavior : Behavior decoded.program program

def construct (decoded : AuthenticatedSource.IndexedSource) (B : Bundle) (request : Spec) :
    Except String (Construction decoded B request) := do
  let [helper] := request.order | throw "expected one authored helper"
  unless recognises decoded B request do throw "expected the second source declaration first in the helper"
  unless helper != request.entry && (request.modules.map Prod.fst).Perm [request.entry, helper] &&
      request.modules.all (fun m => m.1.endsWith ".mjs") do
    throw "expected a two-file .mjs placement"
  let seq ← IndexedSourceSchedule.checkSequence decoded B
  -- Count authored selectors before mkPlan can discard any duplicates.
  unless ((request.modules.flatMap Prod.snd).map (fun
      | .stmt id => some id | .name _ => none)).Perm ((List.range B.length).map some) do
    throw "select each original ordinal exactly once"
  let plan := mkPlan B request
  let .isTrue valid := (inferInstance : Decidable (Valid B request plan)) |
    throw "authored plan failed validity checks"
  unless plan.mods.all (fun m => safeModulePath m.name) do throw "unsafe module path"
  let .isTrue fullOrder := (inferInstance : Decidable (plan.execOrder = 1 :: 0 :: (List.range B.length).drop 2)) |
    throw "authored plan changes ordinals beyond the adjacent declaration swap"
  unless decide (IndexedInputChecks.FootprintChecks decoded.source.body B ∧
      IndexedInputChecks.DefinitionChecks decoded.source.body B) do
    throw "source and planner footprints differ"
  for (id, stmt) in decoded.source.body do
    unless IndexedExportedFunction.concreteRetainedStatement stmt do
      throw s!"source unit {id}: expected concrete code"
  let .isTrue table := (inferInstance : Decidable (decoded.source.exports = decoded.localExports.flatMap (·.names))) |
    throw "expected only original standalone publication"
  unless !decoded.localExports.isEmpty && decide (ExportRows.LocalsDeclared decoded.source B) do
    throw "expected original export rows with declared locals"
  let .isTrue owner := (inferInstance : Decidable
      (∀ row ∈ decoded.localExports, request.claimants B row.ordinal = [request.entry])) |
    throw "export metadata must remain in the entry"
  let program := splitProgramIndexed decoded.source.body B plan
  let .isTrue order := (inferInstance : Decidable (program.evalOrder = request.order ++ [request.entry])) |
    throw "constructed module order differs from authored order"
  let ⟨entry, ⟨found⟩⟩ ← (show Except String (Σ m, PLift (plan.find plan.entry = some m)) from
    match plan.find plan.entry with
    | none => .error "missing plan entry"
    | some m => .ok ⟨m, ⟨rfl⟩⟩)
  let some helperMod := plan.find helper | throw "missing plan helper"
  unless entry.imports.map Prod.fst == [helper] && helperMod.imports.map Prod.fst == [request.entry] do
    throw "expected the entry-helper import cycle"
  let .isTrue matched := (inferInstance : Decidable
    (∀ id ∈ entry.stmts, ExportRows.UnitMatches B decoded.localExports id)) |
    throw "entry units differ from their original export rows"
  let .isTrue selected := (inferInstance : Decidable
    (entry.stmts.filterMap (ExportRows.rowAt decoded.localExports) = decoded.localExports)) |
    throw "entry loses or reorders an original export row"
  let .isTrue noGlue := (inferInstance : Decidable (entry.exports = [])) |
    throw "entry needs extra public exports"
  let ⟨places⟩ ← checkPlaces (IndexedCheckedSplit.assignments decoded.source.body plan) decoded.program program
  let ⟨behavior⟩ ← checkBehavior decoded.program program
  have rows : ExportRows.SourceRows decoded.source decoded.localExports :=
    ⟨table, seq.metadataIdsUnique, by simpa only [seq.sourceEq] using seq.disjoint⟩
  have exports := (ExportRows.exact_entry_exports matched selected noGlue).trans table.symm
  return ⟨plan, rfl, valid, fullOrder, program, rfl, order,
    fun _ _ h => PlanPlacement.authored_owner valid.2.2.1 h,
    places, rows, owner, ⟨entry, found, selected, exports⟩, behavior⟩

/-- Lift executable ownership, the authored priority and the full link and
evaluation certificate to the independently parsed output modules. The
original export rows are also verified in the staged ESTree comparison. -/
def Construction.checkReadback {decoded B request} (built : Construction decoded B request)
    (files : Lean.Json) : Except String (PLift (∃ observed,
      ConstructedOutput.decodeConstructedOutput files built.program = .ok observed ∧
      Places (IndexedCheckedSplit.assignments decoded.source.body built.plan) decoded.program observed ∧
      observed.evalOrder = request.order ++ [request.entry] ∧ Behavior decoded.program observed)) := do
  let ⟨same⟩ ← ConstructedOutput.checkConstructedOutput files built.program
  return ⟨built.program, same, built.places, built.authoredOrder, built.behavior⟩

end CoreJS.IndexedHoistedSwap
