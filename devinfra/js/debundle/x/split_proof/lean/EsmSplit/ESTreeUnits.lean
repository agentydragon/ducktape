import EsmSplit.ESTreeSource

open Lean

namespace ESTreeUnits

private inductive Origin where
  | statement
  | declarator (node : Json) (kind : String) (exported : Bool)

private structure SourceUnit where
  ast : Json
  origin : Origin := .statement

private def replaceField (node : Json) (key : String) (value : Json) : Except String Json := do
  return .obj ((← node.getObj?).insert key value)

private def split (statement : Json) : Except String (Array SourceUnit) := do
  let statementType ← statement.getObjValAs? String "type"
  let exported := statementType == "ExportNamedDeclaration"
  let declaration ← if exported then statement.getObjVal? "declaration" else pure statement
  if declaration == .null then return #[{ ast := statement }]
  if (← declaration.getObjValAs? String "type") != "VariableDeclaration" then
    return #[{ ast := statement }]
  let kind ← declaration.getObjValAs? String "kind"
  let declarators ← declaration.getObjValAs? (Array Json) "declarations"
  unless declarators.size > 1 && ["var", "let", "const"].contains kind do
    return #[{ ast := statement }]
  declarators.mapM fun node => do
    let declaration ← replaceField declaration "declarations" (.arr #[node])
    let ast ← if exported then replaceField statement "declaration" declaration else pure declaration
    return { ast, origin := .declarator node kind exported }

private def extract (program : Json) : Except String (Array SourceUnit) := do
  unless (← program.getObjValAs? String "type") == "Program" do throw "expected an ESTree Program"
  unless (← program.getObjValAs? String "sourceType") == "module" do
    throw "expected an ESTree module Program"
  let body ← program.getObjValAs? (Array Json) "body"
  let mut units := Array.mkEmpty body.size
  for statement in body do
    for unit in (← split statement) do
      units := units.push unit
  return units

/-- The array indices are source ordinals. Only top-level `var`, `let`, and `const` lists split,
including under `export`. Existing annotations remain attached to their nodes. -/
def nodes (program : Json) : Except String (Array Json) := do
  return (← extract program).map (·.ast)

structure RenderUnit where
  ast : Json
  src : String

/-- Rebuild the declaration prefix and terminator only when splitting a comma-separated list. -/
def renderUnits (program : Json) : Except String (Array RenderUnit) := do
  let units ← extract program
  let source := ESTreeSource.ranges (← program.getObjValAs? String "sourceText")
  units.mapM fun unit => do
    let src ← match unit.origin with
      | .statement => source.slice unit.ast
      | .declarator node kind exported => do
        let text ← source.slice node
        let exportKeyword := if exported then "export " else ""
        pure s!"{exportKeyword}{kind} {text};"
    return { ast := unit.ast, src }

end ESTreeUnits
