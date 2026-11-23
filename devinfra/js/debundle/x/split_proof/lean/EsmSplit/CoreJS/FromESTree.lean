import Lean.Data.Json
import EsmSplit.CoreJS.HostProfile
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

/-- A string literal consumed directly by a relational comparison has no use that can
observe its length or index its code units. The comparison itself uses UTF-16 order. -/
private def relationalOperand (lower : Json → Except String Expr) (j : Json) : Except String Expr := do
  if (← field j "type") == "Literal" then
    if (← field j "lit").startsWith "s:" then
      return .str (← stringLiteral j)
  lower j

private def literal (key : String) : Except String Expr := do
  match key with
  | "null" => return .null
  | "b:true" => return .bool true
  | "b:false" => return .bool false
  | _ =>
    if key.startsWith "s16:" then throw "string literal contains an isolated UTF-16 surrogate"
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

/-- These expressions produce strings whenever they complete normally. Loose equality and inequality
between two such operands never invoke numeric or object coercion. -/
private def stringOperand : Expr → Bool
  | .str _ | .unary .typeof _ | .prim (.globalTypeof _) [] => true
  | _ => false

/-- Decimal conversion is exact for nonnegative safe integer property names. -/
private def staticPropertyName (key : Json) : Except String Name := do
  if (← field key "type") == "Literal" then
    if let .ok annotation := field key "lit" then
      if annotation.startsWith "n:" then
        let n ← key.getObjValAs? Nat "value"
        unless n ≤ 9007199254740991 && annotation == s!"n:{n}" do throw "unsupported numeric property key"
        return toString n
  moduleExportName key

/-- Evaluate static-name elements in source order. Only a colon-form __proto__ initializer
sets the prototype. Shorthand properties and methods with that name define own properties. -/
private def linkedObject (lower : Name → Json → Except String Expr)
    (method : Name → Json → Except String Func) (j : Json) : Except String Expr := do
  let mut seen : Std.HashMap String String := {}
  let mut props := #[]
  for p in ← j.getObjValAs? (Array Json) "properties" do
    unless (← field p "type") == "Property" do throw "expected a static data property or method"
    if ← p.getObjValAs? Bool "computed" then throw "expected a static data property or method"
    let key ← staticPropertyName (← p.getObjVal? "key")
    let keyExpr ← stringExpr key
    let kind ← field p "kind"
    if let some previous := seen[key]? then
      unless (previous == "get" && kind == "set") || (previous == "set" && kind == "get") do
        throw "duplicate object property"
      seen := seen.insert key "pair"
    else seen := seen.insert key kind
    let value ← p.getObjVal? "value"
    let prop ← match kind with
      | "get" => pure (.getter keyExpr (← method ("get " ++ key) value))
      | "set" => pure (.setter keyExpr (← method ("set " ++ key) value))
      | "init" =>
        if ← p.getObjValAs? Bool "method" then
          pure (.init keyExpr (.func (← method key value)))
        else if key == "__proto__" && !(← p.getObjValAs? Bool "shorthand") then
          pure (.proto (← lower "" value))
        else pure (.init keyExpr (← lower key value))
      | _ => throw "unsupported object property kind"
    props := props.push prop
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
    if let .ok false := q.getObjValAs? Bool "cookedWellFormed" then
      throw "template text contains an isolated UTF-16 surrogate"
    stringExpr (← field (← q.getObjVal? "value") "cooked")
  let mut result ← cooked first
  for (expression, quasi) in expressions.zip rest do
    let value := Expr.unary .toString (← lower expression)
    result := .binary .add result value
    result := .binary .add result (← cooked quasi)
  return result

/-- Decode a property access, retaining the receiver and the unevaluated key for
the interpreter's property read, call, or write. -/
private def linkedMember (lower : Json → Except String Expr) (j : Json) (allowOptional := false) :
    Except String (Expr × Expr) := do
  if (← j.getObjValAs? Bool "optional") && !allowOptional then throw "unsupported optional member access"
  let obj ← j.getObjVal? "object"
  let prop ← j.getObjVal? "property"
  if (← field obj "type") == "Super" then throw "unsupported super member access"
  if (← field prop "type") == "PrivateIdentifier" then throw "unsupported private member access"
  let base ← lower obj
  let key ← if ← j.getObjValAs? Bool "computed" then lower prop else identifier prop >>= stringExpr
  return (base, key)

