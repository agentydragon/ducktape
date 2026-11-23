import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.ModuleLocation
import EsmSplit.CoreJS.OpaqueInput

open Lean (Json toJson)

private def location (input : Json) : Except String Json := do
  let originalName ← input.getObjValAs? String "originalName"
  let emittedName ← input.getObjValAs? String "emittedName"
  let plan : EsmSplit.Plan := { entry := emittedName, mods := [
    ⟨emittedName, ← input.getObjValAs? (List Nat) "retained", [], []⟩,
    ⟨"new/scroll.mjs", ← input.getObjValAs? (List Nat) "moved", [], []⟩] }
  let assets (j : Json) : Except String (String → Option CoreJS.ModuleLocation.Resource) := do
    let entries : List (String × CoreJS.ModuleLocation.Resource) ← (← j.getObj?).toList.mapM fun (name, r) => do
      return (name, ⟨← r.getObjValAs? String "url", ← r.getObjValAs? String "source"⟩)
    let table := Std.HashMap.ofList entries
    return fun name => table[name]?
  let sites ← CoreJS.ModuleLocation.check (← input.getObjVal? "source") originalName plan
    (← assets (← input.getObjVal? "before")) (← assets (← input.getObjVal? "after"))
  return .arr (sites.map fun (id, use) => match use with
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
      .arr (funs.toArray.map fun (n, arity, ctor) => .arr #[toJson n, toJson arity, toJson ctor]),
      toJson free, toJson body])
  return .arr units

private def fixtures (input : Json) : Except String Json := do
  let cases ← input.getArr?
  return .arr (← cases.mapM fun fixture => do
    let result ← match ← fixture.getObjValAs? String "kind" with
      | "location" => pure (location (← fixture.getObjVal? "input"))
      | "linked" => pure (check (← fixture.getObjVal? "input"))
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
