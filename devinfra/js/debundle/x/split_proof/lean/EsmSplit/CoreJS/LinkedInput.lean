import EsmSplit.CoreJS.DeploymentPlacement
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
private partial def unitSyntax (j : Json) (taggedQuasi := false) : Json :=
  match j with
  | .obj fields => Json.mkObj <| fields.toList.filterMap fun (k, v) =>
    if ["start", "end", "loc", "range", "b"].contains k || (k == "raw" && !taggedQuasi) then none
    else if k == "k" && (v == .str "T" || v == .str "V") then some (k, .str "T")
    else
      -- A tag observes segment spellings, including when the enclosing function is opaque.
      let quasi := (k == "quasi" && fields.get? "type" == some (.str "TaggedTemplateExpression")) ||
        (taggedQuasi && (k == "quasis" || k == "value"))
      some (k, unitSyntax v quasi)
  | .arr nodes => .arr (nodes.map fun node => unitSyntax node taggedQuasi)
  | _ => j

private structure TemplateSite where
  unit : Nat
  part : Nat
  quasis : Json

private def TemplateSite.binding (site : TemplateSite) := s!":template:{site.unit}:{site.part}"

private structure Numbering where
  callable : Nat := 1
  templates : Array TemplateSite := #[]

private partial def callableOffsets (j : Json) (offsets : Array (Option Nat)) : Array (Option Nat) :=
  let offsets := if (j.getObjVal? "opaqueCaptures").isOk then
    offsets.push (j.getObjValAs? Nat "start").toOption
  else offsets
  (Lower.children j).foldl (fun acc child => callableOffsets child acc) offsets

/-- Give callables lexical ordinals within the original unit. Template sites use a
separate namespace. Both identities survive printing and reparsing. -/
private partial def annotateSites (unit : Nat) (parts : Std.HashMap Nat Nat)
    (scripts : Std.HashSet (Nat × Nat)) : Json → StateM Numbering Json
  | .obj fields => do
    let site ← if (fields.get? "opaqueCaptures").isSome then do
      let fallback := (← get).callable
      modify fun s => { s with callable := fallback + 1 }
      let offset := (fields.get? "start").bind (·.getNat?.toOption)
      let part := (offset.bind (fun offset => parts[offset]?)).getD fallback
      pure [(":opaque-unit", Lean.toJson unit), (":opaque-part", Lean.toJson part),
        (":opaque-scripted", Lean.toJson (scripts.contains (unit, part)))]
    else pure []
    let template ← if fields.get? "type" == some (.str "TaggedTemplateExpression") then do
      let q := ((fields.get? "quasi").bind (fun q => (q.getObjVal? "quasis").toOption)).getD .null
      let t : TemplateSite := ⟨unit, (← get).templates.size, q⟩
      modify fun s => { s with templates := s.templates.push t }
      pure [(":template-binding", Json.str t.binding)]
    else pure []
    let children ← fields.toList.mapM fun (key, child) => do
      pure (key, ← annotateSites unit parts scripts child)
    return Json.mkObj (children ++ site ++ template)
  | .arr children => return .arr (← children.mapM (annotateSites unit parts scripts))
  | j => pure j

private def numberSites (unit : Nat) (scripts : Std.HashSet (Nat × Nat)) (j : Json) : Json × Numbering :=
  let offsets := callableOffsets j #[]
  let starts := (offsets.toList.filterMap id).mergeSort
  let parts := Std.HashMap.ofList (starts.zipIdx.map fun (offset, index) => (offset, index + 1))
  -- Compact AST fixtures without ranges still have a deterministic traversal.
  let parts := if parts.size == offsets.size then parts else {}
  (annotateSites unit parts scripts j).run {}

private structure Parsed where
  imports : List Import
  units : List (Nat × Stmt × Json)
  exports : List (Name × Name)
  templates : List TemplateSite

/-- A structural proof retains non-pivots. A recorded replay retains a unit
with a part-zero script and can script individual callable bodies. -/
private inductive BodyFormat where
  | linked
  | structural (pivots : Std.HashSet Nat) (location : LinkedDeployment.Access)
  | recorded (scripts : Std.HashSet (Nat × Nat))

private def BodyFormat.scripts : BodyFormat → Std.HashSet (Nat × Nat)
  | .recorded scripts => scripts
  | _ => {}

