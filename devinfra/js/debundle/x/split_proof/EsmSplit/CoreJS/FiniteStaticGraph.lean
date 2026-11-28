import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.StaticProviderConstruction
import EsmSplit.LowerSplit.Input
import EsmSplit.ModulePath

/-! A closed, named-import DAG with one authored split and independently
decoded fixed modules. Match the entire linked schedule, including the
fixed modules, before claiming any equality of evaluator results. -/

namespace CoreJS.FiniteStaticGraph

open LinkedBehavior StaticProviderConstruction

private def numberFixed (offset : Nat) : List LinkedInput.DecodedSource → List Module
  | [] => []
  | decoded :: rest =>
    { decoded.source with body := decoded.source.body.map fun (id, stmt) => (offset + id, stmt) } ::
      numberFixed (offset + decoded.nodes.size) rest

private def checkSource (decoded : LinkedInput.DecodedSource) : Except String Unit := do
  unless !decoded.hasTemplates && completeSource decoded &&
      decoded.nodes.all ordinarySynchronousSyntax do
    throw s!"{decoded.source.name}: expected only primitive synchronous module syntax with every row accounted for"
  unless decoded.source.imports.all (fun edge => !edge.names.isEmpty) do
    throw s!"{decoded.source.name}: expected named imports"

/-- Checking all edges in DFS postorder excludes both missing routes and
cycles. The permutation check also excludes undeclared and unreachable files. -/
private def checkOrder (program : Program) : Except String Unit := do
  let order := program.evalOrder
  unless order.Perm (program.modules.map (·.name)) do
    throw "every declared module must be reached from the entry and every import must be declared"
  for module in program.modules do
    let earlier := order.takeWhile (· != module.name)
    for edge in module.imports do
      unless earlier.contains edge.source do
        throw s!"{module.name}: import {edge.source} is missing or creates a static cycle"

/-- Project even unused named imports. A same-valued binding from a different
source cell does not count as the same import. -/
private def inputCells (inputs : List Module) (linked : List (Module × Env)) :
    Option (List (List Addr)) :=
  inputs.mapM fun m => do
    let (_, env) ← linked.find? (fun (linkedModule, _) => linkedModule.name == m.name)
    (m.imports.flatMap (·.names)).mapM fun (_, localName) => env.vars.lookup localName

structure Construction where
  before : Program
  plan : EsmSplit.Plan
  program : Program
  assignment : List UnitPlacement
  places : Places assignment before program
  sameInputCells : ∀ w, ∃ a : CheckedLink before w, ∃ b : CheckedLink program w, ∃ cells,
    inputCells before.modules a.linked = some cells ∧ inputCells before.modules b.linked = some cells
  sameExports : SameExports before program
  allOutcomes : ∀ w fuel, runProgram w fuel before = runProgram w fuel program

