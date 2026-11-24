import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.LinkedDeployment
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.ModuleInput
import EsmSplit.CoreJS.OpaqueInput
import EsmSplit.Plan
import EsmSplit.ESTreeUnits
import EsmSplit.ModulePath

/-! Lower the original and emitted module trees, with shared, unchanged
providers. The parser's AST, scope annotations and literals are frontend premises.
Placement gives emitted units their original identities; the actual module syntax
gives both programs their bodies and links. -/

namespace CoreJS.LinkedInput

open Lean (Json)

private def field (j : Json) (key : String) := j.getObjValAs? String key
private def typ (j : Json) := field j "type"

/-- Reparsing can change offsets, literal spellings, and local binder indices. A module
binding marked `T` can become an import marked `V`. The linker checks its actual cell. -/
private partial def unitSyntax : Json → Json
  | .obj fields => Json.mkObj <| fields.toList.filterMap fun (k, v) =>
    if ["start", "end", "loc", "range", "raw", "b"].contains k then none
    else if k == "k" && (v == .str "T" || v == .str "V") then some (k, .str "T")
    else some (k, unitSyntax v)
  | .arr nodes => .arr (nodes.map unitSyntax)
  | j => j

/-- Give each callable a stable part number within its original unit. The order
comes from the unchanged syntax tree, independent of on-disk byte offsets. -/
private partial def numberFunctions (unit : Nat) : Json → StateM Nat Json
  | .obj fields => do
    let site ← if (fields.get? "opaqueCaptures").isSome then do
      let part ← get
      modify (· + 1)
      pure [(":opaque-unit", Lean.toJson unit), (":opaque-part", Lean.toJson part)]
    else pure []
    let children ← fields.toList.mapM fun (key, child) => do
      pure (key, ← numberFunctions unit child)
    return Json.mkObj (children ++ site)
  | .arr children => return .arr (← children.mapM (numberFunctions unit))
  | j => pure j

private structure Parsed where
  imports : List Import
  units : List (Nat × Stmt × Json)
  exports : List (Name × Name)

/-- Structural proofs can lower selected source units concretely and interpret the remaining whole units
through their lexical capability lists. The ordinary linked checker lowers every unit concretely. -/
private inductive BodyFormat where
  | linked
  | structural (pivots : Std.HashSet Nat) (location : LinkedDeployment.Access)

private def BodyFormat.lower (format : BodyFormat) (profile : HostProfile) (imports : List Name)
    (origin : Nat) (j : Json) (exported : Bool) : Except String (Stmt × List Name) := do
  if let .structural pivots location := format then
    unless pivots.contains origin do
      let s ← OpaqueInput.unit origin j location.unitNames[origin]?
      return (s, if exported then (Lower.topDefs j).eraseDups else [])
  let ast := ((numberFunctions origin j).run 1).1
  if exported then
    let (bindings, s) ← FromESTree.linkedDeclaration imports ast profile
    return (s, bindings)
  return (← FromESTree.linkedUnit imports ast profile, [])

private def parse (format : BodyFormat) (profile : HostProfile) (path : String) (nodes : Array Json)
    (base : Nat := 0) (origins : Option (Array Nat) := none) : Except String Parsed := do
  let mut imports := #[]
  for j in nodes do
    if (← typ j) == "ImportDeclaration" then
      imports := imports.push (← ModuleInput.importDecl path j)
  let names := imports.toList.flatMap fun i => i.names.map Prod.snd
  let mut units := #[]
  let mut exports := #[]
  for (j, i) in nodes.zipIdx do
    let decode (ast : Json) (exported : Bool) : Except String (Stmt × List Name) := do
      let origin ← match origins with
        | none => pure (base + i)
        | some ids =>
          let some id := ids[units.size]? | throw "emitted body length differs from the placement"
          pure id
      format.lower profile names origin ast exported
    match ← typ j with
    | "ImportDeclaration" => pure ()
    | "ExportNamedDeclaration" =>
      ModuleInput.noAttributes j
      unless (← j.getObjVal? "source") == .null do throw "unsupported re-export from another module"
      let declaration ← j.getObjVal? "declaration"
      let specs ← j.getObjValAs? (Array Json) "specifiers"
      if declaration == .null then
        for sp in specs do
          exports := exports.push (← FromESTree.exportSpec sp)
      else
        unless specs.isEmpty do throw "expected an inline declaration export"
        let (s, names) ← decode declaration true
        units := units.push (i, s, declaration)
        for name in names do exports := exports.push (name, name)
    | _ =>
      let (s, _) ← (decode j false).mapError fun why => s!"statement {i}: {why}"
      units := units.push (i, s, j)
  return { imports := imports.toList, units := units.toList, exports := exports.toList }