private def linkedTarget (imports : List Name) (inFunction allowFrozen : Bool)
    (lower : Json → Except String Expr) (j : Json) : Except String Target := do
  match ← field j "type" with
  | "Identifier" =>
    let .var name ← reference imports inFunction j
      | throw "expected a binding in linked assignment"
    -- Glue imports keep the original module reference annotation. Local references shadow imports.
    let scope ← field j "k"
    if scope != "L" && imports.contains name then
      unless allowFrozen do throw s!"unsupported linked assignment to imported binding {name}"
      return .frozen name
    return .var name
  | "MemberExpression" =>
    let (base, key) ← linkedMember lower j
    return .member base key
  | kind => throw s!"unsupported linked assignment target {kind}"

private def pairSequences : List Expr → List Expr
  | a :: b :: rest => .seq a b :: pairSequences rest
  | rest => rest

/-- Successive pairing keeps ESTree's left-to-right order and final value.
A flat operand list then needs logarithmic expression depth in CoreJS. -/
private partial def sequenceTree (head : Expr) : List Expr → Expr
  | [] => head
  | next :: tail => sequenceTree (.seq head next) (pairSequences tail)

private partial def containsBreak (j : Json) : Bool :=
  match j with
  | .obj fields => (field j "type").toOption == some "BreakStatement" ||
      fields.toList.any (fun (_, child) => containsBreak child)
  | .arr items => items.any containsBreak
  | _ => false

mutual

