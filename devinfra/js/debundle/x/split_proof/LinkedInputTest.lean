import EsmSplit.Cli
import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.DynamicImports
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.ModuleLocation
import EsmSplit.CoreJS.OpaqueInput
import EsmSplit.CoreJS.StateMatch
import EsmSplit.LowerSplit.Input

open Lean (Json toJson)

private def assets (j : Json) : Except String (String → Option CoreJS.ModuleLocation.Resource) := do
  let entries : List (String × CoreJS.ModuleLocation.Resource) ← (← j.getObj?).toList.mapM fun (name, r) => do
    return (name, ⟨← r.getObjValAs? String "url", ← r.getObjValAs? String "source"⟩)
  let table := Std.HashMap.ofList entries
  return fun name => table[name]?

private def imports (input : Json) : Except String Json := do
  let b ← CoreJS.DynamicImports.check (← input.getObjValAs? String "target")
    (← input.getObjValAs? (List String) "linked") (← input.getObjVal? "parsed")
    (← assets (← input.getObjVal? "before")) (← assets (← input.getObjVal? "after"))
  return Json.arr #[toJson b.scripts, .arr (b.requests.toArray.map fun edge =>
    .arr #[toJson edge.importer, toJson edge.source, toJson edge.exports])]

private def location (input : Json) : Except String Json := do
  let originalName ← input.getObjValAs? String "originalName"
  let emittedName ← input.getObjValAs? String "emittedName"
  let plan : EsmSplit.Plan := { entry := emittedName, mods := [
    ⟨emittedName, ← input.getObjValAs? (List Nat) "retained", [], []⟩,
    ⟨"new/scroll.mjs", ← input.getObjValAs? (List Nat) "moved", [], []⟩] }
  let sites ← CoreJS.ModuleLocation.check (← input.getObjVal? "source") originalName plan
    (← assets (← input.getObjVal? "before")) (← assets (← input.getObjVal? "after"))
    {emptyMap := (← input.getObjValAs? Bool "emptyMap")}
  return .arr (sites.map fun (id, use) => match use with
    | .rejectedImport req => .arr #[toJson id, toJson "rejected unmapped import", toJson req]
    | .metadata => .arr #[toJson id, toJson "import.meta"]
    | .metaUrl => .arr #[toJson id, toJson "import.meta.url"]
    | .dynamicImport request target => .arr #[toJson id, toJson request, toJson target])

/-- The original entry keeps the import, wrapped call, and export. The split entry
imports a captured callback dependency from the module holding the two moved arrows. -/
private def placement (glueBinding : String) : EsmSplit.Plan where
  entry := "entry/main.mjs"
  mods := [
    ⟨"entry/main.mjs", [0, 1, 4], [("lib/closures.mjs", ["extra"])], []⟩,
    ⟨"lib/closures.mjs", [2, 3], [], [("extra", glueBinding)]⟩]

private def check (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let providers ← input.getObjVal? "providers"
  let files ← input.getObjVal? "files"
  let glueBinding ← input.getObjValAs? String "glueBinding"
  let (original, emitted) ← CoreJS.LinkedInput.decodePair source providers files (placement glueBinding)
  let ordinals (p : CoreJS.Program) := p.loaded.flatMap fun m => m.body.map Prod.fst
  return toJson [ordinals original, ordinals emitted]

private def singletonPlacement (input : Json) : Except String Json := do
  let plan := placement (← input.getObjValAs? String "glueBinding")
  let decoded ← CoreJS.LinkedInput.decodeSingleton (← input.getObjVal? "source")
    (← input.getObjVal? "providers") (← input.getObjVal? "files") plan
  let _ ← CoreJS.checkPlaces decoded.assignment decoded.before decoded.after
  let some owner := decoded.after.find "lib/closures.mjs" | throw "missing fixture owner"
  let unit :: rest := owner.body | throw "missing fixture unit"
  let misplaced := { decoded.after with modules := decoded.after.modules.map fun m =>
    if m.name == owner.name then { m with body := rest }
    else if m.name == plan.entry then { m with body := m.body ++ [unit] }
    else m }
  let .error reason := CoreJS.checkPlaces decoded.assignment decoded.before misplaced
    | throw "accepted a unit assigned to a different module"
  return toJson (decoded.assignment, reason)

private def structural (input : Json) : Except String Json := do
  let binding ← input.getObjValAs? String "glueBinding"
  let plan : EsmSplit.Plan := { entry := "entry.mjs", mods := [
    ⟨"entry.mjs", [0, 1, 2, 4, 5], [("lib/scroll.mjs", ["scroll"])], []⟩,
    ⟨"lib/scroll.mjs", [3], [], [("scroll", binding)]⟩] }
  let pivot ← input.getObjValAs? Nat "pivot"
  let pair ← (CoreJS.LinkedInput.decodeStructuralPair (← input.getObjVal? "source") (← input.getObjVal? "providers")
    (← input.getObjVal? "files") plan [pivot]).mapError CoreJS.LinkedBehavior.StructuralRefusal.reason
  let inspect (p : CoreJS.Program) := (p.loaded.flatMap (·.body)).mapM fun (id, stmt) => do
    match stmt with
    | .decl _ name (.func f) =>
      unless f.kind == .arrow && f.opaqueBody.isNone do throw "expected the concrete pivot arrow"
      return Json.arr #[toJson id, toJson name]
    | .opaque code decls funs free _ =>
      unless code == id do throw "opaque code lost its source ordinal"
      return Json.arr #[toJson id, toJson (decls.map Prod.fst ++ funs.map Prod.fst), toJson free]
    | _ => throw "unexpected structural statement"
  return toJson [← inspect pair.1, ← inspect pair.2]

private def structuralSparseHole (source : Json) : Except String Json := do
  let plan : EsmSplit.Plan := {
    entry := "entry.mjs"
    mods := [⟨"entry.mjs", [0], [], []⟩]
  }
  let files := Json.mkObj [("entry.mjs", source)]
  return match CoreJS.LinkedInput.decodeStructuralSingleton source (Json.mkObj []) files plan [] with
  | .error refusal => Json.mkObj [
      ("reason", toJson refusal.reason),
      ("reason_code", toJson refusal.reasonCode)
    ]
  | .ok _ => Json.mkObj [("accepted", toJson true)]

private def constructorContexts (source : Json) : Except String Json := do
  let #[cls] ← ESTreeUnits.nodes source | throw "expected one class"
  let #[method] ← (← cls.getObjVal? "body").getObjValAs? (Array Json) "body" | throw "expected one constructor"
  let body ← method.getObjVal? "value" >>= (·.getObjVal? "body")
  return toJson [CoreJS.FromESTree.needsOpaqueContext cls,
    CoreJS.FromESTree.needsOpaqueContext cls (wholeUnit := true),
    CoreJS.FromESTree.needsOpaqueContext body (wholeUnit := true)]

private def opaqueUnits (input : Json) : Except String Json := do
  let mut units := #[]
  for (j, i) in (← ESTreeUnits.nodes input).zipIdx do
    if Lower.typ j == "ImportDeclaration" then continue
    let .opaque code decls funs free run ← CoreJS.OpaqueInput.unit i j | throw "expected opaque unit"
    let kind : CoreJS.DeclKind → String
      | .const => "const" | .var => "var" | .let_ => "let"
    let body ← match run with
      | .world => pure "world"
      | .closures [] => pure "skip"
      | .closures _ => throw "unexpected closure initializer"
    units := units.push (.arr #[toJson code, toJson (decls.map fun (n, k) => [n, kind k]),
      .arr (funs.toArray.map fun (n, arity, kind) => .arr #[toJson n, toJson arity, match kind with
        | .ordinary ctor => toJson ctor | .asyncFn => toJson "async"]),
      toJson free, toJson body])
  return .arr units

