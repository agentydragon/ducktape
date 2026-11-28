import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.LinkedDeployment
import EsmSplit.CoreJS.ModuleInput
import EsmSplit.CoreJS.OpaqueInput
import EsmSplit.CoreJS.StructuralRefusal
import EsmSplit.ESTreeUnits
import EsmSplit.LocalExportUnit
import EsmSplit.ModulePath
import EsmSplit.Plan

/-! Lower the original and emitted module trees, with shared, unchanged
providers. The parser's AST, scope annotations and literals are frontend premises.
Placement gives emitted units their original identities; the actual module syntax
gives both programs their bodies and links. -/

namespace CoreJS.LinkedInput

open CoreJS.LinkedBehavior
open Lean (Json)

private def field (j : Json) (key : String) :=
  j.getObjValAs? String key

private def typ (j : Json) :=
  field j "type"

/-- Reparsing can change offsets, literal spellings, and local binder indices. A module
binding marked `T` can become an import marked `V`. The linker checks its actual cell. -/
private partial def unitSyntax (j : Json) (taggedQuasi := false) : Json :=
  match j with
  | .obj fields =>
    Json.mkObj <|
      fields.toList.filterMap fun (k, v) =>
        if ["start", "end", "loc", "range", "b"].contains k || (k == "raw" && !taggedQuasi) then
          none
        else
          if k == "k" && (v == .str "T" || v == .str "V") then some (k, .str "T")
          else
            -- A tag observes segment spellings, including when the enclosing function is opaque.
            let quasi :=
              (k == "quasi" && fields.get? "type" == some (.str "TaggedTemplateExpression")) ||
                (taggedQuasi && (k == "quasis" || k == "value"))
            some (k, unitSyntax v quasi)
  | .arr nodes => .arr (nodes.map fun node => unitSyntax node taggedQuasi)
  | _ => j

private structure TemplateSite where
  unit : Nat
  part : Nat
  quasis : Json

private def TemplateSite.binding (site : TemplateSite) :=
  s!":template:{site.unit}:{site.part}"

private structure Numbering where
  callable : Nat := 1
  templates : Array TemplateSite := #[]

/-- One top-level source slot in the linked decoder's trace. Non-executable
items remain explicit omitted slots so indexed-source coverage can refuse
them without shifting later source ordinals. -/
inductive SourceItem where
  | executable (ordinal : Nat) (statement : Stmt)
  | localExport (row : EsmSplit.LocalExportUnit)
  | omitted (ordinal : Nat)

def SourceItem.ordinal : SourceItem → Nat
  | .executable ordinal _ => ordinal
  | .localExport row => row.ordinal
  | .omitted ordinal => ordinal

def SourceItem.executable? : SourceItem → Option (Nat × Stmt)
  | .executable ordinal statement => some (ordinal, statement)
  | .localExport _ | .omitted _ => none

def SourceItem.localExport? : SourceItem → Option EsmSplit.LocalExportUnit
  | .executable _ _ | .omitted _ => none
  | .localExport row => some row

def SourceItem.omitted? : SourceItem → Option Nat
  | .omitted ordinal => some ordinal
  | .executable _ _ | .localExport _ => none

private theorem perm_cons_append_rotate (item : Nat) :
    ∀ (front back : List Nat), List.Perm (item :: (front ++ back)) (front ++ item :: back)
  | [], _ => by simp
  | head :: tail, back => by
    simp only [List.cons_append]
    exact
      (List.Perm.swap item head (tail ++ back)).symm.trans
        (List.Perm.cons head (perm_cons_append_rotate item tail back))

/-- Partitioning the decoder's trace into executable and metadata projections
preserves every original ordinal, up to the projection's grouping order. -/
theorem sourceItemOrdinalsPermProjections (items : List SourceItem) :
    List.Perm (items.map SourceItem.ordinal)
      (((items.filterMap SourceItem.executable?).map Prod.fst ++
        ((items.filterMap SourceItem.localExport?).map (·.ordinal) ++
          items.filterMap SourceItem.omitted?))) := by
  induction items with
  | nil => simp
  | cons item tail ih =>
    cases item with
    | executable ordinal
      statement =>
      change
        List.Perm (ordinal :: tail.map SourceItem.ordinal)
          (ordinal ::
            ((tail.filterMap SourceItem.executable?).map Prod.fst ++
              ((tail.filterMap SourceItem.localExport?).map (·.ordinal) ++
                tail.filterMap SourceItem.omitted?)))
      exact List.Perm.cons ordinal ih
    | localExport
      row =>
      let executableIds := (tail.filterMap SourceItem.executable?).map Prod.fst
      let metadataIds := (tail.filterMap SourceItem.localExport?).map (·.ordinal)
      let omittedIds := tail.filterMap SourceItem.omitted?
      have prepend :
        List.Perm (row.ordinal :: tail.map SourceItem.ordinal)
          (row.ordinal :: (executableIds ++ (metadataIds ++ omittedIds))) :=
        List.Perm.cons row.ordinal ih
      have rotate :
        List.Perm (row.ordinal :: (executableIds ++ (metadataIds ++ omittedIds)))
          (executableIds ++ row.ordinal :: (metadataIds ++ omittedIds)) := by
        exact perm_cons_append_rotate row.ordinal executableIds (metadataIds ++ omittedIds)
      change
        List.Perm (row.ordinal :: tail.map SourceItem.ordinal)
          (executableIds ++ row.ordinal :: (metadataIds ++ omittedIds))
      exact prepend.trans rotate
    | omitted
      ordinal =>
      let executableIds := (tail.filterMap SourceItem.executable?).map Prod.fst
      let metadataIds := (tail.filterMap SourceItem.localExport?).map (·.ordinal)
      let omittedIds := tail.filterMap SourceItem.omitted?
      have prepend :
        List.Perm (ordinal :: tail.map SourceItem.ordinal)
          (ordinal :: (executableIds ++ (metadataIds ++ omittedIds))) :=
        List.Perm.cons ordinal ih
      have rotate :
        List.Perm (ordinal :: (executableIds ++ (metadataIds ++ omittedIds)))
          (executableIds ++ (metadataIds ++ ordinal :: omittedIds)) := by
        simpa [List.append_assoc] using
          perm_cons_append_rotate ordinal (executableIds ++ metadataIds) omittedIds
      change
        List.Perm (ordinal :: tail.map SourceItem.ordinal)
          (executableIds ++ (metadataIds ++ ordinal :: omittedIds))
      exact prepend.trans rotate

