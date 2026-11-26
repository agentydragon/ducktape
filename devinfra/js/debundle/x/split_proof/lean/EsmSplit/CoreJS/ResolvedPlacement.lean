import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.Split

/-! Resolve authored source selectors to original CoreJS unit identities.
This is a separate checked interface; the existing splitter is unchanged. -/

namespace CoreJS.ResolvedPlacement

open LinkedBehavior

/-- The original unit ID is the original source position supplied by the
CoreJS frontend. An absent position, including omitted metadata, is refused. -/
def select (m : Module) : EsmSplit.Selector → Except String (Nat × Nat)
  | .stmt ordinal =>
    match (m.body.zipIdx.filter fun (unit, _) => unit.1 == ordinal) with
    | [((id, _), index)] => .ok (index, id)
    | [] => .error s!"source position {ordinal} has no executable unit"
    | _ => .error s!"source position {ordinal} is ambiguous"
  | .name name =>
    let hits := m.body.zipIdx.flatMap fun ((id, stmt), index) =>
      (stmt.bindings.filter fun binding => binding.1 == name).map fun _ => (index, id)
    match hits with
    | [hit] => .ok hit
    | [] => .error s!"source binding {name} is missing"
    | _ => .error s!"source binding {name} is ambiguous"

/-- Translate source ordinal selectors to the compact indexes consumed by a supplied
compact planner, while recording exactly which original units were selected. -/
def selectRequests (m : Module) (request : EsmSplit.Spec) : Except String (EsmSplit.Spec × List UnitPlacement) := do
  let rows ← request.modules.mapM fun (owner, selectors) => do
    let selected ← selectors.mapM fun selector => do
      let (index, id) ← select m selector
      let compact := match selector with
        | .name name => EsmSplit.Selector.name name
        | .stmt _ => EsmSplit.Selector.stmt index
      return (compact, (id, owner))
    return ((owner, selected.map Prod.fst), selected.map Prod.snd)
  return ({ request with modules := rows.map Prod.fst }, rows.flatMap Prod.snd)

/-- The supported single-module boundary and owner labels. -/
def supported (m : Module) (request : EsmSplit.Spec) : Bool :=
  request.order.isEmpty && !request.sequential && !request.privateEntryBody &&
    EsmSplit.safeModulePath m.name && request.modules.all (fun row => EsmSplit.safeModulePath row.1) &&
    request.modules.any (fun row => row.1 == request.entry) && decide ((request.modules.map Prod.fst).Nodup)

/-- The selected labels are derived from the original source and authored
selectors. Their order may differ from the planner's order, but no selected ID
may be absent, duplicated or assigned to another module. -/
structure CheckedPlacement (source : Program) (request : EsmSplit.Spec)
    (compact : EsmSplit.Spec → List UnitPlacement) where
  module : Module
  singleton : source.modules = [module]
  entry : source.entry = module.name
  noImports : module.imports = []
  supported : ResolvedPlacement.supported module request = true
  normalized : EsmSplit.Spec
  selected : List UnitPlacement
  selection : selectRequests module request = .ok (normalized, selected)
  sourceUnique : (module.body.map Prod.fst).Nodup
  selectedUnique : (selected.map Prod.fst).Nodup
  coverage : (selected.map Prod.fst).Perm (module.body.map Prod.fst)
  assignment : List UnitPlacement
  labels : assignment.Perm selected
  denseCompatible : module.body.map Prod.fst = List.range module.body.length →
    assignment = compact request

private def selectionWitness (m : Module) (request : EsmSplit.Spec) :
    Except String (Σ normalized : EsmSplit.Spec, Σ selected : List UnitPlacement,
      PLift (selectRequests m request = .ok (normalized, selected))) := do
  match h : selectRequests m request with
  | .ok (normalized, selected) => return ⟨normalized, selected, ⟨h⟩⟩
  | .error why => throw why

private def sourceWitness (source : Program) : Except String
    (Σ m : Module, PLift (source.modules = [m])) := do
  match h : source.modules with
  | [m] => return ⟨m, ⟨h⟩⟩
  | _ => throw "placement requires one source module"

