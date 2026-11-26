import EsmSplit.CoreJS.TerminalConstructorInput
import EsmSplit.CoreJS.TerminalExportPlacement
import EsmSplit.CoreJS.TerminalPlanSelection
import EsmSplit.ModulePath

/-! A checked CoreJS constructor for an order-kept singleton source whose
entire public table comes from one terminal local-export unit. The bundle and
spec are the actual inputs to the ordinary planner. -/

namespace CoreJS.TerminalConstructor

/-- Evidence used to construct exactly the ordinary planned CoreJS program.
The source rows and full planner bundle are supplied by the trusted frontend. -/
structure Construction (input : Program × List EsmSplit.LocalExportUnit)
    (B : EsmSplit.Bundle) (S : EsmSplit.Spec) where
  checked : TerminalConstructorInput.Checked input
  placement : TerminalExportPlacement.Selection ⟨checked.terminal.source, checked.terminal.row.ordinal⟩ S
  matching : TerminalPlanSelection.ClaimantsMatch B S placement.selected
  complete : B.length = checked.terminal.row.ordinal + 1
  evaluationOrder : (EsmSplit.mkPlan B S).evalOrder = S.order ++ [S.entry]
  valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)
  footprints : MetadataCells.Footprints (checked.terminal.source.body.map Prod.snd) B
  ordered : ProjectedOrder.Kept (checked.terminal.source.body.map Prod.snd) (EsmSplit.mkPlan B S)
  entry : EsmSplit.Module
  found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some entry
  units : entry.stmts = [checked.terminal.row.ordinal]
  glue : entry.exports = []
  rowFootprint : checked.terminal.row.FootprintMatches (B.stmt checked.terminal.row.ordinal)
  sourceDefs : MetadataCells.RowLocalsDefined (checked.terminal.source.body.map Prod.snd) B checked.terminal.row

/-- The output is computed from the same bundle and plan validated in the
record; callers do not provide a candidate program. -/
def Construction.program {input B S} (result : Construction input B S) : Program :=
  splitProgram (result.checked.terminal.source.body.map Prod.snd) B (EsmSplit.mkPlan B S)

/-- Check the terminal rule without evaluating either program. -/
def construct (input : Program × List EsmSplit.LocalExportUnit) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) :
    Except String (Construction input B S) := do
  unless S.sequential && !S.privateEntryBody do
    throw "terminal constructor requires sequential placement and no private entry body"
  EsmSplit.validateModulePaths (S.entry :: S.modules.map Prod.fst)
  let checked ← TerminalConstructorInput.check input
  let placement ← TerminalExportPlacement.resolve ⟨checked.terminal.source, checked.terminal.row.ordinal⟩ S
  let ss := checked.terminal.source.body.map Prod.snd
  let plan := EsmSplit.mkPlan B S
  let .isTrue matching := (inferInstance : Decidable (TerminalPlanSelection.ClaimantsMatch B S placement.selected)) |
    throw "resolved source owners do not match the original-bundle planner"
  let .isTrue complete := (inferInstance : Decidable (B.length = checked.terminal.row.ordinal + 1)) |
    throw "terminal export row must be the final original source unit"
  let .isTrue evaluationOrder := (inferInstance : Decidable (plan.evalOrder = S.order ++ [S.entry])) |
    throw "terminal plan does not follow the authored module order"
  let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid B S plan)) |
    throw "terminal plan failed placement, linkage or scope checks"
  let .isTrue footprints := (inferInstance : Decidable (MetadataCells.Footprints ss B)) |
    throw "terminal source footprints do not match the original bundle"
  let .isTrue ordered := (inferInstance : Decidable (ProjectedOrder.Kept ss plan)) |
    throw "terminal plan changes original executable order"
  let .isTrue rowFootprint :=
    (inferInstance : Decidable (checked.terminal.row.FootprintMatches (B.stmt checked.terminal.row.ordinal))) |
    throw "terminal export row does not match its original bundle footprint"
  let .isTrue sourceDefs := (inferInstance : Decidable (MetadataCells.RowLocalsDefined ss B checked.terminal.row)) |
    throw "terminal export row names an undeclared source binding"
  match found : plan.find plan.entry with
  | none => throw "terminal plan has no entry"
  | some entry =>
    if units : entry.stmts = [checked.terminal.row.ordinal] then
      if glue : entry.exports = [] then
        return ⟨checked, placement, matching, complete, evaluationOrder, valid, footprints, ordered, entry, found, units, glue, rowFootprint, sourceDefs⟩
      else throw "terminal entry has generated glue exports"
    else throw "terminal entry must contain only the original export row"

/-- Every accepted output preserves live exports and complete runs in every
modeled world and at every fuel. -/
theorem sound {input B S} (result : Construction input B S) :
    LinkedBehavior.SameExports input.1 result.program ∧
      ∀ w fuel, runProgram w fuel input.1 = runProgram w fuel result.program :=
  TerminalConstructorInput.planned_family result.checked B S result.entry result.valid result.footprints
    result.ordered result.found result.units result.glue result.rowFootprint result.sourceDefs


/-- The exact executable owner labels and the terminal metadata owner are
those resolved from the original source and authored request. -/
theorem selected_owners {input B S} (result : Construction input B S) :
    (∀ m ∈ (EsmSplit.mkPlan B S).mods, ∀ i (hi : i < (result.checked.terminal.source.body.map Prod.snd).length),
      (i, (result.checked.terminal.source.body.map Prod.snd)[i]) ∈
        (moduleOf (result.checked.terminal.source.body.map Prod.snd) B (EsmSplit.mkPlan B S) m).body ↔
          (i, m.name) ∈ result.placement.selected) ∧
    (result.checked.terminal.row.ordinal, result.entry.name) ∈ result.placement.selected ∧
    (∀ owner, (result.checked.terminal.row.ordinal, owner) ∈ result.placement.selected → owner = result.entry.name) := by
  have terminal : result.checked.terminal.row.ordinal = (result.checked.terminal.source.body.map Prod.snd).length := by
    simpa using result.checked.terminal.terminal.2
  have ids : result.checked.terminal.source.body.map Prod.fst =
      List.range (result.checked.terminal.source.body.map Prod.snd).length := by
    simpa using result.checked.terminal.terminal.1
  exact TerminalPlanSelection.selected_owners result.checked.terminal.row terminal result.complete result.valid
    result.entry result.found result.units ⟨result.checked.terminal.source, result.checked.terminal.row.ordinal⟩
    rfl ids result.placement result.matching

/-- Every original source ordinal is either one executable unit or the single
terminal metadata unit; metadata creates no executable entry body. -/
theorem original_units {input B S} (result : Construction input B S) :
    (∀ i, i < B.length → i < (result.checked.terminal.source.body.map Prod.snd).length ∨
      i = result.checked.terminal.row.ordinal) ∧
    (moduleOf (result.checked.terminal.source.body.map Prod.snd) B (EsmSplit.mkPlan B S) result.entry).body = [] := by
  have terminal : result.checked.terminal.row.ordinal = (result.checked.terminal.source.body.map Prod.snd).length := by
    simpa using result.checked.terminal.terminal.2
  have all := TerminalPlanOwnership.all_original_owners result.checked.terminal.row terminal result.complete
    result.valid result.entry result.found result.units
  exact ⟨all.1, all.2.2.2.2⟩

#print axioms Construction.program
#print axioms construct
#print axioms sound
#print axioms selected_owners
#print axioms original_units

end CoreJS.TerminalConstructor
