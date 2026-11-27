import Lean.Data.Json
import EsmSplit.Lower
import EsmSplit.ModulePath
import EsmSplit.Plan
import EsmSplit.CoreJS.Number
import EsmSplit.CoreJS.Syntax

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

private def glueImports (exportsByModule : Std.HashMap ModName (List (Name × Name))) (m : Module) :=
  m.imports.map fun (src, names) =>
    let exports := exportsByModule.getD src []
    let specs := names.map fun localName =>
      let publicName := exportNameFor exports localName
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
    (exportsByModule : Std.HashMap ModName (List (Name × Name)))
    (m : Module) (hashbang : List (String × Json)) (fromFile : String) : Except String Json := do
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
    return statement
  -- Keep original directive strings in the prologue even when the plan adds imports.
  let (directives, rest) := statements.partition fun s =>
    Lower.typ s == "ExpressionStatement" && (s.getObjValAs? String "directive").isOk
  let (requests, body) := rest.partition isRequest
  let requests ← requests.mapM (request · fromFile m.name)
  let output := directives ++ requests ++ duplicated.toList ++ glueImports exportsByModule m ++ body ++ glueExports m
  return node "Program" ([("sourceType", .str "module"), ("body", toJson output)] ++
    (if m.name == P.entry then hashbang else []))

def programs (asts : Array Json) (B : Bundle) (P : Plan) (source : String)
    (fromFile : String := "bundle.mjs") : Except String Json := do
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
    return (m.name, ← moduleProgram asts imports B P exportsByModule m hashbang fromFile))

/-- The direct CoreJS printer currently handles ASCII binding names. The
source parser has already checked scope; this check keeps unsupported names
out of the generated ESTree. -/
private def printableBinding (name : String) : Bool :=
  let initial := fun c => ('a' ≤ c && c ≤ 'z') || ('A' ≤ c && c ≤ 'Z') || c == '_' || c == '$'
  let rest := fun c => initial c || ('0' ≤ c && c ≤ '9')
  name.toList.head?.any initial && name.toList.all rest &&
    !["await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete",
      "do", "else", "enum", "export", "extends", "false", "finally", "for", "function", "if", "import",
      "in", "instanceof", "let", "new", "null", "return", "super", "switch", "this", "throw", "true",
      "try", "typeof", "var", "void", "while", "with", "yield", "implements", "interface", "package",
      "private", "protected", "public", "static", "eval", "arguments"].contains name

private def coreIdentifier (name : String) : Except String Json := do
  unless printableBinding name do throw s!"unsupported CoreJS binding name `{name}`"
  return identifier name