/-- The supplied compact planner is untrusted: its labels are checked against the
resolved selectors. No output behavior is consulted here. Every accepted assignment comes
from unambiguous selectors and covers the original units exactly once. -/
def resolve (source : Program) (request : EsmSplit.Spec) (compact : EsmSplit.Spec → List UnitPlacement) :
    Except String (CheckedPlacement source request compact) := do
  let ⟨m, ⟨single⟩⟩ ← sourceWitness source
  if sourceEntry : source.entry = m.name then
    if noImports : m.imports = [] then
      unless request.order.isEmpty && !request.sequential && !request.privateEntryBody do
        throw "placement requires an unordered non-sequential spec"
      EsmSplit.validateModulePaths [m.name]
      EsmSplit.validateModulePaths (request.modules.map Prod.fst)
      unless request.modules.any (fun row => row.1 == request.entry) do throw "entry module is missing"
      unless decide ((request.modules.map Prod.fst).Nodup) do throw "duplicate module name"
      if uniqueSource : (m.body.map Prod.fst).Nodup then
        let ⟨normalized, selected, ⟨selection⟩⟩ ← selectionWitness m request
        if uniqueSelected : (selected.map Prod.fst).Nodup then
          if coverage : (selected.map Prod.fst).Perm (m.body.map Prod.fst) then
            let assignment ← (compact normalized).mapM fun (position, owner) => do
              let some (id, _) := m.body[position]? | throw s!"planner position {position} is out of range"
              return (id, owner)
            if labels : assignment.Perm selected then
              if compatible : m.body.map Prod.fst = List.range m.body.length →
                  assignment = compact request then
                if valid : supported m request = true then
                  return {
                    module := m
                    singleton := single
                    entry := sourceEntry
                    noImports := noImports
                    supported := valid
                    normalized := normalized
                    selected := selected
                    selection := selection
                    sourceUnique := uniqueSource
                    selectedUnique := uniqueSelected
                    coverage := coverage
                    assignment := assignment
                    labels := labels
                    denseCompatible := compatible
                  }
                else throw "placement spec is not supported"
              else throw "dense placement differs from the existing planner"
            else throw "planner placement differs from the selected original units"
          else throw "selectors do not cover every original unit"
        else throw "an original unit is selected more than once"
      else throw "source unit identities are duplicated"
    else throw "placement requires an import-free source"
  else throw "source entry does not name its module"

/-- The accepted labels are exactly the labels resolved from the selectors;
they are unique and cover the original unit IDs. -/
theorem resolve_sound {source request compact} (result : CheckedPlacement source request compact)
    (_accepted : resolve source request compact = .ok result) :
    result.assignment.Perm result.selected ∧ (result.selected.map Prod.fst).Nodup ∧
      (result.selected.map Prod.fst).Perm (result.module.body.map Prod.fst) ∧
      selectRequests result.module request = .ok (result.normalized, result.selected) ∧
      supported result.module request = true :=
  ⟨result.labels, result.selectedUnique, result.coverage, result.selection, result.supported⟩

/-- With consecutive source IDs, every accepted assignment is exactly the
supplied compact placement, including owner labels and order. -/
theorem dense_compatible {source request compact} (resolved : CheckedPlacement source request compact)
    (dense : resolved.module.body.map Prod.fst = List.range resolved.module.body.length) :
    resolved.assignment = compact request :=
  resolved.denseCompatible dense

/-- The strict obligations use this specific resolved assignment; they do not
existentially choose a more convenient placement. -/
def Sound {source request compact} (resolved : CheckedPlacement source request compact)
    (guarantee : Guarantee) (output : Program) : Prop :=
  Places resolved.assignment source output ∧ SameExports source output ∧
    ScheduleEquivalent guarantee (fun _ => True) source output

/-- Dense consumers recover the same strict placement, live export, and behavior
requirements with their supplied compact planner. -/
theorem dense_sound {source request compact guarantee output} (resolved : CheckedPlacement source request compact)
    (dense : resolved.module.body.map Prod.fst = List.range resolved.module.body.length)
    (sound : Sound resolved guarantee output) :
    Places (compact request) source output ∧ SameExports source output ∧
      ScheduleEquivalent guarantee (fun _ => True) source output := by
  unfold Sound at sound
  rw [← dense_compatible resolved dense]
  exact sound

#print axioms resolve
#print axioms resolve_sound
#print axioms dense_compatible
#print axioms dense_sound

end CoreJS.ResolvedPlacement