private def BodyFormat.lower (format : BodyFormat) (profile : HostProfile) (imports : List Name)
    (origin : Nat) (providerSite : Option (String × Nat)) (j : Json) (exported : Bool) : Except String (Stmt × List Name × List TemplateSite) := do
  let retain (loaderNames : Option (List Name)) := do
    let s ← OpaqueInput.unit origin j loaderNames
    return (s, (if exported then (Lower.topDefs j).eraseDups else []), [])
  match format with
  | .structural pivots location =>
    unless pivots.contains origin do
      -- Generators need their own intrinsic and fresh prototype.
      if Lower.typ j == "FunctionDeclaration" && Lower.flag j "generator" then
        if let some why := Lower.outsideModel false j then throw why
      else
        let names := match providerSite with
          | none => location.entryAccess.unitNames[origin]?
          | some (name, ordinal) => (location.forModule name).unitNames[ordinal]?
        return ← retain names
  | .recorded scripts =>
    if scripts.contains (origin, 0) then return ← retain none
  | .linked => pure ()
  let (ast, sites) := numberSites origin format.scripts j
  if exported then
    let (bindings, s) ← FromESTree.linkedDeclaration imports ast profile
    return (s, bindings, sites.templates.toList)
  return (← FromESTree.linkedUnit imports ast profile, [], sites.templates.toList)

/-- `export default …` lowers concretely. A structural proof can keep it only as a pivot. -/
private def BodyFormat.lowerDefault (format : BodyFormat) (profile : HostProfile) (imports : List Name)
    (origin : Nat) (j : Json) : Except String (Stmt × Name × List TemplateSite) := do
  if let .structural pivots _ := format then
    unless pivots.contains origin do throw "unsupported opaque default export"
  if format.scripts.contains (origin, 0) then throw "unsupported opaque default export"
  let (ast, sites) := numberSites origin format.scripts j
  let (name, s) ← FromESTree.linkedDefaultExport imports ast profile
  return (s, name, sites.templates.toList)

private def parse (format : BodyFormat) (profile : HostProfile) (path : String) (nodes : Array Json)
    (base : Nat := 0) (origins : Option (Array Nat) := none) (provider : Option String := none) : Except String Parsed := do
  let mut imports := #[]
  for j in nodes do
    if (← typ j) == "ImportDeclaration" then
      imports := imports.push (← ModuleInput.importDecl path j)
  let names := imports.toList.flatMap fun i => i.names.map Prod.snd
  let mut units := #[]
  let mut exports := #[]
  let mut templates := #[]
  for (j, i) in nodes.zipIdx do
    let origin : Except String Nat := match origins with
      | none => pure (base + i)
      | some ids => match ids[units.size]? with
        | some id => pure id
        | none => throw "emitted body length differs from the placement"
    let decode (ast : Json) (exported : Bool) : Except String (Stmt × List Name × List TemplateSite) := do
      let unitOrigin ← origin
      format.lower profile names unitOrigin (provider.map fun referrer => (referrer, unitOrigin - base)) ast exported
    match ← typ j with
    | "ImportDeclaration" => pure ()
    -- Neither statement changes the modeled state, so neither gets a unit.
    | "EmptyStatement" | "DebuggerStatement" => pure ()
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
    | "ExportDefaultDeclaration" =>
      let (s, name, sites) ← (format.lowerDefault profile names (← origin) (← j.getObjVal? "declaration")).mapError
        fun why => s!"statement {i}: {why}"
      templates := templates ++ sites.toArray
      units := units.push (i, s, j)
      exports := exports.push ("default", name)
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

private def readProviders (format : BodyFormat) (profile : HostProfile) (providers : List (String × Json))
    (base : Nat) (roots : List String := []) : Except String (List Module × List TemplateSite) := do
  let mut templates := #[]
  let names := roots ++ providers.map Prod.fst
  let mut modules := #[]
  let mut offset := base
  for (name, file) in providers do
    let nodes ← (ESTreeUnits.nodes file).mapError fun why => s!"{name}: {why}"
    let p ← (parse format profile name nodes offset (provider := some name)).mapError fun why => s!"{name}: {why}"
    for req in p.imports do
      unless names.contains req.source do
        throw s!"{name}: missing provider dependency {req.source}"
    templates := templates ++ p.templates.toArray
    modules := modules.push (p.numbered name offset)
    offset := offset + nodes.size
  return (modules.toList, templates.toList)

