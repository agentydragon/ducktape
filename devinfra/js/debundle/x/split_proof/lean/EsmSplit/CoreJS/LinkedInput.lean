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

private structure TemplateSite where
  unit : Nat
  part : Nat
  quasis : Json

private def TemplateSite.binding (site : TemplateSite) := s!":template:{site.unit}:{site.part}"

private structure Numbering where
  callable : Nat := 1
  templates : Array TemplateSite := #[]

/-- Give callables and templates independent ordinals within the original unit.
Emitters change offsets and module boundaries. Corresponding syntax retains these identities. -/
private partial def numberSites (unit : Nat) : Json → StateM Numbering Json
  | .obj fields => do
    let site ← if (fields.get? "opaqueCaptures").isSome then do
      let part := (← get).callable
      modify fun s => { s with callable := part + 1 }
      pure [(":opaque-unit", Lean.toJson unit), (":opaque-part", Lean.toJson part)]
    else pure []
    let template ← if fields.get? "type" == some (.str "TaggedTemplateExpression") then do
      let q := ((fields.get? "quasi").bind (fun q => (q.getObjVal? "quasis").toOption)).getD .null
      let t : TemplateSite := ⟨unit, (← get).templates.size, q⟩
      modify fun s => { s with templates := s.templates.push t }
      pure [(":template-binding", Json.str t.binding)]
    else pure []
    let children ← fields.toList.mapM fun (key, child) => do
      pure (key, ← numberSites unit child)
    return Json.mkObj (children ++ site ++ template)
  | .arr children => return .arr (← children.mapM (numberSites unit))
  | j => pure j

private structure Parsed where
  imports : List Import
  units : List (Nat × Stmt × Json)
  exports : List (Name × Name)
  templates : List TemplateSite

/-- Structural proofs can lower selected source units concretely and interpret the remaining whole units
through their lexical capability lists. The ordinary linked checker lowers every unit concretely. -/
private inductive BodyFormat where
  | linked
  | structural (pivots : Std.HashSet Nat) (location : LinkedDeployment.Access)

private def BodyFormat.lower (format : BodyFormat) (profile : HostProfile) (imports : List Name)
    (origin : Nat) (providerSite : Option (String × Nat)) (j : Json) (exported : Bool) : Except String (Stmt × List Name × List TemplateSite) := do
  if let .structural pivots location := format then
    unless pivots.contains origin do
      -- A whole-unit opaque hoist represents ordinary and async functions.
      -- Generator declarations need their own intrinsic and fresh prototype.
      -- Lower the declaration by the same numbered, capture-checked path as a pivot.
      if Lower.typ j == "FunctionDeclaration" && Lower.flag j "generator" then
        if let some why := Lower.outsideModel false j then throw why
      else
        let names := match providerSite with
          | none => location.entryAccess.unitNames[origin]?
          | some (name, ordinal) => location.providers[name]?.bind fun a => a.unitNames[ordinal]?
        let s ← OpaqueInput.unit origin j names
        return (s, (if exported then (Lower.topDefs j).eraseDups else []), [])
  let (ast, sites) := (numberSites origin j).run {}
  if exported then
    let (bindings, s) ← FromESTree.linkedDeclaration imports ast profile
    return (s, bindings, sites.templates.toList)
  return (← FromESTree.linkedUnit imports ast profile, [], sites.templates.toList)

private def parse (format : BodyFormat) (profile : HostProfile) (path : String) (nodes : Array Json)
    (base : Nat := 0) (origins : Option (Array Nat) := none) (provider : Bool := false) : Except String Parsed := do
  let mut imports := #[]
  for j in nodes do
    if (← typ j) == "ImportDeclaration" then
      imports := imports.push (← ModuleInput.importDecl path j)
  let names := imports.toList.flatMap fun i => i.names.map Prod.snd
  let mut units := #[]
  let mut exports := #[]
  let mut templates := #[]
  for (j, i) in nodes.zipIdx do
    let decode (ast : Json) (exported : Bool) : Except String (Stmt × List Name × List TemplateSite) := do
      let origin ← match origins with
        | none => pure (base + i)
        | some ids =>
          let some id := ids[units.size]? | throw "emitted body length differs from the placement"
          pure id
      format.lower profile names origin (if provider then some (path, i) else none) ast exported
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
        let (s, names, sites) ← decode declaration true
        templates := templates ++ sites.toArray
        units := units.push (i, s, declaration)
        for name in names do exports := exports.push (name, name)
    | _ =>
      let (s, _, sites) ← (decode j false).mapError fun why => s!"statement {i}: {why}"
      templates := templates ++ sites.toArray
      units := units.push (i, s, j)
  return { imports := imports.toList, units := units.toList, exports := exports.toList, templates := templates.toList }

private def Parsed.numbered (p : Parsed) (name : String) (base : Nat) : Module where
  name := name
  imports := p.imports
  body := p.units.map fun (i, s, _) => (base + i, s)
  exports := p.exports

