import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.ResolvedPlacement
import EsmSplit.CoreJS.PrimitiveRhs

/-! A construction for actual source modules containing unique `const`
bindings and initialized lexical `let` bindings. Initializers are recursively
formed from primitive values, reads of earlier bindings, and source unary and
binary expressions. The supplied sequential placement is used unchanged by the
existing planner. No assignment statements are admitted, so the proof does not
cover later mutations or reordered evaluation. -/

namespace CoreJS.NamedDeclarationSplit

open LinkedBehavior

/-- The accumulator makes freshness and the earlier-read requirement explicit
for every declaration, for lists of any length. -/
inductive Declarations : List Name → List (Nat × Stmt) → Prop where
  | nil (earlier) : Declarations earlier []
  | const {earlier id name value rest} :
      name ∉ earlier → PrimitiveRhs.Rhs earlier.contains value →
      Declarations (name :: earlier) rest →
      Declarations earlier ((id, .decl .const name value) :: rest)
  | initializedLet {earlier id name value rest} :
      name ∉ earlier → value ≠ .undef → PrimitiveRhs.Rhs earlier.contains value →
      Declarations (name :: earlier) rest →
      Declarations earlier ((id, .decl .let_ name value) :: rest)

private def initializedValue : Expr → Bool
  | .undef => false
  | _ => true

private theorem initializedValue_ne_undef {value : Expr} (h : initializedValue value = true) :
    value ≠ .undef := by
  intro equal
  subst value
  simp [initializedValue] at h

/-- Decide admission without evaluating an initializer or running Lean on a
particular input. A duplicate, forward read or unsupported statement refuses. -/
def checkDeclarations : (earlier : List Name) → (units : List (Nat × Stmt)) →
    Option (PLift (Declarations earlier units))
  | earlier, [] => some ⟨.nil earlier⟩
  | earlier, (_, .decl .const name value) :: rest => do
    if fresh : name ∉ earlier then
      let ⟨init⟩ ← PrimitiveRhs.check earlier.contains value
      let ⟨tail⟩ ← checkDeclarations (name :: earlier) rest
      return ⟨.const fresh init tail⟩
    else none
  | earlier, (_, .decl .let_ name value) :: rest => do
    if initialized : initializedValue value = true then
      if fresh : name ∉ earlier then
        let ⟨init⟩ ← PrimitiveRhs.check earlier.contains value
        let ⟨tail⟩ ← checkDeclarations (name :: earlier) rest
        return ⟨.initializedLet fresh (initializedValue_ne_undef initialized) init tail⟩
      else none
    else none
  | _, _ => none

/-- The admitted declarations belong to the actual original module. -/
def Admitted (source : Program) : Prop :=
  ∃ module, source.modules = [module] ∧ Declarations [] module.body

/-- The exact output returned by the constructor carries all modeled outcomes,
original live export cells, authored placement and sequential module order. -/
structure Construction (source : Program) (request : EsmSplit.Spec) where
  split : CheckedSplit.Construction source request .allOutcomes
  admitted : Admitted source
  ordered : split.program.evalOrder = request.order ++ [request.entry]
  originalModule : Module
  singleton : source.modules = [originalModule]
  normalized : EsmSplit.Spec
  selected : List UnitPlacement
  selection : ResolvedPlacement.selectRequests originalModule request = .ok (normalized, selected)
  selectedUnique : (selected.map Prod.fst).Nodup
  coverage : (selected.map Prod.fst).Perm (originalModule.body.map Prod.fst)
  placement : selected.Perm (CheckedSplit.requestedPlacement source request)

private def selectionWitness (module : Module) (request : EsmSplit.Spec) : Except String
    (Σ normalized : EsmSplit.Spec, Σ selected : List UnitPlacement,
      PLift (ResolvedPlacement.selectRequests module request = .ok (normalized, selected))) := do
  match h : ResolvedPlacement.selectRequests module request with
  | .ok (normalized, selected) => return ⟨normalized, selected, ⟨h⟩⟩
  | .error why => throw why

/-- Construct the ordinary planner's output, or refuse. No finished candidate
or sampled execution is used to justify the all-world equality. -/
def construct (source : Program) (request : EsmSplit.Spec) : Except String (Construction source request) := do
  unless request.sequential do throw "named declarations require an explicit sequential placement"
  match single : source.modules with
  | [module] =>
    let some ⟨admitted⟩ := checkDeclarations [] module.body |
      throw "expected unique const or initialized let declarations with primitive initializers or source unary/binary expressions over earlier reads"
    let ⟨normalized, selected, ⟨selection⟩⟩ ← selectionWitness module request
    let .isTrue unique := (inferInstance : Decidable ((selected.map Prod.fst).Nodup)) |
      throw "an original unit is selected more than once"
    let .isTrue coverage := (inferInstance : Decidable ((selected.map Prod.fst).Perm (module.body.map Prod.fst))) |
      throw "selectors do not cover every original unit"
    let .isTrue placement := (inferInstance : Decidable (selected.Perm (CheckedSplit.requestedPlacement source request))) |
      throw "resolved original selectors differ from the constructed placement"
    let split ← CheckedSplit.checkedSplit source request .allOutcomes
    if ordered : split.program.evalOrder = request.order ++ [request.entry] then
      return ⟨split, ⟨module, single, admitted⟩, ordered, module, single, normalized,
        selected, selection, unique, coverage, placement⟩
    else throw "constructed module order differs from the authored order"
  | _ => throw "named declarations require one original module"

/-- Successful construction meets the complete model contract for the actual
program and actual requested placement, at every fuel in every world. -/
theorem construct_sound {source : Program} {request : EsmSplit.Spec} (result : Construction source request)
    (_accepted : construct source request = .ok result) :
    Admitted source ∧
    FootprintsOfSyntax (CheckedSplit.sourceStatements source) result.split.bundle ∧
    result.split.plan = EsmSplit.mkPlan result.split.bundle request ∧
    result.split.program = splitProgram (CheckedSplit.sourceStatements source) result.split.bundle result.split.plan ∧
    Places (CheckedSplit.requestedPlacement source request) source result.split.program ∧
    SameExports source result.split.program ∧
    (∀ w fuel, runProgram w fuel source = runProgram w fuel result.split.program) ∧
    result.split.program.evalOrder = request.order ++ [request.entry] ∧
    source.modules = [result.originalModule] ∧
    ResolvedPlacement.selectRequests result.originalModule request = .ok (result.normalized, result.selected) ∧
    (result.selected.map Prod.fst).Nodup ∧
    (result.selected.map Prod.fst).Perm (result.originalModule.body.map Prod.fst) ∧
    result.selected.Perm (CheckedSplit.requestedPlacement source request) ∧
    Places result.selected source result.split.program := by
  refine ⟨result.admitted, result.split.footprints, result.split.planned, result.split.constructed,
    result.split.sound.1, result.split.sound.2.1, ?_, result.ordered,
    result.singleton, result.selection, result.selectedUnique, result.coverage, result.placement, ?_⟩
  · intro w fuel
    exact result.split.sound.2.2 w True.intro fuel
  · exact ⟨result.selectedUnique,
      result.split.sound.1.2.1.trans (result.placement.map Prod.fst).symm,
      result.split.sound.1.2.2.trans result.placement.symm⟩

#print axioms checkDeclarations
#print axioms construct
#print axioms construct_sound

end CoreJS.NamedDeclarationSplit