/-- Translate the initializer fragment handled by the direct
constructor route. Unsupported CoreJS syntax fails closed. -/
private def coreExpr (shadowed : Bool) (expr : CoreJS.Expr) (operand := false) : Except String Json :=
  match expr with
  | .arr elems => do
    let elements ← elems.attach.mapM fun ⟨elem, _⟩ => coreExpr shadowed elem
    return node "ArrayExpression" [("elements", toJson elements)]
  | .obj [] => return node "ObjectExpression" [("properties", .arr #[])]
  | .undef => do
    if shadowed then throw "undefined initializer is shadowed in the output module"
    return identifier "undefined"
  | .null => pure <| node "Literal" [("value", .null)]
  | .bool value => pure <| node "Literal" [("value", .bool value)]
  | .str value => pure <| node "Literal" [("value", .str value)]
  | .var name => coreIdentifier name
  | .num value => do
    if value.isNaN || value.isInf then throw "unsupported non-finite CoreJS number"
    let negative := (CoreJS.Num.decompose value).1
    if negative && operand then throw "negative numeric operand requires an explicit unary minus"
    let magnitude := if negative then -value else value
    let number ← Json.parse (CoreJS.Num.toString magnitude)
    let literalNode := node "Literal" [("value", number)]
    if negative then
      return node "UnaryExpression" [("operator", .str "-"), ("prefix", .bool true), ("argument", literalNode)]
    return literalNode
  | .unary op argument => do
    let operator ← match op with
      | .neg => pure "-"
      | .pos => pure "+"
      | .not => pure "!"
      | .bitNot => pure "~"
      | .typeof => pure "typeof"
      | .void => pure "void"
      | .toString => throw "unsupported CoreJS string conversion for direct printing"
    return node "UnaryExpression" [("operator", .str operator), ("prefix", .bool true),
      ("argument", ← coreExpr shadowed argument true)]
  | .binary op left right => do
    let operator ← match op with
      | .add => pure "+"
      | .sub => pure "-"
      | .mul => pure "*"
      | .div => pure "/"
      | .mod => pure "%"
      | .bitAnd => pure "&"
      | .bitOr => pure "|"
      | .bitXor => pure "^"
      | .shl => pure "<<"
      | .shr => pure ">>"
      | .ushr => pure ">>>"
      | .eq => pure "=="
      | .ne => pure "!="
      | .strictEq => pure "==="
      | .strictNe => pure "!=="
      | .lt => pure "<"
      | .le => pure "<="
      | .gt => pure ">"
      | .ge => pure ">="
    return node "BinaryExpression" [("operator", .str operator),
      ("left", ← coreExpr shadowed left true), ("right", ← coreExpr shadowed right true)]
  | .logical op left right => do
    let operator ← match op with
      | .and => pure "&&"
      | .or => pure "||"
      | .nullish => pure "??"
    return node "LogicalExpression" [("operator", .str operator),
      ("left", ← coreExpr shadowed left true), ("right", ← coreExpr shadowed right true)]
  | .cond test yes no => do
    return node "ConditionalExpression" [("test", ← coreExpr shadowed test true),
      ("consequent", ← coreExpr shadowed yes true), ("alternate", ← coreExpr shadowed no true)]
  | _ => throw "unsupported CoreJS initializer for direct printing"

private def coreStatement (shadowed : Bool) : CoreJS.Stmt → Except String Json
  | .decl .const name value => do
    let id ← coreIdentifier name
    let init ← coreExpr shadowed value
    return node "VariableDeclaration" [("kind", .str "const"), ("declarations", toJson [
      node "VariableDeclarator" [("id", id), ("init", init)]])]
  | .decl .let_ name value => do
    let id ← coreIdentifier name
    let init ← coreExpr shadowed value
    return node "VariableDeclaration" [("kind", .str "let"), ("declarations", toJson [
      node "VariableDeclarator" [("id", id), ("init", init)]])]
  | .decl .var name value => do
    let id ← coreIdentifier name
    let init ← coreExpr shadowed value
    return node "VariableDeclaration" [("kind", .str "var"), ("declarations", toJson [
      node "VariableDeclarator" [("id", id), ("init", init)]])]
  | _ => throw "unsupported CoreJS statement for direct printing"

private def coreModule (m : CoreJS.Module) (hashbang : List (String × Json)) : Except String Json := do
  let imports ← m.imports.mapM fun req => do
    let specs ← req.names.mapM fun (published, localName) => do
      return node "ImportSpecifier" [("local", ← coreIdentifier localName), ("imported", moduleName published)]
    return node "ImportDeclaration" [("source", literal (relativeUrlSpecifier m.name req.source)),
      ("specifiers", toJson specs), ("attributes", .arr #[])]
  -- An `.undef` is printed as the global identifier; reject it only when
  -- this output module shadows that identifier with a binding or import.
  let shadowed := m.imports.any (fun req => req.names.any (fun (_, localName) => localName == "undefined")) ||
    m.body.any (fun (_, stmt) => match stmt with
      | .decl _ name _ => name == "undefined"
      | _ => false)
  let body ← m.body.mapM fun (_, stmt) => coreStatement shadowed stmt
  let exports ← m.exports.mapM fun (published, localName) => do
    return node "ExportSpecifier" [("local", ← coreIdentifier localName), ("exported", moduleName published)]
  let exportNodes := if exports.isEmpty then [] else [node "ExportNamedDeclaration" [
    ("declaration", .null), ("source", .null), ("specifiers", toJson exports), ("attributes", .arr #[])]]
  return node "Program" ([("sourceType", .str "module"), ("body", toJson (imports ++ body ++ exportNodes))] ++ hashbang)

/-- Adapt the constructor's actual CoreJS modules to the existing ESTree
printer. No plan is reconstructed here. -/
def corePrograms (program : CoreJS.Program) (source : String) : Except String Json := do
  let names := program.modules.map (·.name)
  validateModulePaths names
  unless names.Nodup && names.contains program.entry do throw "invalid CoreJS output module paths"
  let hashbang := if source.startsWith "#!" then
    [("hashbang", Json.str (source.takeWhile fun c => !['\n', '\r', '\u2028', '\u2029'].contains c).toString)]
    else []
  return Json.mkObj (← program.modules.mapM fun m => do
    return (m.name, ← coreModule m (if m.name == program.entry then hashbang else [])))

end EsmSplit.ESTreeEmit
