import Lean.Data.Json
import EsmSplit.CoreJS.Syntax
import EsmSplit.CoreJS.Number

/-! Lower a module from the annotated ESTree used by the split checker.
Statement identities are the source ordinals Lean derives from the module body. -/

namespace CoreJS.FromESTree
open Lean (Json)

private def field (j : Json) (key : String) := j.getObjValAs? String key

def identifier (j : Json) : Except String Name := do
  unless (← field j "type") == "Identifier" do throw "expected an identifier"
  let name ← field j "name"
  -- Generated bindings start with a colon, which cannot begin an ECMAScript identifier.
  if name.startsWith ":" then throw "invalid identifier name"
  return name

/-- Module requests and quoted export names carry their strings in ESTree's value field.
Synthesized glue supplies this syntax directly. -/
def stringLiteral (j : Json) : Except String String := do
  unless (← field j "type") == "Literal" && !(j.getObjVal? "bigint").isOk && !(j.getObjVal? "regex").isOk do
    throw "expected a string literal"
  let value ← field j "value"
  if let .ok lit := j.getObjVal? "lit" then
    unless lit == .str ("s:" ++ value) do throw "string literal differs from its annotation"
  return value

/-- The public side of a named import or export can be an identifier or a quoted name. -/
def moduleExportName (j : Json) : Except String Name := do
  if (← field j "type") == "Literal" then stringLiteral j else identifier j

private def stringExpr (s : String) : Except String Expr := do
  unless s.toList.all (·.toNat ≤ 0xffff) do throw "string outside the Basic Multilingual Plane"
  return .str s

private def literal (key : String) : Except String Expr := do
  match key with
  | "null" => return .null
  | "b:true" => return .bool true
  | "b:false" => return .bool false
  | _ =>
    if key.startsWith "s:" then
      return ← stringExpr (key.drop 2).toString
    if key.startsWith "n:" then
      let s := (key.drop 2).toString
      let n := Num.ofString s
      unless !n.isNaN && Num.toString n == s do throw s!"invalid numeric literal {key}"
      return .num n
    throw "unsupported literal"

/- A JSON object stores its children in a tree map. A successful field lookup therefore
   gives a smaller JSON value for recursive expression lowering. -/
private theorem treeGetSizeOfLt
    (t : Std.DTreeMap.Internal.Impl String (fun _ => Json)) (key : String)
    {v : Json} (h : Std.DTreeMap.Internal.Impl.Const.get? t key = some v) :
    sizeOf v < sizeOf t := by
  induction t with
  | leaf => simp [Std.DTreeMap.Internal.Impl.Const.get?] at h
  | inner sz k value l r ihl ihr =>
    dsimp [Std.DTreeMap.Internal.Impl.Const.get?] at h
    split at h
    · exact Nat.lt_of_lt_of_le (ihl h) (by simp +arith)
    · exact Nat.lt_of_lt_of_le (ihr h) (by simp +arith)
    · injection h with hv
      subst v
      simp +arith

private theorem objValSizeOfLt {j v : Json} {key : String}
    (h : j.getObjVal? key = .ok v) : sizeOf v < sizeOf j := by
  cases j <;> try contradiction
  rename_i kvs
  cases hget : kvs.get? key with
  | none => simp [Json.getObjVal?, hget] at h
  | some w =>
    simp [Json.getObjVal?, hget] at h
    injection h with heq
    subst v
    obtain ⟨⟨tree⟩⟩ := kvs
    have smaller := treeGetSizeOfLt tree key hget
    simp +arith
    omega

/-- Read a scope-annotated reference. The linked fragment also accepts local references
inside function bodies. -/
private def reference (imports : List Name) (inFunction : Bool) (j : Json) : Except String Expr := do
  let x ← identifier j
  match ← field j "k" with
  | "T" => return .var x
  | "L" =>
    unless inFunction do throw s!"unexpected function-local name {x}"
    if x == "arguments" then throw "unsupported implicit arguments reference"
    return .var x
  | "V" =>
    unless imports.contains x do throw s!"unlinked imported name {x}"
    return .var x
  | "G" =>
    match x with
    | "undefined" => return .undef
    | "Infinity" => return .num Num.inf
    | "NaN" => return .num Num.nan
    | _ => throw s!"unsupported global {x}"
  | k => throw s!"unsupported binding class {k}"

