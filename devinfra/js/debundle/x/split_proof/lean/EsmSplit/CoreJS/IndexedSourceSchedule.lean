import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.SparseProjectedOrder
import EsmSplit.Split

/-! A checked schedule for source units whose original ordinals include
non-executable local-export rows. The decoder owns source recognition; this
layer checks that its classified rows form the exact original schedule and
that the actual authored planner preserves that schedule. -/

namespace CoreJS.IndexedSourceSchedule

open EsmSplit

/-- A source row keeps its original ordinal and kind. In particular, an empty
local export list remains a metadata unit rather than disappearing. -/
inductive SourceUnit where
  | executable (ordinal : Nat) (statement : Stmt)
  | localExport (row : LocalExportUnit)

def SourceUnit.ordinal : SourceUnit → Nat
  | .executable ordinal _ => ordinal
  | .localExport row => row.ordinal

def SourceUnit.executable? : SourceUnit → Option Nat
  | .executable ordinal _ => some ordinal
  | .localExport _ => none

def SourceUnit.localExport? : SourceUnit → Option (Nat × List (Name × Name))
  | .executable _ _ => none
  | .localExport row => some (row.ordinal, row.names)

/-- Merge executable and metadata units by their original source ordinal. -/
def orderedUnits (source : CoreJS.Module) (rows : List LocalExportUnit) : List SourceUnit :=
  ((source.body.map fun (ordinal, statement) => SourceUnit.executable ordinal statement) ++
    rows.map SourceUnit.localExport).mergeSort fun left right => decide (left.ordinal ≤ right.ordinal)

/-- The decoder's separate executable and metadata rows form one complete
source sequence against the actual original node count and bundle. -/
structure Sequence (decoded : LinkedInput.IndexedSource) (bundle : Bundle) where
  source : CoreJS.Module
  singleton : decoded.program.modules = [source]
  decoderCoverage : List.Perm
    (LinkedInput.sourceUnitIds decoded.program decoded.localExports)
    (List.range decoded.nodes.size)
  units : List SourceUnit
  unitsEq : units = orderedUnits source decoded.localExports
  bundleCount : bundle.length = decoded.nodes.size
  ordinals : units.map SourceUnit.ordinal = List.range decoded.nodes.size
  executableIds : units.filterMap SourceUnit.executable? = source.body.map Prod.fst
  metadataRows : units.filterMap SourceUnit.localExport? =
    decoded.localExports.map fun row => (row.ordinal, row.names)
  disjoint : ∀ row ∈ decoded.localExports, row.ordinal ∉ source.body.map Prod.fst

/-- Check the complete ordinal classification from the shared source decoder.
This rejects missing or duplicate units and never discards empty metadata rows. -/
def checkSequence (decoded : LinkedInput.IndexedSource) (bundle : Bundle) :
    Except String (Sequence decoded bundle) := do
  let .isTrue countMatches := (inferInstance : Decidable
      (bundle.length = decoded.nodes.size)) |
    throw "source bundle length differs from decoded original node count"
  match modulesMatch : decoded.program.modules with
  | [source] =>
    let units := orderedUnits source decoded.localExports
    let .isTrue ordinals := (inferInstance : Decidable
        (units.map SourceUnit.ordinal = List.range decoded.nodes.size)) |
      throw "executable and metadata ordinals do not form the complete original source order"
    let .isTrue executableIds := (inferInstance : Decidable
        (units.filterMap SourceUnit.executable? = source.body.map Prod.fst)) |
      throw "sorting source units changed the executable source order"
    let .isTrue metadataRows := (inferInstance : Decidable
        (units.filterMap SourceUnit.localExport? =
          decoded.localExports.map fun row => (row.ordinal, row.names))) |
      throw "sorting source units changed the ordered metadata rows"
    let .isTrue disjoint := (inferInstance : Decidable
        (∀ row ∈ decoded.localExports, row.ordinal ∉ source.body.map Prod.fst)) |
      throw "a source ordinal is both executable and metadata"
    return {
      source := source
      singleton := modulesMatch
      decoderCoverage := decoded.coverage
      units := units
      unitsEq := rfl
      bundleCount := countMatches
      ordinals := ordinals
      executableIds := executableIds
      metadataRows := metadataRows
      disjoint := disjoint
    }
  | _ => throw "indexed source decoder did not return exactly one source module"

/-- Evidence that the actual `mkPlan` preserves the full source-unit order;
the sparse executable projection is checked separately for the real indexed
CoreJS constructor. -/
structure Evidence (decoded : LinkedInput.IndexedSource) (bundle : Bundle)
    (request : Spec) where
  sequence : Sequence decoded bundle
  plan : Plan
  planEq : plan = EsmSplit.mkPlan bundle request
  valid : EsmSplit.Valid bundle request plan
  fullOrder : plan.execOrder = sequence.units.map SourceUnit.ordinal
  executableOrder : SparseProjectedOrder.OrderKept sequence.source.body plan
  safePaths : ∀ module ∈ plan.mods, EsmSplit.safeModulePath module.name = true

