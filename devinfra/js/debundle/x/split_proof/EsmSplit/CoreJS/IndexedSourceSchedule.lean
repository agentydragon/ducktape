import EsmSplit.CoreJS.AuthenticatedSource
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

/-- The decoder's source-order classification carries the original ordinal and
the exact executable statement or export row it produced. -/
abbrev SourceUnit := LinkedInput.SourceItem

def SourceUnit.ordinal : SourceUnit → Nat := LinkedInput.SourceItem.ordinal

def SourceUnit.executable? (unit : SourceUnit) : Option Nat :=
  (LinkedInput.SourceItem.executable? unit).map Prod.fst

def SourceUnit.executableRow? (unit : SourceUnit) : Option (Nat × Stmt) :=
  LinkedInput.SourceItem.executable? unit

def SourceUnit.localExport? (unit : SourceUnit) : Option (Nat × List (Name × Name)) :=
  (LinkedInput.SourceItem.localExport? unit).map fun row => (row.ordinal, row.names)

def SourceUnit.localExportRow? : SourceUnit → Option LocalExportUnit :=
  LinkedInput.SourceItem.localExport?

def SourceUnit.omitted? : SourceUnit → Option Nat :=
  LinkedInput.SourceItem.omitted?

private theorem executableIdsFromRows (units : List SourceUnit) :
    units.filterMap SourceUnit.executable? =
      (units.filterMap SourceUnit.executableRow?).map Prod.fst := by
  induction units with
  | nil => rfl
  | cons unit tail ih =>
    cases unit with
    | executable ordinal statement =>
      simp [SourceUnit.executable?, SourceUnit.executableRow?,
        LinkedInput.SourceItem.executable?, ih]
    | localExport row =>
      change tail.filterMap SourceUnit.executable? =
        (tail.filterMap SourceUnit.executableRow?).map Prod.fst
      exact ih
    | omitted ordinal =>
      change tail.filterMap SourceUnit.executable? =
        (tail.filterMap SourceUnit.executableRow?).map Prod.fst
      exact ih

private theorem localExportRowsFromUnits (units : List SourceUnit) :
    units.filterMap SourceUnit.localExport? =
      (units.filterMap LinkedInput.SourceItem.localExport?).map fun row => (row.ordinal, row.names) := by
  induction units with
  | nil => rfl
  | cons unit tail ih =>
    cases unit with
    | executable ordinal statement =>
      change tail.filterMap SourceUnit.localExport? =
        (tail.filterMap LinkedInput.SourceItem.localExport?).map fun row => (row.ordinal, row.names)
      exact ih
    | localExport row =>
      simp [SourceUnit.localExport?, LinkedInput.SourceItem.localExport?, ih]
    | omitted ordinal =>
      change tail.filterMap SourceUnit.localExport? =
        (tail.filterMap LinkedInput.SourceItem.localExport?).map fun row => (row.ordinal, row.names)
      exact ih

