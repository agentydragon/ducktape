import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.ModuleInput
import EsmSplit.ESTreeUnits
import EsmSplit.Lower

/-!
Resolve ordinary star exports in an explicit finite DAG of parsed ESM files.
The projection is for linked readback and diagnostics. It neither rewrites JS
nor admits a deployment to the ordinary splitter.
-/
namespace CoreJS.StaticStarExports

open Lean (Json)
open EsmSplit

abbrev Origin := String × Name

private inductive Request where
  | named (edge : Import)
  | namespaceImport (source : String) (localName : Name)
  | indirect (source : String) (names : List (Name × Name))
  | star (source : String)
  deriving DecidableEq

private def Request.source : Request → String
  | .named edge => edge.source
  | .namespaceImport source _ | .indirect source _ | .star source => source

private structure Record where
  path : String
  nodes : Array Json
  requests : List (Nat × Request)
  explicit : List (Name × Name)
  locals : List Name

private def readRequest (path : String) (node : Json) : Except String Request := do
  let specs ← node.getObjValAs? (Array Json) "specifiers"
  if specs.any (fun sp => Lower.typ sp == "ImportNamespaceSpecifier") then
    let #[sp] := specs | throw "unsupported mixed namespace import"
    return .namespaceImport (← ModuleInput.importSource path node)
      (← sp.getObjVal? "local" >>= FromESTree.identifier)
  return .named (← ModuleInput.importDecl path node)

private def forwardingLocal (kind : String) (slot : Nat) (name : Name) : Name := s!":{kind}:{slot}:{name}"

private def readRecord (path : String) (ast : Json) : Except String Record := do
  unless ModuleInput.supportedFilename path do throw "unsupported module path"
  let nodes ← ESTreeUnits.checkedNodes ast (← ast.getObjValAs? String "sourceText")
  let mut requests := #[]
  let mut explicit := #[]
  let mut locals := nodes.toList.flatMap Lower.topDefs
  for (node, id) in nodes.zipIdx do
    match Lower.typ node with
    | "ImportDeclaration" => requests := requests.push (id, ← readRequest path node)
    | "ExportAllDeclaration" =>
      ModuleInput.noAttributes node
      unless (← node.getObjVal? "exported") == .null do throw "unsupported namespace re-export"
      let source ← node.getObjVal? "source" >>= FromESTree.stringLiteral >>= ModuleInput.resolve path
      requests := requests.push (id, .star source)
    | "ExportNamedDeclaration" =>
      ModuleInput.noAttributes node
      let source ← node.getObjVal? "source"
      if source != .null then
        let target ← FromESTree.stringLiteral source >>= ModuleInput.resolve path
        let names ← (← node.getObjValAs? (List Json) "specifiers").mapM FromESTree.exportSpec
        requests := requests.push (id, .indirect target names)
        for (publicName, _) in names do
          explicit := explicit.push (publicName, forwardingLocal "indirect" id publicName)
      else if (← node.getObjVal? "declaration") == .null then
        for sp in ← node.getObjValAs? (Array Json) "specifiers" do
          explicit := explicit.push (← FromESTree.exportSpec sp)
      else
        explicit := explicit ++ ((Lower.topDefs node).map fun x => (x, x)).toArray
    | "ExportDefaultDeclaration" =>
      let localName := (Lower.topDefs node).headD "default"
      explicit := explicit.push ("default", localName)
      locals := localName :: locals
    | _ => pure ()
  unless (explicit.toList.map Prod.fst).Nodup do throw "duplicate explicit export"
  return ⟨path, nodes, requests.toList, explicit.toList, locals⟩

private inductive Resolution where
  | found (origin : Origin) (starSlot : Option Nat)
  | ambiguous
  deriving DecidableEq

private abbrev Table := List (Name × Resolution)

private def requireName (table : Table) (source name : String) : Except String Origin :=
  match table.lookup name with
  | some (.found origin _) => pure origin
  | some .ambiguous => throw s!"{source}: ambiguous export {name}"
  | none => throw s!"{source}: missing export {name}"

private def mergeStar (row : Record) (slot : Nat) (table : Table) (exported : Name × Resolution) : Table :=
  let (name, candidate) := exported
  if name == "default" || (row.explicit.lookup name).isSome then table
  else
    let next := match candidate with
      | .found origin _ => Resolution.found origin (some slot)
      | .ambiguous => Resolution.ambiguous
    match table.lookup name with
    | none => table ++ [(name, next)]
    | some (.found previous _) =>
      match next with
      | .found origin _ => if origin == previous then table else conflict table name
      | .ambiguous => conflict table name
    | some .ambiguous => table
where
  conflict (rows : Table) (name : Name) := rows.map fun (key, old) =>
    (key, if key == name then .ambiguous else old)

