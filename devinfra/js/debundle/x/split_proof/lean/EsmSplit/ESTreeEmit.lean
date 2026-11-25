import Lean.Data.Json
import EsmSplit.Lower
import EsmSplit.ESTreeUnits
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
private def request (decl : Json) (fromFile file : String) : Except String Json := do
  let original ← (← decl.getObjVal? "source").getObjValAs? String "value"
  let target ← rebaseRelativeRequest fromFile file original
  if target == original then return decl
  setField decl "source" (literal target)

private def glueImports (exportsByModule : Std.HashMap ModName (List (Name × Name)))
    (rename : Name → Name) (m : Module) :=
  m.imports.map fun (src, names) =>
    let exports := exportsByModule.getD src []
    let specs := names.map fun localName =>
      let publicName := exportNameFor exports localName
      node "ImportSpecifier" [("local", identifier (rename localName)), ("imported", moduleName publicName)]
    node "ImportDeclaration" [("source", literal (relativeUrlSpecifier m.name src)), ("specifiers", toJson specs),
      ("attributes", .arr #[])]

private def glueExports (rename : Name → Name) (m : Module) : List Json :=
  if m.exports.isEmpty then [] else
    let specs := m.exports.map fun (published, localName) =>
      node "ExportSpecifier" [("local", identifier (rename localName)), ("exported", moduleName published)]
    [node "ExportNamedDeclaration" [("declaration", .null), ("source", .null), ("specifiers", toJson specs),
      ("attributes", .arr #[])]]

private def isRequest (s : Json) : Bool :=
  Lower.typ s == "ImportDeclaration" ||
    (Lower.typ s == "ExportNamedDeclaration" && !Lower.isNull (Lower.fld s "source"))

private def moduleProgram (asts : Array Json) (imports : List (Nat × Json)) (B : Bundle) (P : Plan)
    (exportsByModule : Std.HashMap ModName (List (Name × Name)))
    (m : Module) (hashbang : List (String × Json)) (fromFile : String)
    (renames : List (Name × Name)) : Except String Json := do
  let rename := fun name => (renames.lookup name).getD name
  let held := Std.HashSet.ofList m.stmts
  let elsewhere := imports.filter fun (i, _) => !held.contains i
  let mut duplicated := #[]
  unless elsewhere.isEmpty do
    let uses := Std.HashSet.ofList (m.stmts.flatMap fun i => (B.stmt i).eager ++ (B.stmt i).lazy)
    for (_, decl) in elsewhere do
      let used := (Lower.items (Lower.fld decl "specifiers")).filter fun sp =>
        uses.contains (Lower.specName (Lower.fld sp "local"))
      unless used.isEmpty do
        duplicated := duplicated.push (← request (← setField decl "specifiers" (.arr used)) fromFile m.name)
  let statements ← m.stmts.mapM fun i => do
    let some statement := asts[i]? | throw s!"statement {i} is missing from the input AST"
    if renames.isEmpty then pure statement else ESTreeUnits.mapModuleBindings (pure ∘ rename) statement
  -- Keep original directive strings in the prologue even when the plan adds imports.
  let (directives, rest) := statements.partition fun s =>
    Lower.typ s == "ExpressionStatement" && (s.getObjValAs? String "directive").isOk
  let (requests, body) := rest.partition isRequest
  let requests ← requests.mapM (request · fromFile m.name)
  let output := directives ++ requests ++ duplicated.toList ++ glueImports exportsByModule rename m ++ body ++ glueExports rename m
  return node "Program" ([("sourceType", .str "module"), ("body", toJson output)] ++
    (if m.name == P.entry then hashbang else []))

def programs (asts : Array Json) (B : Bundle) (P : Plan) (source : String)
    (fromFile : String := "bundle.mjs") (renames : List (Name × Name) := []) : Except String Json := do
  let imports := asts.toList.zipIdx.filterMap fun (s, i) =>
    if Lower.typ s == "ImportDeclaration" then some (i, s) else none
  let hashbang := if source.startsWith "#!" then
    [("hashbang", Json.str (source.takeWhile fun c => !['\n', '\r', '\u2028', '\u2029'].contains c).toString)]
    else []
  validateModulePaths (P.mods.map (·.name))
  let arr := B.toArray
  let exportsByModule : Std.HashMap ModName (List (Name × Name)) :=
    P.mods.foldr (fun m exports => exports.insert m.name (m.namedExportsFrom arr)) ∅
  return Json.mkObj (← P.mods.mapM fun m => do
    return (m.name, ← moduleProgram asts imports B P exportsByModule m hashbang fromFile renames))

end EsmSplit.ESTreeEmit
