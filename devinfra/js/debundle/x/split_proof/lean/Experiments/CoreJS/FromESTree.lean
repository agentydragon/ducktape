import Lean.Data.Json
import Experiments.CoreJS.Syntax
import Experiments.CoreJS.Number

/-! Lower a module from the annotated ESTree used by the split checker.
Statement identities are the source ordinals Lean derives from the module body. -/

namespace CoreJS.FromESTree
open Lean (Json)

private def field (j : Json) (key : String) := j.getObjValAs? String key

private def identifier (j : Json) : Except String Name := do
  unless (← field j "type") == "Identifier" do throw "expected an identifier"
  field j "name"

private def literal (key : String) : Except String Expr := do
  match key with
  | "null" => return .null
  | "b:true" => return .bool true
  | "b:false" => return .bool false
  | _ =>
    if key.startsWith "s:" then
      let s := (key.drop 2).toString
      unless s.toList.all (·.toNat ≤ 0xffff) do throw "string outside the Basic Multilingual Plane"
      return .str s
    if key.startsWith "n:" then
      let s := (key.drop 2).toString
      let n := Num.ofString s
      unless !n.isNaN && Num.toString n == s do throw s!"invalid numeric literal {key}"
      return .num n
    throw "unsupported literal"

def expr (j : Json) : Except String Expr := do
  match ← field j "type" with
  | "Literal" => literal (← field j "lit")
  | "Identifier" =>
    let x ← identifier j
    match ← field j "k" with
    | "T" => return .var x
    | "G" =>
      match x with
      | "undefined" => return .undef
      | "Infinity" => return .num Num.inf
      | "NaN" => return .num Num.nan
      | _ => throw s!"unsupported global {x}"
    | k => throw s!"unsupported binding class {k}"
  | kind => throw s!"unsupported expression {kind}"

def moduleBinding (id : Json) : Except String Name := do
  let x ← identifier id
  unless (← field id "k") == "T" do throw "expected a module binding"
  return x

private def declaration (j : Json) : Except String (Name × Stmt) := do
  let #[d] ← j.getObjVal? "declarations" >>= Json.getArr? | throw "expected a single declarator"
  let x ← d.getObjVal? "id" >>= moduleBinding
  let init ← d.getObjVal? "init"
  match ← field j "kind", init with
  | "var", .null => return (x, .varDecl x)
  | "let", .null => return (x, .decl .let_ x .undef)
  | "var", e => return (x, .decl .var x (← expr e))
  | "let", e => return (x, .decl .let_ x (← expr e))
  | "const", e => return (x, .decl .const x (← expr e))
  | kind, _ => throw s!"unsupported declaration {kind}"

def unit (j : Json) : Except String (Option Stmt × List (Name × Name)) := do
  match ← field j "type" with
  | "VariableDeclaration" => return (some (← declaration j).2, [])
  | "ExpressionStatement" => return (some (.expr (← expr (← j.getObjVal? "expression"))), [])
  | "ThrowStatement" => return (some (.throw (← expr (← j.getObjVal? "argument"))), [])
  | "EmptyStatement" | "DebuggerStatement" => return (none, [])
  | "ExportNamedDeclaration" =>
    unless (← j.getObjVal? "source") == .null do throw "re-export from another module"
    let d ← j.getObjVal? "declaration"
    if d != .null then
      unless (← field d "type") == "VariableDeclaration" do throw "unsupported exported declaration"
      let (x, s) ← declaration d
      return (some s, [(x, x)])
    let specs ← j.getObjVal? "specifiers" >>= Json.getArr?
    let names ← specs.toList.mapM fun sp => do
      return (← identifier (← sp.getObjVal? "exported"), ← identifier (← sp.getObjVal? "local"))
    return (none, names)
  | kind => throw s!"unsupported statement {kind}"

def program (asts : Array Json) : Except String Program := do
  let mut body := #[]
  let mut exports := #[]
  for (j, i) in asts.zipIdx do
    let (s, names) ← (unit j).mapError fun why => s!"statement {i}: {why}"
    if let some st := s then body := body.push (i, st)
    exports := exports ++ names.toArray
  return { entry := "main", modules := [{ name := "main", imports := [], body := body.toList, exports := exports.toList }] }

end CoreJS.FromESTree