private def unary (op : String) (argument : Json) (lower : Unit → Except String Expr) :
    Except String Expr := do
  match op with
  | "!" => return .unary .not (← lower ())
  | "-" =>
    unless (← field argument "type") == "Literal" do throw "expected a numeric literal after unary minus"
    let .num n ← literal (← field argument "lit") | throw "expected a numeric literal after unary minus"
    return .unary .neg (.num n)
  | _ => throw s!"unsupported unary operator {op}"

private def exprWithImports (imports : List Name) (j : Json) : Except String Expr := do
  match ← field j "type" with
  | "Literal" => literal (← field j "lit")
  | "Identifier" => reference imports false j
  | "UnaryExpression" =>
    match _lookup : j.getObjVal? "argument" with
    | .error err => throw err
    | .ok argument =>
      unary (← field j "operator") argument (fun _ => exprWithImports imports argument)
  | kind => throw s!"unsupported expression {kind}"
termination_by sizeOf j
decreasing_by
  exact objValSizeOfLt _lookup

def expr := exprWithImports []

def moduleBinding (id : Json) : Except String Name := do
  let x ← identifier id
  unless (← field id "k") == "T" do throw "expected a module binding"
  return x

private def declaratorWith (binder : Json → Except String Name) (initializer : Name → Json → Except String Expr)
    (kind : String) (d : Json) : Except String (Name × Stmt) := do
  unless (← field d "type") == "VariableDeclarator" do throw "expected a variable declarator"
  let x ← d.getObjVal? "id" >>= binder
  let init ← d.getObjVal? "init"
  match kind, init with
  | "var", .null => return (x, .varDecl x)
  | "let", .null => return (x, .decl .let_ x .undef)
  | "var", e => return (x, .decl .var x (← initializer x e))
  | "let", e => return (x, .decl .let_ x (← initializer x e))
  | "const", .null => throw "const declaration needs an initializer"
  | "const", e => return (x, .decl .const x (← initializer x e))
  | kind, _ => throw s!"unsupported declaration {kind}"

private def declarationWith (binder : Json → Except String Name) (initializer : Name → Json → Except String Expr)
    (j : Json) : Except String (Name × Stmt) := do
  unless (← field j "type") == "VariableDeclaration" do throw "expected a variable declaration"
  let #[d] ← j.getObjVal? "declarations" >>= Json.getArr? | throw "expected a single declarator"
  declaratorWith binder initializer (← field j "kind") d

/-- Decode an emitted declaration. Retained references to module bindings keep their source
annotation and resolve by name in the emitted module's scope. -/
def declaration (imports : List Name) (j : Json) : Except String (Name × Stmt) :=
  declarationWith moduleBinding (fun _ => exprWithImports imports) j

private def statementWith (lower : Json → Except String Expr) (decl : Json → Except String (Name × Stmt))
    (inFunction : Bool) (j : Json) : Except String Stmt := do
  match ← field j "type" with
  | "VariableDeclaration" => return (← decl j).2
  | "ExpressionStatement" => return .expr (← lower (← j.getObjVal? "expression"))
  | "ThrowStatement" => return .throw (← lower (← j.getObjVal? "argument"))
  | "ReturnStatement" =>
    unless inFunction do throw "unexpected module-level return"
    let arg ← j.getObjVal? "argument"
    let value ← if arg == .null then pure .undef else lower arg
    return .ret value
  | kind => throw s!"unsupported statement {kind}"

private def localBinding (id : Json) : Except String Name := do
  let name ← identifier id
  unless (← field id "k") == "L" do throw "expected a function-local binding"
  return name