private inductive ParsedSourceItem where
  | executable (statement : Stmt) (syntaxNode : Json)
  | localExport (names : List (Name × Name))
  | omitted

private def ParsedSourceItem.sourceItem (ordinal : Nat) : ParsedSourceItem → SourceItem
  | .executable statement _ => .executable ordinal statement
  | .localExport names => .localExport ⟨ordinal, names⟩
  | .omitted => .omitted ordinal

private theorem ParsedSourceItem.sourceItem_ordinal (ordinal : Nat) (item : ParsedSourceItem) :
    (item.sourceItem ordinal).ordinal = ordinal := by cases item <;> rfl

private def ParsedSourceItem.executable? : ParsedSourceItem → Bool
  | .executable _ _ => true
  | .localExport _ | .omitted => false

private def ParsedSourceItem.unit? (ordinal : Nat) : ParsedSourceItem → Option (Nat × Stmt × Json)
  | .executable statement syntaxNode => some (ordinal, statement, syntaxNode)
  | .localExport _ | .omitted => none

private partial def callableOffsets (j : Json) (offsets : Array (Option Nat)) :
    Array (Option Nat) :=
  let offsets :=
    if (j.getObjVal? "opaqueCaptures").isOk then offsets.push (j.getObjValAs? Nat "start").toOption
    else offsets
  (Lower.children j).foldl (fun acc child => callableOffsets child acc) offsets

/-- Give callables lexical ordinals within the original unit. Template sites use a
separate namespace. Both identities survive printing and reparsing. -/
private partial def annotateSites (unit : Nat) (parts : Std.HashMap Nat Nat)
    (scripts : Std.HashSet (Nat × Nat)) : Json → StateM Numbering Json
  | .obj fields => do
    let site ←
      if (fields.get? "opaqueCaptures").isSome then
        do
          let fallback := (← get).callable
          modify fun s => { s with callable := fallback + 1 }
          let offset := (fields.get? "start").bind (·.getNat?.toOption)
          let part := (offset.bind (fun offset => parts[offset]?)).getD fallback
          pure
              [(":opaque-unit", Lean.toJson unit), (":opaque-part", Lean.toJson part),
                (":opaque-scripted", Lean.toJson (scripts.contains (unit, part)))]
      else
        pure []
    let template ←
      if fields.get? "type" == some (.str "TaggedTemplateExpression") then
        do
          let q :=
            ((fields.get? "quasi").bind (fun q => (q.getObjVal? "quasis").toOption)).getD .null
          let t : TemplateSite := ⟨unit, (← get).templates.size, q⟩
          modify fun s => { s with templates := s.templates.push t }
          pure [(":template-binding", Json.str t.binding)]
      else
        pure []
    let children ←
      fields.toList.mapM fun (key, child) => do
          pure (key, ← annotateSites unit parts scripts child)
    return Json.mkObj (children ++ site ++ template)
  | .arr children => return .arr (← children.mapM (annotateSites unit parts scripts))
  | j => pure j

private def numberSites (unit : Nat) (scripts : Std.HashSet (Nat × Nat)) (j : Json) :
    Json × Numbering :=
  let offsets := callableOffsets j #[]
  let starts := (offsets.toList.filterMap id).mergeSort
  let parts := Std.HashMap.ofList (starts.zipIdx.map fun (offset, index) => (offset, index + 1))
  -- Compact AST fixtures without ranges still have a deterministic traversal.
  let parts := if parts.size == offsets.size then parts else {}
  (annotateSites unit parts scripts j).run {}

private structure Parsed (nodeCount : Nat) where
  imports : List Import
  exports : List (Name × Name)
  sourceItems : Array ParsedSourceItem
  templates : List TemplateSite

private def Parsed.units {nodeCount : Nat} (p : Parsed nodeCount) : List (Nat × Stmt × Json) :=
  (List.range nodeCount).filterMap fun ordinal =>
    (p.sourceItems[ordinal]?).bind fun item => item.unit? ordinal

private def Parsed.coreSourceItems {nodeCount : Nat} (p : Parsed nodeCount) : List SourceItem :=
  (List.range nodeCount).map fun ordinal =>
    (p.sourceItems[ordinal]?).getD .omitted |>.sourceItem ordinal

private theorem Parsed.coreSourceItems_ordinals {nodeCount : Nat} (p : Parsed nodeCount) :
    p.coreSourceItems.map SourceItem.ordinal = List.range nodeCount := by
  simp only [Parsed.coreSourceItems, List.map_map]
  have ordinalFn :
    SourceItem.ordinal ∘
        (fun ordinal => (p.sourceItems[ordinal]?.getD .omitted).sourceItem ordinal) =
      id := by
    funext ordinal
    exact ParsedSourceItem.sourceItem_ordinal ordinal _
  rw [ordinalFn]
  simp

private def Parsed.body {nodeCount : Nat} (p : Parsed nodeCount) (base : Nat) : List (Nat × Stmt) :=
  (p.coreSourceItems.filterMap SourceItem.executable?).map fun (ordinal, statement) =>
    (base + ordinal, statement)