/-- Diagnose the ordered inline constructor, separately from the CLI's
standalone-export and literal-reorder alternatives. -/
private def inlineConstructor (input : Json) : Except String Json := do
  let ast ← input.getObjVal? "source"
  let sourceText ← input.getObjValAs? String "sourceText"
  let request ← EsmSplit.parseSpec (← input.getObjVal? "spec")
  let classified ← (CoreJS.AuthenticatedSource.decodeSourceClassification ast sourceText request.entry).mapError
    ("source classification: " ++ ·)
  let decoded ← (CoreJS.AuthenticatedSource.checkIndexedSource classified).mapError ("source provenance: " ++ ·)
  unless decoded.localExports.isEmpty do
    throw "source provenance: standalone export rows require source-indexed placement"
  let built ← (CoreJS.IndexedInlineConstructor.construct decoded (CoreJS.CheckedSplit.summarize decoded.program)
    request).mapError ("indexed inline construction: " ++ ·)
  let output := built.program
  let mut fields := [("entry", toJson output.entry), ("order", toJson output.evalOrder),
    ("placements", toJson output.fileUnitPlacements)]
  if let .ok fuel := input.getObjValAs? Nat "checkFuel" then
    let exportCells (program : CoreJS.Program) : Except String Json := do
      let some cells := CoreJS.LinkedBehavior.exports CoreJS.World.probe program |
        throw "probe could not link public export cells"
      return .arr (cells.toArray.map fun (name, addr) => .arr #[toJson name, toJson addr.region, toJson addr.index])
    let originalCells ← exportCells decoded.program
    let outputCells ← exportCells output
    let .ok () before := CoreJS.runProgram CoreJS.World.probe fuel decoded.program |
      throw "source probe did not complete"
    let .ok () after := CoreJS.runProgram CoreJS.World.probe fuel output |
      throw "constructed probe did not complete"
    unless (CoreJS.StateMatch.probe before after).isSome do throw "constructed probe changed final state"
    match CoreJS.runProgram CoreJS.World.probe 0 decoded.program, CoreJS.runProgram CoreJS.World.probe 0 output with
    | .fuel, .fuel => pure ()
    | _, _ => throw "constructed probe changed zero-fuel exhaustion"
    fields := fields ++ [("sourceCells", originalCells), ("outputCells", outputCells)]
  return Json.mkObj fields

/-- Inspect the original decoded module without projecting or renumbering its body. -/
private def singletonSourceShape (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let entry ← input.getObjValAs? String "entry"
  let original ← CoreJS.LinkedInput.decodeSingletonSource source entry
  return Json.mkObj [("entry", toJson original.entry), ("modules", toJson (original.modules.map fun m =>
    Json.mkObj [("name", toJson m.name), ("body", toJson (m.body.map fun (id, stmt) =>
      (id, stmt.bindings.map Prod.fst))), ("exports", toJson m.exports)]))]

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
  let ownerChecks := output.fileUnitPlacements.all fun (ordinal, owner) =>
    request.claimants bundle ordinal == [owner]
  return .arr #[
    toJson (decoded.sourceItems.map CoreJS.LinkedInput.SourceItem.ordinal),
    toJson (output.loaded.flatMap fun m => m.body.map Prod.fst),
    toJson output.fileUnitPlacements,
    toJson ownerChecks,
    toJson (decoded.source.exports.map Prod.fst)
  ]

private def fixtures (input : Json) : Except String Json := do
  let cases ← input.getArr?
  return .arr (← cases.mapM fun fixture => do
    let result ← match ← fixture.getObjValAs? String "kind" with
      | "imports" => pure (imports (← fixture.getObjVal? "input"))
      | "location" => pure (location (← fixture.getObjVal? "input"))
      | "linked" => pure (check (← fixture.getObjVal? "input"))
      | "singletonPlacement" => pure (singletonPlacement (← fixture.getObjVal? "input"))
      | "structural" => pure (structural (← fixture.getObjVal? "input"))
      | "structuralSparseHole" => pure (structuralSparseHole (← fixture.getObjVal? "input"))
      | "singletonSourceShape" => pure (singletonSourceShape (← fixture.getObjVal? "input"))
      | "inlineConstructor" => pure (inlineConstructor (← fixture.getObjVal? "input"))
      | "indexedInlinePlan" => pure (indexedInlinePlan (← fixture.getObjVal? "input"))
      | "constructorContexts" => pure (constructorContexts (← fixture.getObjVal? "input"))
      | "opaque" => pure (opaqueUnits (← fixture.getObjVal? "input"))
      | kind => throw s!"unknown test case {kind}"
    return match result with
      | .ok output => Json.mkObj [("output", output)]
      | .error reason => Json.mkObj [("error", toJson reason)])

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= fixtures with
  | .error why =>
    IO.eprintln why
    return 1
  | .ok results =>
    IO.println results.compress
    return 0