private def resolveRecord (row : Record) (done : List (String × Table)) : Except String Table := do
  let mut imported : List (Name × Origin) := []
  for (slot, req) in row.requests do
    match req with
    | .named edge =>
      for (publicName, localName) in edge.names do
        let cell ← requireName ((done.lookup edge.source).getD []) edge.source publicName
        imported := imported ++ [(localName, cell)]
    | .indirect source names =>
      for (publicName, requested) in names do
        let cell ← requireName ((done.lookup source).getD []) source requested
        imported := imported ++ [(forwardingLocal "indirect" slot publicName, cell)]
    -- This is only the origin of the namespace binding, not a modeled namespace object.
    | .namespaceImport _ localName => imported := imported ++ [(localName, (row.path, localName))]
    | .star _ => pure ()
  let mut table ← row.explicit.mapM fun (name, localName) => do
    if let some origin := imported.lookup localName then
      return (name, Resolution.found origin none)
    unless row.locals.contains localName do throw s!"unbound export {name}"
    return (name, Resolution.found (row.path, localName) none)
  for (slot, req) in row.requests do
    if let .star source := req then
      table := ((done.lookup source).getD []).foldl (mergeStar row slot) table
  return table

/-- Resolved static metadata. The origins are source binding names rather than heap
addresses; only the actual CoreJS linker can check cells after body lowering. -/
structure Graph where
  private mk ::
  private records : List Record
  private tables : List (String × Table)

def Graph.origin (graph : Graph) (path name : String) : Except String Origin := do
  let some table := graph.tables.lookup path | throw s!"missing module {path}"
  requireName table path name

/-- Refuse cycles explicitly; no prefix of a cyclic graph is admitted.
Lookup uses normalized relative paths, with no host or package resolution. -/
def resolve (files : Json) : Except String Graph := do
  let records ← (← files.getObj?).toList.mapM fun (path, ast) =>
    (readRecord path ast).mapError (fun why => s!"{path}: {why}")
  let paths := records.map (·.path)
  for row in records do
    for (_, req) in row.requests do
      unless paths.contains req.source do throw s!"{row.path}: missing static provider {req.source}"
  let mut todo := records
  let mut done : List (String × Table) := []
  for _ in records do
    let some row := todo.find? (fun row => row.requests.all (fun (_, req) => (done.lookup req.source).isSome))
      | throw "cyclic static module records are unsupported"
    let table ← (resolveRecord row done).mapError (fun why => s!"{row.path}: {why}")
    done := (row.path, table) :: done
    todo := todo.filter (·.path != row.path)
  return ⟨records, done⟩

/-- Keep each header in request order, even when it forwards no names. -/
private def moduleRecord (graph : Graph) (row : Record) (body : List (Nat × Stmt)) : Module := Id.run do
  let table := (graph.tables.lookup row.path).getD []
  let mut imports := #[]
  let mut exports := row.explicit.toArray
  for (slot, req) in row.requests do
    match req with
    | .named edge => imports := imports.push edge
    | .namespaceImport source _ => imports := imports.push ⟨source, []⟩
    | .indirect source names =>
      imports := imports.push ⟨source, names.map fun (publicName, requested) =>
        (requested, forwardingLocal "indirect" slot publicName)⟩
    | .star source =>
      let mut names := #[]
      for (name, binding) in table do
        if let .found _ (some selected) := binding then
          if selected == slot then
            let localName := forwardingLocal "star" slot name
            names := names.push (name, localName)
            exports := exports.push (name, localName)
      imports := imports.push ⟨source, names.toList⟩
  return ⟨row.path, imports.toList, body, exports.toList⟩

/-- Project out remote re-export headers and preserve executable ordinals.
Every forwarded name imports from that immediate provider, so the existing
linker follows the chain and allocates no new binding cell. -/
private def lowerRecord (graph : Graph) (row : Record) (identify : Nat → Nat) : Except String Module := do
  if row.requests.any (fun (_, req) => match req with | .namespaceImport .. => true | _ => false) then
    throw "namespace object evaluation is unsupported"
  let mut nodes := #[]
  let mut ids := #[]
  for (node, id) in row.nodes.zipIdx do
    let kind := Lower.typ node
    if kind == "ExportAllDeclaration" ||
        (kind == "ExportNamedDeclaration" && !Lower.isNull (Lower.fld node "source")) then continue
    nodes := nodes.push node
    if !["ImportDeclaration", "EmptyStatement", "DebuggerStatement"].contains kind &&
        !(kind == "ExportNamedDeclaration" && Lower.isNull (Lower.fld node "declaration")) then
      ids := ids.push (identify id)
  let ordinary ← LinkedInput.parseLinkedModuleWithOrdinals row.path nodes ids.toList
  let expected := row.requests.filterMap fun (_, req) =>
    if let .named edge := req then some edge else none
  let localExports := row.explicit.filter fun (_, localName) => !localName.startsWith ":indirect:"
  unless ordinary.imports == expected && ordinary.exports == localExports do
    throw "ordinary decoder disagrees with module records"
  return moduleRecord graph row ordinary.body