private def Parsed.numbered (p : Parsed) (name : String) (base : Nat) : Module where
  name := name
  imports := p.imports
  body := p.units.map fun (i, s, _) => (base + i, s)
  exports := p.exports

private def readProviders (format : BodyFormat) (profile : HostProfile) (providers : List (String × Json)) (base : Nat) : Except String (List Module) := do
  let names := providers.map Prod.fst
  let mut modules := #[]
  let mut offset := base
  for (name, file) in providers do
    let nodes ← (ESTreeUnits.nodes file).mapError fun why => s!"{name}: {why}"
    let p ← (parse format profile name nodes offset).mapError fun why => s!"{name}: {why}"
    for req in p.imports do
      unless names.contains req.source do throw s!"{name}: missing provider dependency {req.source}"
    modules := modules.push (p.numbered name offset)
    offset := offset + nodes.size
  return modules.toList

private def readSplit (format : BodyFormat) (profile : HostProfile) (files : Json) (P : EsmSplit.Plan) (providers : List String) (original : Parsed)
    (originalSyntax : Std.HashMap Nat Json) (m : EsmSplit.Module) : Except String Module := do
  let nodes ← ESTreeUnits.nodes (← files.getObjVal? m.name)
  let placed := m.stmts.filterMap fun i => originalSyntax[i]?.map (i, ·)
  let p ← parse format profile m.name nodes (origins := some (placed.map Prod.fst).toArray)
  let (external, glue) := p.imports.partition fun req => providers.contains req.source
  if m.name == P.entry then
    unless p.imports.take original.imports.length == original.imports && external == original.imports do
      throw "entry requests for providers differ from the original entry"
  else
    for req in external do
      unless !req.names.isEmpty && original.imports.any (fun i =>
          i.source == req.source && req.names.all i.names.contains) do
        throw s!"unexpected provider import from {req.source}"
  let exportsOf (md : EsmSplit.Module) := md.exports ++ (if md.name == P.entry then original.exports else [])
  let expectedGlue : List Import := m.imports.map fun (source, names) =>
    let exports := ((P.find source).map exportsOf).getD []
    { source, names := names.map fun localName => (EsmSplit.exportNameFor exports localName, localName) }
  unless glue = expectedGlue do throw "module's glue imports differ from the plan"
  unless p.exports.Perm (exportsOf m) do throw "module's named exports differ from the plan"
  unless p.units.length == placed.length do throw "emitted body length differs from the placement"
  let body ← (p.units.zip placed).mapM fun ((_, actual, ast), (id, expected)) => do
    unless unitSyntax ast == unitSyntax expected do throw s!"emitted syntax differs from original unit {id}"
    return (id, actual)
  return { name := m.name, imports := p.imports, body, exports := p.exports }

private def explain : Abort → String
  | .error _ why | .unknown why => why

private def scopes (p : Program) : Except String (List (Module × Env)) := do
  unless p.evalOrder.Perm (p.modules.map (·.name)) do
    throw "linked files are missing or unreachable from the entry"
  let .ok frames _ := declareTops (W := World.probe) p.tops (initialState World.probe p.regions)
    | throw "the linked linker could not allocate declaration cells"
  for m in p.modules do
    let locals := (m.scope frames).map Prod.fst
    unless (locals ++ m.imports.flatMap (fun i => i.names.map Prod.snd)).Nodup do
      throw s!"{m.name}: repeated local or imported binding"
    unless (m.exports.map Prod.fst).Nodup do throw s!"{m.name}: repeated exported name"
  let linked ← (p.scopes frames p.loaded).mapError explain
  for (m, env) in linked do
    for (publicName, localName) in m.exports do
      unless (env.vars.lookup localName).isSome do
        throw s!"{m.name}: unbound export {publicName}"
  return linked