/-- A structural proof retains non-pivots. A recorded replay retains a unit
with a part-zero script and can script individual callable bodies. -/
private inductive BodyFormat where
  | linked
  | structural (pivots : Std.HashSet Nat) (location : LinkedDeployment.Access) (fixed : Json)
  | recorded (scripts : Std.HashSet (Nat × Nat))

private def BodyFormat.scripts : BodyFormat → Std.HashSet (Nat × Nat)
  | .recorded scripts => scripts
  | _ => {}

private def BodyFormat.lower (format : BodyFormat) (profile : HostProfile) (imports : List Name)
    (origin : Nat) (providerSite : Option (String × Nat)) (j : Json) (exported : Bool) :
    Except StructuralRefusal (Stmt × List Name × List TemplateSite) := do
  let retain (loaderNames : Option (List Name)) := do
    let s ←
      match format with
      | .structural _ _ _ =>
        OpaqueInput.unitStructural origin j loaderNames
      | _ =>
        (OpaqueInput.unit origin j loaderNames).mapError StructuralRefusal.unclassified
    return (s, (if exported then (Lower.topDefs j).eraseDups else []), [])
  match format with
  | .structural pivots location _ =>
    if FromESTree.containsUnsupportedSparseArrayHole j then
      throw (.coded .unsupportedSparseArrayHole "unsupported array hole")
    unless pivots.contains origin do
      -- Generators need their own intrinsic and fresh prototype.
      if Lower.typ j == "FunctionDeclaration" && Lower.flag j "generator" then
        if let some why := Lower.outsideModel false j then
          throw (.unclassified why)
      else
        let names :=
          match providerSite with
          | none => location.entryAccess.unitNames[origin]?
          | some (name, ordinal) => (location.forModule name).unitNames[ordinal]?
        return ← retain names
  | .recorded scripts =>
    if scripts.contains (origin, 0) then
      return ← retain none
  | .linked =>
    pure ()
  let (ast, sites) := numberSites origin format.scripts j
  if exported then
    let (bindings, s) ←
      (FromESTree.linkedDeclaration imports ast profile).mapError StructuralRefusal.unclassified
    return (s, bindings, sites.templates.toList)
  return (← (FromESTree.linkedUnit imports ast profile).mapError StructuralRefusal.unclassified, [],
      sites.templates.toList)

/-- A named default declaration exports its module binding. A retained named
declaration uses the same checked captures and execution as the corresponding
ordinary declaration. Anonymous defaults still require concrete lowering. -/
private def BodyFormat.lowerDefault (format : BodyFormat) (profile : HostProfile)
    (imports : List Name) (origin : Nat) (providerSite : Option (String × Nat)) (j : Json) :
    Except StructuralRefusal (Stmt × Name × List TemplateSite) := do
  if let .structural pivots _ _ := format then
    unless pivots.contains origin do
      unless
        ["FunctionDeclaration", "ClassDeclaration"].contains (Lower.typ j) &&
          Lower.typ (Lower.fld j "id") == "Identifier" do
        throw (.unclassified "unsupported opaque default export")
      let name ←
        (j.getObjVal? "id" >>= FromESTree.moduleBinding).mapError StructuralRefusal.unclassified
      let (s, bindings, sites) ← format.lower profile imports origin providerSite j true
      unless bindings == [name] do
        throw (.unclassified "expected one named default binding")
      return (s, name, sites)
  if format.scripts.contains (origin, 0) then
    throw (.unclassified "unsupported opaque default export")
  let (ast, sites) := numberSites origin format.scripts j
  let (name, s) ←
    (FromESTree.linkedDefaultExport imports ast profile).mapError StructuralRefusal.unclassified
  return (s, name, sites.templates.toList)

/-- Lower one linked statement with the parser's callable and template
numbering. The census uses this without linking a deployment. -/
def lowerLinkedStatement (profile : HostProfile) (imports : List Name) (ordinal : Nat) (ast : Json)
    (defaultExport namedExport : Bool) : Except String Stmt := do
  if defaultExport then
    return (←
          (BodyFormat.lowerDefault .linked profile imports ordinal none ast).mapError
              StructuralRefusal.reason).1
  return (←
        (BodyFormat.lower .linked profile imports ordinal none ast namedExport).mapError
            StructuralRefusal.reason).1

/-- A retained unit can observe every live export through a namespace, even when the
source spells only a method call (the receiver is the namespace). Give opaque code
all those cells in a stable order. The source URL and local spelling are part of
each slot name; neither the namespace nor its methods are rewritten in the files. -/
private def namespaceImport (fixed : Json) (path : String) (j sp : Json) :
    Except String (Import × (Name × List Name)) := do
  let source ← ModuleInput.importSource path j
  let .ok file := fixed.getObjVal? source | throw "namespace import requires a fixed provider"
  let localName ← FromESTree.identifier (← sp.getObjVal? "local")
  let mut exports := #[]
  for node in ← ESTreeUnits.nodes file do
    match Lower.typ node with
    | "ExportNamedDeclaration" =>
      let decl := Lower.fld node "declaration"
      unless Lower.isNull (Lower.fld node "source") && !Lower.isNull decl do
        throw "namespace provider requires inline named exports"
      exports := exports ++ (Lower.topDefs decl).toArray
    | "ExportAllDeclaration" | "ExportDefaultDeclaration" =>
      throw "namespace provider requires inline named exports"
    | _ =>
      pure ()
  if exports.isEmpty then
    throw "namespace provider has no named exports"
  let names := exports.toList.mergeSort.map fun x => (x, s!":namespace:{source}:{localName}:{x}")
  return (⟨source, names⟩, (localName, names.map Prod.snd))

