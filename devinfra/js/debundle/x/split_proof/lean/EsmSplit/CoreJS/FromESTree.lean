import Lean.Data.Json
import EsmSplit.CoreJS.Fragment
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

private def literal (key : String) : Except String Expr := do
  match key with
  | "null" => return .null
  | "b:true" => return .bool true
  | "b:false" => return .bool false
  | _ =>
    if key.startsWith "s16:" then throw "string literal contains an isolated UTF-16 surrogate"
    if key.startsWith "s:" then
      return .str (key.drop 2).toString
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
private def reference (imports : List Name) (inFunction : Bool) (j : Json)
    (profile : HostProfile := .empty) : Except String Expr := do
  let x ← identifier j
  match ← field j "k" with
  | "T" => return .var x
  | "L" =>
    unless inFunction do throw s!"unexpected function-local name {x}"
    return .var x
  | "V" =>
    unless imports.contains x do throw s!"unlinked imported name {x}"
    return .var x
  | "G" =>
    if (profile.bindingIdentity x).isSome then return .prim (.globalRef x) []
    if profile.recordedHostValue x then return .host x
    -- A successful typeof guard does not establish what a direct lookup would do.
    if profile.opaqueBinding x then
      return .prim (.opaqueAmbientRef x) []
    match x with
    | "undefined" => return .undef
    | "Infinity" => return .num Num.inf
    | "NaN" => return .num Num.nan
    | _ =>
      if profile.undeclaredGlobal x then return .var x
      throw s!"unsupported global {x}"
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

private def parseDeclKind : String → Except String DeclKind
  | "var" => pure .var
  | "let" => pure .let_
  | "const" => pure .const
  | kind => throw s!"unsupported declaration {kind}"

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
private def linkedObjectPattern (j : Json) (binding : Json → Except String α) :
    Except String (List (Name × α)) := do
  unless (← field j "type") == "ObjectPattern" do throw "expected an object pattern"
  let props ← j.getObjValAs? (List Json) "properties"
  if props.isEmpty then throw "unsupported empty object pattern"
  props.mapM fun p => do
    unless (← field p "type") == "Property" do throw "expected a static object pattern property"
    unless (← field p "kind") == "init" &&
        !(← p.getObjValAs? Bool "computed") && !(← p.getObjValAs? Bool "method") do
      throw "expected a static object pattern property"
    let key ← moduleExportName (← p.getObjVal? "key")
    return (key, ← p.getObjVal? "value" >>= binding)

private def linkedPatternIdentifier (binder : Json → Except String Name) (target : Json) : Except String Name := do
  unless (← field target "type") == "Identifier" do throw "expected an identifier in binding pattern"
  binder target

private def splitBindingDefault (j : Json) : Except String (Json × Option Json) := do
  if (← field j "type") == "AssignmentPattern" then
    return (← j.getObjVal? "left", some (← j.getObjVal? "right"))
  return (j, none)

private def linkedParameterBinding (j : Json) : Except String (Name × Option Json) := do
  let (target, fallback) ← splitBindingDefault j
  return (← linkedPatternIdentifier localBinding target, fallback)

/-- A trailing rest binding with static exclusions and no parameter expressions. -/
private def linkedObjectRestParameter (j : Json) : Except String (List (Name × Name) × Name) := do
  let props ← j.getObjValAs? (Array Json) "properties"
  let some tail := props.back? | throw "expected an object rest parameter"
  unless (← field tail "type") == "RestElement" do throw "expected an object rest parameter"
  let rest ← tail.getObjVal? "argument" >>= localBinding
  let selected := props.pop
  let fields ← if selected.isEmpty then pure [] else do
    let object := Json.obj ((← j.getObj?).insert "properties" (.arr selected))
    linkedObjectPattern object linkedParameterBinding
  let fields ← fields.mapM fun (key, name, fallback) => do
    if fallback.isSome then throw "unsupported default in object rest parameter"
    return (key, name)
  return (fields, rest)

/-- Flat rest-array bindings still use the array iterator, including for elisions. -/
private def linkedRestArrayPattern (j : Json) : Except String BindingPattern := do
  unless (← field j "type") == "ArrayPattern" do throw "expected an array rest pattern"
  let elements ← j.getObjValAs? (List Json) "elements"
  let items ← elements.mapM fun elem => do
    if elem == .null then return none
    let name ← linkedPatternIdentifier localBinding elem
    return some (.name name)
  return .array items none

private def hasObjectParameterDefault (param : Json) : Bool :=
  (field param "type").toOption == some "ObjectPattern" &&
    ((param.getObjValAs? (List Json) "properties").toOption.getD []).any fun prop =>
      (prop.getObjVal? "value" >>= (field · "type")).toOption == some "AssignmentPattern"

/-- A default reads a saved value. An undefined value triggers the initializer. -/
private def withBindingDefault (name : Name) (fallback : Option Expr) : Expr :=
  match fallback with
  | none => .var name
  | some init => .cond (.binary .strictEq (.var name) .undef) init (.var name)

/-- A numeric property name uses the string form of the parsed Number, independent of its source spelling. -/
private def staticPropertyName (key : Json) : Except String Name := do
  if (← field key "type") == "Literal" then
    if let .ok annotation := field key "lit" then
      if annotation.startsWith "n:" then
        let name ← match ← key.getObjVal? "value" with
          | .num value => pure (Num.toString (Num.ofString (toString value)))
          -- The frontend transports an overflowing numeric literal as a JSON string.
          | .str "Infinity" => pure "Infinity"
          | _ => throw "expected a numeric property key"
        unless annotation == "n:" ++ name do throw "numeric property key differs from its annotation"
        return name
  moduleExportName key

private def isAnonymousDefinition (value : Json) : Except String Bool := do
  if value == .null then return false
  match ← field value "type" with
  | "ArrowFunctionExpression" => return true
  | "FunctionExpression" | "ClassExpression" => return (← value.getObjVal? "id") == .null
  | _ => return false

/-- Evaluate object elements in source order. Only a colon-form __proto__ initializer
sets the prototype. Shorthand properties and methods with that name define own properties. -/
private def linkedObject (lower : Name → Json → Except String Expr)
    (method : Name → Json → Except String Func) (j : Json) : Except String Expr := do
  let mut props := #[]
  for p in ← j.getObjValAs? (Array Json) "properties" do
    let propType ← field p "type"
    if propType == "SpreadElement" then
      props := props.push (.spread (← lower "" (← p.getObjVal? "argument")))
      continue
    unless propType == "Property" do throw "expected an object property or spread"
    let kind ← field p "kind"
    let value ← p.getObjVal? "value"
    if ← p.getObjValAs? Bool "computed" then
      let key ← lower "" (← p.getObjVal? "key")
      if kind != "init" then
        let prop ← match kind with
          | "get" => pure (.getter key (← method "" value))
          | "set" => pure (.setter key (← method "" value))
          | _ => throw "unsupported object property kind"
        props := props.push prop
        continue
      if ← p.getObjValAs? Bool "method" then
        props := props.push (.method key (← method "" value))
        continue
      if ← isAnonymousDefinition value then throw "unsupported function or class named by a computed key"
      props := props.push (.init key (← lower "" value))
      continue
    let key ← staticPropertyName (← p.getObjVal? "key")
    let keyExpr := Expr.str key
    let prop ← match kind with
      | "get" => pure (.getter keyExpr (← method ("get " ++ key) value))
      | "set" => pure (.setter keyExpr (← method ("set " ++ key) value))
      | "init" =>
        if ← p.getObjValAs? Bool "method" then
          pure (.method keyExpr (← method key value))
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
    return Expr.str (← field (← q.getObjVal? "value") "cooked")
  let mut result ← cooked first
  for (expression, quasi) in expressions.zip rest do
    let value := Expr.unary .toString (← lower expression)
    result := .binary .add result value
    result := .binary .add result (← cooked quasi)
  return result

private def ambientSymbolMember? (j : Json) : Option Name := do
  guard ((field j "type").toOption == some "MemberExpression")
  let receiver ← (j.getObjVal? "object").toOption
  if (field receiver "k").toOption != some "G" || (identifier receiver).toOption != some "Symbol" ||
      (j.getObjValAs? Bool "computed").toOption != some false ||
      (j.getObjValAs? Bool "optional").toOption != some false then
    none
  else
    let prop ← (j.getObjVal? "property").toOption
    (identifier prop).toOption

/-- Source identifiers cannot name these cells. A new evaluation of the class
allocates a separate table for every declared private name. -/
private def privateTable (name : String) := ":private:" ++ name
private def privateMethodValue (name : String) := ":private-method:" ++ name

private def privateName? (node : Json) : Option String := do
  guard (["PrivateIdentifier", "CoreJSInstancePrivateIdentifier"].contains (← (field node "type").toOption))
  (field node "name").toOption

private partial def markPrivateNames (names : List String) (j : Json) : Json := Id.run do
  match j with
  | .obj fields =>
    if let some name := privateName? j then
      if names.contains name then return .obj (fields.insert "type" "CoreJSInstancePrivateIdentifier")
    return Json.mkObj (fields.toList.map fun (key, child) => (key, markPrivateNames names child))
  | .arr items => return .arr (items.map (markPrivateNames names))
  | _ => return j

