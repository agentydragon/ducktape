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

structure ModuleAccess where
  unitNames : Std.HashMap Nat (List Name) := {}
  imports : List Import := []
  dynamicImportUnits : Std.HashSet Nat := {}
  nativeUrl : Option String := none

structure Access where
  entryAccess : ModuleAccess := {}
  providers : Std.HashMap String ModuleAccess := {}

/-- Check lazy targets from one referrer, including every static edge back to
a linked module. The extra cells belong to this referrer's scope. -/
private def checkSites (inputs : Input) (plan : EsmSplit.Plan) (name : String) (linked : List String)
    (sites : Array (Nat × ModuleLocation.Use)) : Except String ModuleAccess := do
  let resources (path : String) := inputs.before[path]?
  let mut requirements : Std.HashMap Nat (List (String × Name)) := {}
  let mut access : ModuleAccess := {}
  for (id, use) in sites do
    let bindings ← match use with
      | .metaUrl | .metadata => pure []
      | .dynamicImport _ target => do
        if (plan.find target).isSome then throw s!"dynamic target is an output module: {target}"
        let boundary ← DynamicImports.check target linked inputs.assets resources resources
        access := { access with dynamicImportUnits := access.dynamicImportUnits.insert id }
        pure <| boundary.requests.flatMap fun edge => edge.exports.map (edge.source, ·)
    requirements := requirements.insert id ((requirements[id]?.getD [] ++ bindings).eraseDups)
  for (id, requests) in requirements.toArray.qsort (fun a b => a.1 < b.1) do
    let mut names := []
    for ((source, exported), index) in requests.zipIdx do
      let localName := s!":module-loader:{id}:{index}"
      names := localName :: names
      access := { access with imports := ⟨source, [(exported, localName)]⟩ :: access.imports }
    access := { access with unitNames := access.unitNames.insert id names.reverse }
  for (id, use) in sites do
    if use == .metadata then
      let some old := resources name | throw "missing metadata resource"
      access := { access with
        nativeUrl := some old.effectiveUrl
        unitNames := access.unitNames.insert id ((NativeRecord.slot :: access.unitNames[id]?.getD []).eraseDups) }
  return { access with imports := access.imports.reverse }

def check (inputs : Input) (source providers : Json) (plan : EsmSplit.Plan) : Except String Access := do
  let resources (name : String) := inputs.before[name]?
  let sites ← ModuleLocation.check source inputs.entry plan resources resources inputs.nativeRecord
  let files := (← providers.getObj?).toList
  let linked := plan.entry :: files.map Prod.fst
  let entryAccess ← checkSites inputs plan plan.entry linked sites
  let mut others := ∅
  for (name, file) in files do
    if (plan.find name).isSome then throw s!"provider overlaps emitted module: {name}"
    let some res := resources name | throw s!"missing provider resource: {name}"
    let uses ← (ModuleLocation.providerUses file name res inputs.nativeRecord).mapError fun why => s!"{name}: {why}"
    let access ← (checkSites inputs plan name linked uses).mapError fun why => s!"{name}: {why}"
    others := others.insert name access
  return {entryAccess, providers := others}

/-- Add the capability aliases to their referring modules. Preserve the actual
order even when a lazy dependency re-enters through a provider or the entry. -/
def Access.attach (access : Access) (program : Program) : Except String Program := do
  let records := access.providers.insert program.entry access.entryAccess
  let result := { program with modules := program.modules.map fun md =>
    {md with imports := md.imports ++ (records[md.name]?.map (·.imports)).getD []} }
  unless result.evalOrder == program.evalOrder do throw "loader capabilities changed the static module order"
  let owners := (records.toList.filterMap fun (name, a) => a.nativeUrl.map (name, ·)).mergeSort
    fun a b => decide (a.1 ≤ b.1)
  if owners.isEmpty then return result
  -- One model-only dependency initializes distinct cells for distinct records.
  -- Stable region identities must not depend on the split module order.
  let helperName := ":native-browser-record"
  if (result.find helperName).isSome then throw "metadata module already exists"
  let exported := fun (owner : String) => ":record:" ++ owner
  let body := owners.zipIdx.map fun ((name, url), idx) =>
    let id := result.regions + idx
    (id, Stmt.decl .const (exported name) (NativeRecord.initial url id))
  let helper : Module := ⟨helperName, [], body, owners.map fun (name, _) => (exported name, exported name)⟩
  let bound := {result with modules := helper :: result.modules.map fun md =>
    if ((records[md.name]?).bind (·.nativeUrl)).isSome then
      {md with imports := ⟨helperName, [(exported md.name, NativeRecord.slot)]⟩ :: md.imports}
    else if md.name == program.entry then {md with imports := ⟨helperName, []⟩ :: md.imports}
    else md}
  unless bound.evalOrder == helperName :: result.evalOrder do throw "metadata dependency changed module order"
  return bound

end CoreJS.LinkedDeployment