private def namespaceCaptures (captures : List (Name × List Name)) (stmt : Stmt) :
    Except String Stmt := do
  if captures.isEmpty then
    return stmt
  match stmt with
  | .opaque code decls funs free run =>
    return .opaque code decls funs (free.flatMap fun x => (captures.lookup x).getD [x]) run
  | _ =>
    if stmt.free.any (fun x => (captures.lookup x).isSome) then
      throw "namespace access requires a retained opaque unit"
    return stmt

private def parse (format : BodyFormat) (profile : HostProfile) (path : String) (nodes : Array Json)
    (base : Nat := 0) (origins : Option (Array Nat) := none) (provider : Option String := none) :
    Except StructuralRefusal (Parsed nodes.size) := do
  let mut imports := #[]
  let mut namespaces := #[]
  for j in nodes do
    if (← (typ j).mapError StructuralRefusal.unclassified) == "ImportDeclaration" then
      if let .structural _ _ fixed := format then
        if let #[sp] := Lower.items (Lower.fld j "specifiers") then
          if Lower.typ sp == "ImportNamespaceSpecifier" then
            let (req, capture) ←
              (namespaceImport fixed path j sp).mapError StructuralRefusal.unclassified
            imports := imports.push req
            namespaces := namespaces.push capture
            continue
      imports :=
        imports.push (← (ModuleInput.importDecl path j).mapError StructuralRefusal.unclassified)
  let names :=
    namespaces.toList.map Prod.fst ++ imports.toList.flatMap fun i => i.names.map Prod.snd
  let mut exports := #[]
  let mut sourceItems : Array ParsedSourceItem := Array.replicate nodes.size .omitted
  let mut templates := #[]
  for (j, i) in nodes.zipIdx do
    let origin : Except StructuralRefusal Nat :=
      match origins with
      | none => pure (base + i)
      | some ids =>
        match ids[(sourceItems.toList.filter ParsedSourceItem.executable?).length]? with
        | some id => pure id
        | none => throw (.unclassified "emitted body length differs from the placement")
    let decode (ast : Json) (exported : Bool) :
      Except StructuralRefusal (Stmt × List Name × List TemplateSite) := do
      let unitOrigin ← origin
      format.lower profile names unitOrigin
          (provider.map fun referrer => (referrer, unitOrigin - base)) ast exported
    match ← (typ j).mapError StructuralRefusal.unclassified with
    | "ImportDeclaration" =>
      sourceItems := sourceItems.set! i .omitted
    -- Neither statement changes the modeled state, so neither gets a unit.
    | "EmptyStatement" | "DebuggerStatement" =>
      sourceItems := sourceItems.set! i .omitted
    | "ExportNamedDeclaration" =>
      (ModuleInput.noAttributes j).mapError StructuralRefusal.unclassified
      unless (← (j.getObjVal? "source").mapError StructuralRefusal.unclassified) == .null do
        throw (.unclassified "unsupported re-export from another module")
      let declaration ← (j.getObjVal? "declaration").mapError StructuralRefusal.unclassified
      let specs ← (j.getObjValAs? (Array Json) "specifiers").mapError StructuralRefusal.unclassified
      if declaration == .null then
        let names ←
          specs.mapM fun sp => (FromESTree.exportSpec sp).mapError StructuralRefusal.unclassified
        exports := exports ++ names
        sourceItems := sourceItems.set! i (.localExport names.toList)
      else
        unless specs.isEmpty do
          throw (.unclassified "expected an inline declaration export")
        let (s, names, sites) ← decode declaration true
        let s ← (namespaceCaptures namespaces.toList s).mapError StructuralRefusal.unclassified
        templates := templates ++ sites.toArray
        sourceItems := sourceItems.set! i (.executable s declaration)
        for name in names do
          exports := exports.push (name, name)
    | "ExportDefaultDeclaration" =>
      let unitOrigin ← origin
      let lowered :=
        format.lowerDefault profile names unitOrigin
          (provider.map fun referrer => (referrer, unitOrigin - base))
          (← (j.getObjVal? "declaration").mapError StructuralRefusal.unclassified)
      let (s, name, sites) ←
        lowered.mapError (StructuralRefusal.mapReason fun why => s!"statement {i}: {why}")
      let s ← (namespaceCaptures namespaces.toList s).mapError StructuralRefusal.unclassified
      templates := templates ++ sites.toArray
      sourceItems := sourceItems.set! i (.executable s j)
      exports := exports.push ("default", name)
    | _ =>
      let (s, _, sites) ←
        (decode j false).mapError (StructuralRefusal.mapReason fun why => s!"statement {i}: {why}")
      let s ← (namespaceCaptures namespaces.toList s).mapError StructuralRefusal.unclassified
      templates := templates ++ sites.toArray
      sourceItems := sourceItems.set! i (.executable s j)
  return {
      imports := imports.toList
      exports := exports.toList
      sourceItems := sourceItems
      templates := templates.toList }

/-- Lower linked output syntax using its original executable ordinals. Imports
and exports come from the syntax, and the body must account for every ordinal
without needing template installation. -/
def parseLinkedModuleWithOrdinals (name : String) (nodes : Array Json) (ids : List Nat) :
    Except String Module := do
  let parsed ←
    (parse .linked .empty name nodes 0 (some ids.toArray)).mapError StructuralRefusal.reason
  unless parsed.units.length == ids.length && parsed.templates.isEmpty do
    throw s!"output body of {name} differs from the constructed layout"
  return {
      name
      imports := parsed.imports
      body := ids.zip (parsed.units.map fun (_, stmt, _) => stmt)
      exports := parsed.exports }

private def Parsed.numbered {nodeCount : Nat} (p : Parsed nodeCount) (name : String) (base : Nat) :
    Module
    where
  name := name
  imports := p.imports
  body := p.body base
  exports := p.exports