private partial def linkedExpr (profile : HostProfile) (imports : List Name) (inFunction : Bool) (nameHint : Name)
    (j : Json) : Except String Expr := do
  let kind ← field j "type"
  match kind with
  | "Literal" => literal (← field j "lit")
  | "Identifier" => reference imports inFunction j
  | "ThisExpression" => return .this
  | "MemberExpression" =>
    let (base, key) ← linkedMember (linkedExpr profile imports inFunction "") j
    return .get base key
  | "ChainExpression" =>
    let body ← j.getObjVal? "expression"
    let lower := linkedExpr profile imports inFunction ""
    if (← field body "type") == "CallExpression" then
      let optionalCallee ← body.getObjValAs? Bool "optional"
      let member ← body.getObjVal? "callee"
      let reason := if optionalCallee then "unsupported optional call" else "unsupported optional chain"
      unless (← field member "type") == "MemberExpression" do throw reason
      let optionalReceiver ← member.getObjValAs? Bool "optional"
      if optionalCallee == optionalReceiver then throw reason
      if ← member.getObjValAs? Bool "computed" then throw "unsupported computed optional method call"
      let (base, key) ← linkedMember lower member (allowOptional := optionalReceiver)
      let args ← (← body.getObjValAs? (List Json) "arguments").mapM lower
      return if optionalCallee then .callOptionalCallee base key args else .callOptionalReceiver base key args
    let member := body
    unless (← field member "type") == "MemberExpression" do throw "unsupported optional chain"
    unless ← member.getObjValAs? Bool "optional" do throw "unsupported optional chain"
    let receiver ← member.getObjVal? "object"
    unless (← field receiver "type") == "Identifier" do
      throw "optional member base is not a lexical binding"
    let (base, key) ← linkedMember lower member (allowOptional := true)
    let .var _ := base | throw "optional member base is not a lexical binding"
    -- The guards only read a lexical cell. No effects intervene before the
    -- property read fetches its base, then evaluates the key and reads the property.
    let absent := Expr.logical .or (.binary .strictEq base .undef) (.binary .strictEq base .null)
    return .cond absent .undef (.get base key)
  | "AssignmentExpression" =>
    let op ← field j "operator"
    let lower := linkedExpr profile imports inFunction ""
    let target ← j.getObjVal? "left" >>= linkedTarget imports inFunction (op == "=") lower
    let rhs ← j.getObjVal? "right"
    let targetName := match target with
      | .var name | .frozen name => name
      | _ => ""
    let named := linkedExpr profile imports inFunction targetName
    match op with
    | "=" => return .assign target (← named rhs)
    | "+=" => return .assignOp target .add (← lower rhs)
    | "-=" => return .assignOp target .sub (← lower rhs)
    | "*=" => return .assignOp target .mul (← lower rhs)
    | "/=" => return .assignOp target .div (← lower rhs)
    | "%=" => return .assignOp target .mod (← lower rhs)
    | "&=" => return .assignOp target .bitAnd (← lower rhs)
    | "|=" => return .assignOp target .bitOr (← lower rhs)
    | "^=" => return .assignOp target .bitXor (← lower rhs)
    | "<<=" => return .assignOp target .shl (← lower rhs)
    | ">>=" => return .assignOp target .shr (← lower rhs)
    | ">>>=" => return .assignOp target .ushr (← lower rhs)
    | "&&=" => return .assignLogical target .and (← named rhs)
    | "||=" => return .assignLogical target .or (← named rhs)
    | "??=" => return .assignLogical target .nullish (← named rhs)
    | _ => throw s!"unsupported linked assignment operator {op}"
  | "UpdateExpression" =>
    let inc ← match ← field j "operator" with
      | "++" => pure true
      | "--" => pure false
      | op => throw s!"unsupported linked update operator {op}"
    let target ← j.getObjVal? "argument" >>= linkedTarget imports inFunction false (linkedExpr profile imports inFunction "")
    return .update target inc (← j.getObjValAs? Bool "prefix")
  | "ObjectExpression" =>
    linkedObject (linkedExpr profile imports inFunction) (fun name => linkedFunction profile imports .method name false) j
  | "ArrayExpression" =>
    let elements ← j.getObjValAs? (List Json) "elements"
    let lower := linkedExpr profile imports inFunction ""
    let values : List (ArrayElementKind × Expr) ← elements.mapM fun element => do
      if element == .null then throw "unsupported array hole"
      if (← field element "type") == "SpreadElement" then
        unless inFunction do throw "unsupported array spread"
        return (.spread, ← lower (← element.getObjVal? "argument"))
      return (.element, ← lower element)
    if values.any (fun (kind, _) => kind == .spread) then return .arrWithSpread values
    return .arr (values.map Prod.snd)
  | "TemplateLiteral" => linkedTemplate (linkedExpr profile imports inFunction "") j
  | "UnaryExpression" =>
    let argument ← j.getObjVal? "argument"
    let operator ← field j "operator"
    let lower := linkedExpr profile imports inFunction ""
    if operator == "delete" then
      unless (← field argument "type") == "MemberExpression" do
        throw "expected a member expression after delete"
      let (base, key) ← linkedMember lower argument
      return .delete base key
    let op : UnOp ← match operator with
      | "+" => pure .pos
      | "-" => pure .neg
      | "!" => pure .not
      | "~" => pure .bitNot
      | "typeof" => pure .typeof
      | "void" => pure .void
      | other => throw s!"unsupported unary operator {other}"
    if op == .typeof && (← field argument "type") == "Identifier" && (field argument "k").toOption == some "G" then
      let name ← identifier argument
      if (profile.stableTypeof name).isSome then return .prim (.globalTypeof name) []
    return .unary op (← linkedExpr profile imports inFunction "" argument)
  | "BinaryExpression" =>
    let op ← field j "operator"
    let lower := linkedExpr profile imports inFunction ""
    let lower := if ["<", "<=", ">", ">="].contains op then relationalOperand lower else lower
    let left ← lower (← j.getObjVal? "left")
    let right ← lower (← j.getObjVal? "right")
    match op with
    | "+" => return .binary .add left right
    | "-" => return .binary .sub left right
    | "*" => return .binary .mul left right
    | "/" => return .binary .div left right
    | "%" => return .binary .mod left right
    | "&" => return .binary .bitAnd left right
    | "|" => return .binary .bitOr left right
    | "^" => return .binary .bitXor left right
    | "<<" => return .binary .shl left right
    | ">>" => return .binary .shr left right
    | ">>>" => return .binary .ushr left right
    | "<" => return .binary .lt left right
    | "<=" => return .binary .le left right
    | ">" => return .binary .gt left right
    | ">=" => return .binary .ge left right
    | "===" => return .binary .strictEq left right
    | "!==" => return .binary .strictNe left right
    | "in" => return .hasProp left right
    | "==" | "!=" =>
      unless stringOperand left && stringOperand right do
        throw s!"linked {op} requires two string operands (string literals or typeof expressions)"
      return .binary (if op == "==" then .eq else .ne) left right
    | _ => throw s!"unsupported linked binary operator {op}"
  | "LogicalExpression" =>
    let op : LogOp ← match ← field j "operator" with
      | "&&" => pure .and
      | "||" => pure .or
      | "??" => pure .nullish
      | other => throw s!"unsupported linked logical operator {other}"
    let lower := linkedExpr profile imports inFunction ""
    return .logical op (← lower (← j.getObjVal? "left")) (← lower (← j.getObjVal? "right"))
  | "ConditionalExpression" =>
    let lower := linkedExpr profile imports inFunction ""
    return .cond (← lower (← j.getObjVal? "test")) (← lower (← j.getObjVal? "consequent"))
      (← lower (← j.getObjVal? "alternate"))
  | "SequenceExpression" =>
    let expressions ← j.getObjValAs? (List Json) "expressions"
    let first :: second :: rest := expressions | throw "expected at least two sequence operands"
    let lower := linkedExpr profile imports inFunction ""
    return sequenceTree (← lower first) (← (second :: rest).mapM lower)
  | "ClassExpression" => linkedClass profile imports inFunction nameHint j
  | "NewExpression" =>
    let lower := linkedExpr profile imports inFunction ""
    let args ← j.getObjValAs? (List Json) "arguments"
    let callee ← j.getObjVal? "callee"
    if (field callee "k").toOption == some "G" && (← field callee "type") == "Identifier" then
      let name ← identifier callee
      if profile.opaqueConstructor name then
        -- Evaluation stops at the callee, before an argument can throw or mutate state.
        -- Retain argument syntax and binding references in the deferred function.
        return .new (.prim (.opaqueConstructorRef name) []) (← args.mapM lower)
    return .new (← lower callee) (← args.mapM lower)
  | "CallExpression" =>
    if ← j.getObjValAs? Bool "optional" then throw "unsupported optional call"
    let lower := linkedExpr profile imports inFunction ""
    let callee ← j.getObjVal? "callee"
    let args ← j.getObjValAs? (List Json) "arguments"
    match ← field callee "type" with
    | "MemberExpression" =>
      let (base, key) ← linkedMember lower callee
      return .callMethod base key (← args.mapM lower)
    | "ChainExpression" =>
      -- Parentheses preserve the selected property's Reference for the call.
      throw "unsupported optional chain as a call target"
    | _ => return .call (← lower callee) (← args.mapM lower)
  | "ArrowFunctionExpression" | "FunctionExpression" =>
    let arrow := kind == "ArrowFunctionExpression"
    let id ← if arrow then pure Json.null else j.getObjVal? "id"
    let name ← if id == .null then pure nameHint else localBinding id
    return .func (← linkedFunction profile imports (if arrow then .arrow else .normal) name (id != .null) j)
  | _ => throw s!"unsupported linked expression {kind}"