private def checkScopes (before after : Program) : Except String Unit := do
  let a ← (scopes before).mapError ("original linker: " ++ ·)
  let b ← (scopes after).mapError ("candidate linker: " ++ ·)
  let original ← (EntryExports.resolve before.entry a).mapError ("original entry: " ++ ·)
  let candidate ← (EntryExports.resolve after.entry b).mapError ("candidate entry: " ++ ·)
  unless original.Perm candidate do throw (EntryExports.mismatch original candidate)
  -- Placement is checked in decodePair. Provider ordinals follow the
  -- source and use disjoint ranges, so each ordinal has only one linked unit.
  let bs := Std.HashMap.ofList (unitsOf b)
  for (id, s, env) in unitsOf a do
    let some (s', env') := bs[id]? | throw s!"missing linked unit {id}"
    for x in (s.free ++ s'.free).eraseDups do
      let some cell := env.vars.lookup x | throw s!"original unit {id} has an unbound reference to {x}"
      unless env'.vars.lookup x == some cell do throw s!"unit {id}: reference to {x} resolves to a different linked cell"

private def decode (format : BodyFormat) (source providers files : Json) (P : EsmSplit.Plan)
    (profile : HostProfile) : Except String (Program × Program) := do
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
  let original ← parse format profile "bundle.mjs" nodes
  if let .structural pivots _ := format then
    let bodyIds := Std.HashSet.ofList (original.units.map (·.1))
    for pivot in pivots do
      unless bodyIds.contains pivot do throw s!"pivot {pivot} is not an executable source unit"
  for req in original.imports do
    unless providerNames.contains req.source do throw s!"missing original dependency {req.source}"
  let shared ← readProviders format profile ps nodes.size
  let before : Program := { entry := P.entry, modules := original.numbered P.entry 0 :: shared }
  let originalSyntax := Std.HashMap.ofList (original.units.map fun (i, _, ast) => (i, ast))
  let split ← P.mods.mapM fun m =>
    (readSplit format profile files P providerNames original originalSyntax m).mapError fun why => s!"{m.name}: {why}"
  let after : Program := { entry := P.entry, modules := split ++ shared }
  let (before, after) ← match format with
    | .linked => pure (before, after)
    | .structural _ location =>
      pure (← location.attach before, ← location.attach after)
  checkScopes before after
  return (before, after)

/-- The original entry is at the virtual root. Provider and emitted file keys are canonical
relative filenames in that same root, with no overlap. Plan ordinals refer to the full
source unit array, including imports and export lists. The two programs share the
providers, whose stable identities start after that array. -/
def decodePair (source providers files : Json) (P : EsmSplit.Plan) (profile : HostProfile := .empty) :
    Except String (Program × Program) := decode .linked source providers files P profile

/-- Experimental input for a structural schedule proof. The body of each returned module retains the
original source ordinals, including when an emitted module has different positions. Providers use
disjoint ordinals. Selected pivots have their actual CoreJS statements on both sides. Retained statements
are decoded from their own syntax and carry the same source code ID between layouts.
This verifies placement, unchanged unit syntax, import/export links and resolved captures. A proof
of the order change and correspondence with the emitted source files are separate obligations. -/
def decodeStructuralPair (source providers files : Json) (P : EsmSplit.Plan) (pivots : List Nat)
    (profile : HostProfile := .empty) (deployment : Option LinkedDeployment.Input := none) :
    Except String (Program × Program) := do
  let location ← deployment.mapM (LinkedDeployment.check · source providers P)
  decode (.structural (Std.HashSet.ofList pivots) (location.getD {})) source providers files P profile

end CoreJS.LinkedInput