private def readProviders (format : BodyFormat) (profile : HostProfile)
    (providers : List (String × Json)) (base : Nat) (roots : List String := []) :
    Except StructuralRefusal (List Module × List TemplateSite) := do
  let mut templates := #[]
  let names := roots ++ providers.map Prod.fst
  let mut modules := #[]
  let mut offset := base
  for (name, file) in providers do
    let nodes ←
      (ESTreeUnits.nodes file).mapError fun why => StructuralRefusal.unclassified s!"{name}: {why}"
    let p ←
      (parse format profile name nodes offset (provider := some name)).mapError
          (StructuralRefusal.mapReason fun why => s!"{name}: {why}")
    for req in p.imports do
      unless names.contains req.source do
        throw (.unclassified s!"{name}: missing provider dependency {req.source}")
    templates := templates ++ p.templates.toArray
    modules := modules.push (p.numbered name offset)
    offset := offset + nodes.size
  return (modules.toList, templates.toList)

private def readSplit (format : BodyFormat) (profile : HostProfile) (files : Json)
    (P : EsmSplit.Plan) (providers : List String) {nodeCount : Nat} (original : Parsed nodeCount)
    (originalSyntax : Std.HashMap Nat Json) (m : EsmSplit.Module) (base : Nat := 0)
    (provider : Option String := none) : Except StructuralRefusal (Module × List TemplateSite) := do
  let file ← (files.getObjVal? m.name).mapError StructuralRefusal.unclassified
  let nodes ← (ESTreeUnits.nodes file).mapError StructuralRefusal.unclassified
  let placed := m.stmts.filterMap fun i => originalSyntax[i]?.map (base + i, ·)
  let p ← parse format profile m.name nodes base (some (placed.map Prod.fst).toArray) provider
  let (external, glue) := p.imports.partition fun req => providers.contains req.source
  if m.name == P.entry then
    unless
      p.imports.take original.imports.length == original.imports && external == original.imports do
      throw (.unclassified "entry requests for providers differ from the original entry")
  else
    for req in external do
      unless
        !req.names.isEmpty &&
          original.imports.any
            (fun i => i.source == req.source && req.names.all i.names.contains) do
        throw (.unclassified s!"unexpected provider import from {req.source}")
  let exportsOf (md : EsmSplit.Module) :=
    md.exports ++ (if md.name == P.entry then original.exports else [])
  let expectedGlue : List Import :=
    m.imports.map fun (source, names) =>
      let exports := ((P.find source).map exportsOf).getD []
      { source,
        names := names.map fun localName => (EsmSplit.exportNameFor exports localName, localName) }
  unless glue = expectedGlue do
    throw (.unclassified "module's glue imports differ from the plan")
  unless p.exports.Perm (exportsOf m) do
    throw (.unclassified "module's named exports differ from the plan")
  unless p.units.length == placed.length do
    throw (.unclassified "emitted body length differs from the placement")
  let body ←
    (p.units.zip placed).mapM fun ((_, actual, ast), (id, expected)) => do
        unless unitSyntax ast == unitSyntax expected do
          throw (.unclassified s!"emitted syntax differs from original unit {id}")
        return (id, actual)
  return ({ name := m.name, imports := p.imports, body, exports := p.exports }, p.templates)

