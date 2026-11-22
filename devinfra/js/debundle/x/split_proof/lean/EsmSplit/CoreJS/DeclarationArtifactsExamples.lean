import EsmSplit.CoreJS.DeclarationArtifacts

namespace CoreJS.DeclarationArtifactsExamples

open Lean (Json)

private def node (kind : String) (fields : List (String × Json)) :=
  Json.mkObj (("type", .str kind) :: fields)

private def identifier (name cls : String) := node "Identifier" [("name", .str name), ("k", .str cls)]

private def program (body : Array Json) := node "Program" [("sourceType", .str "module"), ("body", .arr body)]

private def declaration (kind name : String) (init : Json) :=
  node "VariableDeclaration" [("kind", .str kind), ("declarations", .arr #[
    node "VariableDeclarator" [("id", identifier name "T"), ("init", init)]])]

private def exportDecl (decl : Json) (specs : Array Json) :=
  node "ExportNamedDeclaration" [("declaration", decl), ("source", .null), ("specifiers", .arr specs)]

private def exampleCheck (request : String) : Bool :=
  let sourceX := declaration "var" "x" .null
  let exportY cls := exportDecl (declaration "const" "y" (identifier "x" cls)) #[]
  let source := program #[sourceX, exportY "T"]
  let sp := node "ImportSpecifier" [("imported", identifier "shared" ""), ("local", identifier "x" "V")]
  let req := node "ImportDeclaration" [("source", node "Literal" [("lit", .str ("s:" ++ request))]),
    ("specifiers", .arr #[sp])]
  let exp := exportDecl .null #[node "ExportSpecifier" [("local", identifier "x" "T"), ("exported", identifier "shared" "")]]
  let files := Json.mkObj [("main/entry.mjs", program #[req, exportY "V"]), ("lib/side.mjs", program #[sourceX, exp])]
  let B : EsmSplit.Bundle := [{ src := "var x;", defs := ["x"] },
    { src := "export const y = x;", defs := ["y"], eager := ["x"], exports := [("y", some "y")] }]
  let P : EsmSplit.Plan := { entry := "main/entry.mjs", mods := [
    ⟨"main/entry.mjs", [1], [("lib/side.mjs", ["x"])], []⟩, ⟨"lib/side.mjs", [0], [], [("shared", "x")]⟩] }
  (DeclarationArtifacts.check source files [.varDecl "x", .decl .const "y" (.var "x")] B P).isOk

-- A nested request imports the live cell under its glue alias. Resolving from the root
-- instead would silently name the wrong module for the second request.
#guard exampleCheck "../lib/side.mjs"
#guard !exampleCheck "./lib/side.mjs"
#guard !exampleCheck "../../lib/side.mjs"

end CoreJS.DeclarationArtifactsExamples