private partial def namesOfType (kind : String) (j : Json) : List String :=
  match j with
  | .obj fields =>
    if (field j "type").toOption == some kind then [(field j "name").toOption.getD ""]
    else fields.toList.flatMap fun (_, child) => namesOfType kind child
  | .arr items => items.toList.flatMap (namesOfType kind)
  | _ => []

private def privateMember? (j : Json) : Option String := do
  guard ((field j "type").toOption == some "MemberExpression" &&
    (j.getObjValAs? Bool "computed").toOption == some false &&
    (j.getObjValAs? Bool "optional").toOption == some false)
  let prop ← (j.getObjVal? "property").toOption
  guard ((field prop "type").toOption == some "CoreJSInstancePrivateIdentifier")
  (field prop "name").toOption

private def memberKey (lower : Json → Except String Expr) (j : Json) : Except String Expr := do
  let prop ← j.getObjVal? "property"
  if (← field prop "type") == "PrivateIdentifier" then throw "unsupported private member access"
  if ← j.getObjValAs? Bool "computed" then lower prop else Expr.str <$> identifier prop

private def isSuperMember (j : Json) : Bool :=
  (field j "type").toOption == some "MemberExpression" &&
    (j.getObjVal? "object" >>= (field · "type")).toOption == some "Super"

/-- Decode a property access, retaining the receiver and the unevaluated key for
the interpreter's property read, call, or write. -/
private partial def linkedMember (lower : Json → Except String Expr) (j : Json) (allowOptional := false) :
    Except String (Expr × Expr) := do
  if (← j.getObjValAs? Bool "optional") && !allowOptional then throw "unsupported optional member access"
  let obj ← j.getObjVal? "object"
  if isSuperMember j then throw "unsupported super member access"
  -- Consecutive optional reads propagate undefined. A non-optional continuation
  -- would need to distinguish a skipped access from a property that returned undefined.
  let lowerBase := do
    if allowOptional && (← field obj "type") == "MemberExpression" &&
        (obj.getObjValAs? Bool "optional").toOption == some true then
      let (base, key) ← linkedMember lower obj (allowOptional := true)
      return Expr.getOptional base key
    lower obj
  let base ← lowerBase.mapError fun reason =>
    match ambientSymbolMember? j with
    | some member => s!"unsupported ambient member Symbol.{member}"
    | none => reason
  return (base, ← memberKey lower j)

private structure LinkedExprScope where
  inFunction : Bool
  allowClassLocals : Bool

private def linkedTarget (profile : HostProfile) (imports : List Name) (scope : LinkedExprScope)
    (lower : Json → Except String Expr) (j : Json) : Except String Target := do
  match ← field j "type" with
  | "Identifier" =>
    let .var name ← reference imports (scope.inFunction || scope.allowClassLocals) j profile
      | throw "expected a binding in linked assignment"
    -- Glue imports keep the original module reference annotation. Local references shadow imports.
    let scope ← field j "k"
    if scope != "L" && imports.contains name then
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

/-- A final abrupt statement prevents fallthrough. A break inside an inner
loop or switch is consumed there, so inspecting its subtree would be unsound. -/
private partial def endsSwitchCase (j : Json) : Bool :=
  match (field j "type").toOption with
  | some "BlockStatement" =>
    ((j.getObjValAs? (Array Json) "body").toOption >>= (·.back?)).any endsSwitchCase
  | some "IfStatement" =>
    (j.getObjVal? "consequent").toOption.any endsSwitchCase &&
      (j.getObjVal? "alternate").toOption.any endsSwitchCase
  | some "BreakStatement" | some "ContinueStatement" | some "ReturnStatement" | some "ThrowStatement" => true
  | _ => false

/-- Find a direct super call among the top-level comma operands. Earlier operands
execute before construction, and the residual sees the initialized receiver and fields. -/
private def directSuper? (stmt : Json) : Option (List Json × Json × Option Json) := do
  let kind ← (field stmt "type").toOption
  let fieldName ← match kind with
    | "ExpressionStatement" => some "expression"
    | "IfStatement" => some "test"
    | "ReturnStatement" => some "argument"
    | _ => none
  let expr ← (stmt.getObjVal? fieldName).toOption
  let operands ← if (field expr "type").toOption == some "SequenceExpression" then
    (expr.getObjValAs? (List Json) "expressions").toOption else pure [expr]
  let isSuperCall := fun operand =>
    (field operand "type").toOption == some "CallExpression" &&
    (operand.getObjVal? "callee" >>= (field · "type")).toOption == some "Super"
  let (earlier, remaining) := operands.span (!isSuperCall ·)
  let call :: rest := remaining | none
  let prior := earlier.map fun operand =>
    Json.mkObj [("type", .str "ExpressionStatement"), ("expression", operand)]
  if kind == "ExpressionStatement" && rest.isEmpty then return (prior, call, none)
  let residual := match rest with
    | [] => Json.mkObj [("type", .str "ThisExpression")]
    | [expr] => expr
    | _ => Json.mkObj [("type", .str "SequenceExpression"), ("expressions", .arr rest.toArray)]
  let fields ← stmt.getObj?.toOption
  return (prior, call, some (.obj (fields.insert fieldName residual)))

/-- An if/else can select the superclass call when both arms end with a direct call.
Keep each arm's prefix, including lexical declaration scope. -/
private def terminalSuper? (arm : Json) : Option (List Json × Json) := do
  let statements ← if (field arm "type").toOption == some "BlockStatement" then
    (arm.getObjValAs? (List Json) "body").toOption else pure [arm]
  let last ← statements.getLast?
  let (earlier, call, residual) ← directSuper? last
  if residual.isNone then some (statements.dropLast ++ earlier, call) else none

private def conditionalSuper? (stmt : Json) : Option (Json × (List Json × Json) × (List Json × Json)) := do
  guard ((field stmt "type").toOption == some "IfStatement")
  let test ← (stmt.getObjVal? "test").toOption
  let yes ← (stmt.getObjVal? "consequent").toOption >>= terminalSuper?
  let no ← (stmt.getObjVal? "alternate").toOption >>= terminalSuper?
  return (test, yes, no)

/-- Each closure reads the selected arm's actual cells. Creating it does not
evaluate its argument. A spread still attempts iteration at the original argument position. -/
private def delayedSuperArguments (arm : Nat) (args : List Expr) : List Name × List Stmt × List Expr :=
  let wrapped := args.zipIdx.map fun (arg, i) =>
    let slot := s!":super-argument:{arm}:{i}"
    let (spread, operand) := arg.argument
    let thunk := Expr.func (.mk "" .arrow false [] [.ret operand])
    let call := Expr.call (.var slot) []
    (slot, Stmt.expr (.assign (.var slot) thunk), if spread then Expr.spreadArg call else call)
  (wrapped.map (·.1), wrapped.map (·.2.1), wrapped.map (·.2.2))

/-- Lower new.target only within its owning function and lexical arrows. A nested ordinary
function is prepared at its own entry. Nested classes have separate contexts for methods,
fields and keys, so leave those nodes for class lowering. -/
private partial def prepareNewTarget (j : Json) : Json :=
  match j with
  | .obj fields =>
    let kind := (field j "type").toOption.getD ""
    if kind == "MetaProperty" &&
        (j.getObjVal? "meta" >>= identifier).toOption == some "new" &&
        (j.getObjVal? "property" >>= identifier).toOption == some "target" then
      Json.mkObj [("type", .str "CoreJSNewTarget")]
    else if ["FunctionExpression", "FunctionDeclaration", "ClassExpression", "ClassDeclaration"].contains kind then
      j
    else Json.mkObj (fields.toList.map fun (key, value) => (key, prepareNewTarget value))
  | .arr entries => .arr (entries.map prepareNewTarget)
  | _ => j

private def prepareNewTargetFunction (j : Json) : Except String Json := do
  let fields ← j.getObj?
  let params := prepareNewTarget (← j.getObjVal? "params")
  let body := prepareNewTarget (← j.getObjVal? "body")
  return .obj ((fields.insert "params" params).insert "body" body)

/-- Resolve lexical `this` in prefix and argument arrows to the constructor's
receiver cell. Ordinary functions, including object methods, bind their own receiver. -/
private partial def prepareBeforeSuper (j : Json) (allowFunctions : Bool) (underArrow := false) : Except String Json := do
  match j with
  | .obj fields =>
    let kind := (field j "type").toOption.getD ""
    if kind == "ThisExpression" && underArrow then
      return Json.mkObj [("type", .str "CoreJSDerivedReceiver")]
    if kind == "FunctionExpression" && allowFunctions then return j
    if ["ThisExpression", "Super", "MetaProperty", "FunctionDeclaration",
        "ClassExpression", "ClassDeclaration"].contains kind ||
        (["ArrowFunctionExpression", "FunctionExpression"].contains kind && !allowFunctions) then
      throw s!"unsupported {kind} before super"
    if kind == "CallExpression" && (j.getObjVal? "callee" >>= identifier).toOption == some "eval" then
      throw "unsupported direct eval before super"
    let capturesReceiver := underArrow || kind == "ArrowFunctionExpression"
    let entries ← fields.toList.mapM fun (key, value) => do
      pure (key, ← prepareBeforeSuper value allowFunctions capturesReceiver)
    return Json.mkObj entries
  | .arr items => return .arr (← items.mapM (prepareBeforeSuper · allowFunctions underArrow))
  | _ => return j

