import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.ResolvedPlacement

/-! A terminal export list has a source ordinal and an owner even though it
does not become an executable CoreJS statement. The source bundle retains that
ordinal and its exports; the CoreJS statement list contains only executable
units. These results concern the actual `moduleOf` construction. -/

namespace CoreJS.TerminalExportPlacement

/-- CoreJS's module stores the export table but not the source ordinal of its
standalone export list. This source form supplies that ordinal explicitly; its
export table is the module's `exports`. -/
structure TerminalSource where
  module : Module
  exportOrdinal : Nat

/-- Select an original unit. A name can select only an executable declaration;
the terminal export list must be selected by its original ordinal. -/
def select (source : TerminalSource) (selector : EsmSplit.Selector) : Except String Nat :=
  match selector with
  | .stmt ordinal =>
    if ordinal = source.exportOrdinal then .ok ordinal
    else (ResolvedPlacement.select source.module selector).map Prod.snd
  | .name _ => (ResolvedPlacement.select source.module selector).map Prod.snd

/-- Resolve every authored selector without renumbering the selected unit. -/
def selectRequests (source : TerminalSource) (request : EsmSplit.Spec) : Except String (List UnitPlacement) := do
  let rows ← request.modules.mapM fun (owner, selectors) => do
    selectors.mapM fun selector => do
      let ordinal ← select source selector
      return (ordinal, owner)
  return rows.flatten

/-- Checked original labels for executable and terminal export units together. -/
structure Selection (source : TerminalSource) (request : EsmSplit.Spec) where
  selected : List UnitPlacement
  resolved : selectRequests source request = .ok selected
  sourceUnique : (source.module.body.map Prod.fst).Nodup
  terminalAfter : ∀ id ∈ source.module.body.map Prod.fst, id < source.exportOrdinal
  selectedUnique : (selected.map Prod.fst).Nodup
  coverage : (selected.map Prod.fst).Perm (source.exportOrdinal :: source.module.body.map Prod.fst)
  exportOwner : (source.exportOrdinal, request.entry) ∈ selected

/-- Resolve all units from the source and request. Missing or repeated units,
ambiguous executable selectors, and a terminal export owned outside the entry
are refused. No candidate output is inspected. -/
def resolve (source : TerminalSource) (request : EsmSplit.Spec) : Except String (Selection source request) := do
  let .isTrue sourceUnique := (inferInstance : Decidable ((source.module.body.map Prod.fst).Nodup)) |
    throw "source unit identities are duplicated"
  let .isTrue terminalAfter := (inferInstance : Decidable
      (∀ id ∈ source.module.body.map Prod.fst, id < source.exportOrdinal)) |
    throw "export list is not after every executable source unit"
  match resolved : selectRequests source request with
  | .error why => throw why
  | .ok selected =>
    let .isTrue unique := (inferInstance : Decidable ((selected.map Prod.fst).Nodup)) |
      throw "an original unit is selected more than once"
    let .isTrue coverage := (inferInstance : Decidable
        ((selected.map Prod.fst).Perm (source.exportOrdinal :: source.module.body.map Prod.fst))) |
      throw "selectors do not cover every original unit"
    let .isTrue exportOwner := (inferInstance : Decidable ((source.exportOrdinal, request.entry) ∈ selected)) |
      throw "terminal export unit must be owned by the entry"
    return ⟨selected, resolved, sourceUnique, terminalAfter, unique, coverage, exportOwner⟩

/-- The accepted assignment includes every original unit exactly once and
retains the explicitly authored entry ownership of the export list. -/
theorem resolve_sound {source request} (result : Selection source request)
    (_accepted : resolve source request = .ok result) :
    selectRequests source request = .ok result.selected ∧
      (result.selected.map Prod.fst).Nodup ∧
      (result.selected.map Prod.fst).Perm (source.exportOrdinal :: source.module.body.map Prod.fst) ∧
      (source.exportOrdinal, request.entry) ∈ result.selected :=
  ⟨result.resolved, result.selectedUnique, result.coverage, result.exportOwner⟩

/-- A faithfully placed terminal metadata unit remains in its authored owner,
contributes its named exports to that module, and creates no executable unit.
This does not by itself establish that the exported local name links to its
original cell. -/
theorem terminal_export_in_owner {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} {owner : EsmSplit.Module} {ordinal : Nat} {pubName localName : Name}
    (faithful : EsmSplit.Faithful B S P) (findOwner : P.find S.entry = some owner)
    (inBundle : ordinal < B.length) (notExecutable : ss.length ≤ ordinal)
    (authored : S.claimants B ordinal = [S.entry])
    (exported : (pubName, some localName) ∈ (B.stmt ordinal).exports) :
    ordinal ∈ owner.stmts ∧ (pubName, localName) ∈ (moduleOf ss B P owner).exports ∧
      ∀ stmt, (ordinal, stmt) ∉ (moduleOf ss B P owner).body := by
  have ownerIn : owner ∈ P.mods := List.mem_of_find?_eq_some findOwner
  have ownerName : owner.name = S.entry := by
    have h := List.find?_some findOwner
    simpa [EsmSplit.Plan.find] using h
  have placed : ordinal ∈ owner.stmts :=
    (faithful.2.2.2.2.1 owner ownerIn ordinal (List.mem_range.2 inBundle)).2 (by simpa [ownerName] using authored)
  refine ⟨placed, ?_, ?_⟩
  · change (pubName, localName) ∈ owner.namedExports B
    unfold EsmSplit.Module.namedExports EsmSplit.Module.namedExportsFrom
    apply List.mem_append.mpr
    right
    unfold EsmSplit.localExports
    apply List.mem_filterMap.mpr
    refine ⟨(pubName, some localName), ?_, rfl⟩
    unfold EsmSplit.Module.publishedFrom
    apply List.mem_flatMap.mpr
    refine ⟨ordinal, placed, ?_⟩
    rw [EsmSplit.Bundle.toArray_getD]
    exact exported
  · intro stmt member
    have h : (ordinal, stmt) ∈ owner.stmts.filterMap (numbered ss) := member
    have h := (mem_numbered ss).1 h
    have none : ss[ordinal]? = none := List.getElem?_eq_none notExecutable
    rw [none] at h
    cases h.2

/-- A faithfully planned metadata unit cannot appear in a different owner
from its unique authored claimant. -/
theorem wrong_owner_not_placed {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    {owner : EsmSplit.Module} {ordinal : Nat} {claimed : String}
    (faithful : EsmSplit.Faithful B S P) (ownerIn : owner ∈ P.mods)
    (inBundle : ordinal < B.length) (authored : S.claimants B ordinal = [claimed])
    (different : owner.name ≠ claimed) : ordinal ∉ owner.stmts := by
  intro placed
  have h := (faithful.2.2.2.2.1 owner ownerIn ordinal (List.mem_range.2 inBundle)).1 placed
  rw [authored] at h
  exact different (by simpa using h.symm)

#print axioms resolve
#print axioms resolve_sound
#print axioms terminal_export_in_owner
#print axioms wrong_owner_not_placed

end CoreJS.TerminalExportPlacement
