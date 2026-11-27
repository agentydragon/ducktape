import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedReorder
import EsmSplit.CoreJS.IndexedSourceSchedule

/-! A checked inline-export source family for the actual indexed decoder and
authored plan. Source declarations retain their original IDs and exact kinds;
the plan must keep executable order. -/

namespace CoreJS.IndexedInlinePlan

open AuthenticatedSource (IndexedSource)

private def topLevelDeclaration : Stmt → Bool
  | .decl _ _ _ => true
  | _ => false

private theorem topLevelDeclaration_sound {stmt : Stmt} (accepted : topLevelDeclaration stmt = true) :
    ∃ kind name init, stmt = .decl kind name init := by
  cases stmt with
  | decl kind name init => exact ⟨kind, name, init, rfl⟩
  | _ => simp [topLevelDeclaration] at accepted

structure InlineOnlySource (decoded : IndexedSource) where
  source : Module
  sourceEq : source = decoded.source
  programEntry : decoded.program.entry = decoded.source.name
  imports : decoded.source.imports = []
  noLocalExports : decoded.localExports = []
  safePath : EsmSplit.safeModulePath decoded.source.name = true
  ordered : decoded.source.body.Pairwise (fun a b => a.1 ≤ b.1)
  declarations : ∀ p ∈ decoded.source.body, topLevelDeclaration p.2 = true

/-- Check the shared decoder's complete source identity, import boundary and
original declaration shape. In particular, `.decl .let_ name .undef` remains
the decoder's no-initializer form and is not reclassified as a const. -/
def checkSource (decoded : IndexedSource) : Except String (InlineOnlySource decoded) := do
  let .isTrue programEntry := (inferInstance : Decidable
      (decoded.program.entry = decoded.source.name)) |
    throw "decoded program entry differs from its source module"
  let .isTrue imports := (inferInstance : Decidable (decoded.source.imports = [])) |
    throw "inline indexed source imports modules"
  let .isTrue noLocalExports := (inferInstance : Decidable (decoded.localExports = [])) |
    throw "inline indexed source contains standalone local-export rows"
  let .isTrue safePath := (inferInstance : Decidable
      (EsmSplit.safeModulePath decoded.source.name = true)) |
    throw "inline indexed source has an unsupported module path"
  let .isTrue ordered := (inferInstance : Decidable
      (decoded.source.body.Pairwise (fun a b => a.1 ≤ b.1))) |
    throw "decoded inline source rows are not in original ordinal order"
  let .isTrue declarations := (inferInstance : Decidable
      (∀ p ∈ decoded.source.body, topLevelDeclaration p.2 = true)) |
    throw "expected simple top-level declaration rows"
  return ⟨decoded.source, rfl, programEntry, imports, noLocalExports, safePath, ordered, declarations⟩

structure Checked (decoded : IndexedSource) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) where
  source : InlineOnlySource decoded
  schedule : IndexedSourceSchedule.Evidence decoded B S
  scheduleSource : schedule.sequence.source = decoded.source
  entry : EsmSplit.Module
  planEntry : schedule.plan.find schedule.plan.entry = some entry
  noGlue : entry.exports = []
  owned : IndexedReorder.InlineExportsOwned decoded.source B entry
  table : (moduleOfIndexed decoded.source.body B schedule.plan entry).exports = decoded.source.exports
  footprints : IndexedInputChecks.FootprintChecks decoded.source.body B
  definitions : IndexedInputChecks.DefinitionChecks decoded.source.body B