/-- Each entry identifies the object field through which the walk reached the child. Arrays
of statements and class members preserve their owning field. Check the nearest function boundary. -/
private def enclosedSuperCall (parents : List (String × Json)) : Bool := Id.run do
  let ("callee", call) :: rest := parents | return false
  unless (field call "type").toOption == some "CallExpression" do return false
  let boundary (j : Json) := ["FunctionExpression", "ArrowFunctionExpression", "FunctionDeclaration",
    "ClassExpression", "ClassDeclaration"].contains ((field j "type").toOption.getD "")
  let ("body", fn) :: ("value", method) :: ("body", body) :: ("body", cls) :: _ :=
    rest.dropWhile (fun (_, j) => !boundary j) | return false
  return (field fn "type").toOption == some "FunctionExpression" &&
    (field method "type").toOption == some "MethodDefinition" && (field method "kind").toOption == some "constructor" &&
    (method.getObjValAs? Bool "static").toOption == some false &&
    (method.getObjValAs? Bool "computed").toOption == some false &&
    (field body "type").toOption == some "ClassBody" &&
    ["ClassExpression", "ClassDeclaration"].contains ((field cls "type").toOption.getD "") &&
    (cls.getObjVal? "superClass").toOption.any (· != .null)

private partial def opaqueContext? (checkPrivateNames wholeUnit moduleLocationChecked retainedSuperMembers : Bool)
    (parents : List (String × Json)) (j : Json) : Option String := Id.run do
  match j with
  | .obj fields =>
    let kind := (field j "type").toOption.getD ""
    let retainedMember := retainedSuperMembers && (match parents with
      | ("object", parent) :: _ => isSuperMember parent
      | _ => false)
    if kind == "CoreJSDerivedReceiver" || kind == "CoreJSNewTarget" ||
        (kind == "Super" && !retainedMember && !(wholeUnit && enclosedSuperCall parents)) ||
        (kind == "ImportExpression" && !moduleLocationChecked) ||
        (kind == "MetaProperty" && !(moduleLocationChecked &&
          (j.getObjVal? "meta" >>= identifier).toOption == some "import" &&
          (j.getObjVal? "property" >>= identifier).toOption == some "meta")) ||
        (checkPrivateNames && kind == "PrivateIdentifier") then
      return some kind
    if kind == "CallExpression" && (j.getObjVal? "callee" >>= identifier).toOption == some "eval" then
      return some "direct eval"
    return fields.toList.findSome? fun (key, child) =>
      opaqueContext? checkPrivateNames wholeUnit moduleLocationChecked retainedSuperMembers
        (if wholeUnit || retainedSuperMembers then (key, j) :: parents else []) child
  | .arr items => return items.findSome? (opaqueContext? checkPrivateNames wholeUnit moduleLocationChecked retainedSuperMembers parents)
  | _ => return none

/-- Detached bodies require their source module and method context. When the entire statement is
retained, a direct super call within the body of its own derived constructor retains its class.
Direct eval can refer to cells absent from the static capture list in either case. -/
def needsOpaqueContext (j : Json) (checkPrivateNames wholeUnit moduleLocationChecked : Bool := false) : Bool :=
  (opaqueContext? checkPrivateNames false false false [] j).isSome &&
    (!wholeUnit || (opaqueContext? checkPrivateNames true moduleLocationChecked false [] j).isSome)

/-- LinkedInput numbers each callable by its original unit and lexical site and checks emitted syntax.
The scope resolver supplies references to cells outside the retained callable. -/
private def deferredFunction (j : Json) (kind : FuncKind) (name : Name) (self : Bool)
    (arity : Nat) (reason : String) (checkPrivateNames := true) (scripted := false) : Except String Func := do
  -- Linked lowering retains the entire parsed method and its lexical arrows. The
  -- closure keeps its home object, and invocation with a refusal always stops replay.
  let refusal (why : String) :=
    if kind == .asyncFn || kind == .asyncGenerator then
      -- The outer function can retain this refusal. Its syntax must still match
      -- across relocated modules, so use the linker's stable callable identity.
      let location := match j.getObjValAs? Nat ":opaque-unit", j.getObjValAs? Nat ":opaque-part" with
        | .ok unit, .ok part => s!" at unit {unit}, callable {part}"
        | _, _ => ""
      let label := if kind == .asyncGenerator then "async generator" else "async function"
      s!"cannot retain {label} definition{location}: {why}"
    else reason
  if let some context := opaqueContext? checkPrivateNames false false true [] j then
    throw (refusal s!"syntax requires lexical or module context ({context})")
  let .ok captures := j.getObjValAs? (List Name) "opaqueCaptures" | throw (refusal "missing capture list")
  let .ok code := j.getObjValAs? Nat ":opaque-unit" | throw (refusal "missing source unit")
  let .ok part := j.getObjValAs? Nat ":opaque-part" | throw (refusal "missing callable site")
  let privateCells := (namesOfType "CoreJSInstancePrivateIdentifier" j).eraseDups.map privateTable
  return .opaque name kind self arity code part (captures ++ privateCells)
    (if scripted then none else some reason)

-- Every admitted body leaves the enumerated keys and prototype chain intact.
-- A plain local assignment may bind a new object. Property writes must name a
-- distinct ordinary destination and use a non-accessor descriptor.
private def guardForInBody (source : Name) : Stmt → Except String Stmt
  | stmt@(.expr (.assign (.member (.var target) (.var key)) value)) =>
    if value.allocOnly then pure (.block [
      .expr (.prim .checkForInWrite [.var source, .var target, .var key]), stmt])
    else throw "for-in property initializer may run code"
  | stmt@(.expr (.assign (.var _) value)) | stmt@(.expr value) | stmt@(.decl _ _ value) |
    stmt@(.ret value) | stmt@(.throw value) =>
    if value.allocOnly then pure stmt else throw "for-in body may run code that changes enumeration"
  | stmt@(.brk) | stmt@(.cont) | stmt@(.varDecl _) => pure stmt
  | .block body => .block <$> body.mapM (guardForInBody source)
  | _ => throw "unsupported for-in body"

private inductive LinkedScope where
  | module
  | moduleBlock
  | function
  deriving BEq

private def LinkedScope.varBinder (scope : LinkedScope) (kind : String) : Json → Except String Name :=
  if scope == .function || kind != "var" then localBinding else moduleBinding

private def linkedArgument (lower : Json → Except String Expr) (j : Json) : Except String Expr := do
  if j == .null then throw "unsupported array hole"
  if (← field j "type") == "SpreadElement" then return .spreadArg (← lower (← j.getObjVal? "argument"))
  lower j

/-- Primitive constant operands cannot observe the array's not-yet-exposed allocation. -/
private def sparseConstant (j : Json) : Bool := Id.run do
  if j == .null then return true
  if (field j "type").toOption == some "Literal" then
    return ((field j "lit").toOption.bind fun key => (literal key).toOption).isSome
  if (field j "type").toOption == some "Identifier" && (field j "k").toOption == some "G" then
    return (field j "name").toOption.any fun name => ["undefined", "NaN", "Infinity"].contains name
  if (field j "type").toOption == some "UnaryExpression" && (field j "operator").toOption == some "-" then
    let arg := (j.getObjVal? "argument").toOption.getD .null
    return (field arg "type").toOption == some "Literal" &&
      ((field arg "lit").toOption.any fun key => key.startsWith "n:")
  return false

/-- A structural opaque unit cannot replace the linked lowering's refusal for
an effectful sparse array. Keep this predicate next to `sparseConstant` so
both paths use the same definition of a safely lowerable sparse literal. -/
partial def containsUnsupportedSparseArrayHole (j : Json) : Bool := Id.run do
  if (field j "type").toOption == some "ArrayExpression" then
    let elements := (j.getObjValAs? (Array Json) "elements").toOption.getD #[]
    if elements.contains .null && !elements.all sparseConstant then return true
  match j with
  | .obj fields => return fields.valuesArray.any containsUnsupportedSparseArrayHole
  | .arr elements => return elements.any containsUnsupportedSparseArrayHole
  | _ => return false

/-- Named defaults initialize lexical parameter cells in order from the raw argument slots.
Ordinary functions then enter a body block; derived constructors run their prefix before super. -/
private inductive LinkedFunctionParts where
  | complete (fn : Func)
  | objectBindings (slots : List Name) (initializers body : List Stmt)
  | defaults (slots : List Name) (arity : Nat) (initializers body : List Stmt)

mutual