/-- Flat object-pattern bindings in property evaluation order. Even an empty pattern
checks coercibility, so it cannot be lowered to an empty sequence of property reads. -/
private def linkedObjectPattern (j : Json) : Except String (List (Name × Name)) := do
  unless (← field j "type") == "ObjectPattern" do throw "expected an object pattern"
  let props ← j.getObjValAs? (List Json) "properties"
  if props.isEmpty then throw "unsupported empty object pattern"
  props.mapM fun p => do
    unless (← field p "type") == "Property" do throw "expected a static object pattern property"
    unless (← field p "kind") == "init" &&
        !(← p.getObjValAs? Bool "computed") && !(← p.getObjValAs? Bool "method") do
      throw "expected a static object pattern property"
    let key ← moduleExportName (← p.getObjVal? "key")
    unless key.toList.all (·.toNat ≤ 0xffff) do throw "property key outside the Basic Multilingual Plane"
    let target ← p.getObjVal? "value"
    unless (← field target "type") == "Identifier" do throw "expected an identifier in object pattern"
    return (key, ← localBinding target)

/-- These expressions produce strings whenever they complete normally. Loose inequality
between two such operands never invokes numeric or object coercion. -/
private def stringOperand : Expr → Bool
  | .str _ | .unary .typeof _ => true
  | _ => false

/-- Static own data properties, evaluated in source order on a fresh ordinary object. -/
private def linkedObject (lower : Json → Except String Expr) (j : Json) : Except String Expr := do
  let mut seen : Std.HashSet String := {}
  let mut props := #[]
  for p in ← j.getObjValAs? (Array Json) "properties" do
    unless (← field p "type") == "Property" do throw "expected a static data property"
    unless (← field p "kind") == "init" &&
        !(← p.getObjValAs? Bool "computed") && !(← p.getObjValAs? Bool "method") do
      throw "expected a static data property"
    let key ← moduleExportName (← p.getObjVal? "key")
    unless key.toList.all (·.toNat ≤ 0xffff) do throw "property key outside the Basic Multilingual Plane"
    if key == "__proto__" then throw "unsupported __proto__ property"
    if seen.contains key then throw "duplicate object property"
    seen := seen.insert key
    let v ← p.getObjVal? "value"
    -- A direct anonymous function initializer would take its name from the property key.
    let value ← match ← field v "type" with
      | "LogicalExpression" => lower v
      | "Literal" | "Identifier" | "ObjectExpression" | "ArrayExpression" | "MemberExpression"
      | "CallExpression" | "SequenceExpression" | "AssignmentExpression" => lower v
      | kind => throw s!"unsupported object property value {kind}"
    props := props.push (.init (.str key) value)
  return .obj props.toList

/-- In an untagged template each substitution is converted before the next
expression runs. Raw escape spellings are observable only in a tagged template. -/
private def linkedTemplate (lower : Json → Except String Expr) (j : Json) : Except String Expr := do
  let quasis ← j.getObjValAs? (List Json) "quasis"
  let expressions ← j.getObjValAs? (List Json) "expressions"
  let first :: rest := quasis | throw "expected an initial template element"
  unless rest.length == expressions.length do throw "template element count does not match substitutions"
  let cooked := fun q => do
    unless (← field q "type") == "TemplateElement" do throw "expected a template element"
    stringExpr (← field (← q.getObjVal? "value") "cooked")
  let mut result ← cooked first
  for (expression, quasi) in expressions.zip rest do
    let value := Expr.unary .toString (← lower expression)
    result := .binary .add result value
    result := .binary .add result (← cooked quasi)
  return result

/-- Decode a property access, retaining the receiver and the unevaluated key for
the interpreter's property read, call, or write. -/
private def linkedMember (lower : Json → Except String Expr) (j : Json) : Except String (Expr × Expr) := do
  if ← j.getObjValAs? Bool "optional" then throw "unsupported optional member access"
  let obj ← j.getObjVal? "object"
  let prop ← j.getObjVal? "property"
  if (← field obj "type") == "Super" then throw "unsupported super member access"
  if (← field prop "type") == "PrivateIdentifier" then throw "unsupported private member access"
  let base ← lower obj
  let key ← if ← j.getObjValAs? Bool "computed" then lower prop else identifier prop >>= stringExpr
  return (base, key)

private def pairSequences : List Expr → List Expr
  | a :: b :: rest => .seq a b :: pairSequences rest
  | rest => rest

/-- Successive pairing keeps ESTree's left-to-right order and final value.
A flat operand list then needs logarithmic expression depth in CoreJS. -/
private partial def sequenceTree (head : Expr) : List Expr → Expr
  | [] => head
  | next :: tail => sequenceTree (.seq head next) (pairSequences tail)