/-- Accept only the actual decoded source and actual authored `mkPlan`. The
source schedule supplies complete raw ordinals, the executable projection,
and the order-kept check consumed by the common indexed constructor. -/
def check (decoded : IndexedSource) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) :
    Except String (Checked decoded B S) := do
  let source ← checkSource decoded
  let schedule ← IndexedSourceSchedule.check decoded B S
  have scheduleSource : schedule.sequence.source = decoded.source := schedule.sequence.sourceEq
  let .isTrue footprints := (inferInstance : Decidable
      (IndexedInputChecks.FootprintChecks decoded.source.body B)) |
    throw "decoded inline rows differ from their planner footprints"
  let .isTrue definitions := (inferInstance : Decidable
      (IndexedInputChecks.DefinitionChecks decoded.source.body B)) |
    throw "planner definitions lack executable source rows"
  match planEntry : schedule.plan.find schedule.plan.entry with
  | none => throw "authored inline plan has no entry module"
  | some entry =>
    let .isTrue noGlue := (inferInstance : Decidable (entry.exports = [])) |
      throw "inline plan entry needs generated glue exports"
    let .isTrue owned := (inferInstance : Decidable
        (IndexedReorder.InlineExportsOwned decoded.source B entry)) |
      throw "inline public binding is not owned by its original entry unit"
    let .isTrue table := (inferInstance : Decidable
        ((moduleOfIndexed decoded.source.body B schedule.plan entry).exports = decoded.source.exports)) |
      throw "planned inline exports differ from the original public table"
    return ⟨source, schedule, scheduleSource, entry, planEntry, noGlue, owned, table,
      footprints, definitions⟩

private theorem sourceFind {decoded B S} (checked : Checked decoded B S) :
    decoded.program.find decoded.program.entry = some decoded.source := by
  simp [Program.find, decoded.singleton, checked.source.programEntry]

private theorem orderedPlan {decoded B S} (checked : Checked decoded B S) :
    SparseProjectedOrder.OrderKept decoded.source.body checked.schedule.plan := by
  have kept := checked.schedule.executableOrder
  rw [checked.scheduleSource] at kept
  exact kept

private theorem declarations {decoded B S} (checked : Checked decoded B S) :
    ∀ p ∈ decoded.source.body, ∃ kind name init, p.2 = .decl kind name init := by
  intro p member
  exact topLevelDeclaration_sound (checked.source.declarations p member)

/-- For every modeled world and fuel, order-kept indexed construction has the
same outcomes, including throws, unknown results and fuel exhaustion. -/
theorem allOutcomes {decoded B S} (checked : Checked decoded B S) :
    ∀ w fuel,
      runProgram w fuel decoded.program =
        runProgram w fuel (splitProgramIndexed decoded.source.body B checked.schedule.plan) := by
  have footprints := IndexedInputChecks.checked_footprints checked.footprints
  have complete := IndexedInputChecks.checked_definitions checked.definitions
  exact IndexedUnitsAgree.indexed_run_same_of_order_kept checked.schedule.valid
    footprints.1 footprints.2 complete rfl checked.source.imports (sourceFind checked)
    decoded.singleton checked.source.ordered (orderedPlan checked)

/-- Inline public names resolve to the same allocated live cells in every
world. Ownership stays attached to the original decoded source ordinals. -/
theorem sameExports {decoded B S} (checked : Checked decoded B S) :
    LinkedBehavior.SameExports decoded.program
      (splitProgramIndexed decoded.source.body B checked.schedule.plan) := by
  have footprints := IndexedInputChecks.checked_footprints checked.footprints
  have complete := IndexedInputChecks.checked_definitions checked.definitions
  have kept := orderedPlan checked
  have covered : (SparseProjectedOrder.executableIds decoded.source.body checked.schedule.plan).Perm
      (decoded.source.body.map Prod.fst) := by
    rw [kept.2]
  exact IndexedReorder.same_inline_exports_of_declarations checked.schedule.valid decoded.singleton
    checked.source.imports (sourceFind checked) footprints.1 complete checked.source.ordered
    (declarations checked) covered checked.planEntry checked.owned checked.table

/-- Every executable output placement retains the unique owner from the
authored plan at its original raw ordinal. -/
theorem placementOwner {decoded B S} (checked : Checked decoded B S)
    {ordinal : Nat} {owner : String}
    (member : (ordinal, owner) ∈
      (splitProgramIndexed decoded.source.body B checked.schedule.plan).fileUnitPlacements) :
    S.claimants B ordinal = [owner] := by
  apply IndexedSourceSchedule.constructedPlacementOwner checked.schedule
  simpa [checked.scheduleSource] using member


end CoreJS.IndexedInlinePlan