private def readSplit (format : BodyFormat) (profile : HostProfile) (files : Json) (P : EsmSplit.Plan) (providers : List String) (original : Parsed)
    (originalSyntax : Std.HashMap Nat Json) (m : EsmSplit.Module) (base : Nat := 0)
    (provider : Option String := none) : Except String (Module × List TemplateSite) := do
  let nodes ← ESTreeUnits.nodes (← files.getObjVal? m.name)
  let placed := m.stmts.filterMap fun i => originalSyntax[i]?.map (base + i, ·)
  let p ← parse format profile m.name nodes base (some (placed.map Prod.fst).toArray) provider
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
      let segment ← if text == .null then pure Expr.undef else Expr.str <$> text.getStr?
      cooked := cooked.push segment
    let array := Expr.prim (.templateArray true) (.prim (.templateArray false) raw.toList :: cooked.toList)
    body := body.push (p.regions + body.size, Stmt.decl .const site.binding array)
  let registry : Module := ⟨provider, [], body.toList, names.map fun name => (name, name)⟩
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

private structure Fragment where
  sourceModule : Module
  emitted : List Module
  unitCount : Nat
  sourceTemplates : List TemplateSite
  emittedTemplates : List TemplateSite

private def decodeFragment (format : BodyFormat) (profile : HostProfile) (source files : Json) (P : EsmSplit.Plan)
    (sourcePath : String) (providerNames : List String) (base : Nat) (provider : Option String := none) : Except String Fragment := do
  let fs := (← files.getObj?).toList.map Prod.fst
  EsmSplit.validateModulePaths (P.entry :: fs ++ providerNames)
  unless fs.Perm (P.mods.map (·.name)) && fs.contains P.entry do throw "output filenames differ from the plan"
  unless !fs.any providerNames.contains do throw "providers overlap emitted modules"
  let nodes ← ESTreeUnits.nodes source
  unless (P.mods.flatMap (·.stmts)).Perm (List.range nodes.size) do
    throw "placement must cover every original unit once"
  let some entry := P.find P.entry | throw "missing entry placement"
  for (j, i) in nodes.zipIdx do
    if ["ImportDeclaration", "ExportNamedDeclaration", "ExportDefaultDeclaration"].contains (← typ j) then
      unless entry.stmts.contains i do throw s!"original import or export unit {i} must stay in the entry"
  let original ← parse format profile sourcePath nodes base (provider := provider)
  if let .structural pivots _ := format then
    let bodyIds := Std.HashSet.ofList (original.units.map fun u => base + u.1)
    for pivot in pivots do
      unless bodyIds.contains pivot do throw s!"pivot {pivot} is not an executable source unit"
  for req in original.imports do
    unless providerNames.contains req.source do throw s!"missing original dependency {req.source}"
  let originalSyntax := Std.HashMap.ofList (original.units.map fun (i, _, ast) => (i, ast))
  let split ← P.mods.mapM fun m =>
    (readSplit format profile files P providerNames original originalSyntax m base provider).mapError fun why => s!"{m.name}: {why}"
  return ⟨original.numbered P.entry base, split.map Prod.fst, nodes.size, original.templates, split.flatMap Prod.snd⟩

private def decode (format : BodyFormat) (source providers files : Json) (P : EsmSplit.Plan)
    (profile : HostProfile) : Except String (Program × Program) := do
  let ps := (← providers.getObj?).toList
  let fragment ← decodeFragment format profile source files P "bundle.mjs" (ps.map Prod.fst) 0
  let (shared, providerSites) ← readProviders format profile ps fragment.unitCount
  let before ← installTemplates { entry := P.entry, modules := fragment.sourceModule :: shared }
    (fragment.sourceTemplates ++ providerSites)
  let after ← installTemplates { entry := P.entry, modules := fragment.emitted ++ shared }
    (fragment.emittedTemplates ++ providerSites)
  let (before, after) ← match format with
    | .linked | .recorded _ => pure (before, after)
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

/-- One original path and the reparsed output files of its plan. Filenames
in both plans live in the same deployment root. -/
structure StaticSplit where
  source : Json
  files : Json
  plan : EsmSplit.Plan