private def readProviders (format : BodyFormat) (profile : HostProfile) (providers : List (String × Json)) (base : Nat) : Except String (List Module × List TemplateSite) := do
  let mut templates := #[]
  let names := providers.map Prod.fst
  let mut modules := #[]
  let mut offset := base
  for (name, file) in providers do
    let nodes ← (ESTreeUnits.nodes file).mapError fun why => s!"{name}: {why}"
    let p ← (parse format profile name nodes offset (provider := true)).mapError fun why => s!"{name}: {why}"
    for req in p.imports do
      unless names.contains req.source do throw s!"{name}: missing provider dependency {req.source}"
    templates := templates ++ p.templates.toArray
    modules := modules.push (p.numbered name offset)
    offset := offset + nodes.size
  return (modules.toList, templates.toList)

private def readSplit (format : BodyFormat) (profile : HostProfile) (files : Json) (P : EsmSplit.Plan) (providers : List String) (original : Parsed)
    (originalSyntax : Std.HashMap Nat Json) (m : EsmSplit.Module) : Except String (Module × List TemplateSite) := do
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
  let byBinding := Std.HashMap.ofList (original.templates.map fun site => (site.binding, site.quasis))
  for site in p.templates do
    -- TemplateElement.value.raw is observable to a tag even when its cooked text agrees.
    let text := fun (segments : Json) => segments.getArr?.toOption.map fun qs => qs.map fun q =>
      (q.getObjVal? "value").toOption
    let some originalText := byBinding[site.binding]? | throw "missing original tagged template site"
    unless text site.quasis == text originalText do throw s!"emitted tagged template differs from original unit {site.unit}"
  let body ← (p.units.zip placed).mapM fun ((_, actual, ast), (id, expected)) => do
    unless unitSyntax ast == unitSyntax expected do throw s!"emitted syntax differs from original unit {id}"
    return (id, actual)
  return ({ name := m.name, imports := p.imports, body, exports := p.exports }, p.templates)

/-- Only the tag evaluator can name these bindings. The synthetic provider allocates each
pair once per linked realm, before any consumer can call the tag, including a hoisted
function invoked from an import cycle. Initializers contain only primitive literals and
intrinsic array allocation. Sites with identical strings still receive distinct bindings. -/
private def installTemplates (p : Program) (sites : List TemplateSite) : Except String Program := do
  if sites.isEmpty then return p
  let provider := ":corejs:template-objects"
  if (p.find provider).isSome then throw "reserved template provider name"
  let used := Std.HashSet.ofList (p.modules.flatMap fun m => m.body.flatMap fun (_, stmt) => stmt.free)
  let live := (sites.filter fun site => used.contains site.binding).mergeSort fun a b =>
    decide (a.unit < b.unit || (a.unit == b.unit && a.part ≤ b.part))
  if live.isEmpty then return p
  let names := live.map TemplateSite.binding
  let table := Std.HashSet.ofList names
  let mut body := #[]
  for site in live do
    let segments ← site.quasis.getArr?
    if segments.isEmpty then throw "empty tagged template"
    let mut raw := #[]
    let mut cooked := #[]
    for q in segments do
      if ["rawWellFormed", "cookedWellFormed"].any (fun key => (q.getObjValAs? Bool key).toOption == some false) then
        throw "tagged template contains an isolated UTF-16 surrogate"
      let value ← q.getObjVal? "value"
      raw := raw.push (.str (← field value "raw"))
      let text ← value.getObjVal? "cooked"
      cooked := cooked.push (if text == .null then Expr.undef else .str (← text.getStr?))
    let array := Expr.prim (.templateArray true) (.prim (.templateArray false) raw.toList :: cooked.toList)
    body := body.push (p.regions + body.size, Stmt.decl .const site.binding array)
  let registry : Module := { name := provider, imports := [], body := body.toList,
    exports := names.map fun name => (name, name) }
  let modules := p.modules.map fun m =>
    let needs := (m.body.flatMap fun (_, stmt) => stmt.free.filter table.contains).eraseDups
    if needs.isEmpty then m else
      { m with imports := { source := provider, names := needs.map fun name => (name, name) } :: m.imports }
  return { p with modules := registry :: modules }

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
  let (shared, providerSites) ← readProviders format profile ps nodes.size
  let before ← installTemplates { entry := P.entry, modules := original.numbered P.entry 0 :: shared }
    (original.templates ++ providerSites)
  let originalSyntax := Std.HashMap.ofList (original.units.map fun (i, _, ast) => (i, ast))
  let split ← P.mods.mapM fun m =>
    (readSplit format profile files P providerNames original originalSyntax m).mapError fun why => s!"{m.name}: {why}"
  let after ← installTemplates { entry := P.entry, modules := split.map Prod.fst ++ shared }
    (split.flatMap Prod.snd ++ providerSites)
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