mutual

private partial def linkedExpr (imports : List Name) (inFunction : Bool) (nameHint : Name)
    (j : Json) : Except String Expr := do
  let kind ← field j "type"
  match kind with
  | "Literal" => literal (← field j "lit")
  | "Identifier" => reference imports inFunction j
  | "ThisExpression" => return .this
  | "MemberExpression" =>
    let (base, key) ← linkedMember (linkedExpr imports inFunction "") j
    return .get base key
  | "AssignmentExpression" =>
    let op ← field j "operator"
    unless op == "=" do throw s!"unsupported linked assignment operator {op}"
    let target ← j.getObjVal? "left"
    let targetKind ← field target "type"
    unless targetKind == "MemberExpression" do throw s!"unsupported linked assignment target {targetKind}"
    -- ECMAScript infers an anonymous RHS function's name only for an identifier target.
    let lower := linkedExpr imports inFunction ""
    let (base, key) ← linkedMember lower target
    return .assign (.member base key) (← lower (← j.getObjVal? "right"))
  | "ObjectExpression" => linkedObject (linkedExpr imports inFunction "") j
  | "ArrayExpression" =>
    let elements ← j.getObjValAs? (List Json) "elements"
    let lower := linkedExpr imports inFunction ""
    let values : List (ArrayElementKind × Expr) ← elements.mapM fun element => do
      if element == .null then throw "unsupported array hole"
      if (← field element "type") == "SpreadElement" then
        unless inFunction do throw "unsupported array spread"
        return (.spread, ← lower (← element.getObjVal? "argument"))
      return (.element, ← lower element)
    if values.any (fun (kind, _) => kind == .spread) then return .arrWithSpread values
    return .arr (values.map Prod.snd)
  | "TemplateLiteral" => linkedTemplate (linkedExpr imports inFunction "") j
  | "UnaryExpression" =>
    let argument ← j.getObjVal? "argument"
    let op ← field j "operator"
    let lower := linkedExpr imports inFunction ""
    if op == "typeof" then return .unary .typeof (← lower argument)
    unary op argument (fun _ => lower argument)
  | "BinaryExpression" =>
    let op ← field j "operator"
    let lower := linkedExpr imports inFunction ""
    let left ← lower (← j.getObjVal? "left")
    let right ← lower (← j.getObjVal? "right")
    match op with
    | "===" => return .binary .strictEq left right
    | "!==" => return .binary .strictNe left right
    | "!=" =>
      unless stringOperand left && stringOperand right do
        throw "linked != requires two string operands (string literals or typeof expressions)"
      return .binary .ne left right
    | _ => throw s!"unsupported linked binary operator {op}"
  | "LogicalExpression" =>
    let op : LogOp ← match ← field j "operator" with
      | "&&" => pure .and
      | "||" => pure .or
      | "??" => pure .nullish
      | other => throw s!"unsupported linked logical operator {other}"
    let lower := linkedExpr imports inFunction ""
    return .logical op (← lower (← j.getObjVal? "left")) (← lower (← j.getObjVal? "right"))
  | "ConditionalExpression" =>
    let lower := linkedExpr imports inFunction ""
    return .cond (← lower (← j.getObjVal? "test")) (← lower (← j.getObjVal? "consequent"))
      (← lower (← j.getObjVal? "alternate"))
  | "SequenceExpression" =>
    let expressions ← j.getObjValAs? (List Json) "expressions"
    let first :: second :: rest := expressions | throw "expected at least two sequence operands"
    let lower := linkedExpr imports inFunction ""
    return sequenceTree (← lower first) (← (second :: rest).mapM lower)
  | "CallExpression" =>
    if ← j.getObjValAs? Bool "optional" then throw "unsupported optional call"
    let lower := linkedExpr imports inFunction ""
    let callee ← j.getObjVal? "callee"
    let args ← j.getObjValAs? (List Json) "arguments"
    if (← field callee "type") == "MemberExpression" then
      let (base, key) ← linkedMember lower callee
      return .callMethod base key (← args.mapM lower)
    return .call (← lower callee) (← args.mapM lower)
  | "ArrowFunctionExpression" | "FunctionExpression" =>
    let arrow := kind == "ArrowFunctionExpression"
    let id ← if arrow then pure Json.null else j.getObjVal? "id"
    let name ← if id == .null then pure nameHint else localBinding id
    return .func (← linkedFunction imports (if arrow then .arrow else .normal) name (id != .null) j)
  | _ => throw s!"unsupported linked expression {kind}"

