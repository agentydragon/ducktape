import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.ResolvedPlacement

/-! A model-only sparse constructor. The existing checked splitter supplies
its compact plan and module bodies; the result is checked again against the
actual original identities, links, public cells and ordered schedules. -/

namespace CoreJS.SparseCheckedSplit

open LinkedBehavior

/-- A placement witness is tied to the existing compact planner. -/
abbrev Placement (source : Program) (request : EsmSplit.Spec) :=
  ResolvedPlacement.CheckedPlacement source request (CheckedSplit.requestedPlacement source)

/-- The compact source is used only to construct a template with the existing
splitter. It is not used as evidence about the original program's execution.
Public exports are restored on the actual entry and verified by the linker. -/
private def compactSource {source request} (placement : Placement source request) : Program :=
  let m := placement.module
  let body := (List.range m.body.length).zip (m.body.map Prod.snd)
  ⟨m.name, [{ m with body := body, exports := [] }]⟩

/-- Restore each actual source identity. Missing planner positions refuse. -/
private def restoreModule (original : Module) (entry : String) (m : Module) : Except String Module := do
  let body ← m.body.mapM fun (index, stmt) => do
    let some (id, _) := original.body[index]? | throw s!"compact unit {index} has no original identity"
    return (id, stmt)
  return { m with body := body, exports := if m.name == entry then m.exports ++ original.exports else m.exports }

private def restoreProgram (original : Module) (program : Program) : Except String Program := do
  let modules ← program.modules.mapM (restoreModule original program.entry)
  return { program with modules := modules }

private theorem forRequest {source output : Program}
    (requested : Guarantee) (runs : ∀ w n, runProgram w n source = runProgram w n output) :
    ScheduleEquivalent requested (fun _ => True) source output := by
  cases requested with
  | allOutcomes => exact fun w _ n => runs w n
  | normalCompletion =>
    apply normalEquivalent_of_bounded
    intro w _ n final
    rw [runs w n]

/-- Construct using the existing splitter and accept only actual linked,
order-preserved all-outcomes evidence. The source has two increasing unit IDs,
the first zero and the second separated by a gap. Omitted source metadata is
not modeled here. -/
def construct {source request} (placement : Placement source request) (requested : Guarantee) :
    Except String { output : Program // ResolvedPlacement.Sound placement requested output } := do
  let [0, second] := placement.module.body.map Prod.fst |
    throw "sparse construction requires two units with first ID 0"
  unless second > 1 do throw "sparse construction requires a gap after the first unit"
  unless decide ((placement.module.exports.map Prod.fst).Nodup) do
    throw "source public export names are duplicated"
  let template ← CheckedSplit.checkedSplit (compactSource placement) placement.normalized .allOutcomes
  let output ← restoreProgram placement.module template.program
  let ⟨places⟩ ← checkPlaces placement.assignment source output
  let links ← checkProbePair source output
  let checked ← (checkLinkedSchedules [] links.before links.after {}).mapError StructuralRefusal.reason
  match checked with
  | ⟨.allOutcomes, .preservedOrder, ⟨equivalent, exports⟩⟩ =>
    have runs (w : World) (fuel : Nat) : runProgram w fuel source = runProgram w fuel output :=
      equivalent w (by simp [StableIntrinsic.Worlds]) fuel
    return ⟨output, places, exports, forRequest requested runs⟩
  | _ => throw "sparse construction requires an order-preserved all-outcomes schedule"

/-- Soundness carries the exact resolved placement, live exports and requested
guarantee for the constructed CoreJS output. -/
theorem construct_sound {source : Program} {request : EsmSplit.Spec} {placement : Placement source request} {requested : Guarantee}
    {result : { output : Program // ResolvedPlacement.Sound (source := source) (request := request) placement requested output }}
    (_accepted : construct placement requested = .ok result) :
    ResolvedPlacement.Sound placement requested result.val := result.property

/-- Independently supplied output must be the exact constructed CoreJS graph. -/
def checkOutput {source : Program} {request : EsmSplit.Spec} {placement : Placement source request} {requested : Guarantee}
    (built : { output : Program // ResolvedPlacement.Sound (source := source) (request := request) placement requested output })
    (decoded : Program) : Except String (PLift (decoded = built.val)) :=
  match SyntaxMatch.programEq decoded built.val with
  | some proof => .ok proof
  | none => .error "decoded output differs from the constructed modules"

theorem checkOutput_sound {source : Program} {request : EsmSplit.Spec} {placement : Placement source request}
    {requested : Guarantee} {decoded : Program}
    (built : { output : Program // ResolvedPlacement.Sound (source := source) (request := request) placement requested output })
    (same : decoded = built.val) : ResolvedPlacement.Sound placement requested decoded :=
  same ▸ built.property

#print axioms construct
#print axioms construct_sound
#print axioms checkOutput_sound

end CoreJS.SparseCheckedSplit