private partial def linkedExprScoped (profile : HostProfile) (imports : List Name) (scope : LinkedExprScope) (nameHint : Name)
    (j : Json) : Except String Expr := do
  let kind ← field j "type"
  match kind with
  | "Literal" =>
    let key ← field j "lit"
    if let .ok rx := j.getObjVal? "regex" then
      let pattern ← field rx "pattern"
      let flags ← field rx "flags"
      unless key == "r:" ++ pattern ++ "/" ++ flags do throw "regexp literal differs from its annotation"
      return .regexp ⟨pattern, flags⟩
    if key.startsWith "s:" then return .str (← stringLiteral j)
    literal key
  | "Identifier" => reference imports (scope.inFunction || scope.allowClassLocals) j profile
  | "ThisExpression" => return .this
  | "CoreJSDerivedReceiver" => return .var derivedReceiverName
  | "CoreJSNewTarget" => return .var newTargetName
  | "MemberExpression" =>
    if let some sym := (ambientSymbolMember? j).bind WellKnownSymbol.ofMember? then
      if profile.ambientSymbols then return .prim (.wellKnownSymbol sym) []
      if scope.inFunction then return .prim (.opaqueAmbientMemberRef "Symbol" sym.member) []
    let lower := linkedExprScoped profile imports scope ""
    if let some name := privateMember? j then
      return .prim (.privateSlot .get) [.var (privateTable name), ← lower (← j.getObjVal? "object")]
    if isSuperMember j then return .getSuper (← memberKey lower j)
    let (base, key) ← linkedMember lower j
    return .get base key
  | "ChainExpression" =>
    let body ← j.getObjVal? "expression"
    let lower := linkedExprScoped profile imports scope ""
    let (body, property) ← if (← field body "type") == "MemberExpression" &&
        (body.getObjValAs? Bool "optional").toOption == some false then do
      let base ← body.getObjVal? "object"
      if (← field base "type") == "CallExpression" && (base.getObjValAs? Bool "optional").toOption == some true then
        pure (base, some (← memberKey lower body))
      else pure (body, none)
    else pure (body, none)
    if (← field body "type") == "CallExpression" then
      let optionalCallee ← body.getObjValAs? Bool "optional"
      let member ← body.getObjVal? "callee"
      let reason := if optionalCallee then "unsupported optional call" else "unsupported optional chain"
      unless (← field member "type") == "MemberExpression" do throw reason
      let optionalReceiver ← member.getObjValAs? Bool "optional"
      if optionalCallee == optionalReceiver then throw reason
      if isSuperMember member then
        unless property.isNone do throw "unsupported continuation after an optional super call"
        return .callSuper (← memberKey lower member) (← (← body.getObjValAs? (List Json) "arguments").mapM (linkedArgument lower)) true
      if ← member.getObjValAs? Bool "computed" then throw "unsupported computed optional method call"
      let (base, key) ← linkedMember lower member (allowOptional := optionalReceiver)
      let args ← (← body.getObjValAs? (List Json) "arguments").mapM (linkedArgument lower)
      return if optionalCallee then .callOptionalCallee base key args property else .callOptionalReceiver base key args
    let member := body
    unless (← field member "type") == "MemberExpression" do throw "unsupported optional chain"
    unless ← member.getObjValAs? Bool "optional" do throw "unsupported optional chain"
    let (base, key) ← linkedMember lower member (allowOptional := true)
    return .getOptional base key
  | "AssignmentExpression" =>
    let op ← field j "operator"
    let lower := linkedExprScoped profile imports scope ""
    let left ← j.getObjVal? "left"
    if let some name := privateMember? left then
      unless op == "=" do throw "unsupported compound private field assignment"
      return .prim (.privateSlot .set) [.var (privateTable name), ← lower (← left.getObjVal? "object"),
        ← lower (← j.getObjVal? "right")]
    let target ← linkedTarget profile imports scope lower left
    let rhs ← j.getObjVal? "right"
    let targetName := match target with
      | .var name | .frozen name => name
      | _ => ""
    let named := linkedExprScoped profile imports scope targetName
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
    let target ← j.getObjVal? "argument" >>= linkedTarget profile imports scope (linkedExprScoped profile imports scope "")
    return .update target inc (← j.getObjValAs? Bool "prefix")
  | "ObjectExpression" =>
    linkedObject (linkedExprScoped profile imports scope) (fun name => linkedFunction profile imports .method name false) j
  | "ArrayExpression" =>
    let elements ← j.getObjValAs? (List Json) "elements"
    let lower := linkedExprScoped profile imports scope ""
    if elements.contains .null then
      unless elements.all sparseConstant do throw "unsupported array hole"
      let holes := (elements.zipIdx.filterMap fun (j, i) => if j == .null then some i else none)
      let values ← elements.mapM fun j => if j == .null then pure .undef else lower j
      return .prim (.sparseArray holes) values
    return .arr (← elements.mapM (linkedArgument lower))
  | "TaggedTemplateExpression" =>
    let slot ← field j ":template-binding"
    let callee ← j.getObjVal? "tag"
    let quasi ← j.getObjVal? "quasi"
    let substitutions ← quasi.getObjValAs? (List Json) "expressions"
    let lower := linkedExprScoped profile imports scope ""
    let args := Expr.var slot :: (← substitutions.mapM lower)
    match ← field callee "type" with
    | "MemberExpression" =>
      if (field (← callee.getObjVal? "object") "type").toOption == some "Super" then
        return .callSuper (← memberKey lower callee) args false
      let (base, key) ← linkedMember lower callee
      return .callMethod base key args
    | "ChainExpression" => throw "unsupported optional chain as a template tag"
    | _ => return .call (← lower callee) args
  | "TemplateLiteral" => linkedTemplate (linkedExprScoped profile imports scope "") j
  | "UnaryExpression" =>
    let argument ← j.getObjVal? "argument"
    let operator ← field j "operator"
    let lower := linkedExprScoped profile imports scope ""
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
      if (profile.bindingType name).isSome then return .prim (.globalTypeof name) []
    return .unary op (← linkedExprScoped profile imports scope "" argument)
  | "BinaryExpression" =>
    let op ← field j "operator"
    let lower := linkedExprScoped profile imports scope ""
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
    | "instanceof" => return .prim .instanceOf [left, right]
    | "in" => return .hasProp left right
    | "==" => return .binary .eq left right
    | "!=" => return .binary .ne left right
    | _ => throw s!"unsupported linked binary operator {op}"
  | "LogicalExpression" =>
    let op : LogOp ← match ← field j "operator" with
      | "&&" => pure .and
      | "||" => pure .or
      | "??" => pure .nullish
      | other => throw s!"unsupported linked logical operator {other}"
    let lower := linkedExprScoped profile imports scope ""
    return .logical op (← lower (← j.getObjVal? "left")) (← lower (← j.getObjVal? "right"))
  | "ConditionalExpression" =>
    let lower := linkedExprScoped profile imports scope ""
    return .cond (← lower (← j.getObjVal? "test")) (← lower (← j.getObjVal? "consequent"))
      (← lower (← j.getObjVal? "alternate"))
  | "SequenceExpression" =>
    let expressions ← j.getObjValAs? (List Json) "expressions"
    let first :: second :: rest := expressions | throw "expected at least two sequence operands"
    let lower := linkedExprScoped profile imports scope ""
    return sequenceTree (← lower first) (← (second :: rest).mapM lower)
  | "ClassExpression" => linkedClassScoped profile imports scope nameHint j
  | "NewExpression" =>
    let lower := linkedExprScoped profile imports scope ""
    let args ← j.getObjValAs? (List Json) "arguments"
    let callee ← j.getObjVal? "callee"
    if (field callee "k").toOption == some "G" && (← field callee "type") == "Identifier" then
      let name ← identifier callee
      if profile.opaqueConstructor name then
        -- Evaluation stops at the callee, before an argument can throw or mutate state.
        -- Retain argument syntax and binding references in the deferred function.
        return .new (.prim (.opaqueConstructorRef name) []) (← args.mapM (linkedArgument lower))
    return .new (← lower callee) (← args.mapM (linkedArgument lower))
  | "CallExpression" =>
    if ← j.getObjValAs? Bool "optional" then throw "unsupported optional call"
    let lower := linkedExprScoped profile imports scope ""
    let callee ← j.getObjVal? "callee"
    let args ← j.getObjValAs? (List Json) "arguments"
    if profile == .browserPage && (field callee "type").toOption == some "MemberExpression" &&
        (callee.getObjValAs? Bool "computed").toOption == some false &&
        (callee.getObjVal? "object" >>= (field · "k")).toOption == some "G" &&
        (callee.getObjVal? "object" >>= identifier).toOption == some "Array" &&
        (callee.getObjVal? "property" >>= identifier).toOption == some "from" then
      if let [source] := args then
        unless (field source "type").toOption == some "SpreadElement" do
          -- Observe the constructor and method before evaluating the argument. On the
          -- supported fresh-realm template iterable, Array.from creates a mutable copy.
          return .seq (.prim .checkArrayFrom []) (.arr [.spreadArg (.prim .templateIterable [← lower source])])
    if profile.ambientSymbols && (field callee "type").toOption == some "Identifier" &&
        (field callee "k").toOption == some "G" && (identifier callee).toOption == some "Symbol" then
      match args with
      | [] => return .prim .freshSymbol []
      | [arg] =>
        if let .ok description := stringLiteral arg then return .prim .freshSymbol [.str description]
      | _ => pure ()
    -- The recorded Node host's ambient `String` is the intrinsic conversion of its one argument.
    if profile == .nodeRecording && (field callee "type").toOption == some "Identifier" &&
        (field callee "k").toOption == some "G" && (identifier callee).toOption == some "String" then
      if let [arg] := args then
        unless (field arg "type").toOption == some "SpreadElement" do
          return .unary .toString (← lower arg)
    if (field callee "type").toOption == some "MemberExpression" && !(callee.getObjValAs? Bool "computed").toOption.getD true then
      let owner ← callee.getObjVal? "object"
      if (field owner "type").toOption == some "Identifier" && (field owner "k").toOption == some "G" then
        if let some p := profile.recordedPrim (← identifier owner) (← identifier (← callee.getObjVal? "property")) then
          if args.any (fun a => (field a "type").toOption == some "SpreadElement") then
            throw "unsupported spread argument to a recorded host primitive"
          return .prim p (← args.mapM lower)
    match ← field callee "type" with
    | "MemberExpression" =>
      if let some name := privateMember? callee then
        let receiver := Expr.var ":private-receiver"
        let method := Expr.prim (.privateSlot .get) [.var (privateTable name), receiver]
        let argv ← args.mapM (linkedArgument lower)
        let invoke := Expr.prim .callWithReceiver (method :: receiver :: argv)
        return .call (.func (.mk "" .arrow false [":private-receiver"] [.ret invoke]))
          [← lower (← callee.getObjVal? "object")]
      if isSuperMember callee then
        return .callSuper (← memberKey lower callee) (← args.mapM (linkedArgument lower)) false
      if ambientSymbolMember? callee == some "for" then
        -- Retain deferred arguments and their captures. Initialization stops at
        -- the opaque callee reference before it can evaluate an argument.
        return .call (.prim (.opaqueAmbientMemberRef "Symbol" "for") []) (← args.mapM (linkedArgument lower))
      let (base, key) ← linkedMember lower callee
      return .callMethod base key (← args.mapM (linkedArgument lower))
    | "ChainExpression" =>
      -- Parentheses preserve the selected property's Reference for the call.
      throw "unsupported optional chain as a call target"
    | _ => return .call (← lower callee) (← args.mapM (linkedArgument lower))
  | "ArrowFunctionExpression" | "FunctionExpression" =>
    let arrow := kind == "ArrowFunctionExpression"
    let id ← if arrow then pure Json.null else j.getObjVal? "id"
    let name ← if id == .null then pure nameHint else localBinding id
    return .func (← linkedFunction profile imports (if arrow then .arrow else .normal) name (id != .null) j)
  | _ => throw s!"unsupported linked expression {kind}"


