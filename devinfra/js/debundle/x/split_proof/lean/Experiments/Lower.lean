import Lean.Data.Json
import Std.Data.HashSet

/-!
# Footprints from raw ESTree, in Lean

The lowering is `Lean.Json`: acorn's ESTree, untyped, so every node type of JavaScript is
representable. The analysis is a total function on it.

Soundness is by construction and monotone:

* A name is treated as local only if a *recognized* binder introduces it (parameter, `let`/`const`/
  `var`/function/class declaration, `catch`, class or function-expression name).
* Any node type not recognized goes through the generic fallback: every identifier below it is a
  reference. That can add dependencies, never remove one.
* A statement is inert only if its shape is in a short allow-list. Everything else is an effect.

So adding precision (recognizing a binder, extending the allow-list) is the only way to get
anything wrong. `Experiments/lowering/compare.mjs` checks it against `eslint-scope`.
-/

open Lean

namespace Lower

structure Acc where
  eager    : Array String := #[]
  lazy     : Array String := #[]
  /-- non-local names this statement assigns to (not declares) -/
  assigned : Array String := #[]
  /-- node types handled by the generic fallback -/
  fallback : Array String := #[]

abbrev M := StateM Acc

def typ (j : Json) : String := (j.getObjValD "type").getStr?.toOption.getD ""
def fld (j : Json) (k : String) : Json := j.getObjValD k
def items (j : Json) : Array Json := j.getArr?.toOption.getD #[]
def str (j : Json) : String := j.getStr?.toOption.getD ""
def flag (j : Json) (k : String) : Bool := (j.getObjValD k).getBool?.toOption.getD false
def isNull : Json → Bool | .null => true | _ => false

def children : Json → Array Json
  | .obj kvs => kvs.valuesArray
  | .arr a => a
  | _ => #[]

/-- Names a pattern binds. -/
partial def binders (j : Json) : List String :=
  match typ j with
  | "Identifier" => [str (fld j "name")]
  | "ObjectPattern" => (items (fld j "properties")).toList.flatMap fun p =>
      if typ p == "RestElement" then binders (fld p "argument") else binders (fld p "value")
  | "ArrayPattern" => (items (fld j "elements")).toList.flatMap binders
  | "AssignmentPattern" => binders (fld j "left")
  | "RestElement" => binders (fld j "argument")
  | _ => []

/-- `let`/`const`/`class`/`function` declared directly in a statement list (block scope). -/
def lexicalNames (stmts : Array Json) : List String :=
  stmts.toList.flatMap fun s =>
    match typ s with
    | "VariableDeclaration" =>
      if str (fld s "kind") == "var" then [] else
        (items (fld s "declarations")).toList.flatMap fun d => binders (fld d "id")
    | "FunctionDeclaration" | "ClassDeclaration" => binders (fld s "id")
    | _ => []

/-- `var` names hoisted to the enclosing function: through blocks, not through functions. -/
partial def varNames (s : Json) : List String :=
  match typ s with
  | "VariableDeclaration" =>
    if str (fld s "kind") == "var" then (items (fld s "declarations")).toList.flatMap fun d => binders (fld d "id") else []
  | "BlockStatement" => (items (fld s "body")).toList.flatMap varNames
  | "IfStatement" => varNames (fld s "consequent") ++ varNames (fld s "alternate")
  | "ForStatement" => varNames (fld s "init") ++ varNames (fld s "body")
  | "ForInStatement" | "ForOfStatement" => varNames (fld s "left") ++ varNames (fld s "body")
  | "WhileStatement" | "DoWhileStatement" | "LabeledStatement" => varNames (fld s "body")
  | "TryStatement" =>
    varNames (fld s "block") ++ varNames (fld (fld s "handler") "body") ++ varNames (fld s "finalizer")
  | "SwitchStatement" =>
    (items (fld s "cases")).toList.flatMap fun c => (items (fld c "consequent")).toList.flatMap varNames
  | _ => []

def ref (bound : List String) (inFn : Bool) (n : String) : M Unit :=
  unless bound.contains n do
    modify fun s => if inFn then { s with lazy := s.lazy.push n } else { s with eager := s.eager.push n }

def noteFallback (t : String) : M Unit := modify fun s => { s with fallback := s.fallback.push t }

mutual

