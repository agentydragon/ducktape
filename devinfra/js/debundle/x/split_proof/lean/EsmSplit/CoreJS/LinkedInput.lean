import EsmSplit.CoreJS.DeclarationArtifacts
import EsmSplit.ESTreeUnits
import EsmSplit.ModulePath

/-! Lower independently parsed original and split modules, with shared, unchanged
providers. The parser's AST, scope annotations and literals are frontend premises.
Placement gives emitted units their original identities; the actual module syntax
gives both programs their bodies and links. -/

namespace CoreJS.LinkedInput

open Lean (Json)

private def field (j : Json) (key : String) := j.getObjValAs? String key
private def typ (j : Json) := field j "type"

private def noAttributes (j : Json) : Except String Unit := do
  if let .ok attrs := j.getObjVal? "attributes" then
    unless (← attrs.getArr?).isEmpty do throw "unsupported module attributes"

/-- Offsets, literal spellings, and scope annotations can change during emission.
Syntax comparison is followed by checking the linked cells of all free names. -/
private partial def unitSyntax : Json → Json
  | .obj fields => Json.mkObj <| fields.toList.filterMap fun (k, v) =>
    if ["start", "end", "loc", "range", "raw", "k", "b", "d"].contains k then none
    else some (k, unitSyntax v)
  | .arr nodes => .arr (nodes.map unitSyntax)
  | j => j

private structure Parsed where
  imports : List Import
  units : List (Nat × Stmt × Json)
  exports : List (Name × Name)

private def parse (path : String) (nodes : Array Json) : Except String Parsed := do
  let mut imports := #[]
  for j in nodes do
    if (← typ j) == "ImportDeclaration" then
      imports := imports.push (← DeclarationArtifacts.importDecl path j)
  let names := imports.toList.flatMap fun i => i.names.map Prod.snd
  let mut units := #[]
  let mut exports := #[]
  for (j, i) in nodes.zipIdx do
    match ← typ j with
    | "ImportDeclaration" => pure ()
    | "ExportNamedDeclaration" =>
      noAttributes j
      unless (← j.getObjVal? "source") == .null do throw "unsupported re-export from another module"
      let declaration ← j.getObjVal? "declaration"
      let specs ← j.getObjValAs? (Array Json) "specifiers"
      if declaration == .null then
        for sp in specs do
          exports := exports.push (← FromESTree.exportSpec sp)
      else
        unless specs.isEmpty do throw "expected an inline declaration export"
        let (name, s) ← FromESTree.linkedDeclaration names declaration
        units := units.push (i, s, declaration)
        exports := exports.push (name, name)
    | _ =>
      let s ← (FromESTree.linkedUnit names j).mapError fun why => s!"statement {i}: {why}"
      units := units.push (i, s, j)
  return { imports := imports.toList, units := units.toList, exports := exports.toList }

private def Parsed.numbered (p : Parsed) (name : String) (base : Nat) : Module where
  name := name
  imports := p.imports
  body := p.units.map fun (i, s, _) => (base + i, s)
  exports := p.exports

private def readProviders (providers : List (String × Json)) (base : Nat) : Except String (List Module) := do
  let names := providers.map Prod.fst
  let mut modules := #[]
  let mut offset := base
  for (name, file) in providers do
    let nodes ← (ESTreeUnits.nodes file).mapError fun why => s!"{name}: {why}"
    let p ← (parse name nodes).mapError fun why => s!"{name}: {why}"
    for req in p.imports do
      unless names.contains req.source do throw s!"{name}: missing provider dependency {req.source}"
    modules := modules.push (p.numbered name offset)
    offset := offset + nodes.size
  return modules.toList

private def readSplit (files : Json) (P : EsmSplit.Plan) (providers : List String) (original : Parsed)
    (m : EsmSplit.Module) : Except String Module := do
  let nodes ← ESTreeUnits.nodes (← files.getObjVal? m.name)
  let p ← parse m.name nodes
  let (external, glue) := p.imports.partition fun req => providers.contains req.source
  if m.name == P.entry then
    unless p.imports.take original.imports.length == original.imports && external == original.imports do
      throw "entry requests for providers differ from the original entry"
  else
    for req in external do
      unless !req.names.isEmpty && original.imports.any (fun i =>
          i.source == req.source && req.names.all i.names.contains) do
        throw s!"unexpected provider import from {req.source}"
  let expectedGlue : List Import := m.imports.map fun (source, names) =>
    { source, names := names.map fun localName =>
      (((P.find source).map (·.publicName localName)).getD localName, localName) }
  unless glue = expectedGlue do throw "module's glue imports differ from the plan"
  let expectedExports := m.exports ++ (if m.name == P.entry then original.exports else [])
  unless p.exports.Perm expectedExports do throw "module's named exports differ from the plan"
  let placed := m.stmts.filterMap fun i => (original.units.lookup i).map (i, ·.2)
  unless p.units.length == placed.length do throw "emitted body length differs from the placement"
  let body ← (p.units.zip placed).mapM fun ((_, actual, ast), (id, expected)) => do
    unless unitSyntax ast == unitSyntax expected do throw s!"emitted syntax differs from original unit {id}"
    return (id, actual)
  return { name := m.name, imports := p.imports, body, exports := p.exports }