private partial def linkedExpr (profile : HostProfile) (imports : List Name) (inFunction : Bool) (nameHint : Name)
    (j : Json) : Except String Expr :=
  linkedExprScoped profile imports ⟨inFunction, false⟩ nameHint j

private partial def linkedFunction (profile : HostProfile) (imports : List Name) (kind : FuncKind) (name : Name) (self : Bool)
    (j : Json) : Except String Func := do
  match ← linkedFunctionParts profile imports kind name self j with
  | .complete fn => return fn
  | .objectBindings slots initializers body =>
    return .mk name kind self slots (initializers ++ body)
  | .defaults slots arity initializers body =>
    return .withDefaults name kind self slots arity (initializers ++ [.block body])

private partial def linkedFunctionParts (profile : HostProfile) (imports : List Name) (kind : FuncKind)
    (name : Name) (self : Bool) (j : Json) : Except String LinkedFunctionParts := do
  let async ← j.getObjValAs? Bool "async"
  let parameters ← j.getObjValAs? (Array Json) "params"
  let tags ← parameters.mapM fun param => field param "type"
  let arity := (tags.toList.takeWhile fun t => t != "AssignmentPattern" && t != "RestElement").length
  let generator ← j.getObjValAs? Bool "generator"
  if profile == .nodeRecording && (j.getObjValAs? Bool ":opaque-scripted").toOption == some true then
    if async || generator || needsOpaqueContext j (checkPrivateNames := true) then
      throw "scripted callable requires unsupported invocation or lexical context"
    return .complete (← deferredFunction j kind name self arity "scripted callable" (scripted := true))
  if generator then
    if async then return .complete (← deferredFunction j .asyncGenerator name self arity
      "async generator invocation requires asynchronous suspension semantics")
    return .complete (← deferredFunction j .generator name self arity "generator invocation requires suspension semantics")
  if async then
    -- makeFunc captures the enclosing this as well as named cells. No async
    -- callable enters the ordinary function evaluator.
    return .complete (← deferredFunction j .asyncFn name self arity "async invocation requires Promise semantics")
  let j ← if kind == .arrow then pure j else prepareNewTargetFunction j
  let parameters ← j.getObjValAs? (Array Json) "params"
  let deferPattern := deferredFunction j kind name self arity (checkPrivateNames := true)
  let hasDefaults := tags.contains "AssignmentPattern" || parameters.any hasObjectParameterDefault
  let mut rest : Option Name := none
  let mut slots : Array Name := #[]
  let mut bound : Array Name := #[]
  let mut initializers : Array Stmt := #[]
  for (param, i) in parameters.zipIdx do
    let slot := s!":parameter:{i}"
    let (target, fallback) ← splitBindingDefault param
    match ← field target "type" with
    | "Identifier" =>
      let x ← localBinding target
      slots := slots.push (if hasDefaults then slot else x)
      bound := bound.push x
      if hasDefaults then
        let value ← fallback.mapM (linkedExpr profile imports true x)
        initializers := initializers.push (.decl .let_ x (withBindingDefault slot value))
    | "ObjectPattern" =>
      let props ← target.getObjValAs? (Array Json) "properties"
      if props.back?.any (fun p => (field p "type").toOption == some "RestElement") then
        if hasDefaults then
          return .complete (← deferPattern "unsupported defaults with object rest parameters")
        let (fields, restName) ← match linkedObjectRestParameter target with
          | .ok result => pure result
          | .error reason => return .complete (← deferPattern reason)
        slots := slots.push slot
        bound := (bound ++ (fields.map Prod.snd).toArray).push restName
        let bindings := fields.map fun (key, name) => (Expr.str key, BindingPattern.name name)
        -- Even a rest-only pattern checks coercibility before copying properties.
        initializers := initializers.push (.bind .var (.object bindings) (.var slot))
        initializers := initializers.push (.decl .var restName (.obj [.spread (.var slot) (fields.map Prod.fst)]))
        continue
      let fields ← match linkedObjectPattern target linkedParameterBinding with
        | .ok fields => pure fields
        | .error reason => return .complete (← deferPattern reason)
      slots := slots.push slot
      bound := bound ++ (fields.map (·.2.1)).toArray
      if hasDefaults then
        let props ← fields.mapM fun (key, x, fallback) => do
          let bind := BindingPattern.name x
          let pattern ← match fallback with
            | none => pure bind
            | some e => .defaultValue bind <$> linkedExpr profile imports true x e
          pure (Expr.str key, pattern)
        let value ← fallback.mapM (linkedExpr profile imports true "")
        initializers := initializers.push (.bind .let_ (.object props) (withBindingDefault slot value))
      else
        for (key, x, _) in fields do
          initializers := initializers.push (.decl .var x (.get (.var slot) (.str key)))
    | "RestElement" =>
      let target ← param.getObjVal? "argument"
      unless i + 1 == parameters.size && tags.toList.all (fun t => t == "Identifier" || t == "RestElement") do
        return .complete (← deferPattern "rest requires a terminal binding and fixed identifier parameters")
      match ← field target "type" with
      | "Identifier" =>
        let x ← localBinding target
        rest := some x
        bound := bound.push x
      | "ArrayPattern" =>
        let pattern ← match linkedRestArrayPattern target with
          | .ok pattern => pure pattern
          | .error reason => return .complete (← deferPattern reason)
        rest := some slot
        bound := bound ++ pattern.names.toArray
        initializers := initializers.push (.bind .var pattern (.var slot))
      | _ => return .complete (← deferPattern "unsupported rest parameter binding")
    | "ArrayPattern" => return .complete (← deferPattern "unsupported function parameter ArrayPattern")
    | other => throw s!"unsupported function parameter {other}"
  unless bound.toList.Nodup do throw "duplicate function parameter"
  let body ← j.getObjVal? "body"
  let lowered := if (← field body "type") == "BlockStatement" then
    linkedBlockBody profile imports .function body (allowFunctions := true)
  else if kind == .arrow then
    linkedExpr profile imports true "" body |>.map (fun e => [.ret e])
  else throw "expected a function body block"
  if let .error reason := lowered then
    return .complete (← deferredFunction j kind name self arity reason
      (checkPrivateNames := hasDefaults || kind == .method || kind == .classCtor))
  let stmts ← lowered
  let functions := (funDecls stmts).map Func.name
  let bodyVars := if hasDefaults || !functions.isEmpty then varNamesOf stmts else []
  -- CoreJS allocates every function name in its own body-scope cell. JavaScript
  -- can instead reuse a parameter or var cell, or select the last declaration.
  unless functions.Nodup do throw "duplicate local function declaration"
  if functions.any (fun x => bound.contains x || bodyVars.contains x) then
    throw "local function declaration overlaps a parameter or var binding"
  if hasDefaults then
    -- The evaluator hoists body vars at entry. Only admit that hoisting when
    -- it cannot shadow a reference in a default or merge a body's var with the parameter cell.
    let reads := freeStmts initializers.toList
    if bodyVars.any reads.contains then
      throw "body var overlaps default parameters or their references"
    return .defaults slots.toList arity initializers.toList stmts
  -- Static keys and identifier targets have no parameter expressions. The reads
  -- run before the body, using the same mutable cells as any body var redeclarations.
  if let some restName := rest then
    return .complete (.withRest name kind self slots.toList restName (initializers.toList ++ stmts))
  if initializers.isEmpty then return .complete (.mk name kind self slots.toList stmts)
  return .objectBindings slots.toList initializers.toList stmts