/-- Visit a node. `bound`: names bound locally. `inFn`: inside a function body (reads are lazy). -/
partial def visit (bound : List String) (inFn : Bool) (j : Json) : M Unit := do
  match typ j with
  | "" => for c in children j do visit bound inFn c
  | "Identifier" => ref bound inFn (str (fld j "name"))
  | "Literal" | "ThisExpression" | "Super" | "PrivateIdentifier" | "TemplateElement"
  | "EmptyStatement" | "DebuggerStatement" | "BreakStatement" | "ContinueStatement" => pure ()
  | "ArrayExpression" => for e in items (fld j "elements") do visit bound inFn e
  | "ObjectExpression" => for p in items (fld j "properties") do visit bound inFn p
  | "Property" =>
    if flag j "computed" then visit bound inFn (fld j "key")
    visit bound inFn (fld j "value")
  | "SpreadElement" | "AwaitExpression" | "YieldExpression" | "UnaryExpression" | "ReturnStatement"
  | "ThrowStatement" => visit bound inFn (fld j "argument")
  | "ChainExpression" | "ParenthesizedExpression" | "ExpressionStatement" =>
    visit bound inFn (fld j "expression")
  | "BinaryExpression" | "LogicalExpression" =>
    visit bound inFn (fld j "left"); visit bound inFn (fld j "right")
  | "ConditionalExpression" | "IfStatement" =>
    visit bound inFn (fld j "test"); visit bound inFn (fld j "consequent")
    visit bound inFn (fld j "alternate")
  | "AssignmentExpression" => target bound inFn (fld j "left"); visit bound inFn (fld j "right")
  | "UpdateExpression" => target bound inFn (fld j "argument")
  | "CallExpression" | "NewExpression" =>
    visit bound inFn (fld j "callee")
    for a in items (fld j "arguments") do visit bound inFn a
  | "MemberExpression" =>
    visit bound inFn (fld j "object")
    if flag j "computed" then visit bound inFn (fld j "property")
  | "SequenceExpression" | "TemplateLiteral" => for e in items (fld j "expressions") do visit bound inFn e
  | "TaggedTemplateExpression" => visit bound inFn (fld j "tag"); visit bound inFn (fld j "quasi")
  | "ImportExpression" => visit bound inFn (fld j "source"); visit bound inFn (fld j "options")
  | "FunctionDeclaration" | "FunctionExpression" | "ArrowFunctionExpression" => visitFn bound j
  | "ClassDeclaration" | "ClassExpression" => visitClass bound inFn j
  | "VariableDeclaration" =>
    for d in items (fld j "declarations") do
      visitPat bound inFn (fld d "id")
      visit bound inFn (fld d "init")
  | "BlockStatement" | "StaticBlock" =>
    let stmts := items (fld j "body")
    let b := lexicalNames stmts ++ bound
    for s in stmts do visit b inFn s
  | "ForStatement" =>
    let b := lexicalNames #[fld j "init"] ++ bound
    visit b inFn (fld j "init"); visit b inFn (fld j "test")
    visit b inFn (fld j "update"); visit b inFn (fld j "body")
  | "ForInStatement" | "ForOfStatement" =>
    let left := fld j "left"
    let b := lexicalNames #[left] ++ bound
    if typ left == "VariableDeclaration" then visit b inFn left else target b inFn left
    visit b inFn (fld j "right"); visit b inFn (fld j "body")
  | "WhileStatement" | "DoWhileStatement" =>
    visit bound inFn (fld j "test"); visit bound inFn (fld j "body")
  | "LabeledStatement" => visit bound inFn (fld j "body")
  | "TryStatement" =>
    visit bound inFn (fld j "block")
    let h := fld j "handler"
    unless isNull h do
      let b := binders (fld h "param") ++ bound
      visitPat b inFn (fld h "param")
      visit b inFn (fld h "body")
    visit bound inFn (fld j "finalizer")
  | "SwitchStatement" =>
    visit bound inFn (fld j "discriminant")
    let cases := items (fld j "cases")
    let b := lexicalNames (cases.flatMap fun c => items (fld c "consequent")) ++ bound
    for c in cases do
      visit b inFn (fld c "test")
      for s in items (fld c "consequent") do visit b inFn s
  | "ExportNamedDeclaration" =>
    visit bound inFn (fld j "declaration")
    if isNull (fld j "source") then
      for s in items (fld j "specifiers") do visit bound inFn (fld s "local")
  | "ExportDefaultDeclaration" => visit bound inFn (fld j "declaration")
  | "ImportDeclaration" | "ExportAllDeclaration" => pure ()
  | t => noteFallback t; for c in children j do visit bound inFn c

