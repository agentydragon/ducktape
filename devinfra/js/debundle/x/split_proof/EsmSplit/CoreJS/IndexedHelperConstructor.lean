import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedReorder
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.PrimitiveRhs

/-! Construct an order-preserving split that calls one hoisted ordinary helper.
The syntax gate confines execution to a direct call and a primitive return.
The indexed link proof establishes the hoisted state and the exact live cells. -/

namespace CoreJS.IndexedHelperConstructor

open AuthenticatedSource (IndexedSource)
open LinkedBehavior

private def lexicalPrefix (earlier : List Name) : List (Nat × Stmt) → Option (List Name)
  | [] => some earlier
  | (_, .decl .let_ name init) :: rest | (_, .decl .const name init) :: rest =>
    if earlier.contains name || (PrimitiveRhs.check earlier.contains init).isNone then none
    else lexicalPrefix (name :: earlier) rest
  | _ => none

/-- The entry calls one nonescaping, nullary helper. The helper may read earlier
lexical seeds, which are initialized before the call in the source schedule. -/
private def checkFamily (source : Module) : Except String (Nat × Nat) := do
  let (callId, .decl .const result (.call (.var callee) [])) ::
      (helperId, .funDecl (.mk name .normal false [] [.ret value])) :: revSeeds := source.body.reverse |
    throw "expected lexical seeds, a nullary return helper and one const call"
  let some seeds := lexicalPrefix [] revSeeds.reverse | throw "expected primitive lexical seeds"
  unless callee == name && !seeds.contains name && !seeds.contains result && name != result do
    throw "expected a direct call to the declared helper"
  unless (PrimitiveRhs.check seeds.contains value).isSome do
    throw "helper return must use only primitives and earlier lexical seeds"
  unless source.exports == [(result, result)] do throw "only the caller's result may be public"
  return (helperId, callId)

structure Construction (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) where
  schedule : IndexedSourceSchedule.Evidence decoded B request
  program : Program
  constructed : program = splitProgramIndexed decoded.source.body B schedule.plan
  ordered : program.evalOrder = request.order ++ [request.entry]
  places : Places (PlanPlacement.assignments schedule.plan) decoded.program program
  sameExports : SameExports decoded.program program
  allOutcomes : ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel program

def construct (decoded : IndexedSource) (B : EsmSplit.Bundle) (request : EsmSplit.Spec) :
    Except String (Construction decoded B request) := do
  let [helperOwner] := request.order | throw "expected one authored helper module"
  let (helperId, callId) ← checkFamily decoded.source
  unless request.sequential && !request.privateEntryBody && helperOwner != request.entry &&
      request.modules.all (fun module => module.1.endsWith ".mjs") do
    throw "expected two .mjs modules in authored order with a public entry"
  unless B.length == decoded.nodes.size &&
      request.claimants B helperId == [helperOwner] && request.claimants B callId == [request.entry] do
    throw "helper or caller does not have the required authored owner"
  unless decide (IndexedInlineConstructor.SelectorsOnce decoded.source.body B request) do
    throw "each original unit must be selected exactly once"
  unless decoded.localExports.isEmpty && !decoded.hasTemplates do
    throw "expected only inline exports without template objects"
  let source := decoded.source
  let original := decoded.program
  let .isTrue imports := (inferInstance : Decidable (source.imports = [])) | throw "expected no source imports"
  let .isTrue sourceEntry := (inferInstance : Decidable (original.entry = source.name)) | throw "wrong source entry"
  have found : original.find original.entry = some source := by
    change decoded.program.find decoded.program.entry = some decoded.source
    change decoded.program.entry = decoded.source.name at sourceEntry
    simp [Program.find, decoded.singleton, sourceEntry]
  let .isTrue sorted := (inferInstance : Decidable (source.body.Pairwise fun a b => a.1 ≤ b.1)) |
    throw "unexpected source ordinal order"
  let schedule ← IndexedSourceSchedule.check decoded B request
  let P := schedule.plan
  have kept : SparseProjectedOrder.OrderKept source.body P := by
    change SparseProjectedOrder.OrderKept decoded.source.body schedule.plan
    rw [← schedule.sequence.sourceEq]
    exact schedule.executableOrder
  let program := splitProgramIndexed source.body B P
  let .isTrue ordered := (inferInstance : Decidable (program.evalOrder = request.order ++ [request.entry])) |
    throw "constructed modules do not follow the authored order"
  let .isTrue fp := (inferInstance : Decidable (IndexedInputChecks.FootprintChecks source.body B)) |
    throw "source and planner footprints differ"
  let .isTrue defs := (inferInstance : Decidable (IndexedInputChecks.DefinitionChecks source.body B)) |
    throw "planner definitions lack source rows"
  let ⟨entry, ⟨planEntry⟩⟩ ← (show Except String (Σ m, PLift (P.find P.entry = some m)) from
    match P.find P.entry with
    | none => .error "missing plan entry"
    | some m => .ok ⟨m, ⟨rfl⟩⟩)
  let .isTrue owned := (inferInstance : Decidable (IndexedReorder.InlineExportsOwned source B entry)) |
    throw "inline export is not owned by the entry"
  let .isTrue table := (inferInstance : Decidable ((moduleOfIndexed source.body B P entry).exports = source.exports)) |
    throw "entry export table differs from the source"
  let ⟨places⟩ ← checkPlaces (PlanPlacement.assignments P) original program
  let before ← checkLink original
  have footprint := IndexedInputChecks.checked_footprints fp
  have linked (w : World) := IndexedReorder.inline_links_of_order_kept schedule.valid decoded.singleton
    imports found footprint.1 footprint.2 (IndexedInputChecks.checked_definitions defs)
    sorted kept planEntry owned table (before.forWorld w)
  have sameExports : SameExports original program := by
    intro w
    obtain ⟨after, _, _, exports, left, right⟩ := linked w
    refine ⟨exports, exports, ?_, ?_, .refl _⟩
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
  return ⟨schedule, program, rfl, ordered, places, sameExports, outcomes⟩

theorem Construction.authoredOwner {decoded B request} (built : Construction decoded B request)
    {id owner} (member : (id, owner) ∈ PlanPlacement.assignments built.schedule.plan) :
    request.claimants B id = [owner] :=
  PlanPlacement.authored_owner built.schedule.valid.2.2.1 member

/-- The exact artifact inherits the full model guarantee, including throws
and fuel, without relying on a successful evaluation of the helper. -/
theorem Construction.readbackGuarantees {decoded B request} (built : Construction decoded B request)
    {observed : Program} (same : observed = built.program) :
    Places (PlanPlacement.assignments built.schedule.plan) decoded.program observed ∧
    SameExports decoded.program observed ∧
    ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel observed := by
  subst observed
  exact ⟨built.places, built.sameExports, built.allOutcomes⟩

end CoreJS.IndexedHelperConstructor