private partial def linkedFunction (imports : List Name) (kind : FuncKind) (name : Name) (self : Bool)
    (j : Json) : Except String Func := do
  if (← j.getObjValAs? Bool "async") || (← j.getObjValAs? Bool "generator") then
    throw "unsupported async or generator function"
  let mut params : Array Name := #[]
  let mut bound : Array Name := #[]
  let mut bindings : Array Stmt := #[]
  for (param, i) in (← j.getObjValAs? (Array Json) "params").zipIdx do
    match ← field param "type" with
    | "Identifier" =>
      let x ← localBinding param
      params := params.push x
      bound := bound.push x
    | "ObjectPattern" =>
      let fields ← linkedObjectPattern param
      let arg := s!":parameter:{i}"
      params := params.push arg
      for (key, x) in fields do
        bound := bound.push x
        bindings := bindings.push (.decl .var x (.get (.var arg) (.str key)))
    | other => throw s!"unsupported function parameter {other}"
  unless bound.toList.Nodup do throw "duplicate function parameter"
  let body ← j.getObjVal? "body"
  let stmts ← if (← field body "type") == "BlockStatement" then
    linkedFunctionBody imports body
  else if kind == .arrow then
    pure [.ret (← linkedExpr imports true "" body)]
  else throw "expected a function body block"
  -- Static keys and identifier targets have no parameter expressions. The reads
  -- run before the body, using the same mutable cells as any body var redeclarations.
  return .mk name kind self params.toList (bindings.toList ++ stmts)

private partial def linkedLocalDeclarations (imports : List Name) (offset : Nat) (j : Json) :
    Except String (Array Stmt) := do
  let declarations ← j.getObjValAs? (Array Json) "declarations"
  if declarations.isEmpty then throw "expected at least one declarator"
  let kind ← field j "kind"
  let mut result := #[]
  for d in declarations do
    let id ← d.getObjVal? "id"
    if (← field id "type") == "ObjectPattern" then
      unless (← field d "type") == "VariableDeclarator" do throw "expected a variable declarator"
      let declKind ← match kind with
        | "var" => pure DeclKind.var
        | "let" => pure DeclKind.let_
        | "const" => pure DeclKind.const
        | _ => throw s!"unsupported declaration {kind}"
      let bindings ← linkedObjectPattern id
      let initializer ← d.getObjVal? "init"
      if initializer == .null then throw "object pattern declaration needs an initializer"
      let value ← linkedExpr imports true "" initializer
      if let [(key, bound)] := bindings then
        result := result.push (.decl declKind bound (.get value (.str key)))
      else
        -- Keep the initializer value while each read and binding runs in source order.
        let source := s!":destructure:{offset + result.size}"
        result := result.push (.decl .const source value)
        for (key, bound) in bindings do
          result := result.push (.decl declKind bound (.get (.var source) (.str key)))
    else
      result := result.push (← Prod.snd <$> declaratorWith localBinding (linkedExpr imports true) kind d)
  return result

/-- Keep every declarator in its enclosing statement list. Function instantiation hoists
all its vars, and block instantiation creates the lexical bindings before initialization. -/
private partial def linkedFunctionBody (imports : List Name) (block : Json) : Except String (List Stmt) := do
  let mut result := #[]
  for stmt in ← block.getObjValAs? (Array Json) "body" do
    if (← field stmt "type") == "VariableDeclaration" then
      result := result ++ (← linkedLocalDeclarations imports result.size stmt)
    else
      result := result.push (← linkedStatement imports true stmt)
  return result.toList

/-- A block introduces a fresh lexical scope each time it executes. Function `var`s
still hoist through it via `Stmt.varNames`. -/
private partial def linkedFunctionBlock (imports : List Name) (j : Json) : Except String Stmt := do
  unless (← field j "type") == "BlockStatement" do throw "expected a statement block"
  return .block (← linkedFunctionBody imports j)

