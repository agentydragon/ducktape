import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.IndexedExportedFunction
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.OrderedArrowInit
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.StaticProviderConstruction
import EsmSplit.LowerSplit.Input
import EsmSplit.ModulePath

/-! Closed static named-import graphs with one authored split and independently
decoded fixed modules. Check the whole linked schedule. The two-file hoist
cycle additionally checks evaluation from both original module routes. -/

namespace CoreJS.FiniteStaticGraph

open LinkedBehavior StaticProviderConstruction

private def numberFixed (offset : Nat) : List LinkedInput.DecodedSource → List Module
  | [] => []
  | decoded :: rest =>
    { decoded.source with body := decoded.source.body.map fun (id, stmt) => (offset + id, stmt) } ::
      numberFixed (offset + decoded.nodes.size) rest

private def checkSource (decoded : LinkedInput.DecodedSource) : Except String Unit := do
  unless
    !decoded.hasTemplates && completeSource decoded &&
      decoded.nodes.all ordinarySynchronousSyntax do
    throw
        s!"{decoded.source.name}: expected only primitive synchronous module syntax with every row accounted for"
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

structure RootBehavior (before program : Program) : Prop where
  sameInputCells :
    ∀ w,
      ∃ a : CheckedLink before w,
        ∃ b : CheckedLink program w,
          ∃ cells,
            inputCells before.modules a.linked = some cells ∧
              inputCells before.modules b.linked = some cells
  sameExports : SameExports before program
  allOutcomes : ∀ w fuel, runProgram w fuel before = runProgram w fuel program
  linked :
    ∀ w, ∃ a : CheckedLink before w, ∃ b : CheckedLink program w,
      a.ready = b.ready ∧ SameUnits (unitsOf a.linked) (unitsOf b.linked)

/-- Calls through an original public cell start with the same value and
complete state. The observer may construct arbitrary modeled arguments,
methods or receivers, and may make further calls after the first one. -/
theorem RootBehavior.observeExport_eq {before program : Program}
    (proof : RootBehavior before program) (w : World) (fuel : Nat) (name : Name) (cell : Addr)
    (observe : Val → M w α) :
    OrderedArrowInit.observeExport before fuel name cell observe =
      OrderedArrowInit.observeExport program fuel name cell observe :=
  OrderedArrowInit.Behavior.observeExport_eq
    ⟨proof.linked, proof.sameExports, proof.allOutcomes⟩ w fuel name cell observe

private def checkRoot (before program : Program) : Except String (PLift (RootBehavior before program)) := do
  let pair ← checkProbePair before program
  let ⟨cells, ⟨beforeCells⟩⟩ ←
    (show Except String (Σ cells, PLift (inputCells before.modules pair.before.linked = some cells))
        from
        match inputCells before.modules pair.before.linked with
        | some cells => .ok ⟨cells, ⟨rfl⟩⟩
        | none => .error "original input edge did not resolve")
  let .isTrue afterCells :=
    (inferInstance : Decidable (inputCells before.modules pair.after.linked = some cells)) |
      throw "original inputs resolve to different cells"
  let some ⟨ready⟩ :=
    StateMatch.probe pair.before.ready pair.after.ready | throw "different state after linking"
  let some ⟨units⟩ :=
    sameUnits? (unitsOf pair.before.linked)
      (unitsOf pair.after.linked) | throw "different ordered linked units or bindings"
  let .isTrue exports :=
    (inferInstance : Decidable (exportsMatchFrom pair.before pair.after = true)) |
      throw "different public export bindings"
  return ⟨⟨fun w => ⟨pair.before.forWorld w, pair.after.forWorld w, cells, beforeCells, afterCells⟩,
    pair.sameExports exports, fun w fuel => by
      rw [run_after_link fuel (pair.before.forWorld w), run_after_link fuel (pair.after.forWorld w)]
      change
        runUnits fuel (unitsOf pair.before.linked) (linkState w pair.before.ready) =
          runUnits fuel (unitsOf pair.after.linked) (linkState w pair.after.ready)
      rw [ready, runUnits_same fuel units],
    fun w => ⟨pair.before.forWorld w, pair.after.forWorld w, congrArg (linkState w) ready, units⟩⟩⟩

