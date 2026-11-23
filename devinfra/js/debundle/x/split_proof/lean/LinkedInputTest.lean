import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.OpaqueInput

open Lean (Json toJson)

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

def main (args : List String) : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= (if args == ["opaque-units"] then opaqueUnits else check) with
  | .error why =>
    IO.eprintln why
    return 1
  | .ok orders =>
    IO.println orders.compress
    return 0
