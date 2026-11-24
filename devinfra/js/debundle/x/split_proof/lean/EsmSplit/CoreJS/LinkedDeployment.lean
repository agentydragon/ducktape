import EsmSplit.CoreJS.DynamicImports
import EsmSplit.CoreJS.Eval

/-! Resolve what a checked dynamic-import target can name on the linked side
of the module boundary. The resulting synthetic scope entries are capabilities
for opaque execution. They are never JavaScript source bindings. -/

namespace CoreJS.LinkedDeployment

open Lean (Json)

structure Input where
  entry : String
  assets : Json
  before : Std.HashMap String ModuleLocation.Resource

structure Access where
  unitNames : Std.HashMap Nat (List Name) := {}
  imports : List Import := []

/-- Read the external graph for each location-sensitive unit. An overlay at the
same effective URLs replaces only the plan's paths. The paired input check
independently verifies the syntax and linkage at each of those paths. -/
def check (inputs : Input) (source providers : Json) (plan : EsmSplit.Plan) : Except String Access := do
  let resources (name : String) := inputs.before[name]?
  let sites ← ModuleLocation.check source inputs.entry plan resources resources
  let static := (← providers.getObj?).toList.map Prod.fst
  let linked := plan.entry :: static
  let mut requirements : Std.HashMap Nat (List (String × Name)) := {}
  for (id, use) in sites do
    let bindings ← match use with
      | .metaUrl => pure []
      | .dynamicImport _ target => do
        let boundary ← DynamicImports.check target linked inputs.assets resources resources
        pure <| boundary.requests.flatMap fun edge => edge.exports.map (edge.source, ·)
    requirements := requirements.insert id ((requirements[id]?.getD [] ++ bindings).eraseDups)
  let mut access : Access := {}
  for (id, requests) in requirements.toArray.qsort (fun a b => a.1 < b.1) do
    let mut names := []
    for ((source, exported), index) in requests.zipIdx do
      let localName := s!":module-loader:{id}:{index}"
      names := localName :: names
      access := { access with imports := ⟨source, [(exported, localName)]⟩ :: access.imports }
    access := { access with unitNames := access.unitNames.insert id names.reverse }
  return { access with imports := access.imports.reverse }

/-- Add only capability aliases to the entry scope. The targets are part of
the already-loaded module graph, including the entry for genuine re-entry. -/
def Access.attach (access : Access) (program : Program) : Except String Program := do
  let result := { program with modules := program.modules.map fun module =>
    if module.name == program.entry then { module with imports := module.imports ++ access.imports }
    else module }
  unless result.evalOrder == program.evalOrder do throw "loader capabilities changed the static module order"
  return result

end CoreJS.LinkedDeployment