private partial def linkedBeforeSuper (profile : HostProfile) (imports : List Name)
    (statements : List Json) : Except String (List Stmt) := do
  let mut before := #[]
  for stmt in statements do
    match ← field stmt "type" with
    | "VariableDeclaration" =>
      let prepared ← prepareBeforeSuper stmt true
      let kind ← field prepared "kind"
      for d in ← prepared.getObjValAs? (Array Json) "declarations" do
        before := before ++ (← linkedPrefixDeclarator (linkedExpr profile imports true) kind d before.size)
    | "ExpressionStatement" =>
      let expr ← prepareBeforeSuper (← stmt.getObjVal? "expression") true
      before := before.push (.expr (← linkedExpr profile imports true "" expr))
    | "IfStatement" =>
      let consequent ← stmt.getObjVal? "consequent"
      let branch ← if (← field consequent "type") == "BlockStatement" then
        consequent.getObjValAs? (List Json) "body"
      else pure [consequent]
      unless (← stmt.getObjVal? "alternate") == .null &&
          branch.length == 1 && branch.head?.any (fun arm => (field arm "type").toOption == some "ThrowStatement") do
        throw "unsupported IfStatement before super"
      -- The guard either falls through or throws before selecting the superclass.
      before := before.push (← linkedStatement profile imports .function (← prepareBeforeSuper stmt true))
    | other => throw s!"unsupported {other} before super"
  return before.toList

private partial def linkedSuperArguments (profile : HostProfile) (imports : List Name)
    (rawArgs : List Json) : Except String (List Expr) :=
  rawArgs.mapM fun arg => do
    linkedArgument (linkedExpr profile imports true "") (← prepareBeforeSuper arg true)

private partial def linkedSuperArm (profile : HostProfile) (imports : List Name) (arm : Nat)
    (statements : List Json) (call : Json) : Except String (List Name × Stmt × List Expr) := do
  let before ← linkedBeforeSuper profile imports statements
  let args ← linkedSuperArguments profile imports (← call.getObjValAs? (List Json) "arguments")
  let (slots, assign, delayed) := delayedSuperArguments arm args
  return (slots, .block (before ++ assign), delayed)

/-- Split a derived constructor around the superclass call. The prefix and
the remaining function body run with the same parameter and local cells. -/
private partial def linkedDerivedConstructor (profile : HostProfile) (imports : List Name)
    (name : Name) (fn : Json) : Except String (List Stmt × SuperCall × Func) := do
  let fn ← prepareNewTargetFunction fn
  for param in ← fn.getObjValAs? (Array Json) "params" do
    let (target, fallback) ← splitBindingDefault param
    if let some e := fallback then discard <| prepareBeforeSuper e false
    match ← field target "type" with
    | "ObjectPattern" =>
      for (_, _, propertyDefault) in ← linkedObjectPattern target linkedParameterBinding do
        if let some e := propertyDefault then discard <| prepareBeforeSuper e false
    | "RestElement" => discard <| localBinding (← target.getObjVal? "argument")
    | _ => discard <| localBinding target
  let block ← fn.getObjVal? "body"
  let statements ← block.getObjValAs? (List Json) "body"
  let (prefixStmts, remaining) := statements.span fun stmt =>
    (directSuper? stmt).isNone && (conditionalSuper? stmt).isNone
  let first :: tail := remaining | throw "expected a direct super call"
  let (before, args, stmts) ←
    if let some (commaPrefix, call, rest) := directSuper? first then do
      let before ← linkedBeforeSuper profile imports (prefixStmts ++ commaPrefix)
      let rawArgs ← call.getObjValAs? (List Json) "arguments"
      let forward := match rawArgs with
        | [arg] => (field arg "type").toOption == some "SpreadElement" &&
          (arg.getObjVal? "argument" >>= identifier).toOption == some "arguments" &&
          (arg.getObjVal? "argument" >>= (field · "k")).toOption == some "L"
        | _ => false
      let args ← if forward && before.isEmpty && (← fn.getObjValAs? (Array Json) "params").isEmpty then
        pure SuperCall.forward
      else SuperCall.explicit <$> linkedSuperArguments profile imports rawArgs
      pure (before, args, rest.toList ++ tail)
    else if let some (test, (yesPrefix, yesCall), (noPrefix, noCall)) := conditionalSuper? first then do
      let before ← linkedBeforeSuper profile imports prefixStmts
      let test ← linkedExpr profile imports true "" (← prepareBeforeSuper test true)
      let (yesSlots, yes, yesArgs) ← linkedSuperArm profile imports 0 yesPrefix yesCall
      let (noSlots, no, noArgs) ← linkedSuperArm profile imports 1 noPrefix noCall
      let choice := ":super-branch"
      let declareSlots := (yesSlots ++ noSlots).map fun slot => Stmt.decl .let_ slot .undef
      let testChoice := Stmt.decl .const choice (.cond test (.bool true) (.bool false))
      let branch := Stmt.ite (.var choice) yes no
      pure (before ++ declareSlots ++ [testChoice, branch], SuperCall.conditional choice yesArgs noArgs, tail)
    else throw "expected a direct super call"
  let post := Json.obj ((← block.getObj?).insert "body" (.arr stmts.toArray))
  let prefixVars := varNamesOf before
  let checkFunctionOverlap := fun body =>
    if (funDecls body).any (fun f => prefixVars.contains f.name) then
      throw "local function declaration overlaps a var binding before super"
    else pure ()
  match ← linkedFunctionParts profile imports .classCtor name false (.obj ((← fn.getObj?).insert "body" post)) with
  | .objectBindings slots initializers body =>
    -- The argument slots are already bound. A getter or coercion can throw before
    -- any constructor statement, superclass call or instance-field initializer runs.
    checkFunctionOverlap body
    return (initializers ++ before, args, .mk name .classCtor false slots body)
  | .complete body =>
    -- An opaque body may depend on the derived constructor's home object or new.target.
    if let some (_, _, _, why) := body.opaqueBody then throw (why.getD "unsupported derived constructor body")
    checkFunctionOverlap body.body
    return (before, args, body)
  | .defaults slots arity initializers body =>
    -- derivedScope enters both halves of the body at once. A body lexical binding must
    -- not hide an outer name used by a default. Check prefix vars against the parameter
    -- cells and default references, as linkedFunctionParts does for vars in the tail.
    let reads := freeStmts initializers
    if (lexNames (before ++ body)).any reads.contains then
      throw "derived constructor body binding shadows a default parameter reference"
    if prefixVars.any reads.contains then
      throw "var before super overlaps default parameters or their references"
    checkFunctionOverlap body
    return (initializers ++ before, args, .withDefaults name .classCtor false slots arity body)

/-- Class elements with default or explicit derived constructors. -/
private partial def linkedClassScoped (profile : HostProfile) (imports : List Name) (scope : LinkedExprScope) (hint : Name) (j : Json) :
    Except String Expr := do
  let id ← j.getObjVal? "id"
  let name ← if id == .null then pure hint else identifier id
  -- A named class gets an immutable inner binding around its heritage and body.
  -- The frontend annotates references to that cell as function-local even when
  -- the class appears at module scope.
  let classScope := { scope with allowClassLocals := scope.allowClassLocals || id != .null }
  let parent ← j.getObjVal? "superClass"
  let body ← j.getObjVal? "body"
  unless (← field body "type") == "ClassBody" do throw "expected class body"
  let mut ctor := Func.mk name .classCtor false [] []
  let mut derivedCtor := none
  let mut members := #[]
  let elements ← body.getObjValAs? (Array Json) "body"
  let privateNames := elements.toList.filterMap fun m => (m.getObjVal? "key").toOption.bind privateName?
  unless privateNames.Nodup do throw "unsupported private accessor pair"
  if (namesOfType "CoreJSInstancePrivateIdentifier" parent).any privateNames.contains then
    throw "shadowed private name in class heritage"
  let mut declarations := privateNames.map fun n => Stmt.decl .const (privateTable n) (.obj [.proto .null])
  let mut brands := #[]
  for raw in elements do
    let member := if privateNames.isEmpty then raw else markPrivateNames privateNames raw
    let tag ← field member "type"
    if tag == "StaticBlock" then
      let body ← linkedBlockBody profile imports .function member
      members := members.push (.staticBlock (.mk "" .arrow false [] body))
      continue
    unless tag == "MethodDefinition" || tag == "PropertyDefinition" do throw s!"unsupported class element {tag}"
    let keyNode ← member.getObjVal? "key"
    let static ← member.getObjValAs? Bool "static"
    let value ← member.getObjVal? "value"
    if let some spelling := privateName? keyNode then
      if static then throw "unsupported static private member"
      let table := Expr.var (privateTable spelling)
      let label := "#" ++ spelling
      if tag == "PropertyDefinition" then
        let init ← if value == .null then pure .undef else linkedExprScoped profile imports classScope label value
        members := members.push (.field false (.str "") (.prim (.privateSlot .addField) [table, .this, init]))
      else
        unless (← field member "kind") == "method" do throw "unsupported private accessor"
        unless (namesOfType "Super" value).isEmpty do throw "unsupported super in private method"
        let f ← linkedFunction profile imports .method label false value
        let binding := privateMethodValue spelling
        declarations := declarations ++ [.decl .let_ binding .undef]
        -- Class members see the inner class-name binding. Create the one shared
        -- function there; every instance receives the same immutable method value.
        let make := Expr.seq (.assign (.var binding) (.func f)) (.str "")
        brands := brands.push (.field false make (.prim (.privateSlot .addMethod) [table, .this, .var binding]))
      continue
    let (key, keyExpr) ← if ← member.getObjValAs? Bool "computed" then do
      if tag == "MethodDefinition" then
        if let some sym := (ambientSymbolMember? keyNode).bind WellKnownSymbol.ofMember? then
          unless profile.ambientSymbols do throw "unsupported computed class key"
          pure (s!"[{sym.description}]", Expr.prim (.wellKnownSymbol sym) [])
        else pure ("", ← linkedExprScoped profile imports classScope "" keyNode)
      else if !static then
        if ← isAnonymousDefinition value then throw "unsupported function or class named by a computed field key"
        pure ("", ← linkedExprScoped profile imports classScope "" keyNode)
      else throw "unsupported computed class key"
    else do
      let name ← moduleExportName keyNode
      pure (name, Expr.str name)
    if tag == "PropertyDefinition" then
      let init ← if value == .null then pure .undef else linkedExprScoped profile imports classScope key value
      members := members.push (.field static keyExpr init)
    else
      match ← field member "kind" with
      | "constructor" =>
        if parent == .null then ctor ← linkedFunction profile imports .classCtor name false value
        else derivedCtor := some (← linkedDerivedConstructor profile imports name value)
      | "method" => members := members.push (.method static keyExpr (← linkedFunction profile imports .method key false value))
      | "get" => members := members.push (.getter static keyExpr (← linkedFunction profile imports .method ("get " ++ key) false value))
      | "set" => members := members.push (.setter static keyExpr (← linkedFunction profile imports .method ("set " ++ key) false value))
      | other => throw s!"unsupported class method kind {other}"
  let ordered := brands.toList ++ members.toList
  let cls ← if parent == .null then pure (Expr.cls name (id != .null) ctor ordered)
    else pure (Expr.derived name (id != .null) (← linkedExprScoped profile imports classScope "" parent) ordered derivedCtor)
  if declarations.isEmpty then return cls
  return .call (.func (.mk "" .arrow false [] (declarations ++ [.ret cls]))) []