def construct (source : LinkedInput.DecodedSource) (fixedSources : List LinkedInput.DecodedSource)
    (request : EsmSplit.Spec) : Except String Construction := do
  unless fixedSources.length >= 2 do throw "expected at least two fixed source modules"
  let [helper] := request.order | throw "expected exactly one authored helper"
  unless source.source.name == request.entry && request.sequential && !request.privateEntryBody &&
      helper != request.entry && request.modules.all (fun row => [request.entry, helper].contains row.1) do
    throw "expected a sequential authored split with a public entry"
  let fixed := numberFixed source.nodes.size fixedSources
  let fixedNames := fixed.map (·.name)
  let paths := request.entry :: helper :: fixedNames
  EsmSplit.validateModulePaths paths
  unless paths.Nodup && paths.all (·.endsWith ".mjs") do throw "expected distinct .mjs module routes"
  for decoded in source :: fixedSources do checkSource decoded
  let before : Program := { entry := source.source.name, modules := source.source :: fixed }
  checkOrder before
  let B := EsmSplit.LowerSplit.sourceBundle source.nodes
  unless (List.range source.nodes.size).all (fun id =>
      (request.modules.flatMap fun row => row.2.filter fun selector => selector.claims B id).length == 1) do
    throw "each original entry ordinal must be selected exactly once"
  let plan := EsmSplit.mkPlan B request
  unless decide (EsmSplit.Valid B request plan) do throw "authored source placement is invalid"
  let picks := PlanPlacement.assignments plan
  unless decide ((picks.map Prod.fst).Perm (List.range source.nodes.size)) do
    throw "authored placement does not cover the entry ordinals"
  unless (metadataOrdinals source.nodes).all (fun id =>
      picks.filter (fun p => p.1 == id) == [(id, plan.entry)]) do
    throw "original import and export metadata must stay in the entry"
  let ids := source.source.body.map Prod.fst
  let moved := picks.filter (fun p => ids.contains p.1)
  unless moved.any (fun p => p.2 == helper) do throw "expected an executable unit in the authored helper"
  let program : Program := { entry := plan.entry, modules := plan.mods.map (outputModule source.source plan) ++ fixed }
  checkOrder program
  unless program.evalOrder == before.evalOrder.dropLast ++ request.order ++ [request.entry] do
    throw "output does not keep the fixed dependency order followed by the authored helper and entry"
  unless ordinaryModuleGraph before && ordinaryModuleGraph program do
    throw "expected reads only from named imports and earlier const declarations"
  let assignment := moved ++ fixed.flatMap Module.unitPlacements
  let ⟨places⟩ ← checkPlaces assignment before program
  let pair ← checkProbePair before program
  let ⟨cells, ⟨beforeCells⟩⟩ ← (show Except String (Σ cells,
      PLift (inputCells before.modules pair.before.linked = some cells)) from
    match inputCells before.modules pair.before.linked with
    | some cells => .ok ⟨cells, ⟨rfl⟩⟩
    | none => .error "original input edge did not resolve")
  let .isTrue afterCells := (inferInstance : Decidable (inputCells before.modules pair.after.linked = some cells)) |
    throw "original inputs resolve to different cells"
  let some ⟨ready⟩ := StateMatch.probe pair.before.ready pair.after.ready |
    throw "different state after linking"
  let some ⟨units⟩ := sameUnits? (unitsOf pair.before.linked) (unitsOf pair.after.linked) |
    throw "different ordered linked units or bindings"
  let .isTrue exports := (inferInstance : Decidable (exportsMatchFrom pair.before pair.after = true)) |
    throw "different public export bindings"
  return {
    before, plan, program, assignment, places
    sameInputCells := fun w => ⟨pair.before.forWorld w, pair.after.forWorld w, cells, beforeCells, afterCells⟩
    sameExports := pair.sameExports exports
    allOutcomes := fun w fuel => by
      rw [run_after_link fuel (pair.before.forWorld w), run_after_link fuel (pair.after.forWorld w)]
      change runUnits fuel (unitsOf pair.before.linked) (linkState w pair.before.ready) =
        runUnits fuel (unitsOf pair.after.linked) (linkState w pair.after.ready)
      rw [ready, runUnits_same fuel units]
  }

/-- The printer emits original imports itself, including duplicates for a
moved unit. Only imports between generated modules are emitted as glue. -/
def Construction.printerPlan (built : Construction) : EsmSplit.Plan :=
  let consumers := built.plan.mods.map (·.name)
  { built.plan with mods := built.plan.mods.map fun m =>
    { m with imports := m.imports.filter (fun edge => consumers.contains edge.1) } }

theorem Construction.readbackGuarantees (built : Construction) {observed : Program}
    (same : observed = built.program) :
    Places built.assignment built.before observed ∧ SameExports built.before observed ∧
    (∀ w, ∃ a : CheckedLink built.before w, ∃ b : CheckedLink observed w, ∃ cells,
      inputCells built.before.modules a.linked = some cells ∧ inputCells built.before.modules b.linked = some cells) ∧
    ∀ w fuel, runProgram w fuel built.before = runProgram w fuel observed :=
  same ▸ ⟨built.places, built.sameExports, built.sameInputCells, built.allOutcomes⟩

end CoreJS.FiniteStaticGraph