/-- Expression parts of a binding pattern: defaults and computed keys. -/
partial def visitPat (bound : List String) (inFn : Bool) (p : Json) : M Unit := do
  match typ p with
  | "" | "Identifier" => pure ()
  | "ObjectPattern" =>
    for q in items (fld p "properties") do
      if typ q == "RestElement" then visitPat bound inFn (fld q "argument")
      else
        if flag q "computed" then visit bound inFn (fld q "key")
        visitPat bound inFn (fld q "value")
  | "ArrayPattern" => for e in items (fld p "elements") do visitPat bound inFn e
  | "AssignmentPattern" => visitPat bound inFn (fld p "left"); visit bound inFn (fld p "right")
  | "RestElement" => visitPat bound inFn (fld p "argument")
  | _ => visit bound inFn p

/-- Assignment target: a write to a non-local name is recorded. -/
partial def target (bound : List String) (inFn : Bool) (t : Json) : M Unit := do
  match typ t with
  | "Identifier" =>
    let n := str (fld t "name")
    unless bound.contains n do modify fun s => { s with assigned := s.assigned.push n }
    ref bound inFn n
  | "ObjectPattern" =>
    for q in items (fld t "properties") do
      if typ q == "RestElement" then target bound inFn (fld q "argument")
      else
        if flag q "computed" then visit bound inFn (fld q "key")
        target bound inFn (fld q "value")
  | "ArrayPattern" => for e in items (fld t "elements") do target bound inFn e
  | "AssignmentPattern" => target bound inFn (fld t "left"); visit bound inFn (fld t "right")
  | "RestElement" => target bound inFn (fld t "argument")
  | _ => visit bound inFn t

/-- Function bodies run when called, so everything inside is lazy. Parameter defaults cannot see
the body's own declarations. -/
partial def visitFn (bound : List String) (j : Json) : M Unit := do
  let params := items (fld j "params")
  let self := if typ j == "FunctionExpression" then binders (fld j "id") else []
  let outer := (params.toList.flatMap binders) ++ self ++ bound
  for p in params do visitPat outer true p
  let body := fld j "body"
  if typ body == "BlockStatement" then
    let stmts := items (fld body "body")
    let inner := (stmts.toList.flatMap varNames) ++ lexicalNames stmts ++ outer
    for s in stmts do visit inner true s
  else visit outer true body

/-- `extends`, computed keys and static initializers run at definition; methods and instance
field initializers run later. -/
partial def visitClass (bound : List String) (inFn : Bool) (j : Json) : M Unit := do
  visit bound inFn (fld j "superClass")
  let inner := binders (fld j "id") ++ bound
  for m in items (fld (fld j "body") "body") do
    match typ m with
    | "MethodDefinition" =>
      if flag m "computed" then visit inner inFn (fld m "key")
      visitFn inner (fld m "value")
    | "PropertyDefinition" =>
      if flag m "computed" then visit inner inFn (fld m "key")
      visit inner (if flag m "static" then inFn else true) (fld m "value")
    | "StaticBlock" => visit inner inFn m
    | t => noteFallback t; for c in children m do visit inner inFn c

end

/-! ## Inert statements: the allow-list -/

def classInert (j : Json) : Bool :=
  isNull (fld j "superClass") && (items (fld (fld j "body") "body")).all fun m =>
    match typ m with
    | "MethodDefinition" => !flag m "computed"
    | "PropertyDefinition" => !flag m "computed" && (!flag m "static" || isNull (fld m "value"))
    | _ => false

partial def inertExpr (j : Json) : Bool :=
  match typ j with
  | "Identifier" | "Literal" | "FunctionExpression" | "ArrowFunctionExpression" => true
  | "TemplateLiteral" => (items (fld j "expressions")).isEmpty
  | "ArrayExpression" =>
    (items (fld j "elements")).all fun e => isNull e || (typ e != "SpreadElement" && inertExpr e)
  | "ObjectExpression" =>
    (items (fld j "properties")).all fun p =>
      typ p == "Property" && !flag p "computed" && inertExpr (fld p "value")
  | "ClassExpression" => classInert j
  | "UnaryExpression" =>
    let op := str (fld j "operator")
    ((op == "void" || op == "typeof") && inertExpr (fld j "argument"))
      || (["-", "+", "!", "~"].contains op && typ (fld j "argument") == "Literal")
  | _ => false