private partial def linkedFunction (profile : HostProfile) (imports : List Name) (kind : FuncKind) (name : Name) (self : Bool)
    (j : Json) : Except String Func := do
  if (← j.getObjValAs? Bool "async") || (← j.getObjValAs? Bool "generator") then
    throw "unsupported async or generator function"
  let parameters ← j.getObjValAs? (Array Json) "params"
  let tags ← parameters.mapM fun param => field param "type"
  let hasDefaults := tags.contains "AssignmentPattern"
  let mut params : Array (Name × Option Expr) := #[]
  let mut bound : Array Name := #[]
  let mut bindings : Array Stmt := #[]
  for ((param, tag), i) in (parameters.zip tags).zipIdx do
    match tag with
    | "Identifier" =>
      let x ← localBinding param
      params := params.push (x, none)
      bound := bound.push x
    | "AssignmentPattern" =>
      let target ← param.getObjVal? "left"
      unless (← field target "type") == "Identifier" do
        throw "expected an identifier in a default parameter"
      let x ← localBinding target
      let value ← linkedExpr profile imports true x (← param.getObjVal? "right")
      params := params.push (x, some value)
      bound := bound.push x
    | "ObjectPattern" =>
      if hasDefaults then throw "unsupported object pattern alongside default parameters"
      let fields ← linkedObjectPattern param
      let arg := s!":parameter:{i}"
      params := params.push (arg, none)
      for (key, x) in fields do
        bound := bound.push x
        bindings := bindings.push (.decl .var x (.get (.var arg) (.str key)))
    | other => throw s!"unsupported function parameter {other}"
  unless bound.toList.Nodup do throw "duplicate function parameter"
  let body ← j.getObjVal? "body"
  let stmts ← if (← field body "type") == "BlockStatement" then
    linkedFunctionBody profile imports body
  else if kind == .arrow then
    pure [.ret (← linkedExpr profile imports true "" body)]
  else throw "expected a function body block"
  if hasDefaults then
    let bodyVars := varNamesOf stmts
    let defaultReads := params.toList.flatMap fun p => p.2.toList.flatMap Expr.free
    -- The evaluator hoists body vars at entry. Only admit that hoisting when
    -- it cannot shadow a reference in a default or merge a body's var with the parameter cell.
    if bodyVars.any (fun x => bound.contains x || defaultReads.contains x) then
      throw "body var overlaps named default parameters or their references"
    let arity := (params.toList.takeWhile fun p => p.2.isNone).length
    let slots := params.zipIdx.toList.map fun (_, i) => s!":parameter:{i}"
    let initializers := (params.toList.zip slots).map fun ((x, defaultValue), slot) =>
      let raw := Expr.var slot
      let value := match defaultValue with
        | some e => Expr.cond (.binary .strictEq raw .undef) e raw
        | none => raw
      Stmt.decl .let_ x value
    return .withDefaults name kind self slots arity (initializers ++ [.block stmts])
  -- Static keys and identifier targets have no parameter expressions. The reads
  -- run before the body, using the same mutable cells as any body var redeclarations.
  return .mk name kind self (params.toList.map Prod.fst) (bindings.toList ++ stmts)