/-- Check only the original decoded source, actual bundle, and authored
request. No replacement request, split-time Lean, or candidate execution is
introduced. -/
def check (decoded : LinkedInput.IndexedSource) (bundle : Bundle) (request : Spec) :
    Except String (Evidence decoded bundle request) := do
  let sequence ← checkSequence decoded bundle
  let plan := EsmSplit.mkPlan bundle request
  let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle request plan)) |
    throw "authored source-unit plan is invalid"
  let .isTrue fullOrder := (inferInstance : Decidable
      (plan.execOrder = sequence.units.map SourceUnit.ordinal)) |
    throw "authored plan does not enumerate every original source ordinal in order"
  let .isTrue executableOrder := (inferInstance : Decidable
      (SparseProjectedOrder.OrderKept sequence.source.body plan)) |
    throw "authored plan changes executable source order"
  let .isTrue safePaths := (inferInstance : Decidable
      (∀ module ∈ plan.mods, EsmSplit.safeModulePath module.name = true)) |
    throw "authored plan contains an unsafe module path"
  return ⟨sequence, plan, rfl, valid, fullOrder, executableOrder, safePaths⟩

/-- The real source-indexed splitter retains every executable statement with
its original ID; non-executable metadata is omitted from CoreJS bodies. -/
theorem constructedRows {decoded bundle request}
    (evidence : Evidence decoded bundle request) :
    (splitProgramIndexed evidence.sequence.source.body bundle evidence.plan).loaded.flatMap (·.body) =
      evidence.sequence.source.body :=
  SparseProjectedOrder.split_indexed_loaded_eq_source evidence.executableOrder

/-- Every file-unit placement emitted by the actual indexed constructor has
the unique owner claimed by the authored request. -/
theorem constructedPlacementOwner {decoded bundle request}
    (evidence : Evidence decoded bundle request) {ordinal : Nat} {owner : String}
    (member : (ordinal, owner) ∈
      (splitProgramIndexed evidence.sequence.source.body bundle evidence.plan).fileUnitPlacements) :
    request.claimants bundle ordinal = [owner] :=
  SparseProjectedOrder.split_indexed_authored_owner evidence.valid.2.2.1 evidence.safePaths member

/-- A classified metadata ordinal is absent from the emitted executable body
sequence while later executable IDs remain unchanged. -/
theorem metadataOmitted {decoded bundle request}
    (evidence : Evidence decoded bundle request) {row : LocalExportUnit}
    (member : row ∈ decoded.localExports) :
    row.ordinal ∉
      ((splitProgramIndexed evidence.sequence.source.body bundle evidence.plan).loaded.flatMap (·.body)).map Prod.fst := by
  rw [constructedRows evidence]
  exact evidence.sequence.disjoint row member

private theorem assignment_exists_of_execOrder {plan : Plan} {ordinal : Nat}
    (member : ordinal ∈ plan.execOrder) :
    ∃ owner, (ordinal, owner) ∈ CoreJS.PlanPlacement.assignments plan := by
  simp only [Plan.execOrder, List.mem_flatMap] at member
  obtain ⟨name, inOrder, inStmts⟩ := member
  cases found : plan.find name with
  | none => simp [found] at inStmts
  | some module =>
    have moduleIn : module ∈ plan.mods := List.mem_of_find?_eq_some found
    refine ⟨module.name, List.mem_flatMap.mpr ⟨module, moduleIn, ?_⟩⟩
    exact List.mem_map.mpr ⟨ordinal, by simpa [found] using inStmts, rfl⟩

private theorem assignment_owner_unique {bundle : Bundle} {request : Spec} {plan : Plan}
    (faithful : EsmSplit.Faithful bundle request plan) {ordinal : Nat} {owner other : String}
    (first : (ordinal, owner) ∈ CoreJS.PlanPlacement.assignments plan)
    (second : (ordinal, other) ∈ CoreJS.PlanPlacement.assignments plan) : owner = other := by
  have firstClaim := CoreJS.PlanPlacement.authored_owner faithful first
  have secondClaim := CoreJS.PlanPlacement.authored_owner faithful second
  have same : [owner] = [other] := firstClaim.symm.trans secondClaim
  injection same

/-- Each classified source unit has one plan owner. The separate
`assignmentOwnerIsAuthored` theorem ties that assignment to the request's
claimant. This covers metadata ordinals as well as executable rows without
treating metadata as code. -/
theorem uniquePlanOwner {decoded bundle request}
    (evidence : Evidence decoded bundle request) {unit : SourceUnit}
    (member : unit ∈ evidence.sequence.units) :
    ∃ owner : String,
      (unit.ordinal, owner) ∈ CoreJS.PlanPlacement.assignments evidence.plan ∧
      ∀ other, (unit.ordinal, other) ∈ CoreJS.PlanPlacement.assignments evidence.plan → other = owner := by
  have scheduled : unit.ordinal ∈ evidence.plan.execOrder := by
    rw [evidence.fullOrder]
    exact List.mem_map.mpr ⟨unit, member, rfl⟩
  obtain ⟨owner, ownerMember⟩ := assignment_exists_of_execOrder scheduled
  refine ⟨owner, ownerMember, ?_⟩
  intro other otherMember
  exact (assignment_owner_unique (bundle := bundle) (request := request) (plan := evidence.plan)
    (ordinal := unit.ordinal) (owner := owner) (other := other)
    evidence.valid.2.2.1 ownerMember otherMember).symm

theorem assignmentOwnerIsAuthored {decoded bundle request}
    (evidence : Evidence decoded bundle request) {ordinal : Nat} {owner : String}
    (owned : (ordinal, owner) ∈ CoreJS.PlanPlacement.assignments evidence.plan) :
    request.claimants bundle ordinal = [owner] :=
  CoreJS.PlanPlacement.authored_owner evidence.valid.2.2.1 owned

#print axioms checkSequence
#print axioms check
#print axioms constructedRows
#print axioms constructedPlacementOwner
#print axioms metadataOmitted
#print axioms uniquePlanOwner
#print axioms assignmentOwnerIsAuthored

end CoreJS.IndexedSourceSchedule