/-- The decoder's separate executable and metadata rows form one complete
source sequence against the actual original node count and bundle. -/
structure Sequence (decoded : AuthenticatedSource.IndexedSource) (bundle : Bundle) where
  source : CoreJS.Module := decoded.source
  sourceEq : source = decoded.source
  singleton : decoded.program.modules = [decoded.source] := decoded.singleton
  decoderCoverage : List.Perm
    (AuthenticatedSource.sourceUnitIds decoded.program decoded.localExports)
    (List.range decoded.nodes.size)
  units : List SourceUnit
  unitsEq : units = decoded.sourceItems
  sourceProjection : List.Perm (units.map SourceUnit.ordinal)
    (((source.body.map Prod.fst ++ decoded.localExports.map (·.ordinal)) ++
      units.filterMap SourceUnit.omitted?))
  bundleCount : bundle.length = decoded.nodes.size
  ordinals : units.map SourceUnit.ordinal = List.range decoded.nodes.size
  executableRows : units.filterMap SourceUnit.executableRow? = source.body
  executableIds : units.filterMap SourceUnit.executable? = source.body.map Prod.fst
  metadataSourceRows : units.filterMap SourceUnit.localExport? =
    decoded.localExports.map fun row => (row.ordinal, row.names)
  executableIdsUnique : (source.body.map Prod.fst).Nodup
  metadataIdsUnique : (decoded.localExports.map (·.ordinal)).Nodup
  disjoint : ∀ row ∈ decoded.localExports, row.ordinal ∉ source.body.map Prod.fst
  sourceCoverage : ((source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Perm
    (List.range bundle.length)

/-- Check the complete ordinal classification from the shared source decoder.
This rejects missing or duplicate units and never discards empty metadata rows. -/
def checkSequence (decoded : AuthenticatedSource.IndexedSource) (bundle : Bundle) :
    Except String (Sequence decoded bundle) := do
  let .isTrue countMatches := (inferInstance : Decidable
      (bundle.length = decoded.nodes.size)) |
    throw "source bundle length differs from decoded original node count"
  let source := decoded.source
  let units := decoded.sourceItems
  have ordinals : units.map SourceUnit.ordinal = List.range decoded.nodes.size :=
    decoded.sourceOrdinals
  have executableRows : units.filterMap SourceUnit.executableRow? = source.body := by
    change decoded.sourceItems.filterMap LinkedInput.SourceItem.executable? = decoded.source.body
    exact decoded.executableProjection.symm
  have executableIds : units.filterMap SourceUnit.executable? = source.body.map Prod.fst := by
    rw [executableIdsFromRows, executableRows]
  have metadataSourceRows : units.filterMap SourceUnit.localExport? =
      decoded.localExports.map fun row => (row.ordinal, row.names) := by
    rw [localExportRowsFromUnits, decoded.localExportProjection]
  have sourceCoverage : ((source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Perm
      (List.range bundle.length) := by
    simpa [AuthenticatedSource.sourceUnitIds, decoded.singleton, countMatches] using decoded.coverage
  have coveredIdsNodup :
      ((source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Nodup :=
    sourceCoverage.nodup_iff.mpr List.nodup_range
  have coveredParts := List.nodup_append.1 coveredIdsNodup
  have executableIdsUnique := coveredParts.1
  have metadataIdsUnique := coveredParts.2.1
  have disjoint : ∀ row ∈ decoded.localExports, row.ordinal ∉ source.body.map Prod.fst := by
    intro row rowMember executableMember
    exact coveredParts.2.2 row.ordinal executableMember row.ordinal
      (List.mem_map.mpr ⟨row, rowMember, rfl⟩) rfl
  return {
    source := source
    sourceEq := rfl
    singleton := decoded.singleton
    decoderCoverage := decoded.coverage
    units := units
    unitsEq := rfl
    sourceProjection := by
      simpa [SourceUnit.ordinal, SourceUnit.omitted?] using decoded.sourceProjection
    bundleCount := countMatches
    ordinals := ordinals
    executableRows := executableRows
    executableIds := executableIds
    metadataSourceRows := metadataSourceRows
    executableIdsUnique := executableIdsUnique
    metadataIdsUnique := metadataIdsUnique
    disjoint := disjoint
    sourceCoverage := sourceCoverage
  }

/-- Induction-facing facts for consumers of the original decoder result. The
classification is the decoder's CoreJS source-order trace, not an inference
from a planner schedule. Its executable and metadata projections provide the
complete source-ID permutation used by the list constructor and export-link
checks. -/
theorem sourceInductionFacts {decoded bundle}
    (sequence : Sequence decoded bundle) :
    sequence.units.map SourceUnit.ordinal = List.range decoded.nodes.size ∧
    sequence.units.filterMap SourceUnit.executableRow? = sequence.source.body ∧
    (sequence.units.filterMap SourceUnit.localExport? =
      (decoded.localExports.map fun row => (row.ordinal, row.names))) ∧
    ((sequence.source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Perm
      (List.range bundle.length) ∧
    (sequence.source.body.map Prod.fst).Nodup ∧
    (decoded.localExports.map (·.ordinal)).Nodup ∧
    (∀ row ∈ decoded.localExports, row.ordinal ∉ sequence.source.body.map Prod.fst) :=
  ⟨sequence.ordinals, sequence.executableRows,
    sequence.metadataSourceRows, sequence.sourceCoverage, sequence.executableIdsUnique,
    sequence.metadataIdsUnique, sequence.disjoint⟩

/-- Once the executable and metadata projections cover the complete source
range, the source-order projection theorem leaves no omitted decoder slots. -/
theorem noOmittedSourceRows {decoded bundle}
    (sequence : Sequence decoded bundle) :
    sequence.units.filterMap SourceUnit.omitted? = [] := by
  have hprojection := sequence.sourceProjection.length_eq
  have hcoverage := sequence.sourceCoverage.length_eq
  have hordinals := congrArg List.length sequence.ordinals
  have hcount := sequence.bundleCount
  simp only [List.length_map, List.length_append, List.length_range] at hprojection hcoverage hordinals
  have omittedLength : (sequence.units.filterMap SourceUnit.omitted?).length = 0 := by
    omega
  cases omitted : sequence.units.filterMap SourceUnit.omitted? with
  | nil => rfl
  | cons head tail => simp [omitted] at omittedLength

/-- Evidence that the actual `mkPlan` preserves the full source-unit order;
the sparse executable projection is checked separately for the real indexed
CoreJS constructor. -/
structure Evidence (decoded : AuthenticatedSource.IndexedSource) (bundle : Bundle)
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
def check (decoded : AuthenticatedSource.IndexedSource) (bundle : Bundle) (request : Spec) :
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

end CoreJS.IndexedSourceSchedule