/-- Public, static-name class elements. Default derived constructors forward to the base
with the original new target. Explicit derived constructors require super-call semantics. -/
private partial def linkedClass (profile : HostProfile) (imports : List Name) (inFunction : Bool) (hint : Name) (j : Json) :
    Except String Expr := do
  let id ← j.getObjVal? "id"
  let name ← if id == .null then pure hint else identifier id
  let parent ← j.getObjVal? "superClass"
  let body ← j.getObjVal? "body"
  unless (← field body "type") == "ClassBody" do throw "expected class body"
  let mut ctor := Func.mk name .classCtor false [] []
  let mut members := #[]
  for member in ← body.getObjValAs? (Array Json) "body" do
    let tag ← field member "type"
    unless tag == "MethodDefinition" || tag == "PropertyDefinition" do throw s!"unsupported class element {tag}"
    if ← member.getObjValAs? Bool "computed" then throw "unsupported computed class key"
    let key ← moduleExportName (← member.getObjVal? "key")
    let keyExpr ← stringExpr key
    let static ← member.getObjValAs? Bool "static"
    let value ← member.getObjVal? "value"
    if tag == "PropertyDefinition" then
      unless parent == .null do throw "unsupported derived class field"
      let init ← if value == .null then pure .undef else linkedExpr profile imports inFunction key value
      members := members.push (.field static keyExpr init)
    else
      match ← field member "kind" with
      | "constructor" =>
        unless parent == .null do throw "unsupported explicit derived constructor"
        ctor ← linkedFunction profile imports .classCtor name false value
      | "method" => members := members.push (.method static keyExpr (← linkedFunction profile imports .method key false value))
      | "get" => members := members.push (.getter static keyExpr (← linkedFunction profile imports .method ("get " ++ key) false value))
      | "set" => members := members.push (.setter static keyExpr (← linkedFunction profile imports .method ("set " ++ key) false value))
      | other => throw s!"unsupported class method kind {other}"
  if parent == .null then return .cls name (id != .null) ctor members.toList
  return .derived name (id != .null) (← linkedExpr profile imports inFunction "" parent) members.toList