private partial def linkedBindingPattern (lower : Name → Json → Except String Expr)
    (binder : Json → Except String Name) (j : Json) : Except String BindingPattern := do
  match ← field j "type" with
  | "Identifier" => return .name (← binder j)
  | "ObjectPattern" =>
    let props ← j.getObjValAs? (List Json) "properties"
    return .object (← props.mapM (linkedBindingProperty lower binder))
  | "ArrayPattern" =>
    let mut items := #[]
    let mut rest := none
    for elem in ← j.getObjValAs? (Array Json) "elements" do
      if rest.isSome then throw "array binding rest element must be last"
      if elem == .null then items := items.push none
      else if (← field elem "type") == "RestElement" then
        rest := some (← elem.getObjVal? "argument" >>= linkedBindingPattern lower binder)
      else items := items.push (some (← linkedBindingPattern lower binder elem))
    return .array items.toList rest
  | "AssignmentPattern" =>
    let target ← j.getObjVal? "left" >>= linkedBindingPattern lower binder
    let name := if let .name name := target then name else ""
    return .defaultValue target (← lower name (← j.getObjVal? "right"))
  | other => throw s!"unsupported binding pattern {other}"

private partial def linkedBindingProperty (lower : Name → Json → Except String Expr)
    (binder : Json → Except String Name) (p : Json) : Except String (Expr × BindingPattern) := do
  unless (← field p "type") == "Property" do throw "unsupported object binding property"
  unless (← field p "kind") == "init" && !(← p.getObjValAs? Bool "method") do
    throw "expected an object binding property initializer"
  let key ← p.getObjVal? "key"
  let key ← if ← p.getObjValAs? Bool "computed" then lower "" key else .str <$> staticPropertyName key
  let target ← p.getObjVal? "value" >>= linkedBindingPattern lower binder
  return (key, target)

private partial def linkedDeclarator (lower : Name → Json → Except String Expr)
    (binder : Json → Except String Name) (kind : String) (d : Json) : Except String (List Name × Stmt) := do
  let id ← d.getObjVal? "id"
  if !["ObjectPattern", "ArrayPattern"].contains (← field id "type") then
    let (name, stmt) ← declaratorWith binder lower kind d
    return ([name], stmt)
  unless (← field d "type") == "VariableDeclarator" do throw "expected a variable declarator"
  let pattern ← linkedBindingPattern lower binder id
  let init ← d.getObjVal? "init"
  if init == .null then throw "binding pattern declaration needs an initializer"
  return (pattern.names, .bind (← parseDeclKind kind) pattern (← lower "" init))

/-- Split off a trailing object rest binding only when the excluded property names are static.
The saved RHS is outside the source identifier namespace. All user bindings keep their original
declaration kind and share the constructor scope with the statements after super. -/
private partial def linkedPrefixDeclarator (lower : Name → Json → Except String Expr)
    (kind : String) (d : Json) (index : Nat) : Except String (Array Stmt) := do
  let id ← d.getObjVal? "id"
  unless (← field id "type") == "ObjectPattern" do
    let (_, stmt) ← declaratorWith localBinding lower kind d
    return #[stmt]
  let props ← id.getObjValAs? (Array Json) "properties"
  let some tail := props.back?.filter (fun p => (field p "type").toOption == some "RestElement") | do
    let (_, stmt) ← linkedDeclarator lower localBinding kind d
    return #[stmt]
  let rest ← tail.getObjVal? "argument" >>= localBinding
  let bindings ← props.pop.toList.mapM (linkedBindingProperty lower localBinding)
  let excluded ← bindings.mapM fun (key, _) => do
    let .str key := key | throw "unsupported computed object rest exclusion before super"
    return key
  unless (← field d "type") == "VariableDeclarator" do throw "expected a variable declarator"
  let init ← d.getObjVal? "init"
  if init == .null then throw "binding pattern declaration needs an initializer"
  let saved := s!":derived:object:{index}"
  let kind ← parseDeclKind kind
  return #[.decl .const saved (← lower "" init), .bind kind (.object bindings) (.var saved),
    .decl kind rest (.obj [.spread (.var saved) excluded])]

private partial def linkedBlockDeclarations (profile : HostProfile) (imports : List Name) (scope : LinkedScope)
    (j : Json) : Except String (Array Stmt) := do
  let declarations ← j.getObjValAs? (Array Json) "declarations"
  if declarations.isEmpty then throw "expected at least one declarator"
  let kind ← field j "kind"
  declarations.mapM fun d =>
    Prod.snd <$> linkedDeclarator (linkedExpr profile imports true) (scope.varBinder kind) kind d

/-- Keep every declarator in its enclosing statement list. Linking or function
instantiation hoists its vars, and block instantiation creates its lexical bindings. -/
private partial def linkedBlockBody (profile : HostProfile) (imports : List Name) (scope : LinkedScope) (block : Json)
    (allowFunctions := false) : Except String (List Stmt) := do
  let mut result := #[]
  for stmt in ← block.getObjValAs? (Array Json) "body" do
    let kind ← field stmt "type"
    if kind == "VariableDeclaration" then
      result := result ++ (← linkedBlockDeclarations profile imports scope stmt)
    else if allowFunctions && kind == "FunctionDeclaration" then
      let name ← stmt.getObjVal? "id" >>= localBinding
      result := result.push (.funDecl (← linkedFunction profile imports .normal name false stmt))
    else
      result := result.push (← linkedStatement profile imports scope stmt)
  return result.toList

/-- A block introduces a fresh lexical scope each time it executes. Its `var`s
hoist through it via `Stmt.varNames`. -/
private partial def linkedBlock (profile : HostProfile) (imports : List Name) (scope : LinkedScope) (j : Json) :
    Except String Stmt := do
  unless (← field j "type") == "BlockStatement" do throw "expected a statement block"
  return .block (← linkedBlockBody profile imports scope j)

/-- Compare labels in order against the saved discriminant and run the first
matching body, with empty labels sharing the following body. Each nonfinal body
must exit abruptly and a trailing default runs after all case tests fail. -/
private partial def linkedSwitch (profile : HostProfile) (imports : List Name) (scope : LinkedScope) (j : Json) :
    Except String Stmt := do
  let lower := linkedExpr profile imports true ""
  let discriminant ← lower (← j.getObjVal? "discriminant")
  let saved := ":switch:discriminant"
  let mut dispatch := Stmt.block []
  let mut following : Option Stmt := none
  let cases ← j.getObjValAs? (Array Json) "cases"
  for clause in cases.toList.reverse do
    unless (← field clause "type") == "SwitchCase" do throw "expected a switch case"
    let test ← clause.getObjVal? "test"
    if test == .null && following.isSome then throw "unsupported nontrailing switch default"
    let statements ← clause.getObjValAs? (Array Json) "consequent"
    let branch ← match following, statements.isEmpty with
      | some body, true => pure body
      | _, _ => do
        if following.isSome && !(statements.back?.any endsSwitchCase) then
          throw "unsupported switch fallthrough"
        let mut body := #[]
        for statement in statements do
          let kind ← field statement "type"
          if kind == "VariableDeclaration" then
            unless (← field statement "kind") == "var" do throw "unsupported switch lexical declaration"
          else if ["ClassDeclaration", "FunctionDeclaration"].contains kind then
            throw "unsupported switch lexical declaration"
          body := body.push (← linkedStatement profile imports scope statement)
        pure (.block body.toList)
    dispatch ← if test == .null then pure branch else do
      pure (.ite (.binary .strictEq (.var saved) (← lower test)) branch dispatch)
    following := some branch
  return .block [.decl .const saved discriminant, .breakScope dispatch]