/-- Only the tag evaluator can name these bindings. The synthetic provider allocates each
pair once per linked realm, before any consumer can call the tag, including a hoisted
function invoked from an import cycle. Initializers contain only primitive literals and
intrinsic array allocation. Sites with identical strings still receive distinct bindings. -/
private def installTemplates (p : Program) (sites : List TemplateSite) : Except String Program := do
  if sites.isEmpty then
    return p
  let provider := ":corejs:template-objects"
  if (p.find provider).isSome then
    throw "reserved template provider name"
  let used :=
    Std.HashSet.ofList (p.modules.flatMap fun m => m.body.flatMap fun (_, stmt) => stmt.free)
  let live :=
    (sites.filter fun site => used.contains site.binding).mergeSort fun a b =>
      decide (a.unit < b.unit || (a.unit == b.unit && a.part ≤ b.part))
  if live.isEmpty then
    return p
  let names := live.map TemplateSite.binding
  let table := Std.HashSet.ofList names
  let mut body := #[]
  for site in live do
    let segments ← site.quasis.getArr?
    if segments.isEmpty then
      throw "empty tagged template"
    let mut raw := #[]
    let mut cooked := #[]
    for q in segments do
      if
          ["rawWellFormed", "cookedWellFormed"].any
            (fun key => (q.getObjValAs? Bool key).toOption == some false) then
        throw "tagged template contains an isolated UTF-16 surrogate"
      let value ← q.getObjVal? "value"
      raw := raw.push (.str (← field value "raw"))
      let text ← value.getObjVal? "cooked"
      let segment ←
        if text == .null then
          pure Expr.undef
        else
          Expr.str <$> text.getStr?
      cooked := cooked.push segment
    let array :=
      Expr.prim (.templateArray true) (.prim (.templateArray false) raw.toList :: cooked.toList)
    body := body.push (p.regions + body.size, Stmt.decl .const site.binding array)
  let registry : Module := ⟨provider, [], body.toList, names.map fun name => (name, name)⟩
  let modules :=
    p.modules.map fun m =>
      let needs := (m.body.flatMap fun (_, stmt) => stmt.free.filter table.contains).eraseDups
      if needs.isEmpty then m
      else
        { m with
          imports :=
            { source := provider, names := needs.map fun name => (name, name) } :: m.imports }
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
    unless (m.exports.map Prod.fst).Nodup do
      throw s!"{m.name}: repeated exported name"
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
  unless original.Perm candidate do
    throw (EntryExports.mismatch original candidate)
  -- Placement is checked in decodePair. Provider ordinals follow the
  -- source and use disjoint ranges, so each ordinal has only one linked unit.
  let bs := Std.HashMap.ofList (unitsOf b)
  for (id, s, env) in unitsOf a do
    let some (s', env') := bs[id]? | throw s!"missing linked unit {id}"
    for x in (s.free ++ s'.free).eraseDups do
      let some cell :=
        env.vars.lookup x | throw s!"original unit {id} has an unbound reference to {x}"
      unless env'.vars.lookup x == some cell do
        throw s!"unit {id}: reference to {x} resolves to a different linked cell"

private structure Fragment where
  sourceModule : Module
  emitted : List Module
  unitCount : Nat
  sourceTemplates : List TemplateSite
  emittedTemplates : List TemplateSite

structure DecodedDeployment where
  before : Program
  after : Program
  assignment : List UnitPlacement

def DecodedDeployment.programs (decoded : DecodedDeployment) : Program × Program :=
  (decoded.before, decoded.after)

/-- Plans also number import and export lists. Keep only the ordinals present
in the original executable body when building the CoreJS assignment. -/
private def plannedPlacements (plan : EsmSplit.Plan) (source : Module) (base : Nat) :
    List UnitPlacement :=
  let ids := Std.HashSet.ofList (source.body.map Prod.fst)
  plan.mods.flatMap fun m =>
    m.stmts.filterMap fun ordinal =>
      let id := base + ordinal
      if ids.contains id then some (id, m.name) else none

private def decodeFragment (format : BodyFormat) (profile : HostProfile) (source files : Json)
    (P : EsmSplit.Plan) (sourcePath : String) (providerNames : List String) (base : Nat)
    (provider : Option String := none) : Except StructuralRefusal Fragment := do
  let fs := (← (files.getObj?).mapError StructuralRefusal.unclassified).toList.map Prod.fst
  (EsmSplit.validateModulePaths (P.entry :: fs ++ providerNames)).mapError
      StructuralRefusal.unclassified
  unless fs.Perm (P.mods.map (·.name)) && fs.contains P.entry do
    throw (.unclassified "output filenames differ from the plan")
  unless !fs.any providerNames.contains do
    throw (.unclassified "providers overlap emitted modules")
  let nodes ← (ESTreeUnits.nodes source).mapError StructuralRefusal.unclassified
  unless (P.mods.flatMap (·.stmts)).Perm (List.range nodes.size) do
    throw (.unclassified "placement must cover every original unit once")
  let some entry := P.find P.entry | throw (.unclassified "missing entry placement")
  for (j, i) in nodes.zipIdx do
    if
        ["ImportDeclaration", "ExportNamedDeclaration", "ExportDefaultDeclaration"].contains
          (← (typ j).mapError StructuralRefusal.unclassified) then
      unless entry.stmts.contains i do
        throw (.unclassified s!"original import or export unit {i} must stay in the entry")
  let original ← parse format profile sourcePath nodes base (provider := provider)
  if let .structural pivots _ _ := format then
    let bodyIds := Std.HashSet.ofList (original.units.map fun u => base + u.1)
    for pivot in pivots do
      unless bodyIds.contains pivot do
        throw (.unclassified s!"pivot {pivot} is not an executable source unit")
  for req in original.imports do
    unless providerNames.contains req.source do
      throw (.unclassified s!"missing original dependency {req.source}")
  let originalSyntax := Std.HashMap.ofList (original.units.map fun (i, _, ast) => (i, ast))
  let split ←
    P.mods.mapM fun m =>
        (readSplit format profile files P providerNames original originalSyntax m base
              provider).mapError
          (StructuralRefusal.mapReason fun why => s!"{m.name}: {why}")
  return ⟨original.numbered P.entry base, split.map Prod.fst, nodes.size, original.templates,
      split.flatMap Prod.snd⟩

/-- Importer modules decoded from source and emitted files, before provider linking.
The caller must supply and check the provider modules and their behavior. -/
structure StructuralImporterFragment where
  source : Module
  emitted : List Module
  assignment : List UnitPlacement
  nextIdentity : Nat

/-- Proof-facing access to the same structural fragment decoder used by the
production path. Template sites need the deployment's shared registry and are
therefore refused by this standalone wrapper. No provider body is synthesized. -/
def decodeStructuralImporterFragment (source files : Json) (P : EsmSplit.Plan)
    (providerNames : List String) (pivots : List Nat) (profile : HostProfile := .empty) :
    Except StructuralRefusal StructuralImporterFragment := do
  let format : BodyFormat := .structural (Std.HashSet.ofList pivots) {} .null
  let fragment ← decodeFragment format profile source files P "bundle.mjs" providerNames 0
  unless fragment.sourceTemplates.isEmpty && fragment.emittedTemplates.isEmpty do
    throw (.unclassified "standalone importer fragment does not install template sites")
  return ⟨fragment.sourceModule, fragment.emitted, plannedPlacements P fragment.sourceModule 0,
      fragment.unitCount⟩

private def decode (format : BodyFormat) (source providers files : Json) (P : EsmSplit.Plan)
    (profile : HostProfile) : Except StructuralRefusal DecodedDeployment := do
  let ps := (← (providers.getObj?).mapError StructuralRefusal.unclassified).toList
  let fragment ← decodeFragment format profile source files P "bundle.mjs" (ps.map Prod.fst) 0
  let (shared, providerSites) ← readProviders format profile ps fragment.unitCount
  let before ←
    (installTemplates { entry := P.entry, modules := fragment.sourceModule :: shared }
            (fragment.sourceTemplates ++ providerSites)).mapError
        StructuralRefusal.unclassified
  let after ←
    (installTemplates { entry := P.entry, modules := fragment.emitted ++ shared }
            (fragment.emittedTemplates ++ providerSites)).mapError
        StructuralRefusal.unclassified
  let (before, after) ←
    match format with
    | .linked | .recorded _ =>
      pure (before, after)
    | .structural _ location _ =>
      pure
          (← (location.attach before).mapError StructuralRefusal.unclassified,
            ← (location.attach after).mapError StructuralRefusal.unclassified)
  (checkScopes before after).mapError StructuralRefusal.unclassified
  let assignment :=
    plannedPlacements P fragment.sourceModule 0 ++ shared.flatMap Module.unitPlacements
  return ⟨before, after, assignment⟩

/-- The original entry is at the virtual root. Provider and emitted file keys are canonical
relative filenames in that same root, with no overlap. Plan ordinals refer to the full
source unit array, including imports and export lists. The two programs share the
providers, whose stable identities start after that array. The assignment comes from
the requested plan and executable source units, including the fixed providers. -/
def decodeSingleton (source providers files : Json) (P : EsmSplit.Plan)
    (profile : HostProfile := .empty) : Except String DecodedDeployment :=
  (decode .linked source providers files P profile).mapError StructuralRefusal.reason

def decodePair (source providers files : Json) (P : EsmSplit.Plan)
    (profile : HostProfile := .empty) : Except String (Program × Program) :=
  DecodedDeployment.programs <$> decodeSingleton source providers files P profile

/-! The one-pass decoded source shape is available before construction-specific
coverage is enforced. Callers that need to distinguish an unsupported source
slot from a different route can inspect these nodes and classifications, then
check construction coverage without parsing the input again. -/

structure DecodedSource where
  nodes : Array Json
  program : Program
  source : Module
  singleton : program.modules = [source]
  localExports : List EsmSplit.LocalExportUnit
  hasTemplates : Bool
  sourceItems : List SourceItem
  localExportProjection : localExports = sourceItems.filterMap SourceItem.localExport?
  executableProjection : source.body = sourceItems.filterMap SourceItem.executable?
  sourceOrdinals : sourceItems.map SourceItem.ordinal = List.range nodes.size
  sourceProjection :
    List.Perm (sourceItems.map SourceItem.ordinal)
      (((source.body.map Prod.fst ++ localExports.map (·.ordinal)) ++
        sourceItems.filterMap SourceItem.omitted?))

/-- Classify already extracted nodes. Callers requiring authenticated source
must obtain them from `ESTreeUnits.checkedNodes`. -/
def decodeSingletonSourceNodes (nodes : Array Json) (entry : String) (importer := "bundle.mjs") :
    Except String DecodedSource := do
  let original ← (parse .linked .empty importer nodes 0).mapError StructuralRefusal.reason
  let source := original.numbered entry 0
  have sourceOrdinals : original.coreSourceItems.map SourceItem.ordinal = List.range nodes.size :=
    original.coreSourceItems_ordinals
  have executableProjection :
    source.body = original.coreSourceItems.filterMap SourceItem.executable? := by
    simp [source, Parsed.numbered, Parsed.body, Parsed.coreSourceItems]
  let localExports := original.coreSourceItems.filterMap SourceItem.localExport?
  let sourceItems := original.coreSourceItems
  have localExportProjection : localExports = sourceItems.filterMap SourceItem.localExport? := rfl
  have sourceProjection :
    List.Perm (sourceItems.map SourceItem.ordinal)
      (((source.body.map Prod.fst ++ localExports.map (·.ordinal)) ++
        sourceItems.filterMap SourceItem.omitted?)) := by
    simpa [executableProjection, localExportProjection] using
      sourceItemOrdinalsPermProjections sourceItems
  return {
      nodes := nodes
      program := { entry, modules := [source] }
      source := source
      singleton := rfl
      localExports := localExports
      hasTemplates := !original.templates.isEmpty
      sourceItems := sourceItems
      localExportProjection := localExportProjection
      executableProjection := executableProjection
      sourceOrdinals := sourceOrdinals
      sourceProjection := sourceProjection }

/-- Lower an import-free original in one pass, retaining each actual standalone
local export list's original ordinal and ordered `(public, local)` names. Inline
and default exports remain in the module export table. -/
def decodeSingletonSourceWithLocalExports (source : Json) (entry : String) :
    Except String (Program × List EsmSplit.LocalExportUnit) := do
  let decoded ← decodeSingletonSourceNodes (← ESTreeUnits.nodes source) entry
  unless decoded.source.imports.isEmpty do
    throw "checked construction requires an import-free original"
  unless !decoded.hasTemplates do
    throw "checked construction does not install template objects"
  return (decoded.program, decoded.localExports)

/-- Project the program from the same source decoder for rules that do not use
standalone export-unit identities. -/
def decodeSingletonSource (source : Json) (entry : String) : Except String Program :=
  Prod.fst <$> decodeSingletonSourceWithLocalExports source entry

/-- Source-only lowering for a constructor that keeps non-pivots as opaque
units. It does not load external modules or assign any location capabilities. -/
def decodeRetainedSource (nodes : Array Json) (entry : String) (pivots : List Nat) :
    Except String Module := do
  let parsed ← (parse (.structural (Std.HashSet.ofList pivots) {} .null) .empty entry nodes 0).mapError
    StructuralRefusal.reason
  unless parsed.imports.isEmpty && parsed.templates.isEmpty do
    throw "retained source requires an import-free module without template sites"
  return parsed.numbered entry 0

/-- Experimental input for a structural schedule proof. The body of each returned module retains the
original source ordinals, including when an emitted module has different positions. Providers use
disjoint ordinals. Selected pivots have their actual CoreJS statements on both sides. Retained statements
are decoded from their own syntax and carry the same source code ID between layouts.
This verifies placement, unchanged unit syntax, import/export links and resolved captures. A proof
of the order change and correspondence with the emitted source files are separate obligations. -/
def decodeStructuralSingleton (source providers files : Json) (P : EsmSplit.Plan)
    (pivots : List Nat) (profile : HostProfile := .empty)
    (deployment : Option LinkedDeployment.Input := none) :
    Except StructuralRefusal DecodedDeployment := do
  let location ←
    deployment.mapM fun d =>
        (LinkedDeployment.check d source providers P).mapError StructuralRefusal.unclassified
  decode
      (.structural (Std.HashSet.ofList pivots) (location.getD {})
        (if deployment.isSome then providers else .null))
      source providers files P profile

def decodeStructuralPair (source providers files : Json) (P : EsmSplit.Plan) (pivots : List Nat)
    (profile : HostProfile := .empty) (deployment : Option LinkedDeployment.Input := none) :
    Except StructuralRefusal (Program × Program) :=
  DecodedDeployment.programs <$>
    decodeStructuralSingleton source providers files P pivots profile deployment

/-- One original path and the reparsed output files of its plan. Filenames
in both plans live in the same deployment root. -/
structure StaticSplit where
  source : Json
  files : Json
  plan : EsmSplit.Plan

/-- Lower and link an unsplit graph for the recorded Node world. Source ordinals
include the entry's import and export syntax. Provider ordinals follow the entry. -/
def program (source providers : Json) (scripts : Std.HashSet (Nat × Nat))
    (entry : String := "main") : Except String Program := do
  let nodes ← (ESTreeUnits.nodes source).mapError id
  let format := BodyFormat.recorded scripts
  let original ← (parse format .nodeRecording entry nodes).mapError StructuralRefusal.reason
  let ps := (← providers.getObj?).toList
  for req in original.imports do
    unless ps.any (·.1 == req.source) do
      throw s!"missing original dependency {req.source}"
  let (shared, providerSites) ←
    (readProviders format .nodeRecording ps nodes.size [entry]).mapError StructuralRefusal.reason
  let p ←
    installTemplates { entry, modules := original.numbered entry 0 :: shared }
        (original.templates ++ providerSites)
  let _ ← scopes p
  return p

/-- Public paths are the original module records, including unchanged dependencies.
The candidate can also contain private files and model-only helper modules. -/
def publicPaths (parts : List StaticSplit) (shared : Json) : Except String (List String) := do
  return parts.map (·.plan.entry) ++ (← shared.getObj?).toList.map Prod.fst

/-- Decode all placements in one namespace. IDs follow the input list and the
sorted fixed-file map, independently of either graph's evaluation order. In
structural mode each fragment's hints and capabilities use its local ordinals. -/
def decodeDeployment (entry : String) (parts : List StaticSplit) (shared : Json)
    (inputs : Option LinkedDeployment.Input) (profile : HostProfile) (hints : String → List Nat) :
    Except StructuralRefusal DecodedDeployment := do
  let fixedFiles := (← (shared.getObj?).mapError StructuralRefusal.unclassified).toList
  let originals ← (publicPaths parts shared).mapError StructuralRefusal.unclassified
  let outputs := parts.flatMap fun part => part.plan.mods.map (·.name)
  let all := outputs ++ fixedFiles.map Prod.fst
  (EsmSplit.validateModulePaths (entry :: originals ++ all)).mapError StructuralRefusal.unclassified
  unless originals.Nodup && all.Nodup do
    throw (.unclassified "joint output path collision")
  unless originals.contains entry do
    throw (.unclassified s!"missing deployment root: {entry}")
  let access ←
    match inputs with
    | some snapshot =>
      do
        unless snapshot.entry == entry do
          throw (.unclassified "deployment root differs")
        (LinkedDeployment.checkGraph snapshot (parts.map fun p => (p.source, p.plan))
                shared).mapError
            StructuralRefusal.unclassified
    | none =>
      do
        for (path, ast) in parts.map (fun p => (p.plan.entry, p.source)) ++ fixedFiles do
          unless
            (← (ModuleLocation.uses path ast).mapError StructuralRefusal.unclassified).isEmpty do
            throw (.unclassified s!"{path}: joint static split requires location-independent units")
        pure {}
  let format (ids : List Nat) : BodyFormat :=
    if inputs.isSome then .structural (Std.HashSet.ofList ids) access shared else .linked
  let mut fragments := #[]
  let mut planned := #[]
  let mut base := 0
  for part in parts do
    let name := part.plan.entry
    let f ←
      (decodeFragment (format ((hints name).map (base + ·))) profile part.source part.files
              part.plan name (originals.erase name) base (some name)).mapError
          (StructuralRefusal.mapReason fun why => s!"{name}: {why}")
    fragments := fragments.push f
    planned := planned ++ (plannedPlacements part.plan f.sourceModule base).toArray
    base := base + f.unitCount
  let (fixed, fixedSites) ←
    readProviders (format []) profile fixedFiles base (parts.map (·.plan.entry))
  let fs := fragments.toList
  let before ←
    (installTemplates ⟨entry, fs.map (·.sourceModule) ++ fixed⟩
            (fs.flatMap (·.sourceTemplates) ++ fixedSites)).mapError
        StructuralRefusal.unclassified
  let after ←
    (installTemplates ⟨entry, fs.flatMap (·.emitted) ++ fixed⟩
            (fs.flatMap (·.emittedTemplates) ++ fixedSites)).mapError
        StructuralRefusal.unclassified
  let before ← (access.attach before).mapError StructuralRefusal.unclassified
  let after ← (access.attach after).mapError StructuralRefusal.unclassified
  (checkScopes before after).mapError StructuralRefusal.unclassified
  return ⟨before, after, planned.toList ++ fixed.flatMap Module.unitPlacements⟩

end CoreJS.LinkedInput