private partial def linkedLocalDeclarations (profile : HostProfile) (imports : List Name) (offset : Nat) (j : Json) :
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
      let value ← linkedExpr profile imports true "" initializer
      if let [(key, bound)] := bindings then
        result := result.push (.decl declKind bound (.get value (.str key)))
      else
        -- Keep the initializer value while each read and binding runs in source order.
        let source := s!":destructure:{offset + result.size}"
        result := result.push (.decl .const source value)
        for (key, bound) in bindings do
          result := result.push (.decl declKind bound (.get (.var source) (.str key)))
    else
      result := result.push (← Prod.snd <$> declaratorWith localBinding (linkedExpr profile imports true) kind d)
  return result

/-- Keep every declarator in its enclosing statement list. Function instantiation hoists
all its vars, and block instantiation creates the lexical bindings before initialization. -/
private partial def linkedFunctionBody (profile : HostProfile) (imports : List Name) (block : Json) : Except String (List Stmt) := do
  let mut result := #[]
  for stmt in ← block.getObjValAs? (Array Json) "body" do
    if (← field stmt "type") == "VariableDeclaration" then
      result := result ++ (← linkedLocalDeclarations profile imports result.size stmt)
    else
      result := result.push (← linkedStatement profile imports true stmt)
  return result.toList

/-- A block introduces a fresh lexical scope each time it executes. Function `var`s
still hoist through it via `Stmt.varNames`. -/
private partial def linkedFunctionBlock (profile : HostProfile) (imports : List Name) (j : Json) : Except String Stmt := do
  unless (← field j "type") == "BlockStatement" do throw "expected a statement block"
  return .block (← linkedFunctionBody profile imports j)

/-- With no fallthrough and no case-scope lexical bindings, a switch is a chain of
strict comparisons against a saved discriminant. A trailing default runs only
after all case tests fail. -/
private partial def linkedSwitch (profile : HostProfile) (imports : List Name) (j : Json) : Except String Stmt := do
  let lower := linkedExpr profile imports true ""
  let discriminant ← lower (← j.getObjVal? "discriminant")
  let saved := ":switch:discriminant"
  let mut dispatch := Stmt.block []
  let mut hasFollowingClause := false
  let cases ← j.getObjValAs? (Array Json) "cases"
  for clause in cases.toList.reverse do
    unless (← field clause "type") == "SwitchCase" do throw "expected a switch case"
    let test ← clause.getObjVal? "test"
    if test == .null && hasFollowingClause then throw "unsupported nontrailing switch default"
    let mut statements ← clause.getObjValAs? (Array Json) "consequent"
    let terminal := statements.back?.bind fun last => (field last "type").toOption
    if terminal == some "BreakStatement" then
      unless (← statements.back!.getObjVal? "label") == .null do
        throw "unsupported labeled switch break"
      statements := statements.pop
    else if hasFollowingClause && terminal != some "ReturnStatement" && terminal != some "ThrowStatement" then
      throw "unsupported switch fallthrough"
    let mut body := #[]
    for statement in statements do
      let kind ← field statement "type"
      if kind == "VariableDeclaration" then
        unless (← field statement "kind") == "var" do throw "unsupported switch lexical declaration"
      else if ["ClassDeclaration", "FunctionDeclaration"].contains kind then
        throw "unsupported switch lexical declaration"
      if containsBreak statement then throw "unsupported nested switch break"
      body := body.push (← linkedStatement profile imports true statement)
    let branch := Stmt.block body.toList
    dispatch ← if test == .null then pure branch else do
      pure (.ite (.binary .strictEq (.var saved) (← lower test)) branch dispatch)
    hasFollowingClause := true
  return .block [.decl .const saved discriminant, dispatch]