private partial def linkedStatement (profile : HostProfile) (imports : List Name) (scope : LinkedScope) (j : Json) :
    Except String Stmt := do
  let kind ← field j "type"
  let nested := scope != .module
  if nested || ["BlockStatement", "IfStatement", "TryStatement", "ForStatement", "ForOfStatement", "ForInStatement",
      "WhileStatement", "SwitchStatement"].contains kind then
    let blockScope := if nested then scope else LinkedScope.moduleBlock
    match kind with
    | "BlockStatement" => return ← linkedBlock profile imports blockScope j
    | "ClassDeclaration" =>
      let name ← j.getObjVal? "id" >>= localBinding
      let classScope : LinkedExprScope := ⟨scope == .function, false⟩
      return .decl .let_ name (← linkedClassScoped profile imports classScope name j)
    | "EmptyStatement" => return .block []
    | "IfStatement" =>
      let test ← linkedExpr profile imports true "" (← j.getObjVal? "test")
      let consequent ← linkedStatement profile imports blockScope (← j.getObjVal? "consequent")
      let alternate ← j.getObjVal? "alternate"
      let elseBranch ← if alternate == .null then pure (.block []) else linkedStatement profile imports blockScope alternate
      return .ite test consequent elseBranch
    | "SwitchStatement" => return ← linkedSwitch profile imports blockScope j
    | "VariableDeclaration" =>
      -- An unbraced branch can declare vars, which already belong to the enclosing module or function.
      unless (← field j "kind") == "var" do throw "expected a var declaration in statement position"
      return .block (← linkedBlockDeclarations profile imports scope j).toList
    | "WhileStatement" =>
      let test ← linkedExpr profile imports true "" (← j.getObjVal? "test")
      return .loop [] test .undef (← linkedStatement profile imports blockScope (← j.getObjVal? "body"))
    | "ForInStatement" =>
      let declaration ← j.getObjVal? "left"
      unless (← field declaration "type") == "VariableDeclaration" do
        throw "for-in requires a declared binding"
      let kindName ← field declaration "kind"
      let kind ← match kindName with
        | "var" => pure DeclKind.var
        | "let" => pure .let_
        | "const" => pure .const
        | _ => throw "unsupported for-in declaration"
      let #[d] ← declaration.getObjValAs? (Array Json) "declarations" | throw "expected one for-in binding"
      unless (← field d "type") == "VariableDeclarator" && (← d.getObjVal? "init") == .null do
        throw "expected an uninitialized for-in declarator"
      let name ← d.getObjVal? "id" >>= scope.varBinder kindName
      let value ← linkedExpr profile imports true "" (← j.getObjVal? "right")
      let source := ":for-in:source"
      let table := ":for-in:keys"
      let index := ":for-in:index"
      let body ← linkedStatement profile imports blockScope (← j.getObjVal? "body")
      let body ← guardForInBody source body
      -- The head for a lexical binding shadows its outer name during evaluation
      -- of the RHS. Even a closure captured there retains the uninitialized cell.
      let head : List Stmt := if kind == .var then [.decl .const source value] else
        [.decl .let_ source .undef,
          .breakScope (.block [.expr (.assign (.var source) value), .brk, .decl .let_ name .undef])]
      return .block (head ++ [
        .decl .const table (.prim .forInKeys [.var source]),
        .decl .let_ index (.num 0),
        .loop [] (.binary .lt (.var index) (.get (.var table) (.str "length"))) (.update (.var index) true false)
          (.block [.decl kind name (.get (.var table) (.var index)), body])])
    | "ForStatement" =>
      let lower := linkedExpr profile imports true ""
      let init ← j.getObjVal? "init"
      let initializer ← if init == .null then pure [] else do
        if (← field init "type") == "VariableDeclaration" then
          -- All declarations share the header scope. Fresh let cells are copied together for each iteration.
          pure (← linkedBlockDeclarations profile imports scope init).toList
        else pure [.expr (← lower init)]
      let test ← j.getObjVal? "test"
      let test ← if test == .null then pure (.bool true) else lower test
      let update ← j.getObjVal? "update"
      let update ← if update == .null then pure .undef else lower update
      return .loop initializer test update (← linkedStatement profile imports blockScope (← j.getObjVal? "body"))
    | "ForOfStatement" =>
      if ← j.getObjValAs? Bool "await" then throw "unsupported for-await-of"
      let lower := linkedExpr profile imports true
      let left ← j.getObjVal? "left"
      let source ← lower "" (← j.getObjVal? "right")
      let body ← linkedStatement profile imports blockScope (← j.getObjVal? "body")
      if (← field left "type") != "VariableDeclaration" then
        let target ← linkedTarget profile imports ⟨true, false⟩ (lower "") left
        let slot := ":for-of"
        return .forOf .const (.name slot) source (.block [.expr (.assign target (.var slot)), body])
      let #[d] ← left.getObjValAs? (Array Json) "declarations" | throw "expected one for-of binding declarator"
      unless (← field d "type") == "VariableDeclarator" && (← d.getObjVal? "init") == .null do
        throw "expected a for-of binding without an initializer"
      let kind ← field left "kind"
      let pattern ← linkedBindingPattern lower (scope.varBinder kind) (← d.getObjVal? "id")
      return .forOf (← parseDeclKind kind) pattern source body
    | "BreakStatement" | "ContinueStatement" =>
      unless (← j.getObjVal? "label") == .null do throw "unsupported labeled loop control"
      return if (← field j "type") == "BreakStatement" then .brk else .cont
    | "TryStatement" =>
      let body ← linkedBlock profile imports blockScope (← j.getObjVal? "block")
      let handler ← j.getObjVal? "handler"
      let finalizer ← j.getObjVal? "finalizer"
      if handler == .null && finalizer == .null then throw "expected catch or finally"
      let protectedBody ← if handler == .null then pure body else do
        unless (← field handler "type") == "CatchClause" do throw "expected a catch clause"
        let param ← handler.getObjVal? "param"
        let name ← if param == .null then pure none else some <$> localBinding param
        pure (.tryCatch body name (← linkedBlock profile imports blockScope (← handler.getObjVal? "body")))
      if finalizer == .null then return protectedBody
      return .tryFinally protectedBody (← linkedBlock profile imports blockScope finalizer)
    | _ => pure ()
  let binder := if nested then localBinding else moduleBinding
  statementWith (linkedExpr profile imports nested "") (declarationWith binder (linkedExpr profile imports nested))
    (scope == .function) j

end

/-- Lower one module declaration and list its declared names in binding order.
Function declarations initialize their cells at link time. -/
def linkedDeclaration (imports : List Name) (j : Json) (profile : HostProfile := .empty) :
    Except String (List Name × Stmt) := do
  match ← field j "type" with
  | "FunctionDeclaration" =>
    let name ← j.getObjVal? "id" >>= moduleBinding
    return ([name], .funDecl (← linkedFunction profile imports .normal name false j))
  | "ClassDeclaration" =>
    let name ← j.getObjVal? "id" >>= moduleBinding
    return ([name], .decl .let_ name (← linkedClassScoped profile imports ⟨false, false⟩ name j))
  | "VariableDeclaration" =>
    let #[d] ← j.getObjVal? "declarations" >>= Json.getArr? | throw "expected a single declarator"
    linkedDeclarator (linkedExpr profile imports false) moduleBinding (← field j "kind") d
  | _ => throw "expected a module declaration"

/-- `export default …`: the binding the module exports as `default`, and its declaration. A named
function or class keeps its binding. Otherwise the binding is `default`, a reserved word that no
source identifier can name: an anonymous function is hoisted, an anonymous class and an expression
are initialized where they stand, and an anonymous definition is named `default`. -/
def linkedDefaultExport (imports : List Name) (d : Json) (profile : HostProfile := .empty) :
    Except String (Name × Stmt) := do
  let kind ← field d "type"
  if kind == "FunctionDeclaration" || kind == "ClassDeclaration" then
    if (d.getObjVal? "id").toOption.getD .null != .null then
      let ([name], s) ← linkedDeclaration imports d profile | throw "expected one default binding"
      return (name, s)
    if kind == "FunctionDeclaration" then
      return ("default", .funDecl (← linkedFunction profile imports .normal "default" false d))
    return ("default", .decl .let_ "default" (← linkedClassScoped profile imports ⟨false, false⟩ "default" d))
  return ("default", .decl .const "default" (← linkedExpr profile imports false "default" d))

/-- A top-level executable statement of the linked fragment. -/
def linkedUnit (imports : List Name) (j : Json) (profile : HostProfile := .empty) : Except String Stmt := do
  if ["FunctionDeclaration", "ClassDeclaration", "VariableDeclaration"].contains (← field j "type") then
    return (← linkedDeclaration imports j profile).2
  linkedStatement profile imports .module j

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

end CoreJS.FromESTree