private def linkingWorld : World where
  σ := Unit
  init := ()
  step := fun _ _ _ st => (.ret .undef, st)
  talk := fun _ st => (.unknown "unexpected code evaluation during linking", st)

private def explain : Abort → String
  | .error _ why | .unknown why => why

private def scopes (p : Program) : Except String (List (Module × Env)) := do
  unless p.evalOrder.Perm (p.modules.map (·.name)) do
    throw "linked files are missing or unreachable from the entry"
  let .ok frames _ := declareTops (W := linkingWorld) p.tops (initialState linkingWorld p.regions)
    | throw "the linked linker could not allocate declaration cells"
  for m in p.modules do
    let locals := (m.scope frames).map Prod.fst
    unless (locals ++ m.imports.flatMap (fun i => i.names.map Prod.snd)).Nodup do
      throw s!"{m.name}: repeated local or imported binding"
    unless (m.exports.map Prod.fst).Nodup do throw s!"{m.name}: repeated exported name"
    for (publicName, _) in m.exports do
      let _ ← (p.importCell frames m.name publicName).mapError fun why => s!"{m.name}: {explain why}"
  (p.scopes frames p.loaded).mapError explain

private def entryExports (entry : String) (linked : List (Module × Env)) : Except String (List (Name × Addr)) := do
  let some (m, env) := linked.find? (fun (m, _) => m.name == entry) | throw "missing linked entry"
  m.exports.mapM fun (publicName, localName) => do
    let some cell := env.vars.lookup localName | throw s!"unbound entry export {publicName}"
    return (publicName, cell)

private def checkScopes (before after : Program) : Except String Unit := do
  let a ← scopes before
  let b ← scopes after
  let exports ← entryExports before.entry a
  unless exports.Perm (← entryExports after.entry b) do throw "entry exports resolve to different linked cells"
  let bs := unitsOf b
  for (id, s, env) in unitsOf a do
    let some (s', env') := bs.lookup id | throw s!"missing linked unit {id}"
    for x in (s.free ++ s'.free).eraseDups do
      let some cell := env.vars.lookup x | throw s!"original unit {id} has an unbound reference to {x}"
      unless env'.vars.lookup x == some cell do throw s!"unit {id}: reference to {x} resolves to a different linked cell"

/-- The original entry is at the virtual root. Provider and emitted file keys are canonical
relative filenames in that same root, with no overlap. Plan ordinals refer to the full
source unit array, including imports and export lists. The two programs share the
providers, whose stable identities start after that array. -/
def decodePair (source providers files : Json) (P : EsmSplit.Plan) : Except String (Program × Program) := do
  let fs := (← files.getObj?).toList.map Prod.fst
  let ps := (← providers.getObj?).toList
  let providerNames := ps.map Prod.fst
  EsmSplit.validateModulePaths (P.entry :: fs ++ providerNames)
  unless fs.Perm (P.mods.map (·.name)) && fs.contains P.entry do throw "output filenames differ from the plan"
  unless !fs.any providerNames.contains do throw "providers overlap emitted modules"
  let nodes ← ESTreeUnits.nodes source
  unless (P.mods.flatMap (·.stmts)).Perm (List.range nodes.size) do
    throw "placement must cover every original unit once"
  let some entry := P.find P.entry | throw "missing entry placement"
  for (j, i) in nodes.zipIdx do
    if ["ImportDeclaration", "ExportNamedDeclaration"].contains (← typ j) then
      unless entry.stmts.contains i do throw s!"original import or export unit {i} must stay in the entry"
  let original ← parse "bundle.mjs" nodes
  for req in original.imports do
    unless providerNames.contains req.source do throw s!"missing original dependency {req.source}"
  let shared ← readProviders ps nodes.size
  let before : Program := { entry := P.entry, modules := original.numbered P.entry 0 :: shared }
  let split ← P.mods.mapM fun m =>
    (readSplit files P providerNames original m).mapError fun why => s!"{m.name}: {why}"
  let after : Program := { entry := P.entry, modules := split ++ shared }
  checkScopes before after
  return (before, after)

end CoreJS.LinkedInput
