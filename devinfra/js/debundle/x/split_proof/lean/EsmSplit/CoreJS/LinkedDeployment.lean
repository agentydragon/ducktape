import EsmSplit.CoreJS.DynamicImports
import EsmSplit.CoreJS.NativeRecord

/-! Resolve what a checked dynamic-import target can name on the linked side
of the module boundary. The resulting synthetic scope entries are capabilities
for opaque execution. They are never JavaScript source bindings. -/

namespace CoreJS.LinkedDeployment

open Lean (Json)

structure Input where
  entry : String
  nativeRecord : Bool := false
  assets : Json
  before : Std.HashMap String ModuleLocation.Resource

structure Access where
  unitNames : Std.HashMap Nat (List Name) := {}
  imports : Std.HashMap String (List Import) := {}
  dynamicImportUnits : Std.HashSet Nat := {}
  nativeUrl : Option String := none

/-- Read the external graph for each location-sensitive unit. An overlay at the
same effective URLs replaces only the plan's paths. The paired input check
independently verifies the syntax and linkage at each of those paths. -/
def check (inputs : Input) (source providers : Json) (plan : EsmSplit.Plan) : Except String Access := do
  let resources (name : String) := inputs.before[name]?
  let entrySites ← ModuleLocation.check source inputs.entry plan resources resources inputs.nativeRecord
  let providers ← (·.toList) <$> providers.getObj?
  let linked := plan.entry :: providers.map Prod.fst
  let mut sites := entrySites.map fun (id, use) => (plan.entry, id, use)
  let mut offset := (← ESTreeUnits.nodes source).size
  for (name, ast) in providers do
    let some resource := resources name | throw s!"missing original provider resource {name}"
    let uses ← (ModuleLocation.entryUses ast name resource.source).mapError (s!"{name}: " ++ ·)
    sites := sites ++ uses.map (fun (id, use) => (name, offset + id, use))
    offset := offset + (← ESTreeUnits.nodes ast).size
  let mut requirements : Std.HashMap (Nat × String) (List (String × Name)) := {}
  let mut access : Access := {}
  for (importer, id, use) in sites do
    let bindings ← match use with
      | .metaUrl | .metadata => pure []
      | .dynamicImport _ target =>
        if (plan.find target).isSome then throw s!"dynamic target is an output module: {target}"
        let boundary ← DynamicImports.check target linked inputs.assets resources resources
        access := { access with dynamicImportUnits := access.dynamicImportUnits.insert id }
        pure <| boundary.requests.flatMap fun edge => edge.exports.map (edge.source, ·)
    requirements := requirements.insert (id, importer) ((requirements[(id, importer)]?.getD [] ++ bindings).eraseDups)
  let ordered := requirements.toArray.qsort (fun a b => a.1.1 < b.1.1)
  for ((id, importer), requests) in ordered do
    let mut names := []
    let mut imports := access.imports[importer]?.getD []
    for ((source, exported), index) in requests.zipIdx do
      let localName := s!":module-loader:{id}:{index}"
      names := localName :: names
      imports := ⟨source, [(exported, localName)]⟩ :: imports
    access := { access with
      imports := if requests.isEmpty then access.imports else access.imports.insert importer imports
      unitNames := access.unitNames.insert id names.reverse }
  for (_, id, use) in sites do
    if use == .metadata then
      let some old := resources inputs.entry | throw "missing entry resource"
      access := { access with
        nativeUrl := some old.effectiveUrl
        unitNames := access.unitNames.insert id ((NativeRecord.slot :: access.unitNames[id]?.getD []).eraseDups) }
  return { access with imports := Std.HashMap.ofList (access.imports.toList.map fun (name, imports) => (name, imports.reverse)) }

/-- Add capability aliases to the importing module scope. All live cells already
belong to the linked graph, including the entry for genuine re-entry. -/
def Access.attach (access : Access) (program : Program) : Except String Program := do
  if access.imports.isEmpty && access.nativeUrl.isNone then return program
  let result := { program with modules := program.modules.map fun module =>
    { module with imports := module.imports ++ access.imports[module.name]?.getD [] } }
  unless result.evalOrder == program.evalOrder do throw "loader capabilities changed the static module order"
  let some url := access.nativeUrl | return result
  -- Model-only dependency. It owns the cell captured by entries in both graphs;
  -- its only unit runs first, after linking but before any original initializer.
  let name := ":native-browser-record"
  if (result.find name).isSome then throw "metadata module already exists"
  let id := result.regions
  let root := NativeRecord.slot
  let helper : Module := ⟨name, [], [(id, .decl .const root (NativeRecord.initial url id))], [(root, root)]⟩
  let bound := {result with modules := helper :: result.modules.map fun md =>
    if md.name == result.entry then {md with imports := ⟨name, [(root, root)]⟩ :: md.imports} else md}
  unless bound.evalOrder == name :: result.evalOrder do throw "metadata dependency changed module order"
  return bound

end CoreJS.LinkedDeployment
