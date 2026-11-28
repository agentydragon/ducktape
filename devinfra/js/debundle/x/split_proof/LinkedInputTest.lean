import EsmSplit.Cli
import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.DynamicImports
import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.HeritageCertificate
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedInputCensus
import EsmSplit.CoreJS.ModuleLocation
import EsmSplit.CoreJS.OpaqueInput
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.StaticImportMotion
import EsmSplit.CoreJS.StaticStarExports
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.CoreJS.WrapperLoader
import EsmSplit.LowerSplit.Input
import EsmSplit.LowerSplit.SliceCertificate

open Lean (Json toJson)

private def assets (j : Json) : Except String (String → Option CoreJS.ModuleLocation.Resource) := do
  let entries : List (String × CoreJS.ModuleLocation.Resource) ←
    (← j.getObj?).toList.mapM fun (name, r) => do
        return (name, ⟨← r.getObjValAs? String "url", ← r.getObjValAs? String "source"⟩)
  let table := Std.HashMap.ofList entries
  return fun name => table[name]?

private def imports (input : Json) : Except String Json := do
  let b ←
    CoreJS.DynamicImports.check (← input.getObjValAs? String "target")
        (← input.getObjValAs? (List String) "linked") (← input.getObjVal? "parsed")
        (← assets (← input.getObjVal? "before")) (← assets (← input.getObjVal? "after"))
  return Json.arr
      #[toJson b.scripts,
        .arr
          (b.requests.toArray.map fun edge =>
            .arr #[toJson edge.importer, toJson edge.source, toJson edge.exports])]

private def location (input : Json) : Except String Json := do
  let originalName ← input.getObjValAs? String "originalName"
  let emittedName ← input.getObjValAs? String "emittedName"
  let plan : EsmSplit.Plan :=
    { entry := emittedName,
      mods :=
        [⟨emittedName, ← input.getObjValAs? (List Nat) "retained", [], []⟩,
          ⟨"new/scroll.mjs", ← input.getObjValAs? (List Nat) "moved", [], []⟩] }
  let sites ←
    CoreJS.ModuleLocation.check (← input.getObjVal? "source") originalName plan
        (← assets (← input.getObjVal? "before")) (← assets (← input.getObjVal? "after"))
        { emptyMap := (← input.getObjValAs? Bool "emptyMap") }
  return .arr
      (sites.map fun (id, use) =>
        match use with
        | .rejectedImport req => .arr #[toJson id, toJson "rejected unmapped import", toJson req]
        | .metadata => .arr #[toJson id, toJson "import.meta"]
        | .metaUrl => .arr #[toJson id, toJson "import.meta.url"]
        | .dynamicImport request target => .arr #[toJson id, toJson request, toJson target])

/-- The original entry keeps the import, wrapped call, and export. The split entry
imports a captured callback dependency from the module holding the two moved arrows. -/
private def placement (glueBinding : String) : EsmSplit.Plan where
  entry := "entry/main.mjs"
  mods :=
    [⟨"entry/main.mjs", [0, 1, 4], [("lib/closures.mjs", ["extra"])], []⟩,
      ⟨"lib/closures.mjs", [2, 3], [], [("extra", glueBinding)]⟩]

private def check (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let providers ← input.getObjVal? "providers"
  let files ← input.getObjVal? "files"
  let glueBinding ← input.getObjValAs? String "glueBinding"
  let (original, emitted) ←
    CoreJS.LinkedInput.decodePair source providers files (placement glueBinding)
  let ordinals (p : CoreJS.Program) := p.loaded.flatMap fun m => m.body.map Prod.fst
  return toJson [ordinals original, ordinals emitted]

private def singletonPlacement (input : Json) : Except String Json := do
  let plan := placement (← input.getObjValAs? String "glueBinding")
  let decoded ←
    CoreJS.LinkedInput.decodeSingleton (← input.getObjVal? "source")
        (← input.getObjVal? "providers") (← input.getObjVal? "files") plan
  let _ ← CoreJS.checkPlaces decoded.assignment decoded.before decoded.after
  let some owner := decoded.after.find "lib/closures.mjs" | throw "missing fixture owner"
  let unit :: rest := owner.body | throw "missing fixture unit"
  let misplaced :=
    { decoded.after with
      modules :=
        decoded.after.modules.map fun m =>
          if m.name == owner.name then { m with body := rest }
          else if m.name == plan.entry then { m with body := m.body ++ [unit] } else m }
  let .error reason := CoreJS.checkPlaces decoded.assignment decoded.before misplaced
    | throw "accepted a unit assigned to a different module"
  return toJson (decoded.assignment, reason)

private def structural (input : Json) : Except String Json := do
  let binding ← input.getObjValAs? String "glueBinding"
  let plan : EsmSplit.Plan :=
    { entry := "entry.mjs",
      mods :=
        [⟨"entry.mjs", [0, 1, 2, 4, 5], [("lib/scroll.mjs", ["scroll"])], []⟩,
          ⟨"lib/scroll.mjs", [3], [], [("scroll", binding)]⟩] }
  let pivot ← input.getObjValAs? Nat "pivot"
  let pair ←
    (CoreJS.LinkedInput.decodeStructuralPair (← input.getObjVal? "source")
            (← input.getObjVal? "providers") (← input.getObjVal? "files") plan [pivot]).mapError
        CoreJS.LinkedBehavior.StructuralRefusal.reason
  let inspect (p : CoreJS.Program) :=
    (p.loaded.flatMap (·.body)).mapM fun (id, stmt) => do
      match stmt with
      | .decl _ name (.func f) =>
        unless f.kind == .arrow && f.opaqueBody.isNone do
          throw "expected the concrete pivot arrow"
        return Json.arr #[toJson id, toJson name]
      | .opaque code decls funs free _ =>
        unless code == id do
          throw "opaque code lost its source ordinal"
        return Json.arr #[toJson id, toJson (decls.map Prod.fst ++ funs.map Prod.fst), toJson free]
      | _ =>
        throw "unexpected structural statement"
  return toJson [← inspect pair.1, ← inspect pair.2]

private def structuralSparseHole (source : Json) : Except String Json := do
  let plan : EsmSplit.Plan :=
    { entry := "entry.mjs"
      mods := [⟨"entry.mjs", [0], [], []⟩] }
  let files := Json.mkObj [("entry.mjs", source)]
  return match
      CoreJS.LinkedInput.decodeStructuralSingleton source (Json.mkObj []) files plan [] with
    | .error refusal =>
      Json.mkObj [("reason", toJson refusal.reason), ("reason_code", toJson refusal.reasonCode)]
    | .ok _ => Json.mkObj [("accepted", toJson true)]

private def constructorContexts (source : Json) : Except String Json := do
  let #[cls] ← ESTreeUnits.nodes source | throw "expected one class"
  let #[method] ←
    (← cls.getObjVal? "body").getObjValAs? (Array Json) "body" | throw "expected one constructor"
  let body ← method.getObjVal? "value" >>= (·.getObjVal? "body")
  return toJson
      [CoreJS.FromESTree.needsOpaqueContext cls,
        CoreJS.FromESTree.needsOpaqueContext cls (wholeUnit := true),
        CoreJS.FromESTree.needsOpaqueContext body (wholeUnit := true)]

private def opaqueUnits (input : Json) : Except String Json := do
  let mut units := #[]
  for (j, i) in (← ESTreeUnits.nodes input).zipIdx do
    if Lower.typ j == "ImportDeclaration" then
      continue
    let .opaque code decls funs free run ←
      CoreJS.OpaqueInput.unit i j | throw "expected opaque unit"
    let kind : CoreJS.DeclKind → String
      | .const => "const"
      | .var => "var"
      | .let_ => "let"
    let body ←
      match run with
      | .world =>
        pure "world"
      | .closures [] =>
        pure "skip"
      | .closures _ =>
        throw "unexpected closure initializer"
    units :=
      units.push
        (.arr
          #[toJson code, toJson (decls.map fun (n, k) => [n, kind k]),
            .arr
              (funs.toArray.map fun (n, arity, kind) =>
                .arr
                  #[toJson n, toJson arity,
                    match kind with
                    | .ordinary ctor => toJson ctor
                    | .asyncFn => toJson "async"]),
            toJson free, toJson body])
  return .arr units

/-- Diagnose the ordered inline constructor, separately from the CLI's
standalone-export and literal-reorder alternatives. -/
private def inlineConstructor (input : Json) : Except String Json := do
  let ast ← input.getObjVal? "source"
  let sourceText ← input.getObjValAs? String "sourceText"
  let request ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let classified ←
    (CoreJS.AuthenticatedSource.decodeSourceClassification ast sourceText request.entry).mapError
        ("source classification: " ++ ·)
  let decoded ←
    (CoreJS.AuthenticatedSource.checkIndexedSource classified).mapError ("source provenance: " ++ ·)
  unless decoded.localExports.isEmpty do
    throw "source provenance: standalone export rows require source-indexed placement"
  let built ←
    (CoreJS.IndexedInlineConstructor.construct decoded
            (CoreJS.CheckedSplit.summarize decoded.program) request).mapError
        ("indexed inline construction: " ++ ·)
  let output := built.program
  let mut fields :=
    [("entry", toJson output.entry), ("order", toJson output.evalOrder),
      ("placements", toJson output.fileUnitPlacements)]
  if let .ok fuel := input.getObjValAs? Nat "checkFuel" then
    let exportCells (program : CoreJS.Program) : Except String Json := do
      let some cells :=
        CoreJS.LinkedBehavior.exports CoreJS.World.probe
          program | throw "probe could not link public export cells"
      return .arr
          (cells.toArray.map fun (name, addr) =>
            .arr #[toJson name, toJson addr.region, toJson addr.index])
    let originalCells ← exportCells decoded.program
    let outputCells ← exportCells output
    let .ok () before :=
      CoreJS.runProgram CoreJS.World.probe fuel
        decoded.program | throw "source probe did not complete"
    let .ok () after :=
      CoreJS.runProgram CoreJS.World.probe fuel output | throw "constructed probe did not complete"
    unless (CoreJS.StateMatch.probe before after).isSome do
      throw "constructed probe changed final state"
    match CoreJS.runProgram CoreJS.World.probe 0 decoded.program,
      CoreJS.runProgram CoreJS.World.probe 0 output with
    | .fuel, .fuel =>
      pure ()
    | _, _ =>
      throw "constructed probe changed zero-fuel exhaustion"
    fields := fields ++ [("sourceCells", originalCells), ("outputCells", outputCells)]
  return Json.mkObj fields

/-- Inspect the original decoded module without projecting or renumbering its body. -/
private def singletonSourceShape (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let entry ← input.getObjValAs? String "entry"
  let original ← CoreJS.LinkedInput.decodeSingletonSource source entry
  return Json.mkObj
      [("entry", toJson original.entry),
        ("modules",
          toJson
            (original.modules.map fun m =>
              Json.mkObj
                [("name", toJson m.name),
                  ("body", toJson (m.body.map fun (id, stmt) => (id, stmt.bindings.map Prod.fst))),
                  ("exports", toJson m.exports)]))]

/-- Inspect authenticated source units before any constructor-specific admission checks. -/
private def sourceClassification (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let text ← source.getObjValAs? String "sourceText"
  let entry ← input.getObjValAs? String "entry"
  let classified ← CoreJS.AuthenticatedSource.decodeSourceClassification source text entry
  let m := classified.source
  return Json.mkObj
      [("imports", toJson (m.imports.map fun i => (i.source, i.names))),
        ("exports", toJson m.exports),
        ("units", toJson (classified.sourceItems.map CoreJS.LinkedInput.SourceItem.ordinal)),
        ("body",
          toJson
            (m.body.map fun (id, stmt) =>
              Json.mkObj
                [("id", toJson id), ("bindings", toJson (stmt.bindings.map Prod.fst)),
                  ("free", toJson stmt.free),
                  ("functionDeclaration",
                    toJson
                      (match stmt with
                      | .funDecl _ => true
                      | _ => false))]))]

/-- Check the factory and loader syntax in isolation; this does not admit the full provider. -/
private def wrapperFactoryShape (input : Json) : Except String Json := do
  let ast ← input.getObjVal? "source"
  let ordinal ← input.getObjValAs? Nat "ordinal"
  let nodes ← ESTreeUnits.checkedNodes ast (← ast.getObjValAs? String "sourceText")
  let some node := nodes[ordinal]? | throw "no such factory unit"
  let lowered ← CoreJS.LinkedInput.lowerLinkedStatement .empty [] ordinal node false false
  let expected := CoreJS.WrapperLoader.factory "n" "a" "b"
  let some _ :=
    CoreJS.SyntaxMatch.stmtEq lowered
      expected | throw "factory syntax differs from the loader proof"
  return toJson true

/-- Lower the two original executable units independently of the unsupported
prefix. Link and destination-cell checks belong to the eventual constructor. -/
private def wrapperCrossingShape (input : Json) : Except String Json := do
  let ast ← input.getObjVal? "source"
  let factoryAlias ← input.getObjValAs? String "factoryAlias"
  let nodes ← ESTreeUnits.checkedNodes ast (← ast.getObjValAs? String "sourceText")
  let some caller := nodes[5]? | throw "no caller unit"
  let some arrow := nodes[1337]? | throw "no arrow unit"
  let .decl .var target (.call (.var callee) [.func callback]) ←
    CoreJS.LinkedInput.lowerLinkedStatement .empty [factoryAlias] 5 caller false false |
    throw "expected one arrow argument to a bare wrapper call"
  let .decl .var moved (.func closure) ←
    CoreJS.LinkedInput.lowerLinkedStatement .empty [factoryAlias] 1337 arrow false false |
    throw "expected an arrow initializer"
  unless callee == factoryAlias && callback.kind == .arrow && !callback.self &&
    closure.kind == .arrow && !closure.self do
    throw "wrapper crossing requires the selected callee and anonymous arrows"
  return Json.mkObj [("wrapper", toJson target), ("arrow", toJson moved)]

/-- Check the actual indexed decoder, source-derived planner bundle and
authored order-kept request for a top-level lexical inline export. -/
private def indexedInlinePlan (input : Json) : Except String Json := do
  let sourceAst ← input.getObjVal? "source"
  let sourceText ← input.getObjValAs? String "sourceText"
  let request ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let decoded ← CoreJS.AuthenticatedSource.decodeIndexedSource sourceAst sourceText request.entry
  let bundle := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let constructed ← CoreJS.IndexedInlineConstructor.construct decoded bundle request
  let output := constructed.program
  let ownerChecks :=
    output.fileUnitPlacements.all fun (ordinal, owner) =>
      request.claimants bundle ordinal == [owner]
  return .arr
      #[toJson (decoded.sourceItems.map CoreJS.LinkedInput.SourceItem.ordinal),
        toJson (output.loaded.flatMap fun m => m.body.map Prod.fst),
        toJson output.fileUnitPlacements, toJson ownerChecks,
        toJson (decoded.source.exports.map Prod.fst)]

/-- Replay the internal slice on parsed candidate files without publishing them. -/
private def indexedArraySlice (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let files ← input.getObjVal? "files"
  let spec ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let sourceText ← source.getObjValAs? String "sourceText"
  let indexed ← CoreJS.AuthenticatedSource.decodeIndexedSource source sourceText spec.entry
  let bundle := EsmSplit.LowerSplit.sourceBundle indexed.nodes
  let plan := EsmSplit.mkPlan bundle spec
  let providers := Json.mkObj []
  match decoded : CoreJS.LinkedInput.decodeSingleton source providers files plan with
  | .error reason =>
    throw reason
  | .ok deployment =>
    let ⟨placed⟩ ← CoreJS.checkPlaces deployment.assignment deployment.before deployment.after
    match probed : CoreJS.LinkedBehavior.probeWitness .empty deployment.before deployment.after with
    | .error refusal =>
      throw refusal.reason
    | .ok probe =>
      let eligible : EsmSplit.LowerSplit.SliceEligibility .empty source providers files plan :=
        { deployment, decodedSingleton := decoded, placed, probe, probeSucceeded := probed
          checkedLinks := CoreJS.LinkedBehavior.checkProbePair deployment.before deployment.after }
      let ⟨kind, _⟩ ←
        (EsmSplit.LowerSplit.certifySlice .empty source providers files plan bundle spec
              eligible) |>.mapError
            (·.reason)
      return toJson (kind.scope, kind.level.label)

/-- Compare the planner's default publication to the real link before and
after evaluation. The callable object can already exist at instantiation. -/
private def defaultLayout (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let text ← source.getObjValAs? String "sourceText"
  let decoded ← CoreJS.AuthenticatedSource.decodeIndexedSource source text "entry.mjs"
  let bundle := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let some (_, defaultOrdinal) :=
    bundle.zipIdx.find?
      (fun (row, _) => row.exports.any (·.1 == "default")) | throw "missing default row"
  let row := bundle.stmt defaultOrdinal
  let checked ← CoreJS.LinkedBehavior.checkLink decoded.program
  let before := checked.forWorld CoreJS.World.probe
  let exports ← CoreJS.EntryExports.resolve decoded.program.entry before.linked
  let some cell := exports.lookup "default" | throw "missing default cell"
  let .ok () after :=
    CoreJS.runProgram CoreJS.World.probe 16
      decoded.program | throw "default probe did not evaluate successfully"
  let value (state : CoreJS.State CoreJS.World.probe) :=
    (state.cellAt cell).bind CoreJS.Binding.value
  let some (.obj object) := value after | throw "default is not an object after evaluation"
  return Json.mkObj
      [("defs", toJson row.defs), ("publicBinding", toJson ((row.exports.lookup "default").join)),
        ("hoisted", toJson row.hoisted),
        ("initializedAtLink", toJson ((value before.ready).isSome)),
        ("sameFunctionObject", toJson (value before.ready == some (.obj object)))]

private def report (result : Except String Json) : Json :=
  match result with
  | .ok output => Json.mkObj [("output", output)]
  | .error reason => Json.mkObj [("error", toJson reason)]

/-- Feed the real source and readback ASTs to the existing deployment decoder.
The other probes are independent and can report after that decoder refuses. -/
private def deploymentDiagnostic (input : Json) : Except String Json := do
  let entry ← input.getObjValAs? String "entry"
  let source ← input.getObjVal? "source"
  let files ← input.getObjVal? "files"
  let fixed ← input.getObjVal? "fixed"
  let spec ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let nodes ← ESTreeUnits.checkedNodes source (← source.getObjValAs? String "sourceText")
  let plan := EsmSplit.mkPlan (EsmSplit.LowerSplit.sourceBundle nodes) spec
  let stages : Except String Json := do
    let decoded ←
      (CoreJS.LinkedInput.decodeDeployment entry [⟨source, files, plan⟩] fixed none .empty
              (fun _ => [])).mapError
          CoreJS.LinkedBehavior.StructuralRefusal.reason
    let _ ← CoreJS.checkPlaces decoded.assignment decoded.before decoded.after
    return toJson [decoded.before.evalOrder, decoded.after.evalOrder]
  return report stages

private def outerCallableRefusal (s : CoreJS.Stmt) : Option String := do
  let f ←
    match s with
    | .decl _ _ (.func f) | .funDecl f | .expr (.call (.func f) _) =>
      some f
    | _ =>
      none
  let (_, _, _, reason) ← f.opaqueBody
  reason

private def dependencyFreeProbe (m : CoreJS.Module) : Json :=
  let result := CoreJS.runProgram CoreJS.World.probe 1024 { entry := m.name, modules := [m] }
  let failed (reason : String) (region : Nat) :=
    Json.mkObj
      [("reason", toJson reason),
        ("unit", toJson (if region == 0 then none else some (region - 1)))]
  match result with
  | .ok () _ => Json.mkObj [("status", .str "ok")]
  | .fuel => Json.mkObj [("status", .str "fuel")]
  | .throw _ state => failed "throw" state.region
  | .unknown reason state => failed reason state.region

private def moduleDiagnostic (input : Json) : Except String Json := do
  let path ← input.getObjValAs? String "path"
  let ast ← input.getObjVal? "ast"
  let nodes ← ESTreeUnits.checkedNodes ast (← ast.getObjValAs? String "sourceText")
  let units ← CoreJS.LinkedInputCensus.censusUnits ast
  let ecma ← CoreJS.LinkedInputCensus.censusUnits ast .ecmascript
  let records :=
    (units.zip ecma).map fun (u, e) =>
      let kind :=
        match u.statement with
        | .ok (.opaque ..) => "retained-unit"
        | .ok _ => "lowered"
        | .error _ => "refused"
      let callable (u : CoreJS.LinkedInputCensus.CensusUnit) :=
        u.statement.toOption.bind outerCallableRefusal
      Json.mkObj
        [("unit", toJson u.ordinal), ("kind", toJson kind),
          ("loweringRefusal", toJson u.concreteRefusal),
          ("outerCallableRefusal", toJson (callable u)),
          ("ecmaOuterCallableRefusal", toJson (callable e))]
  let ids := units.toList.map (·.ordinal)
  let hasImports :=
    nodes.any fun j => (j.getObjValAs? String "type").toOption == some "ImportDeclaration"
  -- The source decoder preserves ordinals even for unhandled export headers.
  -- For relative imports use the path-aware decoder instead.
  let parsed ←
    pure <|
        if hasImports then CoreJS.LinkedInput.parseLinkedModuleWithOrdinals path nodes ids
        else (CoreJS.LinkedInput.decodeSingletonSourceNodes nodes path).map (·.source)
  return Json.mkObj
      [("units", .arr records),
        ("module",
          report
            (parsed.map fun m =>
              Json.mkObj
                ([("exports", toJson m.exports)] ++
                  (if m.imports.isEmpty then [("isolatedExecution", dependencyFreeProbe m)]
                  else []))))]

/-- Exercise the class move theorem with authenticated linked lowering of the
whole original. Local export lists are metadata and stay in the entry. -/
private def heritagePlan (input : Json) : Except String Json := do
  let ast ← input.getObjVal? "source"
  let text ← ast.getObjValAs? String "sourceText"
  let request ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let decoded ← CoreJS.AuthenticatedSource.decodeIndexedSource ast text request.entry
  let ss := decoded.sourceItems.map fun
    | .executable _ s => s
    | _ => .block []
  let B := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let P := EsmSplit.mkPlan B request
  let inversions := P.execOrder.flatMap fun i =>
    ((P.execOrder.dropWhile (· != i)).drop 1).filterMap fun j =>
      if j < i then some (i, j) else none
  return Json.mkObj [
    ("inversions", toJson inversions),
    ("valid", toJson (decide (EsmSplit.Valid B request P))),
    ("footprints", toJson (decide (CoreJS.FootprintsOfSyntax ss B))),
    ("ordinaryMoves", toJson (CoreJS.certifiedMovesOk ss B P.execOrder)),
    ("heritageMoves", toJson (CoreJS.heritageMovesOk ss B P.execOrder))]

private def starMetadata (input : Json) : Except String Json := do
  let graph ← CoreJS.StaticStarExports.resolve (← input.getObjVal? "files")
  let queries ← input.getObjValAs? (List (String × String)) "queries"
  return .arr (queries.toArray.map fun (path, name) => report (toJson <$> graph.origin path name))

/-- Link and run a freshly parsed small graph, then inspect actual export cells.
This is a readback test and does not publish files. -/
private def starReadback (input : Json) : Except String Json := do
  let graph ← CoreJS.StaticStarExports.resolve (← input.getObjVal? "files")
  let program ← graph.lower (← input.getObjValAs? String "entry")
  let linked ← CoreJS.LinkedBehavior.checkLink program
  let .ok () ready :=
    CoreJS.runProgram CoreJS.World.probe 256 program | throw "execution did not complete"
  let queries ← input.getObjValAs? (List (String × String)) "queries"
  let values ←
    queries.mapM fun (path, name) => do
        let exports ← CoreJS.EntryExports.resolve path linked.linked
        let some addr := exports.lookup name | throw s!"{path}: no linked export {name}"
        let some (.bool val) :=
          (ready.cellAt addr).bind (·.value) | throw s!"{path}: not a boolean: {name}"
        return Json.mkObj [("cell", toJson [addr.region, addr.index]), ("value", toJson val)]
  let mut result := [("order", toJson program.evalOrder), ("exports", toJson values)]
  if let .ok (path, name) := input.getObjValAs? (String × String) "namespaceRead" then
    let view ← CoreJS.StaticStarExports.NamespaceView.ofLinked path linked.linked
    let read initial := view.read name initial
    let .throw (.error .reference _) _ := read linked.ready
      | throw "namespace read should see the uninitialized exported cell"
    let .ok (.bool value) _ := read ready | throw "namespace read did not see the final boolean"
    let .ok .undef _ :=
      view.read "missing" ready | throw "missing namespace member did not yield undefined"
    result := result ++ [("liveRead", toJson value)]
  return Json.mkObj result

private def namespaceLinks (input : Json) : Except String Json := do
  let graph ← CoreJS.StaticStarExports.resolve (← input.getObjVal? "files")
  let entry ← input.getObjValAs? String "entry"
  let localName ← input.getObjValAs? String "namespace"
  let links ← graph.linkNamespaceCells entry
  let localCell ← links.localCell entry localName
  let view ← links.viewAt localCell
  let addr (cell : CoreJS.Addr) := toJson [cell.region, cell.index]
  let members := view.members.map fun (name, cell) => (name, addr cell)
  let publicExports := (← links.exports entry).map fun (name, cell) => (name, addr cell)
  let queries ← input.getObjValAs? (List (String × String)) "queries"
  let locals ←
    queries.mapM fun (path, name) => do
        return ((path, name), addr (← links.localCell path name))
  return Json.mkObj
      [("target", toJson view.target), ("members", toJson members),
        ("exports", toJson publicExports), ("locals", toJson locals),
        ("namespaceBinding", addr localCell)]

private def importMotion (input : Json) : Except String Json := do
  let entry ← input.getObjValAs? String "entry"
  let spec ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let checked ← CoreJS.StaticImportMotion.check entry spec
    (← input.getObjVal? "before") (← input.getObjVal? "reread")
  let concrete : Except String Json := do
    let programs ← checked.checkPrograms
    let .ok () _ := CoreJS.runProgram CoreJS.World.probe 256 programs.before |
      throw "program proved equal but did not complete in the probe"
    return toJson "all modeled runs agree"
  return Json.mkObj [("requests", toJson checked.requestCount), ("before", toJson checked.order),
    ("after", toJson checked.orderAfter), ("concrete", report concrete)]

private def fixtures (input : Json) : Except String Json := do
  let cases ← input.getArr?
  return .arr
      (←
        cases.mapM fun fixture => do
            let result ←
              match ← fixture.getObjValAs? String "kind" with
              | "imports" =>
                pure (imports (← fixture.getObjVal? "input"))
              | "location" =>
                pure (location (← fixture.getObjVal? "input"))
              | "linked" =>
                pure (check (← fixture.getObjVal? "input"))
              | "singletonPlacement" =>
                pure (singletonPlacement (← fixture.getObjVal? "input"))
              | "structural" =>
                pure (structural (← fixture.getObjVal? "input"))
              | "structuralSparseHole" =>
                pure (structuralSparseHole (← fixture.getObjVal? "input"))
              | "importMotion" =>
                pure (importMotion (← fixture.getObjVal? "input"))
              | "namespaceLinks" =>
                pure (namespaceLinks (← fixture.getObjVal? "input"))
              | "starMetadata" =>
                pure (starMetadata (← fixture.getObjVal? "input"))
              | "starReadback" =>
                pure (starReadback (← fixture.getObjVal? "input"))
              | "deploymentDiagnostic" =>
                pure (deploymentDiagnostic (← fixture.getObjVal? "input"))
              | "heritagePlan" =>
                pure (heritagePlan (← fixture.getObjVal? "input"))
              | "moduleDiagnostic" =>
                pure (moduleDiagnostic (← fixture.getObjVal? "input"))
              | "singletonSourceShape" =>
                pure (singletonSourceShape (← fixture.getObjVal? "input"))
              | "wrapperCrossingShape" =>
                pure (wrapperCrossingShape (← fixture.getObjVal? "input"))
              | "wrapperFactoryShape" =>
                pure (wrapperFactoryShape (← fixture.getObjVal? "input"))
              | "sourceClassification" =>
                pure (sourceClassification (← fixture.getObjVal? "input"))
              | "inlineConstructor" =>
                pure (inlineConstructor (← fixture.getObjVal? "input"))
              | "defaultLayout" =>
                pure (defaultLayout (← fixture.getObjVal? "input"))
              | "indexedInlinePlan" =>
                pure (indexedInlinePlan (← fixture.getObjVal? "input"))
              | "indexedArraySlice" =>
                pure (indexedArraySlice (← fixture.getObjVal? "input"))
              | "constructorContexts" =>
                pure (constructorContexts (← fixture.getObjVal? "input"))
              | "opaque" =>
                pure (opaqueUnits (← fixture.getObjVal? "input"))
              | kind =>
                throw s!"unknown test case {kind}"
            return report result)

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= fixtures with
  | .error why =>
    IO.eprintln why
    return 1
  | .ok results =>
    IO.println results.compress
    return 0