partial def inertTop (s : Json) : Bool :=
  match typ s with
  | "FunctionDeclaration" | "EmptyStatement" => true
  | "ClassDeclaration" => classInert s
  | "VariableDeclaration" =>
    (items (fld s "declarations")).all fun d =>
      typ (fld d "id") == "Identifier" && (isNull (fld d "init") || inertExpr (fld d "init"))
  | "ExpressionStatement" => inertExpr (fld s "expression")
  | "ExportNamedDeclaration" => isNull (fld s "declaration") || inertTop (fld s "declaration")
  | "ExportDefaultDeclaration" =>
    let d := fld s "declaration"
    typ d == "FunctionDeclaration" || (typ d == "ClassDeclaration" && classInert d) || inertExpr d
  | _ => false

/-! ## Top-level statements -/

partial def topDefs (s : Json) : List String :=
  match typ s with
  | "FunctionDeclaration" | "ClassDeclaration" => binders (fld s "id")
  | "VariableDeclaration" => (items (fld s "declarations")).toList.flatMap fun d => binders (fld d "id")
  | "ImportDeclaration" => (items (fld s "specifiers")).toList.map fun sp => str (fld (fld sp "local") "name")
  | "ExportNamedDeclaration" => if isNull (fld s "declaration") then [] else topDefs (fld s "declaration")
  | "ExportDefaultDeclaration" =>
    let d := fld s "declaration"
    if typ d == "FunctionDeclaration" || typ d == "ClassDeclaration" then binders (fld d "id") else []
  -- a `var` inside a block, loop or `try` at the top level still declares a module-scope binding
  | _ => varNames s

def isHoisted (s : Json) : Bool :=
  typ s == "FunctionDeclaration"
    || ((typ s == "ExportNamedDeclaration" || typ s == "ExportDefaultDeclaration")
        && typ (fld s "declaration") == "FunctionDeclaration")

structure Top where
  defs     : List String
  rawEager : Array String
  rawLazy  : Array String
  assigned : Array String
  fallback : Array String
  hoisted  : Bool
  inert    : Bool

def analyzeTop (s : Json) : Top :=
  let acc := (visit [] false s).run {} |>.2
  { defs := topDefs s, rawEager := acc.eager, rawLazy := acc.lazy, assigned := acc.assigned,
    fallback := acc.fallback, hoisted := isHoisted s, inert := inertTop s }

structure Footprint where
  defs     : List String
  eager    : List String
  lazy     : List String
  hoisted  : Bool
  effect   : Bool
  assigned : List String
  fallback : List String
  rawEager : List String
  rawLazy  : List String

def dedupe (a : Array String) : List String := a.toList.eraseDups

/-- The footprint the checker needs, with the same two rules as `extract.mjs`:
reading an undeclared name makes a statement an effect, and a statement with effects counts
everything it mentions as eager. -/
def finalize (tops : Array Top) : Array Footprint :=
  let declared : Std.HashSet String := tops.foldl (fun s t => t.defs.foldl (·.insert ·) s) {}
  tops.map fun t =>
    let effect := !t.inert || t.rawEager.any (!declared.contains ·)
    let eager := if effect then dedupe (t.rawEager ++ t.rawLazy) else dedupe t.rawEager
    { defs := t.defs, eager, lazy := dedupe t.rawLazy, hoisted := t.hoisted, effect,
      assigned := dedupe t.assigned, fallback := dedupe t.fallback,
      rawEager := dedupe t.rawEager, rawLazy := dedupe t.rawLazy }

def Footprint.toJson (f : Footprint) : Json :=
  Json.mkObj [("defs", Lean.toJson f.defs), ("eager", Lean.toJson f.eager), ("lazy", Lean.toJson f.lazy),
    ("hoisted", Lean.toJson f.hoisted), ("effect", Lean.toJson f.effect),
    ("assigned", Lean.toJson f.assigned), ("fallback", Lean.toJson f.fallback),
    ("rawEager", Lean.toJson f.rawEager), ("rawLazy", Lean.toJson f.rawLazy)]

end Lower
