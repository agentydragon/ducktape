import EsmSplit.ESTreeSource

open Lean

namespace ESTreeUnits

private def replaceField (node : Json) (key : String) (value : Json) : Except String Json := do
  return .obj ((← node.getObj?).insert key value)

private def split (statement : Json) : Except String (Array Json) := do
  let exported := (← statement.getObjValAs? String "type") == "ExportNamedDeclaration"
  let declaration ← if exported then statement.getObjVal? "declaration" else pure statement
  if declaration == .null then return #[statement]
  if (← declaration.getObjValAs? String "type") != "VariableDeclaration" then return #[statement]
  let kind ← declaration.getObjValAs? String "kind"
  let declarators ← declaration.getObjValAs? (Array Json) "declarations"
  unless declarators.size > 1 && ["var", "let", "const"].contains kind do return #[statement]
  declarators.mapM fun d => do
    let single ← replaceField declaration "declarations" (.arr #[d])
    if exported then replaceField statement "declaration" single else pure single

/-- Source ordinals are array indices. A split declaration keeps the original declarator node. -/
def nodes (program : Json) : Except String (Array Json) := do
  unless (← program.getObjValAs? String "type") == "Program" do throw "expected an ESTree Program"
  unless (← program.getObjValAs? String "sourceType") == "module" do throw "expected an ESTree module Program"
  let body ← program.getObjValAs? (Array Json) "body"
  let mut units := Array.mkEmpty body.size
  for statement in body do
    for unit in (← split statement) do units := units.push unit
  return units

/-- Validate source unit positions before using any ranges in the printer. Model-only ASTs
need no positions and use `nodes` directly. -/
def checkedNodes (program : Json) (source : String) : Except String (Array Json) := do
  let units ← nodes program
  let offsets := ESTreeSource.ranges source
  for ast in units do
    offsets.validate ast
    let decl := if (ast.getObjVal? "type").toOption == some (.str "ExportNamedDeclaration")
      then (ast.getObjVal? "declaration").toOption.getD .null else ast
    if let .ok ds := decl.getObjValAs? (Array Json) "declarations" then
      for d in ds do offsets.validate d
  return units

/-- Map module lexical occurrences in a checked declaration graph. Acorn can
share an identifier between the two sides of a shorthand import or export. -/
partial def mapModuleBindings (rename : String → Except String String) : Json → Except String Json
  | .obj fields => do
    let kind := fields.get? "type"
    if kind == some (.str "Identifier") &&
        (fields.get? "k" == some (.str "T") || fields.get? "k" == some (.str "V")) then
      let name ← (Json.obj fields).getObjValAs? String "name"
      return .obj (fields.insert "name" (.str (← rename name)))
    return Json.mkObj (← fields.toList.mapM fun (key, value) => do
      let label := (kind == some (.str "ImportSpecifier") && key == "imported") ||
        (kind == some (.str "ExportSpecifier") && key == "exported")
      if label then pure (key, value) else return (key, ← mapModuleBindings rename value))
  | .arr values => return .arr (← values.mapM (mapModuleBindings rename))
  | value => pure value

end ESTreeUnits