private partial def linkedStatement (profile : HostProfile) (imports : List Name) (inFunction : Bool) (j : Json) :
    Except String Stmt := do
  if inFunction then
    match ← field j "type" with
    | "BlockStatement" => return ← linkedFunctionBlock profile imports j
    | "ClassDeclaration" =>
      let name ← j.getObjVal? "id" >>= localBinding
      return .decl .let_ name (← linkedClass profile imports true name j)
    | "EmptyStatement" => return .block []
    | "IfStatement" =>
      let test ← linkedExpr profile imports true "" (← j.getObjVal? "test")
      let consequent ← linkedStatement profile imports true (← j.getObjVal? "consequent")
      let alternate ← j.getObjVal? "alternate"
      let elseBranch ← if alternate == .null then pure (.block []) else linkedStatement profile imports true alternate
      return .ite test consequent elseBranch
    | "SwitchStatement" => return ← linkedSwitch profile imports j
    | "VariableDeclaration" =>
      -- An unbraced branch can declare vars, which already belong to the enclosing function.
      unless (← field j "kind") == "var" do throw "expected a var declaration in statement position"
      return .block (← linkedLocalDeclarations profile imports 0 j).toList
    | "WhileStatement" =>
      let test ← linkedExpr profile imports true "" (← j.getObjVal? "test")
      return .loop (.block []) test .undef (← linkedStatement profile imports true (← j.getObjVal? "body"))
    | "ForStatement" =>
      let lower := linkedExpr profile imports true ""
      let init ← j.getObjVal? "init"
      let initializer ← if init == .null then pure (.block []) else do
        if (← field init "type") == "VariableDeclaration" then
          -- Keep the declaration directly in the header so each iteration captures its own let cell.
          pure (← declarationWith localBinding (linkedExpr profile imports true) init).2
        else pure (.expr (← lower init))
      let test ← j.getObjVal? "test"
      let test ← if test == .null then pure (.bool true) else lower test
      let update ← j.getObjVal? "update"
      let update ← if update == .null then pure .undef else lower update
      return .loop initializer test update (← linkedStatement profile imports true (← j.getObjVal? "body"))
    | "BreakStatement" | "ContinueStatement" =>
      unless (← j.getObjVal? "label") == .null do throw "unsupported labeled loop control"
      return if (← field j "type") == "BreakStatement" then .brk else .cont
    | "TryStatement" =>
      let body ← linkedFunctionBlock profile imports (← j.getObjVal? "block")
      let handler ← j.getObjVal? "handler"
      let finalizer ← j.getObjVal? "finalizer"
      if handler == .null && finalizer == .null then throw "expected catch or finally"
      let protectedBody ← if handler == .null then pure body else do
        unless (← field handler "type") == "CatchClause" do throw "expected a catch clause"
        let param ← handler.getObjVal? "param"
        let name ← if param == .null then pure none else some <$> localBinding param
        pure (.tryCatch body name (← linkedFunctionBlock profile imports (← handler.getObjVal? "body")))
      if finalizer == .null then return protectedBody
      return .tryFinally protectedBody (← linkedFunctionBlock profile imports finalizer)
    | _ => pure ()
  let binder := if inFunction then localBinding else moduleBinding
  statementWith (linkedExpr profile imports inFunction "") (declarationWith binder (linkedExpr profile imports inFunction))
    inFunction j

end

/-- Function declarations initialize their module cells at link time. Recursive reads resolve
through the module scope. -/
def linkedDeclaration (imports : List Name) (j : Json) (profile : HostProfile := .empty) : Except String (Name × Stmt) := do
  if (← field j "type") == "FunctionDeclaration" then
    let name ← j.getObjVal? "id" >>= moduleBinding
    return (name, .funDecl (← linkedFunction profile imports .normal name false j))
  if (← field j "type") == "ClassDeclaration" then
    let name ← j.getObjVal? "id" >>= moduleBinding
    return (name, .decl .let_ name (← linkedClass profile imports false name j))
  declarationWith moduleBinding (linkedExpr profile imports false) j

/-- A top-level executable statement of the linked fragment. -/
def linkedUnit (imports : List Name) (j : Json) (profile : HostProfile := .empty) : Except String Stmt := do
  if ["FunctionDeclaration", "ClassDeclaration"].contains (← field j "type") then
    return (← linkedDeclaration imports j profile).2
  linkedStatement profile imports false j

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