private partial def linkedStatement (imports : List Name) (inFunction : Bool) (j : Json) :
    Except String Stmt := do
  if inFunction then
    match ← field j "type" with
    | "BlockStatement" => return ← linkedFunctionBlock imports j
    | "EmptyStatement" => return .block []
    | "IfStatement" =>
      let test ← linkedExpr imports true "" (← j.getObjVal? "test")
      let consequent ← linkedStatement imports true (← j.getObjVal? "consequent")
      let alternate ← j.getObjVal? "alternate"
      let elseBranch ← if alternate == .null then pure (.block []) else linkedStatement imports true alternate
      return .ite test consequent elseBranch
    | "VariableDeclaration" =>
      -- An unbraced branch can declare vars, which already belong to the enclosing function.
      unless (← field j "kind") == "var" do throw "expected a var declaration in statement position"
      return .block (← linkedLocalDeclarations imports 0 j).toList
    | "TryStatement" =>
      let body ← linkedFunctionBlock imports (← j.getObjVal? "block")
      let handler ← j.getObjVal? "handler"
      let finalizer ← j.getObjVal? "finalizer"
      if handler == .null && finalizer == .null then throw "expected catch or finally"
      let protectedBody ← if handler == .null then pure body else do
        unless (← field handler "type") == "CatchClause" do throw "expected a catch clause"
        let param ← handler.getObjVal? "param"
        let name ← if param == .null then pure none else some <$> localBinding param
        pure (.tryCatch body name (← linkedFunctionBlock imports (← handler.getObjVal? "body")))
      if finalizer == .null then return protectedBody
      return .tryFinally protectedBody (← linkedFunctionBlock imports finalizer)
    | _ => pure ()
  let binder := if inFunction then localBinding else moduleBinding
  statementWith (linkedExpr imports inFunction "") (declarationWith binder (linkedExpr imports inFunction))
    inFunction j

end

/-- Function declarations initialize their module cells at link time. Recursive reads resolve
through the module scope. -/
def linkedDeclaration (imports : List Name) (j : Json) : Except String (Name × Stmt) := do
  if (← field j "type") == "FunctionDeclaration" then
    let name ← j.getObjVal? "id" >>= moduleBinding
    return (name, .funDecl (← linkedFunction imports .normal name false j))
  declarationWith moduleBinding (linkedExpr imports false) j

/-- A top-level executable statement of the linked fragment. -/
def linkedUnit (imports : List Name) (j : Json) : Except String Stmt := do
  if (← field j "type") == "FunctionDeclaration" then
    return (← linkedDeclaration imports j).2
  linkedStatement imports false j

/-- A named export of a local binding, in (public, local) order. -/
def exportSpec (j : Json) : Except String (Name × Name) := do
  unless (← field j "type") == "ExportSpecifier" do throw "expected a local named export"
  return (← moduleExportName (← j.getObjVal? "exported"), ← identifier (← j.getObjVal? "local"))

def unit (j : Json) : Except String (Option Stmt × List (Name × Name)) := do
  match ← field j "type" with
  | "EmptyStatement" | "DebuggerStatement" => return (none, [])
  | "ExportNamedDeclaration" =>
    unless (← j.getObjVal? "source") == .null do throw "re-export from another module"
    let d ← j.getObjVal? "declaration"
    if d != .null then
      unless (← field d "type") == "VariableDeclaration" do throw "unsupported exported declaration"
      let (x, s) ← declaration [] d
      return (some s, [(x, x)])
    let specs ← j.getObjValAs? (List Json) "specifiers"
    let names ← specs.mapM exportSpec
    return (none, names)
  | _ => return (some (← statementWith expr (declaration []) false j), [])

def program (asts : Array Json) : Except String Program := do
  let mut body := #[]
  let mut exports := #[]
  for (j, i) in asts.zipIdx do
    let (s, names) ← (unit j).mapError fun why => s!"statement {i}: {why}"
    if let some st := s then body := body.push (i, st)
    exports := exports ++ names.toArray
  return { entry := "main", modules := [{ name := "main", imports := [], body := body.toList, exports := exports.toList }] }

end CoreJS.FromESTree
