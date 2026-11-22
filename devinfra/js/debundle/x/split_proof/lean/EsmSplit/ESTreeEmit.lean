import Lean.Data.Json
import EsmSplit.Lower
import EsmSplit.ModulePath
import EsmSplit.Plan

/-! Assemble each output module as ESTree. The original nodes supply declarations, code and
import attributes; the plan supplies ownership, glue requests and exported binding names. -/
namespace EsmSplit.ESTreeEmit

open Lean (Json toJson)

private def node (kind : String) (fields : List (String × Json)) : Json :=
  Json.mkObj (("type", .str kind) :: fields)

private def identifier (name : String) := node "Identifier" [("name", .str name)]
private def literal (value : String) := node "Literal" [("value", .str value)]

private def moduleName (name : String) :=
  let initial := fun (c : Char) => c.isAlpha || c == '_' || c == '$'
  if name.toList.head?.any initial && name.toList.all (fun c => initial c || c.isDigit) then
    identifier name
  else literal name

private def setField (j : Json) (key : String) (v : Json) : Except String Json := do
  return .obj ((← j.getObj?).insert key v)

/-- Retained and duplicated requests resolve relative to the file that now imports them. -/
private def request (decl : Json) (file : String) : Except String Json := do
  let original ← (← decl.getObjVal? "source").getObjValAs? String "value"
  let target ← rebaseRelativeRequest file original
  if target == original then return decl
  setField decl "source" (literal target)

private def glueImports (P : Plan) (m : Module) := m.imports.map fun (src, names) =>
  let source := P.find src
  let specs := names.map fun localName =>
    let publicName := (source.map (Module.publicName · localName)).getD localName
    node "ImportSpecifier" [("local", identifier localName), ("imported", moduleName publicName)]
  node "ImportDeclaration" [("source", literal (relativeUrlSpecifier m.name src)), ("specifiers", toJson specs),
    ("attributes", .arr #[])]

private def glueExports (m : Module) : List Json :=
  if m.exports.isEmpty then [] else
    let specs := m.exports.map fun (published, localName) =>
      node "ExportSpecifier" [("local", identifier localName), ("exported", moduleName published)]
    [node "ExportNamedDeclaration" [("declaration", .null), ("source", .null), ("specifiers", toJson specs),
      ("attributes", .arr #[])]]

private def isRequest (s : Json) : Bool :=
  Lower.typ s == "ImportDeclaration" ||
    (Lower.typ s == "ExportNamedDeclaration" && !Lower.isNull (Lower.fld s "source"))

private def moduleProgram (asts : Array Json) (imports : List (Nat × Json)) (B : Bundle) (P : Plan)
    (m : Module) (hashbang : List (String × Json)) : Except String Json := do
  let held := Std.HashSet.ofList m.stmts
  let elsewhere := imports.filter fun (i, _) => !held.contains i
  let mut duplicated := #[]
  unless elsewhere.isEmpty do
    let uses := Std.HashSet.ofList (m.stmts.flatMap fun i => (B.stmt i).eager ++ (B.stmt i).lazy)
    for (_, decl) in elsewhere do
      let used := (Lower.items (Lower.fld decl "specifiers")).filter fun sp =>
        uses.contains (Lower.specName (Lower.fld sp "local"))
      unless used.isEmpty do
        duplicated := duplicated.push (← request (← setField decl "specifiers" (.arr used)) m.name)
  let statements ← m.stmts.mapM fun i => do
    let some statement := asts[i]? | throw s!"statement {i} is missing from the input AST"
    return statement
  -- Keep original directive strings in the prologue even when the plan adds imports.
  let (directives, rest) := statements.partition fun s =>
    Lower.typ s == "ExpressionStatement" && (s.getObjValAs? String "directive").isOk
  let (requests, body) := rest.partition isRequest
  let requests ← requests.mapM (request · m.name)
  let output := directives ++ requests ++ duplicated.toList ++ glueImports P m ++ body ++ glueExports m
  return node "Program" ([("sourceType", .str "module"), ("body", toJson output)] ++
    (if m.name == P.entry then hashbang else []))

def programs (asts : Array Json) (B : Bundle) (P : Plan) (source : String) : Except String Json := do
  let imports := asts.toList.zipIdx.filterMap fun (s, i) =>
    if Lower.typ s == "ImportDeclaration" then some (i, s) else none
  let hashbang := if source.startsWith "#!" then
    [("hashbang", Json.str (source.takeWhile fun c => !['\n', '\r', '\u2028', '\u2029'].contains c).toString)]
    else []
  validateModulePaths (P.mods.map (·.name))
  return Json.mkObj (← P.mods.mapM fun m => do
    return (m.name, ← moduleProgram asts imports B P m hashbang))

end EsmSplit.ESTreeEmit