/-- Lower and link an unsplit graph for the recorded Node world. Source ordinals
include the entry's import and export syntax. Provider ordinals follow the entry. -/
def program (source providers : Json) (scripts : Std.HashSet (Nat × Nat)) (entry : String := "main") :
    Except String Program := do
  let nodes ← ESTreeUnits.nodes source
  let format := BodyFormat.recorded scripts
  let original ← parse format .nodeRecording entry nodes
  let ps := (← providers.getObj?).toList
  for req in original.imports do
    unless ps.any (·.1 == req.source) do throw s!"missing original dependency {req.source}"
  let (shared, providerSites) ← readProviders format .nodeRecording ps nodes.size [entry]
  let p ← installTemplates { entry, modules := original.numbered entry 0 :: shared }
    (original.templates ++ providerSites)
  let _ ← scopes p
  return p

/-- Public paths are the original module records, including unchanged dependencies.
The candidate can also contain private files and model-only helper modules. -/
def publicPaths (parts : List StaticSplit) (shared : Json) : Except String (List String) := do
  return parts.map (·.plan.entry) ++ (← shared.getObj?).toList.map Prod.fst

structure DecodedDeployment where
  before : Program
  after : Program
  assignment : List UnitPlacement

/-- Plans also number import and export lists. Keep only the ordinals present
in the original executable body when building the CoreJS assignment. -/
private def plannedPlacements (plan : EsmSplit.Plan) (source : Module) (base : Nat) : List UnitPlacement :=
  let ids := Std.HashSet.ofList (source.body.map Prod.fst)
  plan.mods.flatMap fun m => m.stmts.filterMap fun ordinal =>
    let id := base + ordinal
    if ids.contains id then some (id, m.name) else none

/-- Decode all placements in one namespace. IDs follow the input list and the
sorted fixed-file map, independently of either graph's evaluation order. In
structural mode each fragment's hints and capabilities use its local ordinals. -/
def decodeDeployment (entry : String) (parts : List StaticSplit) (shared : Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile)
    (hints : String → List Nat) : Except String DecodedDeployment := do
  let fixedFiles := (← shared.getObj?).toList
  let originals ← publicPaths parts shared
  let outputs := parts.flatMap fun part => part.plan.mods.map (·.name)
  let all := outputs ++ fixedFiles.map Prod.fst
  EsmSplit.validateModulePaths (entry :: originals ++ all)
  unless originals.Nodup && all.Nodup do throw "joint output path collision"
  unless originals.contains entry do throw s!"missing deployment root: {entry}"
  let access ← match inputs with
    | some snapshot => do
      unless snapshot.entry == entry do throw "deployment root differs"
      LinkedDeployment.checkGraph snapshot (parts.map fun p => (p.source, p.plan)) shared
    | none => do
      for (path, ast) in parts.map (fun p => (p.plan.entry, p.source)) ++ fixedFiles do
        unless (← ModuleLocation.uses path ast).isEmpty do
          throw s!"{path}: joint static split requires location-independent units"
      pure {}
  let format (ids : List Nat) : BodyFormat :=
    if inputs.isSome then .structural (Std.HashSet.ofList ids) access else .linked
  let mut fragments := #[]
  let mut planned := #[]
  let mut base := 0
  for part in parts do
    let name := part.plan.entry
    let f ← (decodeFragment (format ((hints name).map (base + ·))) profile part.source part.files part.plan
      name (originals.erase name) base (some name)).mapError fun why => s!"{name}: {why}"
    fragments := fragments.push f
    planned := planned ++ (plannedPlacements part.plan f.sourceModule base).toArray
    base := base + f.unitCount
  let (fixed, fixedSites) ← readProviders (format []) profile fixedFiles base (parts.map (·.plan.entry))
  let fs := fragments.toList
  let before ← installTemplates ⟨entry, fs.map (·.sourceModule) ++ fixed⟩
    (fs.flatMap (·.sourceTemplates) ++ fixedSites)
  let after ← installTemplates ⟨entry, fs.flatMap (·.emitted) ++ fixed⟩
    (fs.flatMap (·.emittedTemplates) ++ fixedSites)
  let before ← access.attach before
  let after ← access.attach after
  checkScopes before after
  return ⟨before, after, planned.toList ++ fixed.flatMap Module.unitPlacements⟩

end CoreJS.LinkedInput