/-- Build an empty-profile program for linked readback. In particular, successful
metadata resolution does not make namespace values or provider bodies executable. -/
def Graph.lower (graph : Graph) (entry : String)
    (identify : Option (String → Nat → Nat) := none) : Except String Program := do
  let mut modules := #[]
  let mut base := 0
  for row in graph.records do
    let m ← (lowerRecord graph row ((identify.map (· row.path)).getD (base + ·))).mapError (fun why => s!"{row.path}: {why}")
    modules := modules.push m
    base := base + row.nodes.size
  let program : Program := ⟨entry, modules.toList⟩
  unless decide (program.evalOrder.Perm (modules.toList.map (·.name))) do
    throw "static graph has missing or unreachable modules"
  return program

/-- Request-derived evaluation order without lowering bodies or realizing
namespace objects. -/
def Graph.order (graph : Graph) (entry : String) : Except String (List String) := do
  let modules := graph.records.map fun row => moduleRecord graph row []
  let program : Program := ⟨entry, modules⟩
  unless decide (program.evalOrder.Perm (modules.map (·.name))) do
    throw "static graph has missing or unreachable modules"
  return program.evalOrder

/-- The names that survive star resolution and their source bindings. -/
def Graph.origins (graph : Graph) (path : String) : Except String (List (Name × Origin)) := do
  let some table := graph.tables.lookup path | throw s!"missing module {path}"
  return table.filterMap fun (name, binding) =>
    if let .found origin _ := binding then some (name, origin) else none

/-- A view of a linked module's named exports. Its members retain addresses,
not the contents of the cells. JS namespace exotic object operations are
outside this view. -/
structure NamespaceView where
  private mk ::
  target : String
  members : EntryExports.Bindings

def NamespaceView.ofLinked (target : String) (linked : List (Module × Env)) :
    Except String NamespaceView := do
  return ⟨target, ← EntryExports.resolve target linked⟩

/-- Observe an export at the time of lookup; an uninitialized cell still throws. -/
def NamespaceView.read (view : NamespaceView) (name : Name) : M W Val :=
  match view.members.lookup name with
  | some cell => readCell cell
  | none => pure .undef

/-- A link-only view of namespace import bindings. Each import binding has
its own address and maps to its source module's shared view. This packet holds
addresses but does not make the namespace itself a CoreJS object. -/
structure NamespaceLinks where
  private mk ::
  private linked : List (Module × Env)
  private views : List (String × NamespaceView)
  private imports : List (Addr × String)

def NamespaceLinks.localCell (links : NamespaceLinks) (path name : String) :
    Except String Addr := do
  let some (_, scope) := links.linked.find? (fun (m, _) => m.name == path)
    | throw s!"missing module {path}"
  let some cell := scope.vars.lookup name | throw s!"{path}: missing binding {name}"
  return cell

def NamespaceLinks.exports (links : NamespaceLinks) (path : String) :
    Except String EntryExports.Bindings :=
  EntryExports.resolve path links.linked

def NamespaceLinks.viewAt (links : NamespaceLinks) (cell : Addr) : Except String NamespaceView := do
  let some target := links.imports.lookup cell | throw "binding is not a namespace import"
  let some view := links.views.lookup target | throw s!"missing namespace view for {target}"
  return view

/-- Allocate addresses from the parsed declaration and request records, and
run the ordinary scope/import resolver. This does not lower or run any
provider body, initialize hoists, or evaluate snapshots. Declaration shells
are internal and expose no executable program or store. -/
def Graph.linkNamespaceCells (graph : Graph) (entry : String) : Except String NamespaceLinks := do
  let modules := graph.records.zipIdx.map fun (row, id) =>
    let namespaceNames := row.requests.filterMap fun (_, req) =>
      if let .namespaceImport _ localName := req then some localName else none
    let names := (row.locals ++ namespaceNames).eraseDups
    let body := if names.isEmpty then [] else
      [(id, Stmt.opaque id (names.map fun name => (name, .const)) [] [] (.closures []))]
    moduleRecord graph row body
  let program : Program := ⟨entry, modules⟩
  unless decide (program.evalOrder.Perm (modules.map (·.name))) do
    throw "static graph has missing or unreachable modules"
  -- Only allocation and scopes run here. No hoist or body can make a value.
  let .ok frames _ := (declareTops program.tops : M World.probe _) (initialState World.probe program.regions)
    | throw "could not allocate namespace link cells"
  let linked ← (program.scopes frames program.loaded).mapError fun
    | .error _ reason | .unknown reason => reason
  let mut imports := #[]
  let mut views := #[]
  for row in graph.records do
    for (_, req) in row.requests do
      if let .namespaceImport target name := req then
        let cell ← (NamespaceLinks.mk linked [] []).localCell row.path name
        imports := imports.push (cell, target)
        unless views.any (·.1 == target) do
          views := views.push (target, ← NamespaceView.ofLinked target linked)
  return ⟨linked, views.toList, imports.toList⟩

end CoreJS.StaticStarExports