def RootCertificate (before program : Program) :=
  { root : String // RootBehavior { before with entry := root } { program with entry := root } }

structure Construction where
  before : Program
  plan : EsmSplit.Plan
  program : Program
  assignment : List UnitPlacement
  places : Places assignment before program
  primary : RootBehavior before program
  otherRoots : List (RootCertificate before program) := []

/-- The additional format-checked family moves a hoisted constructor together
with the single prototype write that uses it. Fixed providers are executable
CoreJS, not opaque retained files. -/
private def prototypeWrite (ctor : Name) (imports : List Name) : Stmt → Bool
  | .expr (.assign (.member (.var receiver) (.str "prototype")) (.obj fields)) =>
    receiver == ctor && !fields.isEmpty &&
      fields.all
        (fun
          | .init (.str name) (.var imported) => name != "__proto__" && imports.contains imported
          | .init (.str name) (.func method) =>
            name != "__proto__" &&
              IndexedExportedFunction.concreteRetainedStatement (.funDecl method) (switchBody :=
                true)
          | _ => false)
  | _ => false

private def concreteHoist (f : Func) : Bool :=
  IndexedExportedFunction.concreteRetainedStatement (.funDecl f) (switchBody := true)

private def checkPrototype (source : LinkedInput.DecodedSource) (fixed : List Module)
    (request : EsmSplit.Spec) (B : EsmSplit.Bundle) (helper : String) : Except String Unit := do
  let original := source.source
  let [⟨left, [(exportedLeft, localLeft)]⟩, ⟨right, [(exportedRight, localRight)]⟩] :=
    original.imports | throw "expected two single-binding imports from the fixed providers"
  unless left != right && (exportedLeft == "default") != (exportedRight == "default") do
    throw "expected a default and a named import from different fixed providers"
  let defaultProvider := if exportedLeft == "default" then left else right
  let namedProvider := if exportedLeft == "default" then right else left
  let namedExport := if exportedLeft == "default" then exportedRight else exportedLeft
  let some leaf := fixed.find? (·.name == defaultProvider) | throw "missing default provider"
  let some provider := fixed.find? (·.name == namedProvider) | throw "missing named provider"
  unless fixed.length == 2 && fixed.all (fun m => m.imports.isEmpty) do
    throw "expected exactly two closed fixed providers"
  let [(_, .funDecl (.mk name .normal _ [] []))] :=
    leaf.body | throw "expected an empty hoisted function in the default provider"
  unless leaf.exports == [("default", name)] do
    throw "expected the default provider's own function"
  let [(_, .funDecl supplied), (_, .funDecl ctor), (_, write), (_, .funDecl factory)] :=
    provider.body | throw "expected two declarations, a prototype write and a default factory in the named provider"
  unless
    provider.exports ==
        [(supplied.name, supplied.name), (ctor.name, ctor.name), ("default", factory.name)] &&
      supplied.name == namedExport &&
      [supplied, ctor, factory].all concreteHoist &&
      prototypeWrite ctor.name [] write do
    throw "named provider's function and prototype must lower concretely"
  let [(ctorId, .funDecl ctor), (writeId, write), (factoryId, .funDecl factory)] :=
    original.body | throw "expected a hoisted constructor, a prototype write and the default factory"
  unless
    original.exports == [("default", factory.name)] && [ctor, factory].all concreteHoist &&
      prototypeWrite ctor.name [localLeft, localRight] write do
    throw "expected a private constructor with a concrete prototype and a public default factory"
  unless
    request.claimants B ctorId == [helper] && request.claimants B writeId == [helper] &&
      request.claimants B factoryId == [request.entry] do
    throw
        "the private constructor and prototype must move together; moving the write alone exposes the constructor"

inductive Family where
  | primitive
  | nodePrototype
  | nodeHoistCycle
  deriving BEq

/-- The moved functions are a concrete, closed two-declaration group. The
mapper keeps its original default, call receiver and lexical formatter. -/
private def checkCycleHoists (modules : List Module) (original : Module) (helper : String)
    (plan : EsmSplit.Plan) : Except String (List Name) := do
  let [core] := modules | throw "expected one fixed module in the cycle"
  let [intoCore] := original.imports | throw "expected one original utility import"
  let [intoUtil] := core.imports | throw "expected one original core import"
  unless intoCore.source == core.name && intoUtil.source == original.name &&
      !intoCore.names.isEmpty && !intoUtil.names.isEmpty do
    throw "expected two original routes with named imports of one another"
  let some owner := plan.find helper | throw "missing helper"
  let [(mapperId, .funDecl (mapper@(.withDefaults name .normal false [first, second] 1
      [.decl .let_ input (.var slot),
        .decl .let_ separator (.cond (.binary .strictEq (.var check) .undef) (.str _) (.var actual)),
        .block [.ret (.callMethod
          (.callMethod (.var receiver) (.str _)
            [.func (.mk _ .arrow false [item] [.ret (.call (.var convert) [.var value])])])
          (.str _) [.var join])]]))),
      (formatId, .funDecl formatter)] := original.body.filter (fun row => owner.stmts.contains row.1) |
      throw "expected two defaulted utility hoists"
  unless owner.stmts == [mapperId, formatId] && first == slot && second == check &&
      second == actual && input == receiver && separator == join && input != separator &&
      item == value && item != convert && ![input, separator].contains convert &&
      name != convert && convert == formatter.name && mapper.free == [convert] &&
      formatter.free.isEmpty && concreteHoist formatter do
    throw "expected the original defaulted mapper and its closed formatter"
  return [name, convert]

def construct (source : LinkedInput.DecodedSource) (fixedSources : List LinkedInput.DecodedSource)
    (request : EsmSplit.Spec) (family : Family := .primitive) : Except String Construction := do
  if family == .nodeHoistCycle then
    unless fixedSources.length == 1 do throw "expected one original cyclic provider"
  else
    unless fixedSources.length >= 2 do throw "expected at least two fixed source modules"
  let [helper] := request.order | throw "expected exactly one authored helper"
  unless
    source.source.name == request.entry && request.sequential && !request.privateEntryBody &&
      helper != request.entry &&
      request.modules.all (fun row => [request.entry, helper].contains row.1) do
    throw "expected a sequential authored split with a public entry"
  let fixed := numberFixed source.nodes.size fixedSources
  let fixedNames := fixed.map (·.name)
  let paths := request.entry :: helper :: fixedNames
  EsmSplit.validateModulePaths paths
  unless paths.Nodup do
    throw "expected distinct module routes"
  match family with
  | .primitive =>
    unless paths.all (·.endsWith ".mjs") do
      throw "expected .mjs module routes"
    for decoded in source :: fixedSources do
      checkSource decoded
  | .nodePrototype | .nodeHoistCycle =>
    unless helper.endsWith ".mjs" && (request.entry :: fixedNames).all (·.endsWith ".js") do
      throw "expected original .js files and one .mjs helper"
    for decoded in source :: fixedSources do
      unless !decoded.hasTemplates && completeSource decoded do
        throw s!"{decoded.source.name}: expected every original row and no template sites"
  let before : Program := { entry := source.source.name, modules := source.source :: fixed }
  if family != .nodeHoistCycle then checkOrder before
  let B := EsmSplit.LowerSplit.sourceBundle source.nodes
  if family == .nodePrototype then
    checkPrototype source fixed request B helper
  unless
    (List.range source.nodes.size).all
      (fun id =>
        (request.modules.flatMap fun row =>
              row.2.filter fun selector => selector.claims B id).length ==
          1) do
    throw "each original entry ordinal must be selected exactly once"
  let basePlan := EsmSplit.mkPlan B request
  unless decide (EsmSplit.Valid B request basePlan) do
    throw "authored source placement is invalid"
  let forwarded ←
    if family == .nodeHoistCycle then
      checkCycleHoists fixed source.source helper basePlan
    else pure []
  let plan :=
    if forwarded.isEmpty then basePlan else
      { basePlan with mods := basePlan.mods.map fun m =>
        if m.name == request.entry then
          { m with imports := [(helper, forwarded)], exports := forwarded.map fun n => (n, n) }
        else m }
  let picks := PlanPlacement.assignments plan
  unless decide ((picks.map Prod.fst).Perm (List.range source.nodes.size)) do
    throw "authored placement does not cover the entry ordinals"
  unless
    (metadataOrdinals source.nodes).all
      (fun id =>
        picks.filter (fun p => p.1 == id) == [(id, plan.entry)] ||
          (!forwarded.isEmpty && picks.filter (fun p => p.1 == id) == [(id, helper)] &&
            ((source.source.body.lookup id).any fun
              | .funDecl f => forwarded.contains f.name
              | _ => false))) do
    throw "original import and export metadata must stay in the entry"
  let ids := source.source.body.map Prod.fst
  let moved := picks.filter (fun p => ids.contains p.1)
  unless moved.any (fun p => p.2 == helper) do
    throw "expected an executable unit in the authored helper"
  if !forwarded.isEmpty then
    unless forwarded.all (fun name => source.source.exports.contains (name, name)) &&
        (plan.find helper).any (fun m => m.imports.isEmpty && m.exports.isEmpty) do
      throw "expected two original inline exports in an independent helper"
    for (id, stmt) in source.source.body ++ fixed.flatMap (·.body) do
      unless (moved.any (fun row => row.1 == id && row.2 == helper)) ||
          IndexedExportedFunction.concreteRetainedStatement stmt do
        throw s!"unit {id}: retained cycle code must lower concretely"
  let modules := plan.mods.map fun m =>
    let out := outputModule source.source plan m
    if forwarded.isEmpty then out else
      let movedExports := forwarded.map fun name => (name, name)
      { out with
        exports := if m.name == helper then movedExports
          else source.source.exports.filter (fun e => !movedExports.contains e) ++ movedExports }
  let program : Program := { entry := plan.entry, modules := modules ++ fixed }
  if family != .nodeHoistCycle then
    checkOrder program
    unless program.evalOrder == before.evalOrder.dropLast ++ request.order ++ [request.entry] do
      throw "output does not keep the fixed dependency order followed by the authored helper and entry"
  else
    unless program.evalOrder == fixedNames ++ [helper, request.entry] do
      throw "utility-rooted graph does not follow the authored helper order"
  if family == .primitive && !(ordinaryModuleGraph before && ordinaryModuleGraph program) then
    throw "expected reads only from named imports and earlier const declarations"
  let assignment := moved ++ fixed.flatMap Module.unitPlacements
  let ⟨places⟩ ← checkPlaces assignment before program
  let ⟨primary⟩ ← checkRoot before program
  let otherRoots ←
    if family == .nodeHoistCycle then
      fixedNames.mapM fun root => do
        let ⟨behavior⟩ ← checkRoot { before with entry := root } { program with entry := root }
        pure (⟨root, behavior⟩ : RootCertificate before program)
    else pure []
  return { before, plan, program, assignment, places, primary, otherRoots }

/-- The printer emits original imports itself, including duplicates for a
moved unit. Only imports between generated modules are emitted as glue. -/
def Construction.printerPlan (built : Construction) : EsmSplit.Plan :=
  let consumers := built.plan.mods.map (·.name)
  { built.plan with
    mods :=
      built.plan.mods.map fun m =>
        { m with imports := m.imports.filter (fun edge => consumers.contains edge.1) } }

theorem Construction.readbackGuarantees (built : Construction) {observed : Program}
    (same : observed = built.program) :
    Places built.assignment built.before observed ∧
      SameExports built.before observed ∧
      (∀ w,
        ∃ a : CheckedLink built.before w,
          ∃ b : CheckedLink observed w,
            ∃ cells,
              inputCells built.before.modules a.linked = some cells ∧
                inputCells built.before.modules b.linked = some cells) ∧
      ∀ w fuel, runProgram w fuel built.before = runProgram w fuel observed :=
  same ▸ ⟨built.places, built.primary.sameExports, built.primary.sameInputCells, built.primary.allOutcomes⟩

theorem Construction.rootReadback (built : Construction) {observed : Program}
    (same : observed = built.program) (root : RootCertificate built.before built.program) :
    RootBehavior { built.before with entry := root.val } { observed with entry := root.val } :=
  same ▸ root.property

end CoreJS.FiniteStaticGraph
